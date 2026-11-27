import base64
import hashlib
import logging
import re
import threading
import time
from datetime import UTC, datetime

import pytest

from art_frames import tv_discovery as discovery
from art_frames import tv_recovery
from art_frames.config import Config, ConfigStore, private_write
from art_frames.controller import Controller
from art_frames.displays import Displays
from art_frames.library import Library
from art_frames.television import Television, WakeError
from art_frames.tv_recovery import SEARCH_AFTER_SECONDS, SEARCH_EVERY_SECONDS, STATE_KEY, AddressRecovery
from art_frames.web import create_app

MAC = "AA:BB:CC:DD:EE:FF"
OFFLINE = {"awake": False, "playing": False, "paired": True, "known": False}
ONLINE = {"awake": True, "playing": False, "paired": True, "known": True}
ARP_HEADER = "IP address       HW type     Flags       HW address            Mask     Device\n"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def paired(configs, ip="192.168.1.50"):
    configs.save(Config(tv={"ip": ip, "mac": MAC}, tv_name="Living Room"))
    cfg = configs.load()
    private_write(Television(cfg.tv, configs.root).token, b"synthetic-token")
    return cfg


def recovery(configs, library, matches, clock, calls=None, probe=None):
    """A recovery whose search returns these matches. A bare IP is a neighbour-table-confirmed match.

    By default a probe of an unconfirmed match reports MAC, as a TV at a proposed address does.
    """
    def find(mac, networks):
        if calls is not None:
            calls.append((mac, networks))
        return [(item, True) if isinstance(item, str) else item for item in matches]

    unconfirmed = {item[0] for item in matches if not isinstance(item, str) and not item[1]}

    def answers(ip):
        return {"ip": ip, "mac": MAC} if ip in unconfirmed else None

    return AddressRecovery(
        configs, library, find=find, probe=probe or answers, networks=lambda ip: ["192.168.1.0/24"], clock=clock
    )


def observe(finder, configs, status=OFFLINE):
    started = finder.observe(configs.load(), status)
    finder.wait(5)
    return started


def outage(finder, configs, clock):
    """Run one outage long enough to search, then apply the result as the next tick does."""
    assert not observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS
    assert observe(finder, configs)
    return finder.apply(configs.load())


def search_now(finder, configs):
    """Press "Find this TV", wait for the search and apply it as the next tick does."""
    assert finder.request(configs.load()) == "started"
    finder.wait(5)
    return finder.apply(configs.load())


def test_unique_match_updates_address_and_keeps_pairing_and_history(configs, library, caplog):
    old = paired(configs)
    library.set_state("owned_images:" + old.tv.identity, ["MY_F0001"])
    library.set_state("display_key:" + old.tv.identity, "revision")
    clock = Clock()
    finder = recovery(configs, library, ["192.168.1.77"], clock)
    with caplog.at_level(logging.INFO):
        assert outage(finder, configs, clock)
    cfg = configs.load()
    assert cfg.tv.ip == "192.168.1.77" and cfg.tv.mac == MAC
    # Only the IP changes: the MAC identity keeps the token path and the state keys.
    assert cfg.tv.identity == old.tv.identity
    assert cfg.model_dump(exclude={"tv": {"ip"}}) == old.model_dump(exclude={"tv": {"ip"}})
    assert Television(cfg.tv, configs.root).token.read_bytes() == b"synthetic-token"
    assert sorted(path.name for path in (configs.root / "tokens").iterdir()) == [f"{old.tv.identity}.txt"]
    assert library.state("owned_images:" + cfg.tv.identity) == ["MY_F0001"]
    assert library.state("display_key:" + cfg.tv.identity) == "revision"
    assert library.state(STATE_KEY)["result"] == "updated"
    assert library.state(STATE_KEY)["device"] == cfg.tv.identity
    assert "TV Living Room: found at a new address; updated" in caplog.text
    assert MAC not in caplog.text.upper() and "192.168.1.77" not in caplog.text


@pytest.mark.parametrize("matches,result", [([], "not-found"), (["192.168.1.77", "192.168.1.78"], "ambiguous"),
                                            ([("192.168.1.77", True), ("192.168.1.78", False)], "ambiguous")])
def test_no_match_or_two_matches_change_nothing(configs, library, matches, result):
    old = paired(configs)
    clock = Clock()
    assert not outage(recovery(configs, library, matches, clock), configs, clock)
    assert configs.load().tv.ip == "192.168.1.50"
    assert Television(old.tv, configs.root).token.read_bytes() == b"synthetic-token"
    assert library.state(STATE_KEY)["result"] == result


