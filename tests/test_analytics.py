import base64
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from art_frames import analytics
from art_frames.config import Config
from art_frames.controller import Controller
from art_frames.television import Television
from art_frames.web import create_app

PASSWORD = "a-long-test-password-not-a-real-secret"
TZ = ZoneInfo("America/New_York")
SECRETS = ("192.168.1.77", "AA:BB:CC:DD:EE:FF", "HomeNetwork", "uuid:1234-duid", "Living Room Frame", "Secret Model")
WRITES = ("wake", "set_artmode", "set_brightness", "set_brightness_sensor_setting", "send_key", "hold_key",
          "upload", "select_image", "delete_list", "screen_on", "screen_off", "publish", "remove_old", "pair")


class Refuses:
    """Records and fails any method not defined on the subclass: writes, wakes and mode changes."""

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            self.forbidden.append(name)
            raise AssertionError(f"the sampler called {name}")
        return refuse


class FakeArt(Refuses):
    def __init__(self, mode="on"):
        self.mode, self.forbidden, self.calls = mode, [], []

    def get_artmode(self):
        self.calls.append("get_artmode")
        return self.mode

    def get_artmode_settings(self):
        self.calls.append("get_artmode_settings")
        return {"data": '[{"item": "brightness", "value": "7", "min": "1", "max": "10"},'
                        ' {"item": "brightness_sensor_setting", "value": "on"},'
                        ' {"item": "color_temperature", "value": "2"}, {"item": "ssid", "value": "HomeNetwork"}]'}

    def get_current(self):
        self.calls.append("get_current")
        return {"content_id": "MY_F0042"}

    def close(self):
        pass


class FakeDevice(Refuses):
    """The SamsungTVWS connection. art() fails the test while REST reports standby or off."""

    def __init__(self, power, mode="on"):
        self.power, self.forbidden, self.art_opened = power, [], 0
        self.channel = FakeArt(mode)

    def rest_device_info(self):
        return {"device": {"PowerState": self.power, "ip": SECRETS[0], "wifiMac": SECRETS[1], "ssid": SECRETS[2],
                           "duid": SECRETS[3], "name": SECRETS[4], "modelName": SECRETS[5]}}

    def art(self, **_):
        if self.power in {"standby", "off"}:
            self.forbidden.append("art in standby")
            raise AssertionError("the sampler opened the art channel while the TV is in standby")
        self.art_opened += 1
        return self.channel


def real_tv(tmp_path, monkeypatch, power, mode="on"):
    """A real Television whose network connection is a fake; records the timeout it was given."""
    cfg = Config(tv={"ip": "192.168.1.50"}).tv
    tv = Television(cfg, tmp_path)
    tv.token.parent.mkdir(parents=True, exist_ok=True)
    tv.token.write_text("token")
    device = FakeDevice(power, mode)
    timeouts = []

    def remote(timeout=None):
        timeouts.append(timeout)
        return device
    monkeypatch.setattr(tv, "remote", remote)
    return tv, device, timeouts


class SamplingTV(Refuses):
    """A controller-level fake: answers tv.sample() with extra private keys the sampler must drop."""

    def __init__(self, power="on", art="on", answers=True):
        self.reading = {"answers": answers, "power": power if answers else None, "art_mode": art if answers else None,
                        "brightness": 7, "sensor": "on", "color_temperature": 2, "content_id": "MY_F0042",
                        "ip": SECRETS[0], "wifiMac": SECRETS[1], "ssid": SECRETS[2], "duid": SECRETS[3],
                        "name": SECRETS[4], "about": {"model_name": SECRETS[5]}}
        self.calls, self.forbidden = [], []

    def sample(self, *, budget):
        self.calls.append("sample")
        return dict(self.reading)


@pytest.fixture
def make(configs, library):
    def build(tv):
        return Controller(configs, library, tv_factory=lambda cfg: tv)
    return build


def rows(root, sql):
    analytics.flush()
    with closing(sqlite3.connect(root / analytics.FILE)) as db:
        return db.execute(sql).fetchall()


