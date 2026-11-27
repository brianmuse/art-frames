"""Sensor control safety: every way control ends restores the TV, writes never overwrite each other, and
the sensor's input has limits. Several tests began as review probes that showed a real bug.

The rig and client fixtures come from test_sensors.py, so the tests take them as arguments.
"""
# ruff: noqa: F811

import http.client
import ipaddress
import sys
import threading
import time
import types
from datetime import UTC, datetime

import pytest
from test_sensors import (  # noqa: F401  (rig and client are fixtures)
    CountingTV,
    client,
    connected,
    light,
    readings_for,
    rig,
    switch,
)
from test_web import auth, csrf

from art_frames import sensor_control, sensor_curve, sensor_discovery, sensors, timewords
from art_frames.config import Sensor
from art_frames.television import DisplayError, Readback

KEY = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="


def samsung(rig):
    screen = rig["screen"]
    return sensor_control.samsung_step(screen, screen.configs.load(), rig["memory"], rig["clock"].now)


def turn_samsung_off(rig):
    sensor_control.request_samsung(rig["screen"].library, "off")
    assert samsung(rig) == "done"
    assert rig["display"]["sensor"] == "off" and rig["display"]["motion_timer"] == "off"


def later(rig, seconds=3600):
    rig["clock"].now += seconds
    rig["status"]()


# Every way control ends restores the Samsung settings and undoes the away dim.


def test_a_restore_asked_for_while_the_hub_turns_them_off_is_kept(rig):
    screen = rig["screen"]

    class RacingTV(CountingTV):
        def set_light_sensor(self, on):
            result = super().set_light_sensor(on)
            sensor_control.request_samsung(screen.library, "restore")  # the page, while the hub works
            return result

    screen.controller.tv_factory = lambda cfg: RacingTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "done"
    assert screen.library.state(sensor_control.SAMSUNG)["want"] == "restore"
    later(rig, 5)
    assert samsung(rig) == "done"  # the next tick, not after the 5-minute retry
    assert rig["display"]["sensor"] == "on" and rig["display"]["motion_timer"] == "15"


def test_connecting_another_sensor_restores_the_samsung_settings(rig):
    screen, app = rig["screen"], rig["app"]
    turn_samsung_off(rig)
    browser = app.test_client()
    response = browser.post("/tv/main/sensor/connect", headers=auth(),
                            data={"csrf": csrf(browser), "host": "192.168.1.231"})
    assert response.status_code == 302 and response.location.endswith("#sensor-status")
    assert screen.configs.load().sensor.enabled is False
    assert screen.library.state(sensor_control.SAMSUNG)["want"] == "restore"
    later(rig)
    assert samsung(rig) == "done"
    assert rig["display"]["sensor"] == "on" and rig["display"]["motion_timer"] == "15"


def test_the_hub_restores_after_any_settings_change_that_ends_control(rig):
    turn_samsung_off(rig)
    rig["screen"].configs.mutate(lambda raw: raw["sensor"].update(enabled=False))  # such as an edited file
    assert rig["run"]() == "off"
    assert rig["screen"].library.state(sensor_control.SAMSUNG)["want"] == "restore"
    later(rig)
    assert samsung(rig) == "done" and rig["display"]["sensor"] == "on"


def test_turning_control_off_undoes_the_away_dim(rig):
    rig["enable"](away_after=5)
    rig["display"]["brightness"] = 7
    light(rig, 1000.0, presence=False)
    rig["screen"].library.set_state(sensor_control.CONTROL, {"sent": 7, "sent_at": rig["clock"].now})
    rig["run"]()
    later(rig, 5 * 60)
    light(rig, 1000.0, presence=False)
    assert rig["run"]() == "away" and rig["display"]["brightness"] == 0
    browser = rig["app"].test_client()
    switch(rig["app"], browser, "off")
    control = rig["screen"].library.state(sensor_control.CONTROL)
    assert control["away"] is False and control["sent"] is None  # the session is forgotten
    later(rig)
    assert samsung(rig) == "done"
    assert rig["display"]["brightness"] == 7


def test_remove_restores_and_forgets_the_session(rig):
    turn_samsung_off(rig)
    rig["screen"].library.set_state(sensor_control.CONTROL, {"sent": 4, "sent_at": 1.0, "paused_until": 9e12})
    browser = rig["app"].test_client()
    browser.post("/tv/main/sensor/remove", headers=auth(), data={"csrf": csrf(browser)})
    control = rig["screen"].library.state(sensor_control.CONTROL)
    assert all(not control.get(key) for key in sensor_control.SESSION)
    assert rig["screen"].library.state(sensor_control.SAMSUNG)["want"] == "restore"


def test_control_waits_until_the_samsung_settings_are_off(rig):
    sensor_control.request_samsung(rig["screen"].library, "off")
    light(rig, 1000.0)
    assert rig["run"]() == "samsung"  # the TV's own light sensor still changes the brightness
    assert samsung(rig) == "done"
    assert rig["run"]() == "wait"


# A new session starts clean.