def test_no_search_during_plain_nightly_deep_standby(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, [], clock, calls)
    for _night in range(3):
        for _ in range(10 * 12):  # ten hours of unknown status, read every 5 minutes
            assert not observe(finder, configs)
            clock.now += 5 * 60
        observe(finder, configs, ONLINE)  # the morning: the TV answers at its saved address
        clock.now += 14 * 3600
    assert calls == []
    assert tv_recovery.summary(library, configs.load()) is None


def test_search_after_24_hours_unknown_and_at_most_every_30_minutes(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, [], clock, calls)
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS - 1
    observe(finder, configs)
    assert calls == []
    clock.now += 1
    observe(finder, configs)
    assert len(calls) == 1 and calls[0][0] == MAC
    clock.now += SEARCH_EVERY_SECONDS - 1
    observe(finder, configs)
    assert len(calls) == 1
    # The limit is persisted, so a restarted process with a new instance respects it too.
    restarted = recovery(configs, library, [], clock, calls)
    observe(restarted, configs)
    assert len(calls) == 1
    clock.now += 1
    observe(restarted, configs)
    assert len(calls) == 2


class WakeFails:
    def status(self):
        return OFFLINE

    def wake(self, *, checks=6):
        raise RuntimeError("no answer")


def test_failed_scheduled_wake_starts_a_search(configs, library):
    paired(configs)
    configs.mutate(lambda raw: raw.update(timezone="UTC", wake={**raw["wake"], "enabled": True, "at": "07:00"}))
    clock, calls = Clock(), []
    finder = recovery(configs, library, [], clock, calls)
    controller = Controller(configs, library, tv_factory=lambda cfg: WakeFails(), capture=lambda *a, **k: b"",
                            recovery=finder)
    assert not observe(finder, configs)  # an unknown status alone starts nothing
    controller.wake_if_due(now=datetime(2026, 11, 2, 7, 1, tzinfo=UTC))
    assert library.state(STATE_KEY)["requested"] == "wake"
    assert observe(finder, configs) and len(calls) == 1
    assert "requested" not in library.state(STATE_KEY)
    # A wake retry in the same window waits for the 30-minute limit.
    finder.wake_failed(configs.load())
    clock.now += 60
    assert not observe(finder, configs)
    clock.now += SEARCH_EVERY_SECONDS
    assert observe(finder, configs) and len(calls) == 2


def test_failed_manual_wake_requests_a_search(configs, library):
    paired(configs)
    finder = recovery(configs, library, [], Clock())
    controller = Controller(configs, library, tv_factory=lambda cfg: WakeFails(), capture=lambda *a, **k: b"",
                            recovery=finder)
    with pytest.raises(RuntimeError):
        controller.wake_screen()
    assert library.state(STATE_KEY)["requested"] == "wake"


def test_reachable_status_resets_the_outage_and_unpaired_tvs_are_not_searched(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, ["192.168.1.77"], clock, calls)
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS - 60
    observe(finder, configs, ONLINE)
    clock.now += 120
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS - 1
    observe(finder, configs, {**OFFLINE, "paired": False})
    assert calls == []


def test_a_tv_that_answers_without_a_clear_state_is_not_searched_for(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, ["192.168.1.77"], clock, calls)
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS - 60
    observe(finder, configs, {**OFFLINE, "answered": True})
    clock.now += 120
    observe(finder, configs)
    assert calls == []


def test_result_and_proposal_are_cleared_when_the_tv_answers(configs, library):
    paired(configs)
    clock = Clock()
    finder = recovery(configs, library, [], clock)
    search_now(finder, configs)
    assert tv_recovery.summary(library, configs.load())["result"] == "not-found"
    observe(finder, configs, ONLINE)
    assert tv_recovery.summary(library, configs.load()) is None
    finder = recovery(configs, library, [("192.168.1.77", False)], clock)
    clock.now += 60
    search_now(finder, configs)
    assert tv_recovery.summary(library, configs.load())["proposal"] == "192.168.1.77"
    observe(finder, configs, ONLINE)
    assert tv_recovery.summary(library, configs.load()) is None
    assert "proposal" not in library.state(STATE_KEY)
    # An update stays on the page after the TV answers at its new address.
    finder = recovery(configs, library, ["192.168.1.77"], clock)
    clock.now += 60
    assert search_now(finder, configs)
    observe(finder, configs, ONLINE)
    assert tv_recovery.summary(library, configs.load())["result"] == "updated"


def test_tv_answering_at_its_saved_address_is_not_moved(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, ["192.168.1.77"], clock, calls)
    finder.probe = lambda ip: {"ip": ip, "mac": MAC}
    assert not outage(finder, configs, clock)
    assert calls == [] and configs.load().tv.ip == "192.168.1.50"
    assert library.state(STATE_KEY)["result"] == "reachable"