def test_sample_never_opens_the_art_channel_in_standby(tmp_path, monkeypatch):
    for power in ("standby", "off"):
        tv, device, timeouts = real_tv(tmp_path, monkeypatch, power)
        row = analytics.measure(tv)
        assert device.forbidden == [] and device.art_opened == 0
        assert (row["power"], row["art_mode"], row["playing"], row["brightness"]) == (power, 0, 0, None)


def test_sample_reads_brightness_only_in_art_mode_and_never_writes(tmp_path, monkeypatch):
    from art_frames.television import DISPLAY_TIMEOUT

    tv, device, timeouts = real_tv(tmp_path, monkeypatch, "on", "on")
    row = analytics.measure(tv)
    assert device.forbidden == [] and device.channel.forbidden == []
    assert timeouts == [DISPLAY_TIMEOUT]
    assert (row["brightness"], row["sensor"], row["color_temperature"], row["content_id"]) == (7, 1, 2, "MY_F0042")
    assert (row["power"], row["art_mode"], row["playing"]) == ("on", 1, 0)
    tv, device, _ = real_tv(tmp_path, monkeypatch, "on", "off")
    watching = analytics.measure(tv)
    assert device.channel.calls == ["get_artmode"]  # no settings read outside Art Mode
    assert (watching["playing"], watching["art_mode"], watching["brightness"]) == (1, 0, None)


def test_sample_stops_reading_when_its_time_budget_is_spent(tmp_path, monkeypatch):
    tv, device, _ = real_tv(tmp_path, monkeypatch, "on", "on")
    reading = tv.sample(budget=0)
    assert reading["answers"] and reading["power"] == "on"
    assert device.art_opened == 0 and reading["brightness"] is None


def test_sampler_never_wakes_or_changes_mode(make):
    cases = (("on", "on"), ("on", "off"), ("standby", "on"), ("off", None), (None, None))
    for index, (power, art) in enumerate(cases):
        tv = SamplingTV(power=power, art=art, answers=power is not None)
        row = analytics.sample_if_due(make(tv), now=time.time() + (index + 1) * analytics.SAMPLE_SECONDS)
        assert isinstance(row, dict) and tv.forbidden == [] and tv.calls == ["sample"]
        if art != "on" or power != "on":
            assert row["brightness"] is None and row["content_id"] is None
    silent = analytics.measure(SamplingTV(answers=False))
    assert (silent["answered"], silent["power"], silent["art_mode"], silent["playing"]) == (0, "unknown", None, None)


