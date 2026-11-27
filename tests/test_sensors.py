"""ESPHome presence and light sensors: picking readings, vendors, discovery, smoothing and TV control."""

import base64
import io
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_web import PASSWORD, auth, csrf

from art_frames import sensor_control, sensor_curve, sensor_web, sensors
from art_frames.config import Config, Sensor
from art_frames.sensor_discovery import Discovery, parse_service
from art_frames.television import DemoTelevision
from art_frames.web import create_app

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "akamatis_smart_device.json").read_text())
ENTITIES = [sensors.Entity(item["domain"], item["object_id"], item.get("name", ""), item.get("device_class", ""),
                           item.get("unit", "")) for item in FIXTURE["entities"]]
KEY = base64.b64encode(b"k" * 32).decode()
FAKE_MAC = FIXTURE["txt"]["mac"]
CONTROL_FORM = {"count": "0", "new_lux": "", "new_level": "", "offset": "0"}


def switch(app, browser, enabled):
    """Post the Sensor control card's one-click form ("on" or "off")."""
    return browser.post("/tv/main/sensor/switch", headers=auth(), data={
        "csrf": csrf(browser), "enabled": enabled,
        "sensor_version": sensor_web.version(app.extensions["configs"].load().sensor)})


def control_form(app, browser, **fields):
    """The Control card's form, drawn for the saved settings (its sensor_version)."""
    return {"csrf": csrf(browser), **CONTROL_FORM,
            "sensor_version": sensor_web.version(app.extensions["configs"].load().sensor), **fields}


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# Picking readings


def test_picks_presence_and_light_from_device_classes():
    assert sensors.pick_presence(ENTITIES, "Akamatis.SmartHomeDevice") == "presence"
    assert sensors.pick_light(ENTITIES, "Akamatis.SmartHomeDevice") == "ambient_light"


def test_generic_device_class_path_works_without_a_profile():
    entities = [sensors.Entity("binary_sensor", "pir", "PIR", "motion"),
                sensors.Entity("binary_sensor", "zone_1_occupancy", "Zone 1 Occupancy", "occupancy"),
                sensors.Entity("binary_sensor", "room", "Room", "occupancy"),
                sensors.Entity("sensor", "lux", "Brightness", "", "lx"),
                sensors.Entity("sensor", "temperature", "Temperature", "temperature", "°C")]
    assert sensors.profile_for("Nobody.Unknown") is None
    assert sensors.pick_presence(entities, "Nobody.Unknown") == "room"  # whole room, not a zone or PIR
    assert sensors.pick_light(entities, "Nobody.Unknown") == "lux"  # by unit alone


def test_prefers_presence_over_moving_and_still_targets_without_device_classes():
    # The web server fallback sends names and units only.
    entities = [sensors.Entity(item.domain, item.object_id, item.name, "", item.unit) for item in ENTITIES]
    assert sensors.pick_presence(entities) == "presence"
    assert sensors.pick_light(entities) == "ambient_light"


def test_override_wins_and_nothing_usable_gives_empty():
    assert sensors.pick_presence(ENTITIES, override="still_target") == "still_target"
    assert sensors.pick_light(ENTITIES, override="moving_distance") == "moving_distance"
    distance = [sensors.Entity("sensor", "distance", "Distance", "distance", "cm")]
    assert sensors.pick_light(distance) == "" and sensors.pick_presence(distance) == ""


def test_profile_suggestion_beats_generic_scoring():
    entities = [sensors.Entity("binary_sensor", "radar_zone_occupancy", "Radar Zone Occupancy", "occupancy"),
                sensors.Entity("binary_sensor", "radar_target", "Radar Target", ""),
                sensors.Entity("sensor", "ltr390_light", "LTR390 Light", "illuminance", "lx")]
    assert sensors.pick_presence(entities, "ApolloAutomation.MSR-2_BLE") == "radar_target"
    assert sensors.pick_light(entities, "ApolloAutomation.MSR-2_BLE") == "ltr390_light"


# Vendors