def test_address_used_by_another_tv_is_not_taken(configs, library):
    old = paired(configs)
    Displays(configs, library, Controller(configs, library, demo=True), demo=True).add("Kitchen", "192.168.1.77")
    clock = Clock()
    assert not outage(recovery(configs, library, ["192.168.1.77"], clock), configs, clock)
    assert configs.load().tv.ip == "192.168.1.50"
    assert Television(old.tv, configs.root).token.exists()
    assert not Television(configs.load().displays[0].tv, configs.root).token.exists()
    assert library.state(STATE_KEY)["result"] == "conflict"


def test_result_is_dropped_when_settings_changed_during_the_search(configs, library):
    old = paired(configs)
    clock = Clock()
    finder = recovery(configs, library, ["192.168.1.77"], clock)
    assert finder.request(configs.load()) == "started"
    finder.wait(5)
    configs.mutate(lambda raw: raw["tv"].update(ip="192.168.1.90"))
    assert not finder.apply(configs.load())
    assert configs.load().tv.ip == "192.168.1.90"
    assert Television(old.tv, configs.root).token.exists()
    assert library.state(STATE_KEY)["result"] == "changed"


def test_no_long_outage_search_starts_just_before_a_scheduled_wake(configs, library):
    paired(configs)
    configs.mutate(lambda raw: raw.update(timezone="UTC", wake={**raw["wake"], "enabled": True, "at": "07:00"}))
    clock, calls = Clock(), []
    clock.now = datetime(2026, 11, 1, 6, 56, tzinfo=UTC).timestamp()
    finder = recovery(configs, library, [], clock, calls)
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS  # 06:56 the next day, four minutes before the wake
    assert not observe(finder, configs)
    assert calls == []
    clock.now += 5 * 60  # 07:01, after the wake time
    assert observe(finder, configs) and len(calls) == 1


def test_added_tv_is_updated_in_its_own_settings(configs, library):
    configs.save(Config(tv_name="Living Room"))
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True)
    screen = displays.get(displays.add("Kitchen", "192.168.1.148", MAC))
    old = screen.configs.load()
    private_write(Television(old.tv, screen.configs.root).token, b"kitchen-token")
    clock = Clock()
    finder = recovery(screen.configs, screen.library, ["192.168.1.61"], clock)
    assert outage(finder, screen.configs, clock)
    cfg = screen.configs.load()
    assert cfg.tv.ip == "192.168.1.61" and configs.load().tv.ip == ""
    assert Television(cfg.tv, screen.configs.root).token.read_bytes() == b"kitchen-token"
    assert library.state(STATE_KEY) is None  # rate-limit state is kept per TV


def test_only_one_search_runs_at_a_time_across_tvs(tmp_path):
    first_root, second_root = tmp_path / "a", tmp_path / "b"
    first_root.mkdir()
    second_root.mkdir()
    first, second = ConfigStore(first_root), ConfigStore(second_root)
    paired(first)
    paired(second, "192.168.1.148")
    clock, release, calls = Clock(), threading.Event(), []

    def blocked(mac, networks):
        assert release.wait(5)
        return []

    one = AddressRecovery(first, Library(first_root), find=blocked, probe=lambda ip: None,
                          networks=lambda ip: ["192.168.1.0/24"], clock=clock)
    two = recovery(second, Library(second_root), [], clock, calls)
    try:
        assert one.request(first.load()) == "started"
        assert one.request(first.load()) == "searching"
        assert two.request(second.load()) == "busy"
        two.wake_failed(second.load())
        assert not two.observe(second.load(), OFFLINE)  # waits for its next turn
        assert two.library.state(STATE_KEY)["requested"] == "wake"
    finally:
        release.set()
        one.wait(5)
    assert calls == []
    assert two.observe(second.load(), OFFLINE)
    two.wait(5)
    assert len(calls) == 1


def test_search_errors_are_logged_by_type_only(configs, library, caplog):
    paired(configs)

    def find(mac, networks):
        raise RuntimeError(f"192.168.1.77 {MAC}")

    finder = AddressRecovery(configs, library, find=find, probe=lambda ip: None,
                             networks=lambda ip: ["192.168.1.0/24"], clock=Clock())
    with caplog.at_level(logging.INFO):
        search_now(finder, configs)
    assert library.state(STATE_KEY)["result"] == "failed"
    assert "address search failed: RuntimeError" in caplog.text
    assert "192.168.1.77" not in caplog.text and MAC not in caplog.text.upper()


def test_unconfirmed_match_becomes_a_proposal_and_is_not_applied(configs, library):
    paired(configs)
    finder = recovery(configs, library, [("192.168.1.77", False)], Clock())
    assert not search_now(finder, configs)
    assert configs.load().tv.ip == "192.168.1.50"
    found = tv_recovery.summary(library, configs.load())
    assert found["proposal"] == "192.168.1.77"
    assert found["text"] == "Art Frames found a TV that reports this TV's MAC address at 192.168.1.77. Use this address?"