def test_busy_lock_skips_the_sample_and_the_next_pass_takes_it(make, configs):
    tv = SamplingTV()
    controller = make(tv)
    offset = analytics.store(configs.root).offset
    slot_start = ((time.time() - offset) // analytics.SAMPLE_SECONDS + 1) * analytics.SAMPLE_SECONDS + offset
    controller.lock.acquire()
    try:
        assert analytics.sample_if_due(controller, now=slot_start + 5) == "busy"
    finally:
        controller.lock.release()
    assert tv.calls == []
    row = analytics.sample_if_due(controller, now=slot_start + 35)  # the next scheduler pass, same slot
    assert isinstance(row, dict) and row["ts"] == slot_start + 35
    assert analytics.sample_if_due(controller, now=slot_start + 65) is None  # one sample per slot
    assert len(rows(configs.root, "SELECT * FROM samples")) == 1
    assert isinstance(analytics.sample_if_due(controller, now=slot_start + analytics.SAMPLE_SECONDS + 1), dict)


def test_tvs_sample_in_different_passes():
    offsets = {analytics.slot_offset(name) for name in ("main", "a1b2", "c3d4", "e5f6")}
    assert len(offsets) == 4 and all(0 <= value < analytics.SAMPLE_SECONDS for value in offsets)
    assert analytics.slot_offset("main") == analytics.slot_offset("main")


def test_only_whitelisted_fields_are_stored(make, configs):
    analytics.sample_if_due(make(SamplingTV()), now=time.time() + analytics.SAMPLE_SECONDS)
    analytics.flush()
    with closing(sqlite3.connect(configs.root / analytics.FILE)) as db:
        columns = [item[1] for item in db.execute("PRAGMA table_info(samples)")]
        dump = "\n".join(db.iterdump())
    assert tuple(columns) == analytics.SAMPLE_FIELDS
    stored = (configs.root / analytics.FILE).read_bytes()
    wal = configs.root / (analytics.FILE + "-wal")
    stored += wal.read_bytes() if wal.exists() else b""
    for secret in SECRETS:
        assert secret not in dump and secret.encode() not in stored
    assert (configs.root / analytics.FILE).stat().st_mode & 0o777 == 0o600


def test_existing_file_is_made_private(tmp_path):
    path = tmp_path / analytics.FILE
    sqlite3.connect(path).close()
    path.chmod(0o644)
    analytics.store(tmp_path).write(lambda db: None)
    assert path.stat().st_mode & 0o777 == 0o600


def at(*parts):
    return datetime(*parts, tzinfo=TZ).timestamp()


def test_on_time_spans_midnight_and_counts_the_open_last_interval():
    transitions = [(at(2026, 10, 1, 20), "standby"), (at(2026, 10, 1, 22), "awake"), (at(2026, 10, 2, 2), "standby")]
    start, end = at(2026, 10, 1, 21), at(2026, 10, 2, 10)
    days, cells = analytics.state_time(transitions, start, end, TZ)
    first, second = datetime(2026, 10, 1).date(), datetime(2026, 10, 2).date()
    assert days[first]["standby"] == 3600  # the state from before the range counts from its start
    assert days[first]["awake"] == 2 * 3600
    assert days[second]["awake"] == 2 * 3600
    assert days[second]["standby"] == 8 * 3600  # the open last state runs until now
    assert cells[(3, 23)]["awake"] == 3600 and cells[(4, 1)]["awake"] == 3600  # Thu 23:00, Fri 01:00
    assert sum(sum(day.values()) for day in days.values()) == end - start


def test_status_changes_are_recorded_once(make, configs):
    controller = make(SamplingTV())
    base = time.time()
    for offset, status in enumerate(({"awake": True, "playing": False, "paired": True, "known": True},) * 3
                                    + ({"awake": False, "playing": True, "paired": True, "known": True},
                                       {"awake": False, "playing": False, "paired": True, "known": True},
                                       {"awake": False, "playing": False, "paired": True, "known": False})):
        analytics.record_status(controller, status, at=base + offset * 30)
    assert [state for _, state in rows(configs.root, "SELECT ts, state FROM transitions ORDER BY ts")] == [
        "awake", "playing", "standby", "deep"]  # it answered before, so no answer now is deep standby


def test_a_tv_that_never_answered_or_is_unpaired_stays_unknown(make, configs):
    controller = make(SamplingTV())
    base = time.time()
    silent = {"awake": False, "playing": False, "paired": True, "known": False}
    standby = {"awake": False, "playing": False, "paired": True, "known": True}
    unpaired = {"awake": False, "playing": False, "paired": False, "known": False}
    for offset, status in enumerate((silent, standby, silent, unpaired)):
        analytics.record_status(controller, status, at=base + offset * 30)
    assert [state for (state,) in rows(configs.root, "SELECT state FROM transitions ORDER BY ts")] == [
        "unknown", "standby", "deep", "unknown"]


def test_an_answered_sample_counts_as_known_for_later_status(make, configs):
    controller = make(SamplingTV(power="standby", art="off"))
    analytics.sample_if_due(controller, now=time.time() - 60)
    analytics.flush()
    analytics._stores.clear()  # a restart: the state comes back from the file
    analytics.record_status(make(SamplingTV()), {"awake": False, "playing": False, "paired": True, "known": False})
    assert rows(configs.root, "SELECT state FROM transitions") == [("deep",)]


def test_old_unknown_spans_with_unanswered_samples_read_as_deep_standby(tmp_path):
    """The main TV answered 0 of 42 samples overnight; that night reads as deep standby, not Unknown."""
    now = time.time()
    night = now - 12 * 3600

    def seed(db):
        db.executemany("INSERT INTO transitions VALUES (?, ?)", [
            (night - 7200, "unknown"),  # before the TV ever answered: stays unknown
            (night - 3600, "awake"),
            (night, "unknown"),  # the TV stopped answering: deep standby
            (night + 10.5 * 3600, "unknown")])  # never happens in a file, but must merge, not split
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 0, 3000, 'unknown', "
                       "NULL, NULL, NULL, NULL, NULL, NULL)", [(night + index * 900, ) for index in range(1, 43)])
    analytics.store(tmp_path).write(seed)
    report = analytics.report(tmp_path, "UTC", 7, now=now)
    assert report["answered"] == 0 and report["samples"] == 42
    assert report["tracked_hours"] == pytest.approx(14)
    deep = sum(day["deep"] for day in report["daily"])
    unknown = sum(day["unknown"] for day in report["daily"])
    assert deep == pytest.approx(12) and unknown == pytest.approx(1)