@pytest.mark.parametrize("project, vendor, product, name", [
    ("Akamatis.SmartHomeDevice", "Akamatis", "Smart Home Device", "Akamatis Smart Home Device"),
    ("ApolloAutomation.MSR-2", "Apollo Automation", "MSR-2", "Apollo Automation MSR-2"),
    ("EverythingSmartTechnology.Everything Presence Lite", "Everything Smart Technology", "Everything Presence Lite",
     "Everything Presence Lite"),
    ("Screek.Human_Presence_Sensor", "Screek", "Human Presence Sensor", "SCREEK Human Presence Sensor"),
    ("seeedstudio.mr60bha2_kit", "seeedstudio", "mr60bha2 kit", "Seeed Studio MR60BHA2 kit"),
    ("AcmeLabs.LD2410Sensor", "Acme Labs", "LD2410 Sensor", "Acme Labs LD2410 Sensor"),
])
def test_vendor_parse(project, vendor, product, name):
    assert sensors.parse_project(project) == (vendor, product)
    assert sensors.product_name(project) == name


def test_product_name_without_project_uses_friendly_name():
    assert sensors.product_name("", "Hall sensor") == "Hall sensor"
    assert sensors.product_name("") == "ESPHome sensor"


def test_every_profile_cites_its_source():
    source = Path(sensors.__file__).read_text()
    for profile in sensors.PROFILES:
        before = source[:source.index(f'Profile("{profile.prefix}"')]
        assert before.rstrip().splitlines()[-1].strip().startswith("# https://") or "# https://" in "\n".join(
            before.rstrip().splitlines()[-3:])


# Discovery


def test_discovery_parses_the_real_txt_and_drops_the_mac():
    found = parse_service(FIXTURE["service"], FIXTURE["addresses"], FIXTURE["port"],
                          {key.encode(): value.encode() for key, value in FIXTURE["txt"].items()})
    assert found["title"] == "Akamatis Smart Home Device"
    assert (found["vendor"], found["product"]) == ("Akamatis", "Smart Home Device")
    assert found["name"] == "akamatis-smart-device"
    assert (found["host"], found["port"]) == ("192.168.1.148", 6053)
    assert found["friendly_name"] == "Akamatis Smart Device"
    assert found["encrypted"] is False
    assert FAKE_MAC not in json.dumps(found) and "mac" not in found


def test_discovery_needs_a_private_ipv4_address_and_sees_encryption():
    assert parse_service("x._esphomelib._tcp.local.", ["8.8.8.8", "fe80::1"], 6053, {}) is None
    found = parse_service("x._esphomelib._tcp.local.", ["fe80::1", "10.0.0.9"], 6053,
                          {b"api_encryption": b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"})
    assert found["host"] == "10.0.0.9" and found["encrypted"] and found["title"] == "x"


def test_a_failed_search_finds_nothing():
    def broken():
        raise OSError("no multicast in bridge mode")
    discovery = Discovery(browse_fn=broken)
    assert discovery.search() == [] and discovery.failed


# The web server fallback


def feed(readings, text):
    event, data = "", []
    for line in text.split("\n"):
        if not line:
            sensors.parse_event(event, "\n".join(data), readings)
            event, data = "", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())


def test_web_events_give_readings_and_ignore_text_sensors():
    readings = sensors.Readings(clock=Clock())
    readings.start("web")
    feed(readings, FIXTURE["events"])
    snap = readings.snapshot()
    ids = {(item.domain, item.object_id) for item in snap["entities"]}
    assert ("binary_sensor", "presence") in ids and ("sensor", "ambient_light") in ids
    assert not any(domain not in sensors.DOMAINS for domain, _ in ids)
    assert FAKE_MAC not in repr(snap)
    assert snap["device"]["friendly_name"] == "Akamatis Smart Device"
    assert readings.value("sensor", "ambient_light") == pytest.approx(3.473279953)
    assert readings.value("binary_sensor", "presence") is True
    light = next(item for item in snap["entities"] if item.object_id == "ambient_light")
    assert light.name == "Ambient Light" and light.unit == "lx"  # kept from the first event


def test_device_info_keeps_no_mac():
    class Info:
        pass
    info = Info()
    for key, value in FIXTURE["device_info"].items():
        setattr(info, key, value)
    readings = sensors.Readings(clock=Clock())
    readings.start("api", sensors._device_fields(info), ENTITIES)
    assert FAKE_MAC not in repr(readings.snapshot())
    assert readings.snapshot()["device"]["project_name"] == "Akamatis.SmartHomeDevice"


class FailingClient:
    transport = "api"

    def run(self, readings, stopping):
        raise ConnectionRefusedError()


class OneShotClient:
    transport = "web"

    def run(self, readings, stopping):
        readings.start(self.transport)
        readings.update("binary_sensor", "presence", True)
        raise ConnectionError("lost")


def test_link_falls_back_to_the_web_server_and_keeps_the_last_value():
    clock = Clock()
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[FailingClient(), OneShotClient()], clock=clock)
    assert link.run_once() is True
    assert link.readings.values[("binary_sensor", "presence")] == (True, clock.now)
    assert not link.readings.connected and link.readings.error == "lost"
    assert link.readings.value("binary_sensor", "presence") is None  # kept, but not live


