import logging
import os
import sqlite3
import threading
from datetime import timedelta

import pytest

from art_frames import controller as controller_module
from art_frames import site
from art_frames.config import Config, Television, private_write
from art_frames.controller import Busy, Controller
from art_frames.displays import DisplayLibrary, Displays
from tests.test_controller import at, setup


def failing(error):
    def capture(*args, **kwargs):
        raise error
    return capture


def test_failed_render_backs_off_persists_and_clears_on_success(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    calls = []
    controller.capture = lambda *a, **kw: calls.append(1) or (_ for _ in ()).throw(RuntimeError("down"))
    with pytest.raises(RuntimeError):
        controller.tick(at("08:00"))
    assert library.state("render_backoff")["failures"] == 1
    assert controller.tick(at("08:00")) == "backoff" and len(calls) == 1
    restarted = Controller(configs, library, tv_factory=lambda _: screen, capture=controller.capture)
    assert restarted.tick(at("08:00")) == "backoff"
    with pytest.raises(RuntimeError):
        restarted.tick(at("08:01"))
    assert library.state("render_backoff")["failures"] == 2
    assert restarted.tick(at("08:02")) == "backoff"
    restarted.capture = lambda *a, **kw: b"recovered"
    # Manual send bypasses backoff; success clears it.
    assert restarted.tick(at("08:02"), force=True) == "published"
    assert library.state("render_backoff") is None


def test_backoff_is_capped_at_the_refresh_period(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    cfg = configs.load()
    cfg.site.refresh_minutes = 3
    configs.save(cfg)
    library.set_state("render_backoff", {"failures": 9, "next_at": at("07:00").isoformat()})
    screen.awake = True
    controller.capture = failing(RuntimeError())
    with pytest.raises(RuntimeError):
        controller.tick(at("08:00"))
    assert library.state("render_backoff")["next_at"] == (at("08:00") + timedelta(minutes=3)).isoformat()


def test_render_backoff_never_blocks_the_scheduled_wake_image(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    library.set_state("render_backoff", {"failures": 5, "next_at": at("09:00").isoformat()})
    assert controller.tick(at("06:55")) == "asleep"
    assert controller._prepared is None  # preparation also waits out the backoff
    assert controller.tick(at("07:00")) == "published"
    assert screen.wakes == 1 and screen.publications == [b"planner"]


def test_logs_stage_and_safe_cause_once_then_recovery(configs, library, monkeypatch, caplog):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    controller.capture = failing(ValueError("https://calendar.example/private-token"))
    caplog.set_level(logging.INFO, "art_frames.controller")
    for _ in range(3):
        library.delete_state("render_backoff")
        controller.run_once()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["TV main: render failed: ValueError"]
    assert "private-token" not in caplog.text
    assert library.state("scheduler_error") == controller_module.ERRORS["render"]
    assert library.state("scheduler_error_at")

    def own(*args, **kwargs):
        site.asset_path(configs.root, ".hidden")
    library.delete_state("render_backoff")
    controller.capture = own
    controller.run_once()
    assert "render failed: Hidden site files cannot be rendered" in caplog.text
    library.delete_state("render_backoff")
    controller.capture = lambda *a, **kw: b"fine"
    controller.run_once()
    assert "TV main: recovered after render failure" in caplog.text
    assert library.state("scheduler_error") == "" and library.state("scheduler_error_at") is None


def test_every_tv_wake_runs_before_any_render(configs, library):
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True)
    second = displays.add("Living room", "192.168.1.51")
    events = []
    for target in ("main", second):
        controller = displays.get(target).controller
        controller.wake_if_due = lambda target=target: events.append(("wake", target))
        controller.tick = lambda target=target: events.append(("render", target))
    displays.tick()
    assert events == [("wake", "main"), ("wake", second), ("render", "main"), ("render", second)]
    assert displays.alive is None and displays.last_pass is not None


def test_first_pass_wake_happens_exactly_once(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    controller.capture = failing(AssertionError("The wake pass must not render"))
    assert controller.wake_if_due(at("06:59")) is None
    controller.wake_if_due(at("07:00"))
    assert screen.wakes == 1 and not screen.publications
    controller.capture = lambda *a, **kw: b"planner"
    assert controller.tick(at("07:00")) == "published"
    controller.wake_if_due(at("07:01"))
    assert screen.wakes == 1


def test_one_render_at_a_time_and_interactive_actions_report_busy(configs, library, monkeypatch):
    monkeypatch.setattr(controller_module, "INTERACTIVE_WAIT", 0.05)
    first, first_screen = setup(configs, library, monkeypatch, wake=False)
    first_screen.awake = True
    started, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        started.set()
        release.wait(5)
        return b"slow"

    first.capture = slow
    other_root = configs.root / "displays" / "other"
    other_root.mkdir(parents=True)
    from art_frames.config import ConfigStore
    other_configs = ConfigStore(other_root)
    other_configs.save(configs.load())
    second = Controller(other_configs, DisplayLibrary(library, "b" * 32),
                        tv_factory=lambda _: first_screen, capture=lambda *a, **kw: b"second")
    worker = threading.Thread(target=lambda: first.tick(at("08:00")))
    worker.start()
    try:
        assert started.wait(5)
        with pytest.raises(Busy):
            second.preview_site()
        assert second.tick(at("08:00"), force=True) == "busy"
    finally:
        release.set()
        worker.join(5)
    assert first_screen.publications == [b"slow"]
    assert second.preview_site() == b"second"


def test_preview_does_not_wait_for_a_running_display_operation(configs, library, monkeypatch):
    controller, _ = setup(configs, library, monkeypatch, wake=False)
    with controller.lock:
        with pytest.raises(Busy):
            controller.preview_site()


def test_identical_image_is_not_uploaded_again_when_the_tv_confirms_it(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    reported = []
    screen.current_artwork = lambda: reported.append(1) or (screen.awake and f"app-{len(screen.publications)}")
    assert controller.tick(at("18:00")) == "published"
    # A new refresh period produces identical pixels; the TV still shows them.
    assert controller.tick(at("18:20")) == "same-image"
    assert screen.publications == [b"website"] and len(reported) == 1
    device = configs.load().tv.identity
    assert library.state("display_key:" + device) is not None
    assert library.state("tv_artwork")["content_id"] == "app-1"
    # Unconfirmed or different TV artwork always uploads again.
    for answer in (None, "someone-else"):
        screen.current_artwork = lambda answer=answer: answer
        assert controller.tick(at("18:20"), force=True) == "published"
    assert screen.publications == [b"website"] * 3


def test_pairing_and_device_state_follow_the_mac_across_ip_changes(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    cfg = configs.load()
    legacy, device = cfg.tv.address_identity, cfg.tv.identity
    assert legacy != device
    private_write(configs.root / "tokens" / f"{legacy}.txt", b"token")
    library.set_state("owned_images:" + legacy, ["MY_F0001"])
    library.set_state("wake_day:" + legacy, "2026-10-26")
    library.set_state("tv_artwork", {"device": legacy, "content_id": "MY_F0001", "image_sha256": "x"})
    library.set_state("last_upload", {"device": legacy, "content_id": "MY_F0001", "image_sha256": "x"})
    screen.awake = True
    assert controller.tick(at("18:00")) == "published"
    assert (configs.root / "tokens" / f"{device}.txt").read_bytes() == b"token"
    assert (configs.root / "tokens" / f"{device}.txt").stat().st_mode & 0o777 == 0o600
    assert library.state("wake_day:" + device) == "2026-10-26"
    assert screen.publications == [b"website"]
    assert library.state("tv_artwork")["device"] == device
    # A rolled-back image still reads its address-based token and state.
    assert (configs.root / "tokens" / f"{legacy}.txt").read_bytes() == b"token"
    assert library.state("owned_images:" + legacy) == ["MY_F0001"]
    assert library.state("wake_day:" + legacy) == "2026-10-26"
    # Migration runs once: an ID cleaned up later is not merged back from the frozen legacy key.
    assert library.state("owned_images:" + device) == ["app-1"]
    assert Controller(configs, library)._device(configs.load()) == device
    assert library.state("owned_images:" + device) == ["app-1"]
    # DHCP moves the TV: the same MAC keeps the same identity, token and ownership.
    configs.mutate(lambda raw: raw["tv"].update(ip="192.168.1.77"))
    moved = configs.load().tv
    assert moved.identity == device and moved.address_identity != legacy
    assert Controller(configs, library)._device(configs.load()) == device
    assert (configs.root / "tokens" / f"{device}.txt").exists()


def test_changing_the_saved_mac_keeps_pairing_and_owned_uploads(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    first = configs.load().tv.identity
    private_write(configs.root / "tokens" / f"{first}.txt", b"token")
    assert controller.tick(at("18:00")) == "published"
    assert library.state("owned_images:" + first) == ["app-1"]
    offered = []
    publish = screen.publish
    screen.publish = lambda image, owned, record, keep=(): offered.append(list(owned)) or publish(image, owned, record, keep=keep)
    for mac in ("AA:BB:CC:DD:EE:01", ""):
        configs.mutate(lambda raw, mac=mac: raw["tv"].update(mac=mac))
        device = configs.load().tv.identity
        assert controller.tick(at("18:00"), force=True) == "published"
        assert (configs.root / "tokens" / f"{device}.txt").read_bytes() == b"token"
        assert library.state("tv_identity") == device
    assert offered == [["app-1"], ["app-2"]]  # earlier uploads stay owned for cleanup
    assert (configs.root / "tokens" / f"{first}.txt").exists()


def test_reverting_tv_details_does_not_resurrect_deleted_uploads(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    original = configs.load().tv.mac
    first = configs.load().tv.identity
    private_write(configs.root / "tokens" / f"{first}.txt", b"token")
    assert controller.tick(at("18:00")) == "published"
    configs.mutate(lambda raw: raw["tv"].update(mac="AA:BB:CC:DD:EE:01"))
    assert controller.tick(at("18:00"), force=True) == "published"
    live = library.state("owned_images:" + configs.load().tv.identity)
    offered = []
    publish = screen.publish
    screen.publish = lambda image, owned, record, keep=(): offered.append(list(owned)) or publish(image, owned, record, keep=keep)
    configs.mutate(lambda raw: raw["tv"].update(mac=original))
    assert controller.tick(at("18:00"), force=True) == "published"
    assert offered == [live]  # the frozen list from the first visit is not merged back


def test_identity_without_mac_and_between_tvs_stays_separate():
    assert Television(ip="192.168.1.50").identity == Television(ip="192.168.1.50").address_identity
    assert Television(ip="192.168.1.50", mac="AA:BB:CC:DD:EE:FF").identity != Television(
        ip="192.168.1.50", mac="AA:BB:CC:DD:EE:01").identity


def test_migration_on_one_tv_never_touches_another(configs, library):
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True)
    second = displays.add("Living room", "192.168.1.51", "AA:BB:CC:DD:EE:01")
    tv = displays.get(second).configs.load().tv
    library.set_state("owned_images:" + tv.address_identity, ["main-owned"])
    displays.get(second).library.set_state("owned_images:" + tv.address_identity, ["second-owned"])
    displays.get(second).controller._device(displays.get(second).configs.load())
    assert library.state("owned_images:" + tv.address_identity) == ["main-owned"]
    assert displays.get(second).library.state("owned_images:" + tv.identity) == ["second-owned"]


def test_unchanged_state_and_existing_daily_art_do_not_write(library, image_bytes):
    watcher = sqlite3.connect(library.path)
    version = lambda: watcher.execute("PRAGMA data_version").fetchone()[0]  # noqa: E731
    before = version()
    assert library.choose("2026-10-26") is None  # empty collection: no write transaction
    library.set_state("tv_status", {"awake": True})
    after_write = version()
    assert after_write != before
    library.add(image_bytes, title="Piece")
    library.choose("2026-10-26")
    settled = version()
    for _ in range(3):
        library.set_state("tv_status", {"awake": True})
        library.choose("2026-10-26")
        library.delete_state("missing")
    assert version() == settled
    watcher.close()


def test_display_library_shares_only_catalog_methods(library):
    scoped = DisplayLibrary(library, "a" * 32)
    assert scoped.path_for is not None and scoped.root == library.root
    with pytest.raises(AttributeError):
        getattr(scoped, "connection")  # noqa: B009


def test_site_tree_scan_is_cached_briefly(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("one")
    first = site.tree_digest(tmp_path)
    (tmp_path / "index.html").write_text("changed size")
    assert site.tree_digest(tmp_path) == first
    monkeypatch.setattr(site, "TREE_SECONDS", 0)
    assert site.tree_digest(tmp_path) != first


def test_private_write_syncs_the_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr("art_frames.config.os.fsync", lambda fd: synced.append(fd) or real(fd))
    private_write(tmp_path / "file", b"data")
    assert len(synced) == 2 and (tmp_path / "file").read_bytes() == b"data"
    assert Config()  # unchanged defaults


def test_later_confirmation_cleans_up_uploads_a_slow_tv_left_behind(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    assert controller.tick(at("18:00")) == "published"
    device = configs.load().tv.identity
    library.set_state("owned_images:" + device, ["stale", "app-1"])  # earlier unconfirmed publish
    removed = []
    screen.remove_old = lambda owned, selected, keep=(): removed.append((owned, selected)) or [selected]
    screen.current_artwork = lambda: "app-1"
    assert controller.tick(at("18:20")) == "same-image"
    assert screen.publications == [b"website"]  # no upload, only cleanup
    assert removed == [(["stale", "app-1"], "app-1")]
    assert library.state("owned_images:" + device) == ["app-1"]