def test_a_host_gap_without_samples_stays_unknown():
    transitions = [(0, "standby"), (1000, "unknown"), (5000, "awake"), (6000, "unknown"), (9000, "standby")]
    assert analytics.resolve(transitions, [6100, 6900], first_known=0) == [
        (0, "standby"), (1000, "unknown"), (5000, "awake"), (6000, "deep"), (9000, "standby")]
    assert analytics.resolve(transitions, [6100], first_known=None)[3] == (6000, "unknown")


@pytest.mark.parametrize("status,state", [
    ({"paired": True, "known": False, "answered": False}, "deep"),
    ({"paired": True, "known": False}, "deep"),  # an older status without "answered"
    ({"paired": True, "known": False, "answered": True}, "unknown"),  # on the network, state unclear
    ({"paired": True, "known": True, "answered": True}, "standby"),
    ({"paired": False, "known": False, "answered": False}, "unknown"),
])
def test_state_of_shows_deep_standby_only_for_a_tv_that_did_not_answer(status, state):
    assert analytics.state_of({"awake": False, "playing": False, **status}) == state


def test_an_answering_tv_with_an_unclear_state_is_not_deep_standby(make, configs):
    controller = make(SamplingTV())
    base = time.time()
    standby = {"awake": False, "playing": False, "paired": True, "known": True, "answered": True}
    unclear = {"awake": False, "playing": False, "paired": True, "known": False, "answered": True}
    for offset, status in enumerate((standby, unclear)):
        analytics.record_status(controller, status, at=base + offset * 30)
    assert [state for (state,) in rows(configs.root, "SELECT state FROM transitions ORDER BY ts")] == [
        "standby", "unknown"]


def test_resolve_leaves_spans_from_after_the_deep_format_alone():
    transitions = [(0, "standby"), (1000, "unknown"), (5000, "awake"), (6000, "unknown"), (9000, "standby")]
    assert analytics.resolve(transitions, [1100, 6100], first_known=0, deep_since=5500) == [
        (0, "standby"), (1000, "deep"), (5000, "awake"), (6000, "unknown"), (9000, "standby")]


def test_the_file_marks_when_it_started_storing_deep_standby(tmp_path):
    """A new file stores "deep" itself, so its "unknown" spans (unpaired, for example) stay unknown."""
    now = time.time()
    analytics.store(tmp_path).write(lambda db: None)
    stamp = float(rows(tmp_path, "SELECT value FROM meta WHERE key = 'deep_since'")[0][0])
    assert now - 5 <= stamp <= time.time()

    def seed(db):
        db.executemany("INSERT INTO transitions VALUES (?, ?)", [
            (stamp + 10, "awake"), (stamp + 3600, "unknown"), (stamp + 7200, "standby")])
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 0, 3000, 'unknown', "
                       "NULL, NULL, NULL, NULL, NULL, NULL)", [(stamp + 3600 + index * 900,) for index in range(1, 4)])
    analytics.store(tmp_path).write(seed)
    report = analytics.report(tmp_path, "UTC", 7, now=stamp + 7300)
    assert sum(day["deep"] for day in report["daily"]) == 0
    assert sum(day["unknown"] for day in report["daily"]) == pytest.approx(1, abs=.01)