def test_turning_control_on_again_starts_no_pause(rig):
    light(rig, 1000.0)
    rig["run"]()
    readings_for(rig, 1000.0, 30)
    assert rig["screen"].library.state(sensor_control.CONTROL)["sent"] == 10
    rig["screen"].configs.mutate(lambda raw: raw["sensor"].update(enabled=False))
    assert rig["run"]() == "off"  # the hub runs while control is off
    rig["clock"].now += 86400
    rig["display"]["brightness"] = 6  # the TV's own light sensor moved it meanwhile
    rig["status"]()
    rig["screen"].controller.display_settings()
    record = rig["screen"].library.state("art_display")
    record["checked_at"] = datetime.fromtimestamp(rig["clock"].now, UTC).isoformat()
    rig["screen"].library.set_state("art_display", record)
    rig["enable"]()
    assert "paused" not in readings_for(rig, 1000.0, 30)


def test_switching_on_clears_the_last_session(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    library = app.extensions["library"]
    library.set_state(sensor_control.CONTROL, {"sent": 4, "sent_at": 1.0, "paused_until": 9e12, "away": True})
    switch(app, browser, "on")
    control = library.state(sensor_control.CONTROL)
    assert all(not control.get(key) for key in sensor_control.SESSION)


# Late readbacks.


def test_a_late_readback_is_no_change_by_hand(rig):
    class LateTV(CountingTV):
        def set_brightness(self, value):
            before = self._settings()
            self.calls.append(("brightness", value))
            self.display["brightness"] = value
            return Readback(value, before["brightness"], before["brightness"], True, before)  # reports the old level

    rig["screen"].controller.tv_factory = lambda cfg: LateTV(rig["display"], rig["calls"])
    light(rig, 1000.0)
    rig["run"]()
    readings_for(rig, 1000.0, 30)
    state = rig["screen"].library.state(sensor_control.CONTROL)
    assert (state["sent"], state["unconfirmed"]) == (10, 1)  # the level asked for, not confirmed yet
    assert "paused" not in readings_for(rig, 1000.0, 120)
    assert rig["calls"].count(("brightness", 10)) == 1


def test_three_unconfirmed_changes_hold_the_brightness(rig):
    # A clock near the real one, so the stored TV readings count as newer than each change.
    rig["clock"].now = time.time() - 3600
    rig["status"]()

    class StuckTV(CountingTV):
        def set_brightness(self, value):
            before = self._settings()
            self.calls.append(("brightness", value))
            return Readback(value, before["brightness"], before["brightness"], True, before)  # nothing changes

    rig["screen"].controller.tv_factory = lambda cfg: StuckTV(rig["display"], rig["calls"])
    light(rig, 1000.0)
    rig["run"]()
    results = readings_for(rig, 1000.0, 600)
    assert rig["calls"].count(("brightness", 10)) == sensor_control.MAX_UNCONFIRMED
    assert results[-1] == "unconfirmed" and "paused" not in results
    browser = rig["app"].test_client()
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "<dd>Holding the brightness" in body and "Resume now" in body
    sensor_control.resume(rig["screen"].library)
    assert rig["screen"].library.state(sensor_control.CONTROL)["unconfirmed"] == 0


# The away dim.


def test_the_away_dim_never_resends_the_current_level(rig):
    rig["enable"](away_after=5)
    rig["display"]["brightness"] = 1  # already the darkest level
    light(rig, 1.0, presence=False)
    rig["screen"].library.set_state(sensor_control.CONTROL, {"sent": 1, "sent_at": rig["clock"].now})
    rig["run"]()
    later(rig, 5 * 60)
    light(rig, 1.0, presence=False)
    assert rig["run"]() == "away"
    assert ("brightness", 1) not in rig["calls"]
    state = rig["screen"].library.state(sensor_control.CONTROL)
    assert state["away"] is True and state["before_away"] == 1


def test_the_away_dim_waits_for_a_pause(rig):
    rig["enable"](away_after=5)
    rig["screen"].library.set_state(sensor_control.CONTROL, {"sent": 5, "paused_until": rig["clock"].now + 3600})
    light(rig, 1.0, presence=False)
    rig["run"]()
    later(rig, 10 * 60)
    light(rig, 1.0, presence=False)
    assert rig["run"]() == "paused" and sensor_curve.paused(rig["screen"].library.state(sensor_control.CONTROL),
                                                             rig["clock"].now)
    assert not any(call[0] == "brightness" for call in rig["calls"])


# Round 2 of the review probes, as tests that must pass.


def test_switching_off_during_the_away_dim_still_restores_the_level(rig):
    screen = rig["screen"]
    rig["display"]["sensor"] = "off"  # the TV's own light sensor was off, so nothing else moves the level
    rig["enable"](away_after=5)
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "done"
    rig["display"]["brightness"] = 7
    light(rig, 1000.0, presence=False)
    screen.library.set_state(sensor_control.CONTROL, {"sent": 7, "sent_at": rig["clock"].now})
    rig["run"]()
    later(rig, 5 * 60)
    light(rig, 1000.0, presence=False)

    class RacingTV(CountingTV):
        def set_brightness(self, value):
            result = super().set_brightness(value)
            # The page turns control off while the hub's dim command runs.
            screen.configs.mutate(lambda raw: raw["sensor"].update(enabled=False))
            sensor_control.release(screen.library, lambda: not screen.configs.load().sensor.enabled)
            return result

    screen.controller.tv_factory = lambda cfg: RacingTV(rig["display"], rig["calls"])
    assert rig["run"]() == "away" and rig["display"]["brightness"] == 0
    assert rig["run"]() == "off"  # the next tick sees the away state and adds its level to the restore
    assert screen.library.state(sensor_control.SAMSUNG)["undo_away"] == 7
    later(rig)
    assert samsung(rig) == "done"
    assert rig["display"]["brightness"] == 7


def test_a_refused_samsung_off_lets_control_run(rig):
    screen = rig["screen"]

    class RefusingTV(CountingTV):
        def set_light_sensor(self, on):
            self.calls.append(("sensor", on))
            raise DisplayError("rejected")

    screen.controller.tv_factory = lambda cfg: RefusingTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "rejected"
    state = screen.library.state(sensor_control.SAMSUNG)
    assert state["want"] is None and state["saved"] is None  # no retries, and nothing Art Frames changed
    results = []
    for _ in range(24):  # 2 hours
        later(rig, 300)
        samsung(rig)
        light(rig, 1000.0)
        results.append(rig["run"]())
    assert "samsung" not in results
    assert [call for call in rig["calls"] if call[0] == "brightness"]
    assert rig["calls"].count(("sensor", False)) == 1
    body = rig["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(True)
    assert "The TV refused to turn off its own light sensor and Sleep After. Turn them off on the TV." in body
    assert "Art Frames turns off the TV" not in body  # no promise to try again


def test_an_unconfirmed_samsung_off_stops_after_three_tries(rig):
    screen = rig["screen"]

    class IgnoringTV(CountingTV):
        def set_light_sensor(self, on):
            before = self._settings()
            self.calls.append(("sensor", on))
            return Readback("off", "on", "on", True, before)  # the TV keeps its own sensor on

    screen.controller.tv_factory = lambda cfg: IgnoringTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "off")
    results = []
    for _ in range(6):
        samsung(rig)
        later(rig, 300)
        light(rig, 1000.0)
        results.append(rig["run"]())
    assert rig["calls"].count(("sensor", False)) == sensor_control.MAX_SAMSUNG_TRIES
    assert results[-1] != "samsung"
    body = rig["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(True)
    assert "The TV did not turn off its own light sensor and Sleep After. Check them on the TV." in body


def test_a_remote_change_while_a_change_is_unconfirmed_pauses(rig):
    class LateTV(CountingTV):
        def set_brightness(self, value):
            before = self._settings()
            self.calls.append(("brightness", value))
            self.display["brightness"] = value
            return Readback(value, before["brightness"], before["brightness"], True, before)

    rig["clock"].now = time.time() - 3600  # near the real clock, so stored TV readings count as newer
    rig["status"]()
    rig["screen"].controller.tv_factory = lambda cfg: LateTV(rig["display"], rig["calls"])
    light(rig, 1000.0)
    rig["run"]()
    readings_for(rig, 1000.0, 30)
    assert rig["screen"].library.state(sensor_control.CONTROL)["unconfirmed"] == 1
    rig["display"]["brightness"] = 3  # someone uses the remote
    results = readings_for(rig, 1000.0, 120)
    assert "paused" in results
    assert rig["calls"].count(("brightness", 10)) == 1  # the person's 3 stays


def test_a_tv_that_rounds_the_level_is_neither_held_nor_asked_again(rig):
    class RoundingTV(CountingTV):
        def set_brightness(self, value):
            before = self._settings()
            self.calls.append(("brightness", value))
            self.display["brightness"] = value - 1  # this TV sets one step lower
            return Readback(value, value - 1, before["brightness"], True, before)

    rig["clock"].now = time.time() - 3600
    rig["status"]()
    rig["screen"].controller.tv_factory = lambda cfg: RoundingTV(rig["display"], rig["calls"])
    light(rig, 1000.0)
    rig["run"]()
    results = readings_for(rig, 1000.0, 600)
    assert "unconfirmed" not in results and "paused" not in results
    assert rig["calls"].count(("brightness", 10)) == 1
    state = rig["screen"].library.state(sensor_control.CONTROL)
    assert (state["sent"], state["unconfirmed"], state["asked"], state["got"]) == (9, 0, 10, 9)


# Writes from the page and the hub.


def test_a_page_write_during_a_slow_change_is_kept(rig):
    """resume() while the hub sends a level: the hub's write keeps the page's change to other fields."""
    screen = rig["screen"]

    class SlowTV(CountingTV):
        def set_brightness(self, value):
            sensor_control.merge(screen.library, sensor_control.CONTROL, note="from the page")
            return super().set_brightness(value)

    screen.controller.tv_factory = lambda cfg: SlowTV(rig["display"], rig["calls"])
    light(rig, 1000.0)
    rig["run"]()
    readings_for(rig, 1000.0, 30)
    state = screen.library.state(sensor_control.CONTROL)
    assert state["sent"] == 10 and state["note"] == "from the page"


def test_one_lock_per_tv():
    class Library:
        root = "/data"

    class Other(Library):
        display_id = "den"
    assert sensor_control.state_lock(Library()) is sensor_control.state_lock(Library())
    assert sensor_control.state_lock(Library()) is not sensor_control.state_lock(Other())


def test_parallel_requests_never_lose_a_write(rig):
    library = rig["screen"].library
    threads = [threading.Thread(target=sensor_control.merge, args=(library, sensor_control.CONTROL),
                                kwargs={f"field{index}": index}) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = library.state(sensor_control.CONTROL)
    assert all(state[f"field{index}"] == index for index in range(20))


# The Picture page.


def test_brightness_set_on_the_picture_page_is_no_change_by_hand(client, monkeypatch):
    app, browser = client
    controller = app.extensions["controller"]
    monkeypatch.setattr(controller, "set_brightness", lambda value: Readback(value, value, 5, True, {"brightness": value}))
    browser.post("/screen/display/brightness", headers=auth(), data={"csrf": csrf(browser), "brightness": "3"},
                 follow_redirects=True)
    assert app.extensions["library"].state(sensor_control.CONTROL)["sent"] == 3


def test_the_picture_page_says_the_sensor_controls_brightness(client):
    app, browser = client
    assert 'id="sensor-note"' not in browser.get("/tv/main/picture", headers=auth()).get_data(as_text=True)
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", enabled=True))
    body = browser.get("/tv/main/picture", headers=auth()).get_data(as_text=True)
    assert "The sensor sets the brightness from the room light." in body
    assert 'href="/tv/main/sensor">Sensor settings</a>' in body and 'name="brightness"' not in body


# Input limits.


def stream(lines, held=None):
    """A fake HTTPConnection that serves `lines`. With `held`, it notes how many bytes of event data the
    client holds before each read, so a test can check the cap while the event is still coming in."""
    class Response:
        status = 200

        class fp:
            @staticmethod
            def readline(limit):
                if held is not None:
                    data = sys._getframe(1).f_locals.get("data") or []
                    held.append(sum(len(part) for part in data))
                return lines.pop(0)

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass
    return Connection


def test_one_web_event_holds_at_most_16_kb(monkeypatch):
    lines = ([b"event: state\n"] + [b"data: " + b"x" * 4000 + b"\n"] * 5000 + [b"\n"]
             + [b"event: state\n", b'data: {"id":"sensor-ambient_light","value":3}\n', b"\n", b""])
    seen, held = [], []
    monkeypatch.setattr(http.client, "HTTPConnection", stream(lines, held))
    monkeypatch.setattr(sensors, "parse_event", lambda event, data, readings: seen.append(len(data)))
    with pytest.raises(ConnectionError):
        sensors.WebClient("192.0.2.1").run(sensors.Readings(), threading.Event())
    assert seen == [len('{"id":"sensor-ambient_light","value":3}')]  # the big event was dropped
    # The client never holds more than 16 KB of one event, not even while it still arrives.
    assert len(held) > 5000 and max(held) <= sensors.MAX_EVENT


def test_readings_keep_at_most_256_entities_and_1000_samples():
    readings = sensors.Readings()
    readings.start("web")
    for index in range(300):
        readings.add_entity(sensors.Entity("sensor", f"s{index}"))
        readings.update("sensor", f"s{index}", 1.0)
    assert len(readings.entities) == sensors.MAX_ENTITIES and len(readings.values) == sensors.MAX_ENTITIES
    for _ in range(1200):
        readings.update("sensor", "s0", 2.0)
    assert len(readings.samples("s0")) == sensors.MAX_SAMPLES


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::ffff:127.0.0.1", "::ffff:8.8.8.8", "::ffff:0.0.0.0"])
def test_the_sensor_address_refuses_unspecified_and_mapped_public_addresses(host):
    with pytest.raises(ValueError):
        Sensor(host=host)


def test_the_sensor_address_accepts_a_mapped_private_address():
    assert ipaddress.ip_address(Sensor(host="::ffff:192.168.1.5").host).ipv4_mapped.is_private


def test_a_sensor_with_a_key_never_falls_back_to_the_web_server():
    clients = sensors.default_clients(Sensor(host="192.168.1.148", key=KEY))
    assert [item.transport for item in clients] == ["api"]
    assert [item.transport for item in sensors.default_clients(Sensor(host="192.168.1.148"))] == ["api", "web"]


# The MAC suffix.


@pytest.mark.parametrize("name, kept", [
    ("akamatis-smart-device-a1b2c3", "akamatis-smart-device"),
    ("Akamatis Smart Device A1B2C3", "Akamatis Smart Device"),
    ("hall_sensor_00ff00", "hall_sensor"),
    ("apollo-msr-2", "apollo-msr-2"),
    ("living room", "living room"),
])
def test_the_mac_suffix_is_dropped_from_names(name, kept):
    assert sensors.without_mac(name) == kept


def test_device_names_lose_the_mac_suffix():
    readings = sensors.Readings()
    readings.start("api", {"name": "akamatis-smart-device-a1b2c3", "friendly_name": "Akamatis Smart Device a1b2c3"})
    readings.set_device(friendly_name="Hall a1b2c3")
    assert "a1b2c3" not in repr(readings.snapshot())
    found = sensor_discovery.parse_service("akamatis-smart-device-a1b2c3._esphomelib._tcp.local.", ["192.168.1.9"],
                                           6053, {b"friendly_name": b"Akamatis Smart Device A1B2C3"})
    assert (found["name"], found["friendly_name"]) == ("akamatis-smart-device", "Akamatis Smart Device")
    for item in sensor_discovery.DEMO_FOUND:
        assert sensors.without_mac(item["name"]) == item["name"]


# Find sensors.


def test_find_sensors_ends_within_its_deadline(monkeypatch):
    timeouts = []

    class Zeroconf:
        def get_service_info(self, type_, name, timeout):
            timeouts.append(timeout)
            time.sleep(timeout / 1000)

        def close(self):
            pass

    class ServiceBrowser:
        def __init__(self, zc, service, listener):
            for index in range(20):
                listener.add_service(zc, service, f"s{index}.{service}")

        def cancel(self):
            pass
    fake = types.SimpleNamespace(Zeroconf=Zeroconf, ServiceBrowser=ServiceBrowser, ServiceListener=object)
    monkeypatch.setitem(sys.modules, "zeroconf", fake)
    started = time.monotonic()
    assert sensor_discovery.browse(seconds=0, deadline_seconds=0.5) == []
    assert time.monotonic() - started < 1.0 and max(timeouts) <= 500


# Shared time words.


@pytest.mark.parametrize("seconds, words", [
    (3, "just now"), (42, "42 seconds ago"), (60, "1 minute ago"), (600, "10 minutes ago"), (7200, "2 hours ago"),
    (86400 * 3, "3 days ago"),
])
def test_ago_takes_epoch_seconds(seconds, words):
    assert timewords.ago(1_000_000.0, 1_000_000.0 + seconds) == words


def test_ago_takes_iso_stamps_and_datetimes():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert timewords.ago("2026-11-01T11:00:00+00:00", now) == "1 hour ago"
    assert timewords.ago("2026-11-01T11:59:55", now) == "just now"  # no zone: UTC
    assert timewords.ago("not a time", now) == ""


# The hub.


def test_the_hub_forgets_removed_tvs(rig):
    hub = rig["app"].extensions["sensors"]
    hub.memory["gone"] = sensor_control.Memory()
    hub.results["gone"] = "off"
    hub.tick(now=rig["clock"].now)
    assert "gone" not in hub.memory and "gone" not in hub.results and hub.last_pass is not None


# The pages.


def test_connect_opens_the_status_and_an_error_stays_at_the_address(client):
    app, browser = client
    response = browser.post("/tv/main/sensor/connect", headers=auth(),
                            data={"csrf": csrf(browser), "host": "192.168.1.148"})
    assert response.location.endswith("#sensor-status")
    response = browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "8.8.8.8"})
    body = response.get_data(as_text=True)
    assert response.status_code == 400 and "Use the sensor&#39;s private IP address" in body
    assert '<details class="card settings-panel fold-card " id="advanced" open data-fold-remember>' in body


def test_a_key_problem_asks_for_the_key(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    app.extensions["sensors"].reconcile()
    app.extensions["sensors"].link("main").readings.lost("key-wrong")
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert '<a class="button sm" href="#connection">Enter the key</a>' in body
    assert '<details class="card settings-panel fold-card " id="advanced" open data-fold-remember>' in body
    assert "The encryption key does not match the sensor." in body


def test_a_found_sensor_that_needs_its_key_asks_for_it_in_its_row(tmp_path):
    from art_frames.web import create_app
    app = create_app(tmp_path / "demo", demo=True, testing=True)
    browser = app.test_client()
    browser.post("/tv/main/sensor/find", data={"csrf": csrf(browser)})
    body = browser.get("/tv/main/sensor").get_data(as_text=True)
    apollo = body[body.index("<strong>Apollo Automation MSR-2</strong>"):]
    apollo = apollo[:apollo.index("</form>")]
    assert 'name="key" type="password"' in apollo and ">Connect</button>" in apollo


def test_the_page_order_and_words_with_a_sensor(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    order = ['id="sensor-status"', 'id="sensor-control"', 'id="room"', 'id="away"', 'id="points"', 'id="control"',
             'id="advanced"', 'id="remove-sensor"']
    assert [body.index(item) for item in order] == sorted(body.index(item) for item in order)
    assert "Turn on sensor control" in body and "No points yet" in body and "What kind of room is this?" in body
    assert "Add a point from the TV" in body and ">Save points</button>" in body
    assert "Moves every brightness a step or two, and keeps the room." in body
    assert "Remove the sensor? Art Frames forgets its brightness points." in body
    assert " lx" not in body and "Automatic brightness" not in body


def test_the_samsung_line_is_a_statement(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    library, name = app.extensions["library"], app.extensions["configs"].load().tv_name
    library.set_state(sensor_control.SAMSUNG, {"want": "off", "result": "waiting"})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert (f"Art Frames turns off the TV&#39;s own light sensor and Sleep After the next time {name} is on in "
            "Art Mode.") in body
    library.set_state(sensor_control.SAMSUNG, {"want": None, "action": "off", "result": "done"})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert f"The TV&#39;s own light sensor and Sleep After are off while the sensor controls {name}." in body
    library.set_state(sensor_control.SAMSUNG, {"want": None, "action": "restore", "result": "done"})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Art Frames turned the TV&#39;s own light sensor and Sleep After back on." in body


def test_live_json_holds_the_control_line(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    connected(app, 120.0)
    app.extensions["library"].set_state(sensor_control.CONTROL, {"sent": 5, "sent_at": time.time() - 120})
    data = browser.get("/tv/main/sensor/live.json", headers=auth()).get_json()
    assert data["control"] == "Last change 2 minutes ago."
    assert data["light"] == "120 lux" and data["curve"].startswith("Room light 120 lux now → would set brightness")
    assert data["check"]["dot"] == "done"


def test_the_tv_settings_card_shows_sensor_control_above_address_recovery(client):
    app, browser = client
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", enabled=True))
    body = browser.get("/tv/main/settings", headers=auth()).get_data(as_text=True)
    assert "<dt>Sensor control</dt><dd>On</dd>" in body
    assert body.index('id="sensor"') < body.index('id="address-recovery"')


# Round 2 of the page review.


def test_a_search_on_another_page_does_not_open_advanced(tmp_path):
    from art_frames.web import create_app
    app = create_app(tmp_path / "demo", demo=True, testing=True)
    browser = app.test_client()
    browser.post("/tv/main/sensor/find", data={"csrf": csrf(browser)})
    browser.post("/tv/main/sensor/connect", data={"csrf": csrf(browser), "host": "192.168.1.148",
                                                  "name": "Akamatis Smart Device"})
    body = browser.get("/tv/main/sensor").get_data(as_text=True)
    assert 'id="advanced" open' not in body  # the shared results stay, but the fold opens only for notes
    assert app.extensions["sensors"].discovery.found  # never cleared for other pages
    row = body[body.index("<strong>Akamatis Smart Home Device</strong>"):]
    row = row[:row.index("</form>")]
    assert '<span class="badge success">Connected</span>' in row and ">Connect</button>" not in row
    browser.post("/tv/main/sensor/find", data={"csrf": csrf(browser)})
    assert 'id="advanced" open' in browser.get("/tv/main/sensor").get_data(as_text=True)


@pytest.mark.parametrize("failed, words", [(False, "No sensors answered."), (True, "Art Frames cannot search")])
def test_an_empty_search_says_so_in_the_find_card(client, monkeypatch, failed, words):
    app, browser = client
    discovery = app.extensions["sensors"].discovery

    def search():
        discovery.found, discovery.failed, discovery.at = [], failed, time.time()
        return []
    monkeypatch.setattr(discovery, "search", search)
    browser.post("/tv/main/sensor/find", headers=auth(), data={"csrf": csrf(browser)})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert body.count(words) == 1


@pytest.mark.parametrize("code, title, button", [
    ("key-needed", "Encryption key needed", "Enter the key"),
    ("key-wrong", "Encryption key does not match", "Enter the key"),
    ("key-not-used", "The sensor does not use a key", "Remove the key"),
])
def test_key_errors_have_their_own_titles_and_fixes(client, code, title, button):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    switch(app, browser, "on")
    app.extensions["sensors"].reconcile()
    app.extensions["sensors"].link("main").readings.lost(code)
    page = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert title in page and f">{button}</" in page
    health = browser.get("/tv/main/health", headers=auth()).get_data(as_text=True)
    assert title in health and "Art Frames holds the brightness until the key works." in health
    assert f'href="/tv/main/sensor#connection">{button}</a>' in health


def test_remove_the_key_from_the_status_card(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(),
                 data={"csrf": csrf(browser), "host": "192.168.1.148", "key": KEY})
    app.extensions["sensors"].reconcile()
    app.extensions["sensors"].link("main").readings.lost("key-not-used")
    browser.post("/tv/main/sensor/connect", headers=auth(),
                 data={"csrf": csrf(browser), "host": "192.168.1.148", "port": "6053", "remove_key": "on"})
    sensor = app.extensions["displays"].get("main").configs.load().sensor
    assert sensor.host == "192.168.1.148" and not sensor.key.get_secret_value()


def test_the_control_card_asks_to_turn_control_on_only_while_it_is_off(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    words = "Turn on sensor control first"
    assert words in browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    switch(app, browser, "on")
    assert words not in browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)


def test_lux_fields_show_short_numbers(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(curve={"points": [{"lux": 500.0, "level": 8}]}))
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert 'name="lux_0" type="number" min="0.1" step="any" value="500"' in body


def test_the_address_fold_says_enter(client):
    _, browser = client
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    fold = body[body.index('id="connection"'):]
    assert ">Enter<" in fold[:fold.index("</summary>")]


@pytest.mark.parametrize("friendly, product, shown", [
    ("Akamatis Smart Device", "Akamatis Smart Home Device", False),
    ("smart-device", "Akamatis Smart Home Device", False),
    ("Living room sensor", "Akamatis Smart Home Device", True),
])
def test_the_subtitle_shows_only_new_words(friendly, product, shown):
    from art_frames import sensor_web
    assert sensor_web.says_more(friendly, product) is shown


@pytest.mark.parametrize("name", ["Living room 202501", "Hall facade", "Den-decade", "Office beaded"])
def test_saved_names_are_kept_as_they_are(name):
    # Only names from the device lose the MAC suffix (discovery, device info, connect), never a saved one.
    assert Sensor(name=name).name == name


def test_a_local_name_keeps_its_suffix_but_the_page_hides_it(client):
    app, browser = client
    host = "akamatis-smart-device-005301.local"
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": host})
    assert app.extensions["displays"].get("main").configs.load().sensor.host == host  # it must still resolve
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "akamatis-smart-device.local · no key" in body
    assert "005301" not in body.replace(f'value="{host}"', "")  # only the address field holds it



# Round 3 of the review probes and page notes.


def near_real_clock(rig):
    rig["clock"].now = time.time() - 3600  # stored TV readings then count as newer than each change
    rig["status"]()


def test_a_remote_press_during_the_readback_pauses(rig):
    near_real_clock(rig)
    rig["display"]["brightness"] = 3

    class PressedTV(CountingTV):
        def set_brightness(self, value):
            before = self._settings()
            self.calls.append(("brightness", value))
            self.display["brightness"] = 5  # the person presses the remote while the TV applies the change
            return Readback(value, 5, before["brightness"], True, self._settings())

    rig["screen"].controller.tv_factory = lambda cfg: PressedTV(rig["display"], rig["calls"])
    light(rig, 1000.0)  # the curve wants 10, more than one step from 5
    rig["run"]()
    results = readings_for(rig, 1000.0, 120)
    asked = [call for call in rig["calls"] if call[0] == "brightness"][0][1]
    assert abs(asked - 5) > sensor_control.ROUNDING
    assert "paused" in results  # the person's 5 is a change by hand, not rounding
    state = rig["screen"].library.state(sensor_control.CONTROL)
    assert state.get("asked") is None and state["sent"] == 5


def test_a_confirmed_change_forgets_the_rounding(rig):
    library = rig["screen"].library
    library.set_state(sensor_control.CONTROL, {"asked": 10, "got": 9})
    sensor_control.merge(library, sensor_control.CONTROL,
                         **sensor_control._sent(7, Readback(7, 7, 9, True, {}), 0, 1.0, 9))
    state = library.state(sensor_control.CONTROL)
    assert (state["asked"], state["got"]) == (None, None)


def test_a_refused_off_leaves_nothing_to_restore(rig):
    screen = rig["screen"]

    class RefusingTV(CountingTV):
        def set_light_sensor(self, on):
            self.calls.append(("sensor", on))
            raise DisplayError("rejected")

    screen.controller.tv_factory = lambda cfg: RefusingTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "rejected"
    screen.configs.mutate(lambda raw: raw["sensor"].update(enabled=False))
    assert rig["run"]() == "off"
    for _ in range(12):  # one hour
        later(rig, 300)
        samsung(rig)
    assert screen.library.state(sensor_control.SAMSUNG)["want"] is None
    assert ("sensor", True) not in rig["calls"]


def test_a_refused_off_keeps_what_dayframe_did_change(rig):
    screen = rig["screen"]

    class HalfTV(CountingTV):
        def set_option(self, name, value):
            self.calls.append((name, value))
            raise DisplayError("rejected")

    screen.controller.tv_factory = lambda cfg: HalfTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "rejected"
    assert screen.library.state(sensor_control.SAMSUNG)["saved"] == {"sensor": "on"}
    assert rig["display"]["sensor"] == "off"


@pytest.mark.parametrize("failure, words", [
    ("rejected", "The TV refused to turn its own light sensor and Sleep After back on. Check them on the TV."),
    ("ignored", "The TV did not turn its own light sensor and Sleep After back on. Check them on the TV."),
])
def test_a_refused_or_unconfirmed_restore_ends(rig, failure, words):
    screen = rig["screen"]
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "done"

    class NoRestoreTV(CountingTV):
        def set_light_sensor(self, on):
            before = self._settings()
            self.calls.append(("sensor", on))
            if failure == "rejected":
                raise DisplayError("rejected")
            return Readback("on", "off", "off", True, before)

        def set_option(self, name, value):
            return super().set_option(name, value)

    screen.controller.tv_factory = lambda cfg: NoRestoreTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "restore")
    for _ in range(6):
        later(rig, 300)
        samsung(rig)
    state = screen.library.state(sensor_control.SAMSUNG)
    assert state["want"] is None and state["saved"] is None
    tries = 1 if failure == "rejected" else sensor_control.MAX_SAMSUNG_TRIES
    assert rig["calls"].count(("sensor", True)) == tries
    body = rig["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(True)
    assert words in body


@pytest.mark.parametrize("code, words", [
    ("key-needed", "Enter the api: encryption: key: from the sensor"),
    ("key-wrong", "Enter the key again from the sensor"),
    ("key-not-used", "Remove the saved key."),
])
def test_key_error_details_say_what_to_do(code, words):
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[])
    link.readings.lost(code)
    row = sensor_control.health_check(link, Sensor(host="192.168.1.148"), time.time())
    assert row["detail"].startswith(words) and row["when"] == ""


def test_a_sensor_saved_by_its_local_name_is_found_as_connected(tmp_path):
    from art_frames.web import create_app
    app = create_app(tmp_path / "demo", demo=True, testing=True)
    browser = app.test_client()
    host = "akamatis-smart-device.local"  # the demo sensor's mDNS name
    browser.post("/tv/main/sensor/connect", data={"csrf": csrf(browser), "host": host})
    screen = app.extensions["displays"].get("main")
    screen.configs.mutate(lambda raw: raw["sensor"].update(enabled=True, presence_entity="presence",
                                                            curve={"dark_lux": 3.0, "dark_level": 2}))
    browser.post("/tv/main/sensor/find", data={"csrf": csrf(browser)})
    body = browser.get("/tv/main/sensor").get_data(as_text=True)
    row = body[body.index("<strong>Akamatis Smart Home Device</strong>"):]
    assert '<span class="badge success">Connected</span>' in row[:row.index("</form>")]
    other = body[body.index("<strong>Apollo Automation MSR-2</strong>"):]
    assert ">Connect</button>" in other[:other.index("</form>")]
    browser.post("/tv/main/sensor/connect", data={"csrf": csrf(browser), "host": "192.168.1.148", "port": "6053",
                                                  "name": "Akamatis Smart Device"})
    sensor = screen.configs.load().sensor
    assert sensor.host == "192.168.1.148"  # only the address changes
    assert sensor.enabled and sensor.presence_entity == "presence"
    assert [(point.lux, point.level) for point in sensor.curve.points] == [(3.0, 2)]  # an older file's curve


def test_a_same_model_sensor_is_not_the_saved_one():
    from art_frames import sensor_web
    item = sensor_discovery.parse_service("akamatis-smart-device-005302._esphomelib._tcp.local.",
                                          ["192.168.1.231"], 6053, {})
    assert item["name"] == "akamatis-smart-device"
    assert not sensor_web.same_sensor(item, "akamatis-smart-device-005301.local")
    assert sensor_web.same_sensor(item, "akamatis-smart-device-005302.local")


@pytest.mark.parametrize("with_sensor", [False, True])
def test_a_failed_search_points_to_the_address(client, monkeypatch, with_sensor):
    from art_frames import sensor_web
    assert sensor_web.FIND_FAILED.endswith("Enter the sensor's address instead.")  # no direction: it moves
    app, browser = client
    if with_sensor:
        browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    discovery = app.extensions["sensors"].discovery

    def search():
        discovery.found, discovery.failed, discovery.at = [], True, time.time()
        return []
    monkeypatch.setattr(discovery, "search", search)
    browser.post("/tv/main/sensor/find", headers=auth(), data={"csrf": csrf(browser)})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert '<a href="#connection">Enter the sensor\'s address</a>' in body and 'id="connection"' in body


def test_key_actions_come_before_resume(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    switch(app, browser, "on")
    app.extensions["sensors"].reconcile()
    app.extensions["sensors"].link("main").readings.lost("key-wrong")
    app.extensions["library"].set_state(sensor_control.CONTROL, {"paused_until": time.time() + 3600,
                                                                  "paused_at": time.time()})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    status = body[body.index('id="sensor-status"'):body.index('id="room"')]
    assert ">Enter the key</a>" in status and "Resume now" not in status


# Round 4.


def test_each_samsung_request_counts_its_own_tries(rig):
    screen = rig["screen"]

    class SlowTV(CountingTV):
        """Turns its own sensor off for real, but each readback still says "on" (a slow TV)."""

        def set_light_sensor(self, on):
            before = self._settings()
            self.calls.append(("sensor", on))
            self.display["sensor"] = "on" if on else "off"
            return Readback("on" if on else "off", "on" if not on else "off", before["sensor"], True, before)

    screen.controller.tv_factory = lambda cfg: SlowTV(rig["display"], rig["calls"])
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "not-confirmed"
    rig["display"]["sensor"] = "on"  # read again as on, so the next try sends again
    later(rig, 300)
    assert samsung(rig) == "not-confirmed"
    assert screen.library.state(sensor_control.SAMSUNG)["tries"] == 2
    rig["display"]["sensor"] = "off"  # it did turn off in the end
    sensor_control.request_samsung(screen.library, "restore")  # the owner turns control off
    assert screen.library.state(sensor_control.SAMSUNG)["tries"] == 0
    later(rig, 300)
    assert samsung(rig) == "not-confirmed"  # one slow readback on the restore
    state = screen.library.state(sensor_control.SAMSUNG)
    assert state["want"] == "restore" and state["saved"] and state["tries"] == 1  # it tries again


def test_an_old_search_result_never_keeps_settings_for_a_typed_address(rig):
    app, screen = rig["app"], rig["screen"]
    saved = "akamatis-smart-device-005301.local"
    screen.configs.mutate(lambda raw: raw["sensor"].update(host=saved, enabled=True, presence_entity="presence"))
    discovery = app.extensions["sensors"].discovery
    discovery.found = [sensor_discovery.parse_service("akamatis-smart-device-005301._esphomelib._tcp.local.",
                                                      ["192.168.1.50"], 6053, {})]
    discovery.at = 0.0  # a search from 1970: the address may belong to another device now
    browser = app.test_client()
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.50"})
    sensor = screen.configs.load().sensor
    assert sensor.host == "192.168.1.50" and not sensor.enabled and sensor.presence_entity == ""


def test_a_failed_restore_shows_after_remove(rig):
    screen = rig["screen"]
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "done"

    class RefusingTV(CountingTV):
        def set_light_sensor(self, on):
            self.calls.append(("sensor", on))
            raise DisplayError("rejected")

    screen.controller.tv_factory = lambda cfg: RefusingTV(rig["display"], rig["calls"])
    browser = rig["app"].test_client()
    browser.post("/tv/main/sensor/remove", headers=auth(), data={"csrf": csrf(browser)})
    later(rig)
    assert samsung(rig) == "rejected"
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert ('<p role="status" class="alert notice info card-note">The TV refused to turn its own light sensor '
            "and Sleep After back on. Check them on the TV.</p>") in body


def test_a_finished_restore_is_not_shown_after_remove(rig):
    screen = rig["screen"]
    sensor_control.request_samsung(screen.library, "off")
    assert samsung(rig) == "done"
    browser = rig["app"].test_client()
    browser.post("/tv/main/sensor/remove", headers=auth(), data={"csrf": csrf(browser)})
    later(rig)
    assert samsung(rig) == "done"
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "back on" not in body
