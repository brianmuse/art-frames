import threading
from datetime import datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

import pytest

from art_frames.config import Config, Week, with_wake
from art_frames.controller import Controller, art_day


class Screen:
    def __init__(self):
        self.awake = False
        self.playing = False
        self.wakes = 0
        self.publications = []

    def status(self):
        return {"awake": self.awake, "playing": self.playing, "paired": True, "known": True}

    def wake(self, *, checks=6):
        self.wakes += 1
        self.awake = True
        return True

    def publish(self, image, owned_ids, record_upload, keep=()):
        if self.playing or not self.awake:
            return None
        self.publications.append(image)
        content_id = f"app-{len(self.publications)}"
        record_upload(content_id)
        return content_id, [content_id], True


def at(clock):
    return datetime.fromisoformat(f"2026-10-26T{clock}:00").replace(tzinfo=ZoneInfo("UTC"))


def set_wake(cfg, at, days=range(7)):
    """Give the TV a wake time `at` on `days` (0 is Monday), and none on the other days."""
    cfg.app_schedule.week = Week.model_validate(with_wake(cfg.app_schedule.week, at, list(days)))


def setup(configs, library, monkeypatch, *, wake=True):
    cfg = Config(
        wake={"enabled": wake},
        site={"providers": ["art"]},
        tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"},
    )
    configs.save(cfg)
    screen = Screen()

    def capture(cfg, library, now, **kwargs):
        if "07:00" <= now.strftime("%H:%M") < "10:00":
            return b"planner"
        art = library.choose(art_day(cfg, now), cfg.art.order)
        return library.path_for(art["id"]).read_bytes() if art else b"website"

    controller = Controller(configs, library, tv_factory=lambda _: screen, capture=capture)
    return controller, screen