def test_an_older_file_with_deep_spans_is_marked_from_its_first_deep_span(tmp_path):
    with closing(sqlite3.connect(tmp_path / analytics.FILE)) as db, db:
        db.executescript(analytics.SCHEMA)
        db.executemany("INSERT INTO transitions VALUES (?, ?)", [(1000.0, "standby"), (2000.0, "deep")])
    analytics.store(tmp_path).write(lambda db: None)
    assert rows(tmp_path, "SELECT value FROM meta WHERE key = 'deep_since'") == [("2000.0",)]


def test_samples_of_an_unpaired_tv_are_no_evidence_of_deep_standby(tmp_path):
    unpaired = Television(Config().tv, tmp_path)  # no address or token: never contacts anything
    row = analytics.measure(unpaired)
    assert row["answered"] == 0 and row["power"] == analytics.UNPAIRED
    status_only = SimpleNamespace(status=lambda: {"awake": False, "playing": False, "paired": False, "known": False})
    assert analytics.measure(status_only)["power"] == analytics.UNPAIRED
    now = time.time()
    night = now - 12 * 3600

    def seed(db):
        db.execute("DELETE FROM meta WHERE key = 'deep_since'")  # an older file
        db.executemany("INSERT INTO transitions VALUES (?, ?)", [(night - 3600, "awake"), (night, "unknown")])
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 0, 0, 'unpaired', "
                       "NULL, NULL, NULL, NULL, NULL, NULL)", [(night + index * 900,) for index in range(1, 40)])
    analytics.store(tmp_path).write(seed)
    report = analytics.report(tmp_path, "UTC", 7, now=now)
    assert sum(day["deep"] for day in report["daily"]) == 0


def test_controller_hooks_record_wake_delivery_and_failures(make, configs):
    controller = make(SamplingTV())
    controller._wake_result("Wake confirmed at 07:00")
    controller._wake_result("Wake was attempted; the TV did not confirm Art Mode", quiet=True)
    analytics.record_delivery(controller, "published", 8.25)
    analytics.record_delivery(controller, "asleep", 0)  # not a delivery
    controller._report("publish", ValueError("x"))
    controller._report("publish", ValueError("x"))  # the same stage again within minutes counts once
    events = rows(configs.root, "SELECT kind, outcome, latency_ms FROM events ORDER BY ts")
    assert events == [("wake", "confirmed", None), ("wake", "failed", None), ("delivery", "published", 8250),
                      ("failure", "publish", None)]


def test_delivery_time_covers_only_the_publish(configs, library):
    configs.save(Config(tv={"ip": "192.168.1.50"}))
    screen = SimpleNamespace(
        status=lambda: {"awake": True, "playing": False, "paired": True, "known": True},
        publish=lambda image, owned, record, keep=(): (record("MY_F1"), ("MY_F1", ["MY_F1"], True))[1])

    def slow_capture(*args, **kwargs):
        time.sleep(.3)
        return b"image"
    controller = Controller(configs, library, tv_factory=lambda cfg: screen, capture=slow_capture)
    assert controller.tick(force=True) == "published"
    [(latency,)] = rows(configs.root, "SELECT latency_ms FROM events WHERE kind = 'delivery'")
    assert latency < 200


def test_scheduler_survives_a_failing_sampler(configs, library, monkeypatch):
    from art_frames.displays import Displays

    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(analytics, "measure", broken)
    controller = Controller(configs, library, tv_factory=lambda cfg: SamplingTV())
    displays = Displays(configs, library, controller)
    displays.tick()
    assert displays.last_pass is not None


def test_prune_keeps_one_year_and_the_state_at_the_cutoff(tmp_path):
    target = analytics.store(tmp_path)
    now = time.time()
    old, older, recent = now - 400 * 86400, now - 500 * 86400, now - 10 * 86400

    def seed(db):
        db.executemany("INSERT INTO samples (ts, answered, power) VALUES (?, 1, 'on')", [(old,), (recent,)])
        db.executemany("INSERT INTO events VALUES (?, 'wake', 'confirmed', NULL)", [(old,), (recent,)])
        db.executemany("INSERT INTO transitions VALUES (?, ?)", [(older, "standby"), (old, "awake"), (recent, "standby")])
    target.write(seed)
    target.write(lambda db: analytics.prune(db, now))
    assert rows(tmp_path, "SELECT ts FROM samples") == [(recent,)]
    assert rows(tmp_path, "SELECT ts FROM events") == [(recent,)]
    assert rows(tmp_path, "SELECT ts, state FROM transitions ORDER BY ts") == [(old, "awake"), (recent, "standby")]