def test_link_reports_the_api_reason_when_both_fail():
    class NoWeb:
        transport = "web"

        def run(self, readings, stopping):
            raise OSError()
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[FailingClient(), NoWeb()], clock=Clock())
    assert link.run_once() is False and link.readings.error == "no-answer"



# The curve, the median and the change decision have table tests in test_sensor_curve.py.

# TV control


class CountingTV(DemoTelevision):
    def __init__(self, display, calls):
        super().__init__(display)
        self.calls = calls

    def set_brightness(self, value):
        self.calls.append(("brightness", value))
        return super().set_brightness(value)

    def set_light_sensor(self, on):
        self.calls.append(("sensor", on))
        return super().set_light_sensor(on)

    def set_option(self, field, value):
        self.calls.append((field, value))
        return super().set_option(field, value)

    def display_settings(self):
        self.calls.append(("read", None))
        return super().display_settings()


@pytest.fixture
def rig(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    screen = app.extensions["displays"].get("main")
    calls, display = [], {"brightness": 5, "sensor": "on", "motion_timer": "15"}
    screen.controller.tv_factory = lambda cfg: CountingTV(display, calls)
    clock = Clock()
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[], clock=clock)
    link.readings.start("api", {"project_name": "Akamatis.SmartHomeDevice"}, ENTITIES)

    def status(awake=True, playing=False, known=True, at=None):
        screen.library.set_state("tv_status", {"awake": awake, "playing": playing, "paired": True, "known": known,
                                               "answered": True})
        moment = datetime.fromtimestamp(clock.now if at is None else at, ZoneInfo("UTC"))
        screen.library.set_state("tv_status_at", moment.isoformat())

    def enable(**fields):
        def change(raw):
            if not enabled:
                # The rigs test the other rules in dark rooms too: the dark rule is off unless a test sets it.
                raw["sensor"]["dark_below"] = "never"
            raw["sensor"].update(host="192.168.1.148", enabled=True, **fields)
        screen.configs.mutate(change)
        enabled.append(True)
    enabled = []

    def run():
        return sensor_control.step(screen, screen.configs.load(), link, memory, clock.now)
    memory = sensor_control.Memory()
    status()
    enable()
    return {"app": app, "screen": screen, "calls": calls, "display": display, "clock": clock, "link": link,
            "status": status, "run": run, "enable": enable, "memory": memory}


def light(rig, lux, presence=True):
    rig["link"].readings.update("sensor", "ambient_light", lux)
    rig["link"].readings.update("binary_sensor", "presence", presence)