def test_wake_once_then_sleep_and_resume_planner(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    controller.tick(at("06:59"))
    assert screen.wakes == 0
    controller.tick(at("07:00"))
    assert screen.wakes == 1
    assert screen.publications == [b"planner"]
    screen.awake = False
    controller.tick(at("07:01"))
    controller.tick(at("08:00"))
    assert screen.wakes == 1
    assert screen.publications == [b"planner"]
    screen.awake = True
    controller.tick(at("08:01"))
    assert screen.publications == [b"planner", b"planner"]


def test_10am_art_change_waits_for_natural_wake(configs, library, image_bytes, monkeypatch):
    piece = library.add(image_bytes, title="Daily piece")
    controller, screen = setup(configs, library, monkeypatch)
    controller.tick(at("07:00"))
    screen.awake = False
    controller.tick(at("10:00"))
    assert screen.wakes == 1
    assert screen.publications == [b"planner"]
    screen.awake = True
    controller.tick(at("10:30"))
    assert screen.publications[-1] == library.path_for(piece).read_bytes()
    assert library.state("last_display")["kind"] == "website"


def test_restart_after_wake_does_not_wake_twice(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    controller.tick(at("07:00"))
    screen.awake = False
    restarted = Controller(configs, library, tv_factory=lambda _: screen, capture=controller.capture)
    restarted.tick(at("07:02"))
    assert screen.wakes == 1
    assert not screen.awake


def test_missed_wake_never_catches_up_late(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    controller.tick(at("08:00"))
    assert screen.wakes == 0
    assert not screen.publications


def test_disabled_wake_never_wakes(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    controller.tick(at("07:00"))
    assert screen.wakes == 0


def test_scheduled_wake_attempts_network_wake_even_when_initial_status_is_unknown(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    monkeypatch.setattr(screen, "status", lambda: {"paired": True, "known": screen.awake, "awake": screen.awake, "playing": False})
    assert controller.tick(at("07:00")) == "published"
    controller.tick(at("07:01"))
    assert screen.wakes == 1 and screen.publications == [b"planner"]


def test_manual_wake_is_serialized_and_does_not_consume_scheduled_wake(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    calls = []
    monkeypatch.setattr(controller, "_tick", lambda now, **kwargs: calls.append(kwargs) or "published")
    with controller.lock:
        assert controller.wake_screen() == "busy"
    assert screen.wakes == 0
    assert controller.wake_screen() == "published"
    assert screen.wakes == 1 and calls[0]["scheduled_wake"] is False
    assert calls[0]["cfg"].tv.ip == "192.168.1.50"
    assert library.state("wake_day:" + configs.load().tv.identity) is None


def test_tv_programme_is_not_replaced(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=True)
    screen.awake, screen.playing = True, True
    controller.tick(at("07:00"))
    controller.tick(at("10:00"))
    assert not screen.publications
    assert screen.wakes == 0


def test_art_boundary_uses_configured_local_time():
    cfg = Config(art={"change_at": "05:00"})
    assert art_day(cfg, at("04:59")) == "2026-10-25"
    assert art_day(cfg, at("05:00")) == "2026-10-26"


@pytest.mark.parametrize("status,outcome", [
    ({"paired": False, "known": False, "awake": False, "playing": False}, "unpaired"),
    ({"paired": True, "known": False, "awake": True, "playing": False}, "unknown"),
    ({"paired": True, "known": True, "awake": True, "playing": True}, "watching"),
    ({"paired": True, "known": True, "awake": False, "playing": False}, "asleep"),
])
def test_skipped_refresh_reports_reason_without_capturing(configs, library, monkeypatch, status, outcome):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    monkeypatch.setattr(screen, "status", lambda: status)
    monkeypatch.setattr(controller, "capture", lambda *a, **kw: pytest.fail("Skipped updates must not capture"))
    assert controller.tick(at("08:00"), force=True) == outcome
    assert not screen.publications
    assert library.state("last_display") is None


def test_busy_and_changed_refresh_never_record_a_successful_display(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    with controller.lock:
        assert controller.tick(at("08:00"), force=True) == "busy"
    screen.awake = True
    monkeypatch.setattr(screen, "publish", lambda *a, **kw: None)
    assert controller.tick(at("08:00"), force=True) == "changed"
    assert library.state("last_display") is None


def test_send_preview_publishes_exact_bytes_without_recapture_and_rejects_stale_revision(configs, library, monkeypatch):
    import hashlib

    from art_frames.config import private_write
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    preview = b"exact dashboard including its calendar and weather"
    private_write(configs.root / "screen-preview.jpg", preview)
    revision = hashlib.sha256(preview).hexdigest()
    monkeypatch.setattr(controller, "capture", lambda *a, **kw: pytest.fail("Sending a preview must not render again"))
    assert controller.send_preview(revision) == "published"
    assert screen.publications == [preview]
    assert library.state("last_display")["image_sha256"] == revision
    private_write(configs.root / "screen-preview.jpg", b"a newer preview")
    assert controller.send_preview(revision) == "preview-changed"
    assert len(screen.publications) == 1


def test_scheduled_update_does_not_replace_the_saved_preview(configs, library, monkeypatch):
    from art_frames.config import private_write
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    private_write(configs.root / "screen-preview.jpg", b"dashboard preview")
    screen.awake = True
    assert controller.tick(at("18:00"), force=True) == "published"
    assert (configs.root / "screen-preview.jpg").read_bytes() == b"dashboard preview"
    assert screen.publications == [b"website"]


def test_preview_send_never_starts_or_consumes_a_scheduled_wake(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=True)
    assert controller.tick(at("07:00"), force=True, preview_revision="a" * 64) == "asleep"
    assert screen.wakes == 0
    device = configs.load().tv.identity
    assert library.state("wake_day:" + device) is None
    controller.tick(at("07:00"))
    assert screen.wakes == 1


def test_prepares_wake_time_image_while_asleep_then_publishes_it_without_recapture(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    captures = []
    original = controller.capture

    def capture(cfg, library, now, **kwargs):
        captures.append(now)
        return original(cfg, library, now, **kwargs)

    controller.capture = capture
    assert controller.tick(at("06:49")) == "asleep"
    assert not captures
    assert controller.tick(at("06:50")) == "asleep"
    controller.tick(at("06:51"))
    controller.tick(at("06:59"))
    assert captures == [at("07:00")]
    assert screen.wakes == 0 and screen.publications == []
    assert library.state("last_display") is None
    assert controller.tick(at("07:00")) == "published"
    assert screen.wakes == 1 and screen.publications == [b"planner"]
    assert captures == [at("07:00")]
    assert controller._prepared is None


def test_preparation_never_shows_a_future_overlay_early_or_skips_an_unaligned_wake(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    cfg = configs.load()
    set_wake(cfg, "07:07")
    configs.save(cfg)
    screen.awake = True
    captures = []

    def capture(cfg, library, now, **kwargs):
        captures.append(now.strftime("%H:%M"))
        return b"planner" if now.strftime("%H:%M") >= "07:07" else b"art"

    controller.capture = capture
    assert controller.tick(at("07:05")) == "published"
    assert screen.wakes == 0 and screen.publications == [b"art"]
    assert captures == ["07:07", "07:05"]
    # The 07:05 and 07:07 presentations share a refresh bucket, but wake must publish.
    assert controller.tick(at("07:07")) == "published"
    assert screen.wakes == 1 and screen.publications == [b"art", b"planner"]
    assert captures == ["07:07", "07:05"]


def test_pending_preparation_retries_before_wake_and_does_not_mark_displayed(configs, library, monkeypatch):
    from art_frames.site_service import WebsitePending

    controller, screen = setup(configs, library, monkeypatch)
    calls = []

    def capture(cfg, library, now, **kwargs):
        calls.append(now)
        if len(calls) == 1:
            raise WebsitePending()
        return b"planner"

    controller.capture = capture
    with pytest.raises(WebsitePending):
        controller.tick(at("06:58"))
    assert controller._prepared is None and not screen.wakes
    assert library.state("last_display") is None
    assert controller.tick(at("06:59")) == "asleep"
    assert controller.tick(at("07:00")) == "published"
    assert calls == [at("07:00"), at("07:00")]
    assert screen.publications == [b"planner"]


def test_prepared_image_is_invalidated_by_settings_changes_and_expiry(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    controller.tick(at("06:58"))
    assert controller._prepared is not None
    cfg = configs.load()
    cfg.site.data = {"changed": True}
    configs.save(cfg)
    calls = []
    controller.capture = lambda cfg, library, now, **kwargs: calls.append(now) or b"new settings"
    assert controller.tick(at("07:00")) == "published"
    assert calls == [at("07:00")]
    assert screen.publications == [b"new settings"]
    controller._prepared = ("expired", b"old")
    controller.tick(at("07:06"))
    assert controller._prepared is None


@pytest.mark.parametrize("condition", ["disabled", "other-day", "watching", "unpaired", "preview"])
def test_preparation_respects_wake_configuration_and_preview_isolation(configs, library, monkeypatch, condition):
    controller, screen = setup(configs, library, monkeypatch, wake=condition != "disabled")
    if condition == "other-day":
        cfg = configs.load()
        set_wake(cfg, "07:00", [1])
        configs.save(cfg)
    if condition == "watching":
        screen.playing = True
    if condition == "unpaired":
        monkeypatch.setattr(screen, "status", lambda: {"paired": False, "known": False, "awake": False, "playing": False})
    controller.capture = lambda *a, **kw: pytest.fail("No early capture for this condition")
    controller.tick(at("06:59"), preview_revision="a" * 64 if condition == "preview" else None)
    assert controller._prepared is None and screen.wakes == 0


def test_failed_upload_retries_prepared_image_without_waking_or_rendering_again(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    captures = []
    controller.capture = lambda cfg, library, now, **kwargs: captures.append(now) or b"ready at wake"
    controller.tick(at("06:58"))
    original = screen.publish
    monkeypatch.setattr(screen, "publish", lambda *args, **kwargs: None)
    assert controller.tick(at("07:00")) == "changed"
    assert library.state("last_display") is None
    assert controller._prepared is not None
    monkeypatch.setattr(screen, "publish", original)
    assert controller.tick(at("07:01")) == "published"
    assert screen.wakes == 1 and captures == [at("07:00")]
    assert screen.publications == [b"ready at wake"]


def test_midnight_preparation_uses_the_next_days_wake_and_weekday(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    cfg = configs.load()
    set_wake(cfg, "00:00", [1])  # Tuesday, not Monday.
    configs.save(cfg)
    tomorrow = at("00:00") + timedelta(days=1)
    captures = []
    controller.capture = lambda cfg, library, now, **kwargs: captures.append(now) or b"midnight screen"
    assert controller.tick(at("23:50")) == "asleep"
    assert captures == [tomorrow] and controller._prepared is not None
    assert screen.wakes == 0
    assert controller.tick(tomorrow) == "published"
    assert captures == [tomorrow] and screen.wakes == 1
    assert screen.publications == [b"midnight screen"]



@pytest.mark.parametrize("failure", ["upload", "pending-capture"])
def test_unaligned_wake_retries_content_without_repeating_wake(configs, library, monkeypatch, failure):
    from art_frames.site_service import WebsitePending

    controller, screen = setup(configs, library, monkeypatch)
    cfg = configs.load()
    set_wake(cfg, "07:07")
    configs.save(cfg)
    screen.awake = True
    controller.capture = lambda cfg, library, now, **kwargs: b"planner" if now.strftime("%H:%M") >= "07:07" else b"art"
    assert controller.tick(at("07:05")) == "published"
    original_publish, original_capture = screen.publish, controller.capture
    if failure == "upload":
        monkeypatch.setattr(screen, "publish", lambda *args, **kwargs: None)
        assert controller.tick(at("07:07")) == "changed"
    else:
        controller._prepared = None  # Cold start or invalidated preparation.
        def pending(*args, **kwargs):
            raise WebsitePending()
        monkeypatch.setattr(controller, "capture", pending)
        with pytest.raises(WebsitePending):
            controller.tick(at("07:07"))
    assert screen.publications == [b"art"]
    assert library.state("display_key:" + cfg.tv.identity) is None
    monkeypatch.setattr(screen, "publish", original_publish)
    monkeypatch.setattr(controller, "capture", original_capture)
    assert controller.tick(at("07:08")) == "published"
    assert screen.publications == [b"art", b"planner"] and screen.wakes == 1


def unreachable(screen):
    """A TV in deep standby: off the network until it wakes."""
    def status():
        if not screen.awake:
            return {"paired": True, "known": False, "awake": False, "playing": False}
        return {"paired": True, "known": True, "awake": True, "playing": screen.playing}

    screen.checks = []

    def wake(*, checks=6):
        screen.wakes += 1
        screen.checks.append(checks)
        raise ValueError("state-unknown")
    screen.status, screen.wake = status, wake


def test_unconfirmed_wake_retries_only_inside_the_grace_window(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    for clock in ("07:00", "07:01", "07:04", "07:05", "07:30"):
        controller.wake_if_due(at(clock))
    assert screen.wakes == 3  # default grace window is five minutes
    assert screen.checks == [6, 1, 1]  # retries are short so they do not stall other TVs
    assert library.state("wake_result") == "Wake was attempted; the TV did not confirm Art Mode"


def test_retry_stops_without_packets_once_the_tv_reaches_art_mode(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    controller.wake_if_due(at("07:00"))
    screen.awake = True
    controller.wake_if_due(at("07:01"))
    controller.wake_if_due(at("07:02"))
    assert screen.wakes == 1
    assert library.state("wake_result") == "Wake not confirmed at 07:00; the TV reached Art Mode at 07:01"


def test_wake_results_are_logged(configs, library, monkeypatch, caplog):
    import logging
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    caplog.set_level(logging.INFO, "art_frames.controller")
    for clock in ("07:00", "07:01", "07:02"):
        controller.wake_if_due(at(clock))
    screen.awake = True
    controller.wake_if_due(at("07:03"))
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert messages == [
        "TV main: Wake was attempted; the TV did not confirm Art Mode",
        "Scheduled wake did not complete; Art Frames retries during the wake window",
        "TV main: Wake not confirmed at 07:00; the TV reached Art Mode at 07:03",
    ]


def test_owner_turning_tv_on_inside_window_clears_backoff_and_delivers(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    controller.wake_if_due(at("07:00"))
    assert controller.tick(at("07:00")) == "unknown"
    library.set_state("render_backoff", {"failures": 5, "next_at": at("07:25").isoformat()})
    screen.awake = True
    controller.wake_if_due(at("07:03"))
    assert library.state("render_backoff") is None
    assert library.state("wake_result") == "Wake not confirmed at 07:00; the TV reached Art Mode at 07:03"
    assert controller.tick(at("07:03")) == "published" and screen.wakes == 1


def test_delivery_pass_never_repeats_a_missed_wake(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    controller.wake_if_due(at("07:00"))
    assert controller.tick(at("07:00")) == "unknown"
    assert controller.tick(at("07:01")) == "unknown"
    assert screen.wakes == 1


def test_one_unknown_reply_is_not_treated_as_a_return_to_art_mode(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    assert controller.tick(at("08:00")) == "published"
    unknown = {"paired": True, "known": False, "awake": False, "playing": False}
    original = screen.status
    screen.status = lambda: unknown
    assert controller.tick(at("08:01")) == "unknown"
    screen.status = original
    assert controller.tick(at("08:02")) == "unchanged"
    assert screen.publications == [b"planner"]


def test_retry_never_interrupts_a_programme(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    controller.wake_if_due(at("07:00"))
    screen.awake, screen.playing = True, True
    controller.wake_if_due(at("07:01"))
    controller.wake_if_due(at("07:02"))
    assert screen.wakes == 1 and not screen.publications


def test_late_art_mode_after_missed_wake_delivers_at_once_and_is_recorded(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    unreachable(screen)
    controller.wake_if_due(at("07:00"))
    assert controller.tick(at("07:00")) == "unknown"
    assert controller.tick(at("07:10")) == "unknown"
    # Backoff from an earlier render failure must not delay the morning image.
    library.set_state("render_backoff", {"failures": 5, "next_at": at("07:25").isoformat()})
    screen.awake = True  # the owner turns the TV on with the remote
    assert controller.tick(at("07:11")) == "published"
    assert screen.publications == [b"planner"]
    assert library.state("wake_result") == "Wake not confirmed at 07:00; the TV reached Art Mode at 07:11"
    assert library.state("render_backoff") is None


def test_returning_to_art_mode_retries_an_unconfirmed_selection(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    confirmed = [False]
    original = screen.publish

    def publish(image, owned_ids, record_upload, keep=()):
        content_id, owned, _ = original(image, owned_ids, record_upload, keep)
        return content_id, owned, confirmed[0]
    screen.publish = publish
    assert controller.tick(at("08:00")) == "unconfirmed"
    assert controller.tick(at("08:01")) == "unchanged"
    screen.awake = False
    assert controller.tick(at("08:02")) == "asleep"
    screen.awake, confirmed[0] = True, True
    assert controller.tick(at("08:03")) == "published"


def test_delivery_outcome_is_logged_once_per_change_without_details(configs, library, monkeypatch, caplog):
    import logging
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    monkeypatch.setattr(controller, "tick", lambda: Controller.tick(controller, at("08:00")))
    caplog.set_level(logging.INFO, "art_frames.controller")
    for awake in (False, False, True, True, False):
        screen.awake = awake
        controller.run_once()
    messages = [r.getMessage() for r in caplog.records if "delivery" in r.getMessage()]
    assert messages == [
        "TV main: delivery: the TV is off; waiting for it to turn on [asleep]",
        "TV main: delivery: the TV shows the new image [published]",
        "TV main: delivery: the TV already shows the current image [unchanged]",
        "TV main: delivery: the TV is off; waiting for it to turn on [asleep]",
    ]
    assert not any(marker in caplog.text for marker in ("http", "token", "calendar", "192.168"))


def test_a_button_waits_for_a_scheduler_pass_to_finish(configs, library, monkeypatch):
    monkeypatch.setattr("art_frames.controller.ACTION_WAIT", 5)
    controller, _ = setup(configs, library, monkeypatch)
    monkeypatch.setattr(controller, "_tick", lambda now, **kwargs: "published")
    controller.lock.acquire()
    threading.Timer(0.3, controller.lock.release).start()
    assert controller.tick(at("08:00"), force=True) == "published"
    # The scheduler itself never waits: a busy pass is skipped.
    with controller.lock:
        assert controller.tick(at("08:00")) == "busy"


def timed(screen):
    """A Screen that counts status reads and artwork checks, and times its steps like Television."""
    screen.reads, screen.checked, screen.phases = 0, 0, {}
    status, wake, publish = screen.status, screen.wake, screen.publish

    def counted_status():
        screen.reads += 1
        return status()

    def timed_wake(*, checks=6):
        screen.phases.update(packet=100.0, answered=106.5, art_mode=108.0)
        return wake(checks=checks)

    def timed_publish(image, owned_ids, record_upload, keep=()):
        screen.phases.update(upload_start=108.5, upload=111.0, selected=111.75, confirmed=113.0)
        return publish(image, owned_ids, record_upload, keep)

    def current_artwork():
        screen.checked += 1
        return None
    screen.status, screen.wake, screen.publish, screen.current_artwork = counted_status, timed_wake, timed_publish, current_artwork


def same_image_uploaded_before(configs, library):
    """Make the reuse check reachable: the TV was last sent these exact bytes."""
    import hashlib
    device = configs.load().tv.identity
    library.set_state("owned_images:" + device, ["app-0"])
    library.set_state("last_upload", {"device": device, "content_id": "app-0",
                                      "image_sha256": hashlib.sha256(b"planner").hexdigest()})


def test_scheduled_wake_hands_its_status_to_delivery_and_logs_timing(configs, library, monkeypatch, caplog):
    import logging
    controller, screen = setup(configs, library, monkeypatch)
    timed(screen)
    same_image_uploaded_before(configs, library)
    caplog.set_level(logging.INFO, "art_frames.controller")
    controller.wake_if_due(at("07:00"))
    assert screen.reads == 1  # the read before the wake; none after wake() confirmed Art Mode
    assert controller.tick(at("07:00")) == "published"
    assert screen.reads == 1 and screen.checked == 0  # no status read and no reuse check before the upload
    lines = [r.getMessage() for r in caplog.records if "timing" in r.getMessage()]
    assert lines == ["TV main: morning image timing from the wake packet: TV answered +6.5 s, Art Mode confirmed +1.5 s, "
                     "upload started +0.5 s, upload done +2.5 s, selected +0.8 s, confirmed +1.2 s; 13.0 s in all [published]"]
    assert "192.168" not in caplog.text and "app-1" not in caplog.text
    # The next pass reads the TV again and keeps the reuse check.
    assert controller.tick(at("07:01"), force=True) == "published"
    assert screen.reads == 2 and screen.checked == 1
    assert len([r for r in caplog.records if "timing" in r.getMessage()]) == 1


def test_wake_inside_the_delivery_pass_also_skips_extra_reads(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    timed(screen)
    same_image_uploaded_before(configs, library)
    assert controller.tick(at("07:00")) == "published"
    assert screen.reads == 1 and screen.checked == 0 and screen.wakes == 1


def test_an_old_or_unconfirmed_wake_reads_the_tv_again(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    timed(screen)
    same_image_uploaded_before(configs, library)
    clock = [1000.0]
    monkeypatch.setattr("art_frames.controller.monotonic", lambda: clock[0])
    controller.wake_if_due(at("07:00"))
    clock[0] += 31  # another TV's wake took longer than the handoff allows
    assert controller.tick(at("07:00")) == "published"
    assert screen.reads == 2 and screen.checked == 1
    # A wake that did not confirm Art Mode hands nothing over.
    controller, screen = setup(configs, library, monkeypatch)
    timed(screen)
    library.delete_state("wake_day:" + configs.load().tv.identity)
    screen.wake = lambda *, checks=6: False  # a programme started
    controller.wake_if_due(at("07:00"))
    assert screen.reads == 2 and controller._handoff is None  # before the wake, then after it


def test_a_preview_send_never_uses_the_wake_handoff(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    timed(screen)
    controller.wake_if_due(at("07:00"))
    controller.tick(at("07:00"), force=True, preview_revision="0" * 64)
    assert screen.reads == 2 and controller._handoff is not None  # left for the scheduler pass


def test_a_manual_update_between_wake_and_delivery_reads_status_itself(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch)
    controller._handoff = {"device": controller._device(configs.load()), "at": monotonic(),
                           "status": {"awake": True, "playing": False, "paired": True, "known": True}}
    reads = []
    real = screen.status
    monkeypatch.setattr(screen, "status", lambda **kwargs: reads.append(1) or real(**kwargs))
    controller.tick(at("08:00"), force=True)
    assert reads  # the manual update did its own status read
    assert controller._handoff is not None  # and left the handoff for the scheduler's pass