def test_flush_returns_within_its_timeout():
    assert analytics.flush(timeout=1) is True


def test_damaged_file_is_logged_once_and_never_raises(tmp_path, caplog):
    (tmp_path / analytics.FILE).write_bytes(b"not a database " * 200)
    target = analytics.store(tmp_path)
    with caplog.at_level(logging.WARNING, logger="art_frames.analytics"):
        target.write(lambda db: None)
        target.write(lambda db: None)
        assert analytics.report(tmp_path, "UTC", 7)["unreadable"]
        assert analytics.report(tmp_path, "UTC", 7)["unreadable"]
    messages = [record.getMessage() for record in caplog.records]
    assert sum("could not be written" in text for text in messages) == 1
    assert sum("could not be read" in text for text in messages) == 1


def test_averages_count_tracked_time_not_calendar_days(tmp_path):
    now = time.time()
    analytics.store(tmp_path).write(lambda db: db.executemany("INSERT INTO transitions VALUES (?, ?)",
                                                              [(now - 6 * 3600, "awake"), (now - 3 * 3600, "standby")]))
    report = analytics.report(tmp_path, "UTC", 7, now=now)
    assert report["tracked_hours"] == pytest.approx(6)
    assert report["per_day"]["awake"] == pytest.approx(12)  # 3 of 6 tracked hours, scaled to 24 hours


def auth():
    return {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


def test_page_needs_auth_and_a_known_tv(app):
    client = app.test_client()
    assert client.get("/analytics").status_code == 401
    assert client.get("/analytics?tv=nope", headers=auth()).status_code == 404
    assert client.get("/tv/main/health", headers=auth()).status_code == 200
    assert client.get("/tv/nope/insights", headers=auth()).status_code == 404


def test_old_analytics_url_redirects_to_health_keeping_the_range(app):
    client = app.test_client()
    response = client.get("/analytics?tv=main&days=30", headers=auth())
    assert response.status_code == 301 and response.location == "/tv/main/health?days=30"
    assert client.get("/analytics?tv=main", headers=auth()).location == "/tv/main/health"


def test_page_renders_empty_state(app):
    response = app.test_client().get("/tv/main/health", headers=auth())
    assert response.status_code == 200
    assert "Charts fill in as samples arrive." in response.text
    assert "<script>" not in response.text and "style=" not in response.text


def test_page_shows_a_plain_state_for_a_damaged_file(app, tmp_path):
    (tmp_path / analytics.FILE).write_bytes(b"not a database " * 200)
    response = app.test_client().get("/tv/main/health", headers=auth())
    assert response.status_code == 200 and "Analytics data could not be read" in response.text


def test_page_renders_synthetic_data(app, tmp_path):
    now = time.time()
    start = now - 20 * 86400
    target = analytics.store(tmp_path)

    def seed(db):
        states = ("standby", "awake", "playing", "awake")
        db.executemany("INSERT INTO transitions VALUES (?, ?)",
                       [(start + index * 3 * 3600, states[index % 4]) for index in range(160)])
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(start + index * 900, index % 9 != 0, 120, "on", 1, 0, 5 + index % 7, (index // 50) % 2, 0, "MY_F1")
                        for index in range(1900)])
        db.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", [
            (now - 3 * 86400, "wake", "confirmed", None), (now - 2 * 86400, "wake", "failed", None),
            (now - 86400, "delivery", "published", 8000), (now - 3600, "delivery", "unconfirmed", 12000),
            (now - 600, "failure", "render", None)])
    target.write(seed)
    client = app.test_client()
    for days in (7, 30, 90):
        response = client.get(f"/tv/main/health?days={days}", headers=auth())
        assert response.status_code == 200
        text = response.text
        assert "Time by state each day" in text and "<svg" in text and "fill-awake" in text
        assert "heat-" in text and "line-brightness" in text and "Turned on between" in text
        assert "The website could not be turned into an image." in text
        assert "1 of 2 (50%)" in text and "10.0 s" in text
        assert "Off (deep standby)" in text and "fill-deep" in text and "Standby or off" not in text
        assert "<td>11 PM</td>" in text  # the heatmap table lists all 24 hours
        assert "style=" not in text and "<script>" not in text
        assert f'href="/tv/main/health?days={days}"' in text  # switching TV keeps the page and range
    assert client.get("/tv/main/health?days=bogus", headers=auth()).status_code == 200