def readings_for(rig, lux, seconds, presence=True, every=15):
    """A reading every `every` seconds for `seconds`, with a step() after each. Returns the results."""
    results = []
    for _ in range(seconds // every):
        rig["clock"].now += every
        light(rig, lux, presence)
        rig["status"]()
        results.append(rig["run"]())
    return results


def changes(rig):
    return [call for call in rig["calls"] if call[0] == "brightness"]


def test_brightens_after_two_readings_and_about_30_seconds(rig):
    light(rig, 1000.0)
    assert rig["run"]() == "wait"  # the first reading: the TV is at 5, the curve wants 10
    assert rig["calls"] == [("read", None)]  # the first pass reads the TV's range once
    assert readings_for(rig, 1000.0, 30) == ["wait", "brightness"]
    assert changes(rig) == [("brightness", 10)]
    assert readings_for(rig, 1000.0, 60) == ["same"] * 4  # the TV is at the curve: nothing is sent


def test_dims_only_after_about_3_minutes(rig):
    rig["display"]["brightness"] = 10
    light(rig, 1.0)
    results = [rig["run"](), *readings_for(rig, 1.0, 180)]
    assert results[:-1] == ["wait"] * 12 and results[-1] == "brightness"
    assert changes(rig) == [("brightness", 0)]


def test_one_bright_flash_does_not_change_the_brightness(rig):
    rig["display"]["brightness"] = 0
    readings_for(rig, 1.0, 60)
    light(rig, 5000.0)  # a lamp for one reading: the median of the last 2 minutes stays dark
    assert rig["run"]() in {"same", "wait"}
    assert readings_for(rig, 1.0, 120) == ["same"] * 8
    assert changes(rig) == []


def test_never_sets_a_level_the_tv_already_reports(rig):
    light(rig, 1000.0)
    rig["run"]()  # reads 5 and stores it
    rig["display"]["brightness"] = 10  # someone set it to the curve's level already
    readings_for(rig, 1000.0, 60)
    assert changes(rig) == []


def test_a_change_on_the_tv_pauses_automatic_brightness(rig):
    light(rig, 1000.0)
    rig["run"]()
    readings_for(rig, 1000.0, 30)
    assert changes(rig) == [("brightness", 10)]
    rig["display"]["brightness"] = 4  # someone uses the remote
    results = readings_for(rig, 1.0, 240)
    assert "paused" in results and changes(rig) == [("brightness", 10)]
    state = rig["screen"].library.state(sensor_control.CONTROL)
    assert state["sent"] == 4 and state["paused_until"] > rig["clock"].now
    assert readings_for(rig, 1.0, 60) == ["paused"] * 4
    rig["clock"].now += sensor_curve.PAUSE_SECONDS
    results = readings_for(rig, 1.0, 240)
    assert "brightness" in results and changes(rig)[-1] == ("brightness", 0)


def test_resume_ends_the_pause(rig):
    library = rig["screen"].library
    library.set_state(sensor_control.CONTROL, {"sent": 4, "paused_until": rig["clock"].now + 3600})
    light(rig, 1000.0)
    assert rig["run"]() == "paused"
    sensor_control.resume(library)
    assert rig["run"]() != "paused"


def test_stale_or_missing_readings_do_nothing(rig):
    light(rig, 1000.0, presence=False)
    rig["clock"].now += sensors.STALE_SECONDS + 1
    rig["status"]()
    assert rig["run"]() == "stale"
    rig["clock"].now += 3600  # long past away_after, but the readings are stale
    rig["status"]()
    assert rig["run"]() == "stale"
    rig["link"].readings.seen()
    rig["link"].readings.lost()
    assert rig["run"]() == "stale"
    assert rig["calls"] == []


def test_no_light_reading_does_nothing(rig):
    rig["link"].readings.update("sensor", "ambient_light", None)
    assert rig["run"]() == "no-light" and changes(rig) == []


def test_no_light_reading_for_2_minutes_holds_the_level(rig):
    light(rig, 1000.0)
    rig["clock"].now += sensor_curve.WINDOW + 1
    rig["link"].readings.update("binary_sensor", "presence", True)  # the link is live, the light is quiet
    rig["status"]()
    assert rig["run"]() == "no-light" and changes(rig) == []


@pytest.mark.parametrize("state, reason", [
    ({"known": False, "awake": False}, "unknown"),
    ({"playing": True, "awake": False}, "watching"),
    ({"awake": False}, "standby"),
])
def test_unknown_watching_or_standby_tv_does_nothing(rig, state, reason):
    rig["status"](**state)
    light(rig, 1000.0)
    assert rig["run"]() == reason and rig["calls"] == []


def test_an_old_tv_status_is_unknown(rig):
    rig["status"](at=rig["clock"].now - 3600)
    light(rig, 1000.0)
    assert rig["run"]() == "unknown" and rig["calls"] == []


def test_disabled_sensor_does_nothing(rig):
    rig["screen"].configs.mutate(lambda raw: raw["sensor"].update(enabled=False))
    light(rig, 1000.0)
    assert rig["run"]() == "off" and rig["calls"] == []


def test_away_dims_to_the_darkest_setting_and_presence_restores_at_once(rig):
    rig["enable"](away_after=5)
    rig["display"]["brightness"] = 7
    light(rig, 1000.0, presence=False)
    rig["screen"].library.set_state(sensor_control.CONTROL, {"sent": 7, "sent_at": rig["clock"].now})
    assert rig["run"]() == "empty"  # the room is empty: the curve never brightens it while the dim waits
    rig["clock"].now += 5 * 60
    light(rig, 1000.0, presence=False)
    rig["status"]()
    assert rig["run"]() == "away"
    assert rig["calls"][-1] == ("brightness", 0)
    rig["clock"].now += 10
    light(rig, 1000.0, presence=False)
    assert rig["run"]() == "away"  # no light changes while away
    light(rig, 1000.0, presence=True)
    assert rig["run"]() == "back"
    assert rig["calls"][-1] == ("brightness", 10)


def test_unknown_presence_never_starts_the_away_timer(rig):
    rig["enable"](away_after=1)
    light(rig, 1000.0, presence=None)
    for _ in range(5):
        rig["clock"].now += 60
        light(rig, 1000.0, presence=None)
        rig["status"]()
        assert rig["run"]() != "away"


def test_enable_turns_off_samsung_settings_and_disable_restores_them(rig):
    screen, calls, memory, clock = rig["screen"], rig["calls"], rig["memory"], rig["clock"]
    sensor_control.request_samsung(screen.library, "off")
    assert sensor_control.samsung_step(screen, screen.configs.load(), memory, clock.now) == "done"
    assert ("sensor", False) in calls and ("motion_timer", "off") in calls
    assert rig["display"]["sensor"] == "off" and rig["display"]["motion_timer"] == "off"
    saved = screen.library.state(sensor_control.SAMSUNG)
    assert saved["saved"] == {"sensor": "on", "motion_timer": "15"} and saved["result"] == "done"
    sensor_control.request_samsung(screen.library, "restore")
    clock.now += sensor_control.SAMSUNG_RETRY
    rig["status"]()
    assert sensor_control.samsung_step(screen, screen.configs.load(), memory, clock.now) == "done"
    assert rig["display"]["sensor"] == "on" and rig["display"]["motion_timer"] == "15"
    assert screen.library.state(sensor_control.SAMSUNG)["saved"] is None


def test_samsung_settings_wait_for_art_mode_and_never_wake(rig):
    screen, calls = rig["screen"], rig["calls"]
    rig["status"](awake=False)
    sensor_control.request_samsung(screen.library, "off")
    assert sensor_control.samsung_step(screen, screen.configs.load(), rig["memory"], rig["clock"].now) == "waiting"
    assert calls == []
    # The TV says standby when Art Frames tries: nothing changes, and the request waits.
    rig["status"]()
    rig["display"]["power"] = "standby"
    assert sensor_control.samsung_step(screen, screen.configs.load(), rig["memory"], rig["clock"].now) == "waiting"
    assert rig["display"]["sensor"] == "on"
    assert screen.library.state(sensor_control.SAMSUNG)["want"] == "off"


def test_restore_without_a_change_does_nothing(rig):
    sensor_control.request_samsung(rig["screen"].library, "restore")
    assert sensor_control.samsung_step(rig["screen"], rig["screen"].configs.load(), rig["memory"],
                                       rig["clock"].now) is None
    assert rig["calls"] == []


def test_restart_between_save_and_change_still_restores_the_first_values(rig):
    screen, memory = rig["screen"], rig["memory"]
    screen.library.set_state(sensor_control.SAMSUNG, {"want": "off", "saved": {"sensor": "on", "motion_timer": "30"}})
    rig["display"].update(sensor="off", motion_timer="off")  # a later try reads Art Frames' own values
    sensor_control.samsung_step(screen, screen.configs.load(), memory, rig["clock"].now)
    assert screen.library.state(sensor_control.SAMSUNG)["saved"] == {"sensor": "on", "motion_timer": "30"}


# Pages and secrets


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["sensors"].link_factory = lambda tv_id, settings: sensors.Link(settings, clients=[])
    return app, app.test_client()


def test_the_key_is_never_in_html_or_logs(client, caplog):
    app, browser = client
    caplog.set_level(logging.DEBUG)
    response = browser.post("/tv/main/sensor/connect", headers=auth(),
                            data={"csrf": csrf(browser), "host": "192.168.1.148", "port": "6053", "key": KEY})
    assert response.status_code == 302
    app.extensions["sensors"].reconcile()
    page = browser.get("/tv/main/sensor", headers=auth())
    settings = browser.get("/tv/main/settings", headers=auth())
    live = browser.get("/tv/main/sensor/live.json", headers=auth())
    for body in (page.get_data(as_text=True), settings.get_data(as_text=True), live.get_data(as_text=True)):
        assert KEY not in body
    assert "192.168.1.148 · key set" in page.get_data(as_text=True)
    assert KEY not in caplog.text
    cfg = app.extensions["configs"].load()
    assert cfg.sensor.key.get_secret_value() == KEY
    assert KEY not in repr(cfg) and KEY not in str(cfg.sensor)


def test_a_bad_key_shows_one_sentence_and_is_not_echoed(client):
    _, browser = client
    response = browser.post("/tv/main/sensor/connect", headers=auth(),
                            data={"csrf": csrf(browser), "host": "192.168.1.148", "key": "not-a-key-secret"})
    body = response.get_data(as_text=True)
    assert response.status_code == 400 and "44-character encryption key" in body
    assert "not-a-key-secret" not in body


def test_an_empty_key_field_keeps_the_saved_key(client):
    app, browser = client
    form = {"csrf": csrf(browser), "host": "192.168.1.148", "port": "6053"}
    browser.post("/tv/main/sensor/connect", headers=auth(), data={**form, "key": KEY})
    browser.post("/tv/main/sensor/connect", headers=auth(), data=form)
    assert app.extensions["configs"].load().sensor.key.get_secret_value() == KEY
    browser.post("/tv/main/sensor/connect", headers=auth(), data={**form, "remove_key": "on"})
    assert app.extensions["configs"].load().sensor.key.get_secret_value() == ""


def test_enable_and_disable_ask_for_the_samsung_settings(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    response = switch(app, browser, "on")
    assert response.status_code == 302 and response.location.endswith("#sensor-control")
    library = app.extensions["library"]
    assert library.state(sensor_control.SAMSUNG)["want"] == "off"
    assert app.extensions["configs"].load().sensor.enabled
    assert "Sensor control is on." in browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    library.set_state(sensor_control.SAMSUNG, {"want": None, "saved": {"sensor": "on", "motion_timer": "15"}})
    switch(app, browser, "off")
    assert library.state(sensor_control.SAMSUNG)["want"] == "restore"
    assert not app.extensions["configs"].load().sensor.enabled
    browser.post("/tv/main/sensor/remove", headers=auth(), data={"csrf": csrf(browser)})
    assert app.extensions["configs"].load().sensor.host == ""
    assert library.state(sensor_control.SAMSUNG)["want"] == "restore"





def connected(app, lux):
    """Connect the sensor and give it one light reading, with a demo TV in Art Mode at brightness 6."""
    display = {"brightness": 6}
    screen = app.extensions["displays"].get("main")
    screen.controller.tv_factory = lambda cfg: DemoTelevision(display)
    hub = app.extensions["sensors"]
    hub.reconcile()
    link = hub.link("main")
    link.readings.start("api", {"project_name": "Akamatis.SmartHomeDevice"}, ENTITIES)
    link.readings.update("sensor", "ambient_light", lux)
    return screen, display





def test_paused_page_shows_resume(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(enabled=True))
    library = app.extensions["library"]
    paused_at = time.time() - 300
    library.set_state(sensor_control.CONTROL, {"sent": 4, "paused_at": paused_at, "paused_until": paused_at + 7200})
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    until = datetime.fromtimestamp(paused_at + 7200, ZoneInfo(app.extensions["configs"].load().timezone)).strftime("%H:%M")
    assert f"<dd>Paused until {until}" in body
    assert (f"Paused until {until}. Someone changed the brightness on the TV 5 minutes ago. "
            "Sensor control starts again then.") in body and "Resume now" in body
    response = browser.post("/tv/main/sensor/resume", headers=auth(), data={"csrf": csrf(browser)})
    assert response.location.endswith("#sensor-status")
    assert not sensor_curve.paused(library.state(sensor_control.CONTROL), time.time())
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Paused until" not in body and "Sensor control is on again." in body


def test_demo_finds_sensors_with_vendor_names(tmp_path):
    app = create_app(tmp_path / "demo", demo=True, testing=True)
    browser = app.test_client()
    browser.post("/tv/main/sensor/find", data={"csrf": csrf(browser)})
    body = browser.get("/tv/main/sensor").get_data(as_text=True)
    assert "<strong>Akamatis Smart Home Device</strong>" in body and "<strong>Apollo Automation MSR-2</strong>" in body
    assert "Found Akamatis" not in body
    assert "Enter the address instead" in body


def test_health_check_rows():
    clock = Clock()
    sensor = Sensor(host="192.168.1.148", enabled=True)
    assert sensor_control.health_check(None, Sensor(), clock.now) is None
    link = sensors.Link(sensor, clients=[], clock=clock)
    link.readings.start("api")
    link.readings.update("sensor", "ambient_light", 4.0)
    clock.now += 12
    link.readings.seen()
    row = sensor_control.health_check(link, sensor, clock.now)
    assert (row["tone"], row["dot"], row["title"], row["when"]) == ("ok", "done", "Sensor connected", "just now")
    assert row["fixes"] == []
    link.readings.lost()
    clock.now += 30
    row = sensor_control.health_check(link, sensor, clock.now)
    assert (row["tone"], row["dot"], row["title"], row["when"]) == ("info", "not-done", "Sensor reconnecting",
                                                                  "30 seconds ago")
    clock.now += 570
    row = sensor_control.health_check(link, sensor, clock.now)
    assert (row["tone"], row["dot"], row["title"], row["when"]) == ("problem", "failed", "Sensor not seen",
                                                                  "10 minutes ago")
    assert row["fixes"] == ["sensor"] and row["detail"].startswith(sensors.NO_ANSWER)
    # Without control, a missing sensor is information, not a problem.
    assert sensor_control.health_check(link, Sensor(host="192.168.1.148"), clock.now)["tone"] == "info"


def test_health_check_says_connecting_at_first_and_a_key_problem_at_once():
    clock = Clock()
    sensor = Sensor(host="192.168.1.148", enabled=True)
    # Before the hub's first tick there is no link: connecting, not a problem.
    row = sensor_control.health_check(None, sensor, clock.now)
    assert (row["tone"], row["title"]) == ("info", "Sensor connecting")
    link = sensors.Link(sensor, clients=[], clock=clock)
    link.readings.lost("no-answer")
    assert sensor_control.health_check(link, sensor, clock.now)["title"] == "Sensor connecting"
    clock.now += sensors.STALE_SECONDS + 1
    row = sensor_control.health_check(link, sensor, clock.now)
    assert (row["tone"], row["title"]) == ("problem", "Sensor not seen")
    fresh = sensors.Link(sensor, clients=[], clock=clock)
    fresh.readings.lost("key-wrong")
    row = sensor_control.health_check(fresh, sensor, clock.now)
    assert row["tone"] == "problem" and row["error"] == "key-wrong"


def test_a_quiet_sensor_that_drops_is_reconnecting_not_a_problem():
    # The last value is 10 minutes old, but the heartbeat came 1 s ago: "last" is the heartbeat.
    now = 1_800_000_000.0
    readings = sensors.Readings(clock=lambda: now - 600)
    readings.start("api")
    readings.update("sensor", "ambient_light", 5.0)
    readings.clock = lambda: now - 1
    readings.seen()
    readings.lost()

    class Quiet:
        pass
    link = Quiet()
    link.readings = readings
    row = sensor_control.health_check(link, Sensor(host="192.168.1.148", enabled=True), now)
    assert (row["tone"], row["title"], row["when"]) == ("info", "Sensor reconnecting", "just now")


def test_a_key_error_right_after_a_drop_is_a_problem_not_reconnecting():
    # The key changed on the sensor: a new key does not arrive by itself, so no "reconnecting".
    clock = Clock()
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[], clock=clock)
    link.readings.start("api")
    link.readings.lost("key-wrong")
    clock.now += 5
    row = sensor_control.health_check(link, Sensor(host="192.168.1.148", enabled=True), clock.now)
    assert (row["tone"], row["title"], row["error"]) == ("problem", "Encryption key does not match", "key-wrong")
    assert row["detail"] == ("Enter the key again from the sensor's ESPHome settings. "
                             "Art Frames holds the brightness until the key works.")
    assert row["fixes"] == ["sensor-key"] and row["when"] == ""
    off = sensor_control.health_check(link, Sensor(host="192.168.1.148"), clock.now)
    assert (off["tone"], off["detail"]) == ("info", "Enter the key again from the sensor's ESPHome settings.")


def test_a_sensor_seen_before_without_an_error_code_says_it_does_not_answer():
    clock = Clock()
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[], clock=clock)
    link.readings.start("api")
    link.readings.connected = False  # gone without a code
    clock.now += 600
    row = sensor_control.health_check(link, Sensor(host="192.168.1.148", enabled=True), clock.now)
    assert row["detail"].startswith(sensors.NO_ANSWER) and "Not connected yet" not in row["detail"]