def test_proposal_accept_and_reject_paths(configs, library):
    paired(configs)
    clock = Clock()
    finder = recovery(configs, library, [("192.168.1.77", False)], clock)
    search_now(finder, configs)
    # Only the stored proposal can be applied.
    assert finder.accept(configs.load(), "192.168.1.99") == "stale"
    assert finder.dismiss(configs.load())
    assert tv_recovery.summary(library, configs.load())["result"] == "dismissed"
    assert finder.accept(configs.load(), "192.168.1.77") == "stale"
    assert configs.load().tv.ip == "192.168.1.50"
    # Applying a proposal uses the same checks as an automatic update.
    Displays(configs, library, Controller(configs, library, demo=True), demo=True).add("Kitchen", "192.168.1.77")
    search_now(finder, configs)
    assert finder.accept(configs.load(), "192.168.1.77") == "conflict"
    assert configs.load().tv.ip == "192.168.1.50"
    finder = recovery(configs, library, [("192.168.1.78", False)], clock)
    search_now(finder, configs)
    # A settings change clears the proposal.
    tv_recovery.clear_proposal(library, configs.load())
    assert tv_recovery.summary(library, configs.load()) is None
    search_now(finder, configs)
    assert finder.accept(configs.load(), "192.168.1.78") == "updated"
    assert configs.load().tv.ip == "192.168.1.78"
    assert tv_recovery.summary(library, configs.load())["result"] == "updated"