def test_page_zooms_to_less_than_a_day_of_readings(app, tmp_path):
    now = time.time()
    analytics.store(tmp_path).write(lambda db: db.executemany(
        f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 1, 100, 'on', 1, 0, ?, 0, 0, NULL)",
        [(now - index * 900, 5 + index) for index in range(4)]))
    text = app.test_client().get("/tv/main/health?days=30", headers=auth()).text
    assert "Less than a day of readings" in text and "dot-brightness" in text


def test_home_and_tv_page_link_to_insights(app):
    client = app.test_client()
    assert 'href="/tv/main/health"' in client.get("/", headers=auth()).text
    assert 'href="/tv/main/health"' in client.get("/tv/main", headers=auth()).text


def test_heatmap_gives_zero_its_own_step_and_lists_every_step():
    from art_frames import analytics_web

    assert analytics_web.heat_class(0) == "heat-zero"
    assert [analytics_web.heat_class(value) for value in (1, 12, 13, 60)] == ["heat-1", "heat-1", "heat-2", "heat-5"]
    assert [css for _, css in analytics_web.HEAT_LEGEND] == ["heat-none", "heat-zero", *[css for *_, css in analytics_web.HEAT]]
    grid = analytics_web.heatmap({(0, 3): {"art": 0, "tracked": 3600}})
    assert grid["rows"][0]["cells"][3]["class"] == "heat-zero" and grid["rows"][0]["cells"][4]["class"] == "heat-none"
    assert len(grid["table"]) == 24 and grid["table"][3]["cells"][0] == 0


@pytest.mark.parametrize("gap", [True, False])
def test_the_first_rows_after_an_upgrade_are_not_resolved(make, configs, gap):
    """deep_since is the earliest time the new code wrote, not the time the writer ran, so the
    restart-gap row and the first transition it stores keep their stored "unknown"."""
    base = time.time() - 2 * 86400
    with closing(sqlite3.connect(configs.root / analytics.FILE)) as db, db:  # a file from before "deep"
        db.executescript(analytics.SCHEMA)
        db.execute("INSERT INTO transitions VALUES (?, 'awake')", (base,))
        last = base + 900 if gap else base + 10 * 3600 - 600
        db.execute(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 1, 100, 'on', "
                   "NULL, NULL, NULL, NULL, NULL, NULL)", (last,))
    unclear = {"awake": False, "playing": False, "paired": True, "known": False, "answered": True}
    first = base + 10 * 3600
    analytics.record_status(make(SamplingTV()), unclear, at=first)
    analytics.flush()

    def seed(db):  # the new code's later samples: the TV did not answer
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 0, 3000, 'unknown', "
                       "NULL, NULL, NULL, NULL, NULL, NULL)", [(first + index * 900,) for index in range(1, 8)])
    analytics.store(configs.root).write(seed)
    expected = last + analytics.SAMPLE_SECONDS if gap else first
    assert float(rows(configs.root, "SELECT value FROM meta WHERE key = 'deep_since'")[0][0]) == expected
    assert rows(configs.root, "SELECT state FROM transitions ORDER BY ts") == [("awake",), ("unknown",)]
    report = analytics.report(configs.root, "UTC", 7, now=first + 4 * 3600)
    assert sum(day["deep"] for day in report["daily"]) == 0