def test_health_page_shows_the_sensor_row_with_its_fix(client):
    app, browser = client
    browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.148"})
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(enabled=True))
    app.extensions["sensors"].reconcile()
    link = app.extensions["sensors"].link("main")
    link.created -= 600  # long past the first tries
    link.readings.lost("no-answer")
    body = browser.get("/tv/main/health", headers=auth()).get_data(as_text=True)
    assert 'id="check-sensor"' in body and "Sensor not seen" in body
    assert 'href="/tv/main/sensor">Open sensor settings' in body
    assert 'id="check-sensor-hub"' not in body  # the hub was never started here


def test_health_shows_a_problem_when_sensor_control_stopped(client):
    app, browser = client
    hub = app.extensions["sensors"]
    assert sensor_control.hub_check(hub, True) is None  # not started: unknown, no row

    class Dead:
        def is_alive(self):
            return False
    hub.thread = Dead()
    assert sensor_control.hub_check(hub, False) is None  # no TV uses a sensor
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", enabled=True))
    body = browser.get("/tv/main/health", headers=auth()).get_data(as_text=True)
    assert 'id="check-sensor-hub"' in body and "Sensor control stopped" in body
    hub.thread = None


def test_the_tv_switcher_keeps_the_sensor_page(client):
    app, browser = client
    app.extensions["configs"].mutate(lambda raw: raw["displays"].append({"id": "b" * 32, "name": "Den"}))
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert f'/tv/{"b" * 32}/sensor"' in body


def test_older_settings_load_without_a_sensor_block(tmp_path):
    old = Config(displays=[{"id": "a" * 32, "name": "Den"}]).model_dump()
    for item in (old, old["displays"][0]):
        del item["sensor"]
    (tmp_path / "settings.json").write_text(json.dumps(old))
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    assert app.extensions["configs"].load().sensor == Sensor()
    assert app.extensions["displays"].get("a" * 32).configs.load().sensor == Sensor()


def test_sensor_settings_stay_per_tv(tmp_path):
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    displays = app.extensions["displays"]
    second = displays.add("Den", "192.168.1.51")
    displays.get(second).configs.mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", key=KEY))
    assert displays.get(second).configs.load().sensor.host == "192.168.1.148"
    assert displays.get("main").configs.load().sensor.host == ""
    saved = io.StringIO((tmp_path / "settings.json").read_text()).read()
    assert KEY in saved  # the private settings file holds it; nothing else does