def test_record_for_another_device_is_ignored(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    library.set_state(STATE_KEY, {"device": "another-tv", "ip": "192.168.1.50", "result": "not-found",
                                  "at": clock.now, "proposal": "192.168.1.77", "searched": clock.now})
    finder = recovery(configs, library, [], clock, calls)
    assert tv_recovery.summary(library, configs.load()) is None
    assert finder.accept(configs.load(), "192.168.1.77") == "stale"
    # Its 30-minute limit belongs to the other identity.
    finder.wake_failed(configs.load())
    assert observe(finder, configs) and len(calls) == 1
    assert library.state(STATE_KEY)["device"] == configs.load().tv.identity


class FakeTV:
    def __init__(self, cfg, seen):
        self.cfg, self.seen = cfg, seen

    def status(self):
        self.seen.append(self.cfg.tv.ip)
        return OFFLINE

    def publish(self, *args, **kwargs):
        pytest.fail("No upload while the TV is unreachable")


def test_tick_returns_without_waiting_for_the_search_and_a_later_tick_applies_it(configs, library):
    cfg = paired(configs)
    clock, seen = Clock(), []
    release = threading.Event()

    def find(mac, networks):
        assert release.wait(5)
        return [("192.168.1.77", True)]

    finder = AddressRecovery(
        configs, library, find=find, probe=lambda ip: None, networks=lambda ip: ["192.168.1.0/24"], clock=clock
    )
    library.set_state(STATE_KEY, {"device": cfg.tv.identity, "ip": "192.168.1.50",
                                  "since": clock.now - SEARCH_AFTER_SECONDS})
    controller = Controller(
        configs, library, tv_factory=lambda cfg: FakeTV(cfg, seen), capture=lambda *a, **k: b"", recovery=finder
    )
    started = time.monotonic()
    assert controller.tick() == "unknown"
    assert time.monotonic() - started < 1 and finder.searching()
    assert configs.load().tv.ip == "192.168.1.50"
    # A tick during the search neither waits nor starts a second search.
    first = finder.thread
    assert controller.tick() == "unknown"
    assert finder.thread is first
    release.set()
    finder.wait(5)
    assert controller.tick() == "unknown"
    assert seen == ["192.168.1.50", "192.168.1.50", "192.168.1.77"]
    assert configs.load().tv.ip == "192.168.1.77"


def test_demo_controller_never_searches(configs, library):
    assert Controller(configs, library, demo=True).recovery is None


def test_spoofed_mac_without_a_neighbour_table_is_unconfirmed(tmp_path, monkeypatch):
    swept = []

    def sweep(network):
        swept.append(str(network))
        return [{"ip": "192.168.1.77", "mac": MAC}, {"ip": "192.168.1.9", "mac": "11:22:33:44:55:66"}]

    monkeypatch.setattr(discovery, "_sweep", sweep)
    monkeypatch.setattr(discovery, "_probe", lambda ip: pytest.fail("No ARP candidates to probe"))
    assert discovery.arp_addresses(MAC, tmp_path / "missing") is None
    assert discovery.find_mac(MAC.lower(), ["192.168.1.0/24"], arp_path=tmp_path / "missing") == [
        ("192.168.1.77", False)
    ]
    assert swept == ["192.168.1.0/24"]
    # Docker bridge networking: the table lists only the gateway, so the match stays unconfirmed.
    table = tmp_path / "arp"
    table.write_text(ARP_HEADER + "172.17.0.1       0x1         0x2         02:42:ac:11:00:01     *        eth0\n")
    assert discovery.find_mac(MAC, ["192.168.1.0/24"], arp_path=table) == [("192.168.1.77", False)]


def test_spoofed_mac_with_another_table_entry_is_dropped(tmp_path, monkeypatch):
    table = tmp_path / "arp"
    monkeypatch.setattr(discovery, "_probe", lambda ip: None)

    def sweep(network):  # the sweep's connections fill the table
        table.write_text(ARP_HEADER + "192.168.1.66     0x1         0x2         66:66:66:66:66:66     *        eth0\n")
        return [{"ip": "192.168.1.66", "mac": MAC}]

    table.write_text(ARP_HEADER)
    monkeypatch.setattr(discovery, "_sweep", sweep)
    assert discovery.find_mac(MAC, ["192.168.1.0/24"], arp_path=table) == []


@pytest.mark.parametrize("reported", [MAC, ""], ids=["wireless", "wired"])
def test_table_entry_read_after_the_sweep_confirms_the_match(tmp_path, monkeypatch, reported):
    table = tmp_path / "arp"
    table.write_text(ARP_HEADER)  # empty before the sweep
    monkeypatch.setattr(discovery, "_probe", lambda ip: pytest.fail("No ARP candidates to probe"))

    def sweep(network):
        table.write_text(ARP_HEADER + f"192.168.1.77     0x1         0x2         {MAC.lower()}     *        eth0\n"
                         "192.168.1.9      0x1         0x2         11:22:33:44:55:66     *        eth0\n")
        # A wired TV reports no Wi-Fi MAC.
        return [{"ip": "192.168.1.77", "mac": reported}, {"ip": "192.168.1.9", "mac": ""}]

    monkeypatch.setattr(discovery, "_sweep", sweep)
    assert discovery.find_mac(MAC, ["192.168.1.0/24"], arp_path=table) == [("192.168.1.77", True)]


def test_find_mac_uses_a_confirmed_arp_entry_and_ignores_a_stale_one(tmp_path, monkeypatch):
    table = tmp_path / "arp"
    table.write_text(
        ARP_HEADER
        + f"192.168.1.77     0x1         0x2         {MAC.lower()}     *        eth0\n"
        f"192.168.1.88     0x1         0x0         {MAC.lower()}     *        eth0\n"
        "192.168.1.9      0x1         0x2         11:22:33:44:55:66     *        eth0\n"
    )
    assert discovery.arp_addresses(MAC, table) == ["192.168.1.77"]
    monkeypatch.setattr(discovery, "_sweep", lambda network: pytest.fail("ARP answer is enough"))
    monkeypatch.setattr(discovery, "_probe", lambda ip: {"ip": ip, "mac": MAC})
    assert discovery.find_mac(MAC, ["192.168.1.0/24"], arp_path=table) == [("192.168.1.77", True)]
    # A stale entry that no longer answers is not trusted; the sweep decides.
    monkeypatch.setattr(discovery, "_probe", lambda ip: None)
    monkeypatch.setattr(discovery, "_sweep", lambda network: [{"ip": "192.168.1.80", "mac": MAC}])
    assert discovery.find_mac(MAC, ["192.168.1.0/24"], arp_path=table) == [("192.168.1.80", False)]


PASSWORD = "a-long-test-password-not-a-real-secret"
# Per-TV state keys that end with the TV identity (controller.DEVICE_KEYS).
DEVICE_KEYS = ("owned_images:", "display_key:", "wake_day:", "wake_missed:", "presence:", "screen_off_at:")


def web_client(tmp_path):
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    client = app.test_client()
    headers = {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}
    client.get("/", headers=headers)
    with client.session_transaction() as session:
        token = session["csrf"]
    return app, client, headers, token


def settings_form(token, ip, **extra):
    return {"csrf": token, "tv_name": "Living Room", "site_entry": "index.html", "site_refresh": "15",
            "site_data": "{}", "timezone": "UTC", "wake_at": "07:00", "days": [str(day) for day in range(7)],
            "order": "shuffle", "change_at": "00:00", "sources": ["met"], "tv_ip": ip,
            "tv_mac": MAC, "broadcast": "255.255.255.255", **extra}


def test_recovered_tv_keeps_every_identity_tagged_record(tmp_path, image_bytes):
    configs, library = ConfigStore(tmp_path), Library(tmp_path)
    old = paired(configs)
    device = old.tv.identity
    digest = hashlib.sha256(image_bytes).hexdigest()
    (tmp_path / "tv-upload.jpg").write_bytes(image_bytes)
    library.set_state("last_upload", {"device": device, "content_id": "MY_F0001",
                                      "at": "2026-11-01T07:00:00+00:00", "image_sha256": digest})
    for key in DEVICE_KEYS:
        library.set_state(key + device, "kept")
    controller = Controller(configs, library)
    controller._device(configs.load())  # sets the identity marker, as any scheduler pass does
    clock = Clock()
    assert outage(recovery(configs, library, ["192.168.1.77"], clock), configs, clock)
    cfg = configs.load()
    assert cfg.tv.ip == "192.168.1.77" and cfg.tv.identity == device
    assert controller._device(cfg) == device and library.state("tv_identity") == device
    for key in DEVICE_KEYS:
        assert library.state(key + device) == "kept"
    assert library.state("last_upload")["device"] == device
    # The sent-image route compares last_upload.device with the current identity.
    _, client, headers, _ = web_client(tmp_path)
    assert client.get(f"/screen/sent.jpg?v={digest}", headers=headers).status_code == 200


def test_manual_ip_change_in_settings_keeps_pairing(tmp_path):
    configs = ConfigStore(tmp_path)
    old = paired(configs)
    app, client, headers, token = web_client(tmp_path)
    form = settings_form(token, "192.168.1.77", tv_ip_shown="192.168.1.50")
    assert client.post("/settings", headers=headers, data=form).status_code == 302
    cfg = configs.load()
    assert cfg.tv.ip == "192.168.1.77" and cfg.tv.identity == old.tv.identity
    assert Television(cfg.tv, tmp_path).token.read_bytes() == b"synthetic-token"
    assert app.extensions["controller"]._device(cfg) == old.tv.identity


def test_stale_settings_form_keeps_the_recovered_ip(tmp_path):
    configs = ConfigStore(tmp_path)
    paired(configs)
    _, client, headers, token = web_client(tmp_path)
    assert 'name="tv_ip_shown" value="192.168.1.50"' in client.get("/tv/main/settings", headers=headers).text
    configs.mutate(lambda raw: raw["tv"].update(ip="192.168.1.77"))  # recovery runs while the page is open
    form = settings_form(token, "192.168.1.50", tv_ip_shown="192.168.1.50", tv_name="Living Room TV")
    assert client.post("/settings", headers=headers, data=form).status_code == 302
    assert configs.load().tv.ip == "192.168.1.77" and configs.load().tv_name == "Living Room TV"


def test_find_this_tv_button_and_proposal_on_the_settings_page(tmp_path):
    configs, library = ConfigStore(tmp_path), Library(tmp_path)
    paired(configs)
    app, client, headers, token = web_client(tmp_path)
    finder = recovery(configs, library, [("192.168.1.77", False)], Clock())
    app.extensions["controller"].recovery = finder
    page = client.get("/tv/main/settings", headers=headers).text
    assert "Find this TV" in page and "Use this address" not in page
    assert client.post("/settings/find-tv", headers=headers, data={"csrf": token}).status_code == 302
    finder.wait(5)
    assert not finder.apply(configs.load())
    page = client.get("/tv/main/settings", headers=headers).text
    assert "Last address search, " in page
    assert "reports this TV&#39;s MAC address at 192.168.1.77" in page and "Use this address" in page
    assert configs.load().tv.ip == "192.168.1.50"
    # The posted address must match the stored proposal; CSRF applies.
    assert client.post("/settings/tv-address", headers=headers, data={"ip": "192.168.1.77"}).status_code == 400
    client.post("/settings/tv-address", headers=headers, data={"csrf": token, "ip": "192.168.1.99"})
    assert configs.load().tv.ip == "192.168.1.50"
    client.post("/settings/tv-address", headers=headers, data={"csrf": token, "ip": "192.168.1.77", "action": "use"})
    assert configs.load().tv.ip == "192.168.1.77"
    assert "Found this TV at a new address and updated it." in client.get("/tv/main/settings", headers=headers).text


def test_keep_button_and_settings_save_drop_the_proposal(tmp_path):
    configs, library = ConfigStore(tmp_path), Library(tmp_path)
    paired(configs)
    app, client, headers, token = web_client(tmp_path)
    finder = recovery(configs, library, [("192.168.1.77", False)], Clock())
    app.extensions["controller"].recovery = finder
    search_now(finder, configs)
    client.post("/settings/tv-address", headers=headers, data={"csrf": token, "ip": "192.168.1.77", "action": "keep"})
    assert "You kept the saved address." in client.get("/tv/main/settings", headers=headers).text
    search_now(finder, configs)
    form = settings_form(token, "192.168.1.50", tv_ip_shown="192.168.1.50")
    assert client.post("/settings", headers=headers, data=form).status_code == 302
    assert "Use this address" not in client.get("/tv/main/settings", headers=headers).text
    assert configs.load().tv.ip == "192.168.1.50"


def recovery_card(page):
    """The HTML of the Settings page's Address recovery card."""
    start = page.index('id="address-recovery"')
    return page[start:page.index("</section>", start)]


def test_find_and_address_results_show_in_the_address_recovery_card(tmp_path):
    configs, library = ConfigStore(tmp_path), Library(tmp_path)
    paired(configs)
    app, client, headers, token = web_client(tmp_path)
    finder = recovery(configs, library, [("192.168.1.77", False)], Clock())
    app.extensions["controller"].recovery = finder
    response = client.post("/settings/find-tv", headers=headers, data={"csrf": token})
    assert response.location.endswith("/tv/main/settings#address-recovery")
    page = client.get(response.location, headers=headers).text
    note = "Searching for this TV. Reload this page in a minute to see the result."
    assert page.count(note) == 1 and note in recovery_card(page)
    finder.wait(5)
    finder.apply(configs.load())
    response = client.post("/settings/tv-address", headers=headers, data={"csrf": token, "ip": "192.168.1.77", "action": "keep"})
    assert response.location.endswith("#address-recovery")
    assert "Kept the saved address." in recovery_card(client.get(response.location, headers=headers).text)


def test_address_recovery_card_has_one_footer(tmp_path):
    configs, library = ConfigStore(tmp_path), Library(tmp_path)
    paired(configs)
    app, client, headers, _ = web_client(tmp_path)
    finder = recovery(configs, library, [("192.168.1.77", False)], Clock())
    app.extensions["controller"].recovery = finder
    card = recovery_card(client.get("/tv/main/settings", headers=headers).text)
    assert card.count('class="card-footer"') == 1 and "Find this TV" in card
    search_now(finder, configs)
    card = recovery_card(client.get("/tv/main/settings", headers=headers).text)
    assert card.count('class="card-footer"') == 1 and "Use this address" in card and "Find this TV" not in card


def test_each_tv_settings_card_has_its_own_save_button(tmp_path):
    configs = ConfigStore(tmp_path)
    paired(configs)
    _, client, headers, _ = web_client(tmp_path)
    page = client.get("/tv/main/settings", headers=headers).text
    start = re.search(r'<form method="post" action="/tv/main/settings[?"#]', page).start()
    form = page[start:page.index("</form>", start)]
    # Buttons of this form: no form= attribute and not type="button". The first one is what Enter presses.
    own = [tag for tag in re.findall(r"<button[^>]*>[^<]*", form) if "form=" not in tag and 'type="button"' not in tag]
    assert own == ['<button class="button" type="submit">Save']
    assert "settings-save-bar" not in page and "Saves every section on this page." not in page


def test_device_records_follow_an_identity_change(configs, library):
    configs.save(Config(tv={"ip": "192.168.1.50"}, tv_name="Living Room"))
    legacy = configs.load().tv.identity
    for key in ("standby_test", "art_display", "last_upload", "tv_artwork", "last_display"):
        library.set_state(key, {"device": legacy, "checked_at": "2026-11-01T07:00:00+00:00"})
    controller = Controller(configs, library, demo=True)
    controller._device(configs.load())
    configs.mutate(lambda raw: raw["tv"].update(mac=MAC))  # adding the MAC moves the TV to a MAC identity
    device = configs.load().tv.identity
    assert device != legacy and controller._device(configs.load()) == device
    for key in ("standby_test", "art_display", "last_upload", "tv_artwork", "last_display"):
        assert library.state(key)["device"] == device, key


@pytest.mark.parametrize("reported", [None, {"ip": "192.168.1.77", "mac": "11:22:33:44:55:66"},
                                      {"ip": "192.168.1.77", "mac": ""}])
def test_accept_probes_the_proposal_again_and_needs_the_same_mac(configs, library, reported):
    paired(configs)
    probes = []

    def probe(ip):
        probes.append(ip)
        return reported if ip == "192.168.1.77" else None

    finder = recovery(configs, library, [("192.168.1.77", False)], Clock(), probe=probe)
    search_now(finder, configs)
    assert finder.accept(configs.load(), "192.168.1.77") == "stale"
    assert probes[-1] == "192.168.1.77"
    assert configs.load().tv.ip == "192.168.1.50"


def test_a_proposal_older_than_an_hour_is_stale(configs, library):
    paired(configs)
    clock = Clock()
    finder = recovery(configs, library, [("192.168.1.77", False)], clock)
    search_now(finder, configs)
    clock.now += tv_recovery.PROPOSAL_SECONDS
    assert finder.accept(configs.load(), "192.168.1.77") == "stale"
    assert configs.load().tv.ip == "192.168.1.50"
    search_now(finder, configs)
    clock.now += tv_recovery.PROPOSAL_SECONDS - 1
    assert finder.accept(configs.load(), "192.168.1.77") == "updated"


def test_outage_search_backs_off_while_nothing_is_found_and_resets_when_the_tv_answers(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, [], clock, calls)

    def gaps():
        """Run the outage for 30 hours, reading the status every minute; return the minutes between searches."""
        times = []
        for _ in range(30 * 60):
            if observe(finder, configs):
                times.append(clock.now)
                finder.apply(configs.load())
            clock.now += 60
        return [round((later - earlier) / 60) for earlier, later in zip(times, times[1:], strict=False)]

    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS
    assert gaps() == [30, 60, 120, 240, 360, 360, 360]
    assert library.state(STATE_KEY)["every"] == tv_recovery.OUTAGE_EVERY_MAX_SECONDS
    # The TV answers: the next outage starts again at 30 minutes.
    observe(finder, configs, ONLINE)
    assert "every" not in library.state(STATE_KEY)
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS
    assert gaps()[:3] == [30, 60, 120]
    # A failed wake keeps the 30-minute limit even while the outage search waits longer.
    clock.now += 31 * 60
    finder.wake_failed(configs.load())
    assert observe(finder, configs)


def test_settings_save_clears_the_proposal_under_the_state_lock(tmp_path):
    configs, library = ConfigStore(tmp_path), Library(tmp_path)
    paired(configs)
    app, client, headers, token = web_client(tmp_path)
    finder = recovery(configs, library, [("192.168.1.77", False)], Clock())
    app.extensions["controller"].recovery = finder
    search_now(finder, configs)
    calls = []
    original = finder.clear_proposal

    def clear(cfg):
        calls.append(cfg.tv.ip)
        original(cfg)
    finder.clear_proposal = clear
    form = settings_form(token, "192.168.1.50", tv_ip_shown="192.168.1.50")
    assert client.post("/settings", headers=headers, data=form).status_code == 302
    assert calls == ["192.168.1.50"]
    assert tv_recovery.summary(library, configs.load()) is None
    # The method waits while another thread holds the state lock.
    del finder.clear_proposal
    search_now(finder, configs)
    finder.state_lock.acquire()
    worker = threading.Thread(target=finder.clear_proposal, args=(configs.load(),))
    try:
        worker.start()
        worker.join(0.2)
        assert worker.is_alive() and tv_recovery.summary(library, configs.load())["proposal"] == "192.168.1.77"
    finally:
        finder.state_lock.release()
    worker.join(5)
    assert tv_recovery.summary(library, configs.load()) is None


@pytest.mark.parametrize("code,searches", [("state-unknown", True), ("not-confirmed", False), ("network", False),
                                           ("unpaired", False), ("missing-mac", False)])
def test_only_a_wake_with_no_answer_requests_a_search(configs, library, code, searches):
    paired(configs)
    finder = recovery(configs, library, [], Clock())

    class Fails(WakeFails):
        def wake(self, *, checks=6):
            raise WakeError(code)

    controller = Controller(configs, library, tv_factory=lambda cfg: Fails(), capture=lambda *a, **k: b"",
                            recovery=finder)
    with pytest.raises(WakeError):
        controller.wake_screen()
    assert ((library.state(STATE_KEY) or {}).get("requested") == "wake") is searches


def test_an_unpaired_status_drops_a_wake_request(configs, library):
    paired(configs)
    clock, calls = Clock(), []
    finder = recovery(configs, library, [], clock, calls)
    finder.wake_failed(configs.load())
    assert not observe(finder, configs, {**OFFLINE, "paired": False})
    assert "requested" not in library.state(STATE_KEY)
    assert not observe(finder, configs) and calls == []


def test_outage_search_keeps_its_wait_while_another_tv_holds_the_search_slot(configs, library):
    paired(configs)
    clock = Clock()
    finder = recovery(configs, library, [], clock)
    observe(finder, configs)
    clock.now += SEARCH_AFTER_SECONDS
    assert observe(finder, configs)
    finder.apply(configs.load())
    assert library.state(STATE_KEY)["every"] == tv_recovery.SEARCH_EVERY_SECONDS
    clock.now += tv_recovery.SEARCH_EVERY_SECONDS
    assert tv_recovery.SEARCH_SLOT.acquire(blocking=False)
    try:
        for _ in range(5):
            assert not observe(finder, configs)
            clock.now += 60
    finally:
        tv_recovery.SEARCH_SLOT.release()
    assert library.state(STATE_KEY)["every"] == tv_recovery.SEARCH_EVERY_SECONDS
    assert observe(finder, configs)
    finder.apply(configs.load())
    assert library.state(STATE_KEY)["every"] == 2 * tv_recovery.SEARCH_EVERY_SECONDS
