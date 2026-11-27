import json
import logging
from types import SimpleNamespace

import pytest
from samsungtvws.exceptions import ResponseError
from test_web import PASSWORD, auth, csrf

from art_frames import television
from art_frames.config import Config, private_write
from art_frames.controller import Controller
from art_frames.library import Library
from art_frames.television import DemoTelevision, DisplayError, Television
from art_frames.web import create_app

IP = "192.168.1.50"
TOKEN = b"synthetic-token-value"


class Art:
    """A fake Art Mode connection. `applies` controls whether a set changes the reading."""

    def __init__(self, mode="on", brightness=5, sensor="off", limits=("1", "10"), applies=True, fail=None):
        self.mode, self.brightness, self.sensor = mode, brightness, sensor
        self.limits, self.applies, self.fail = limits, applies, fail
        self.calls = []
        self.modes = []  # replies to return first, in order, before `mode`
        self.read_error = None  # raised by every settings read after a set

    def get_artmode(self):
        self.calls.append(("get_artmode",))
        if self.modes:
            return self.modes.pop(0)
        if isinstance(self.mode, Exception):
            raise self.mode
        return self.mode

    def get_artmode_settings(self):
        self.calls.append(("settings",))
        if self.read_error and any(call[0] in SETS for call in self.calls):
            raise self.read_error
        brightness = {"item": "brightness", "value": str(self.brightness), "extra": "dropped"}
        if self.limits:
            brightness.update(min=self.limits[0], max=self.limits[1])
        return {"data": json.dumps([
            brightness,
            {"item": "brightness_sensor_setting", "value": self.sensor},
            {"item": "color_temperature", "value": "2", "min": "-5", "max": "5"},
        ])}

    def set_brightness(self, value):
        self.calls.append(("set_brightness", value))
        if self.fail:
            raise self.fail
        if self.applies:
            self.brightness = value

    def set_brightness_sensor_setting(self, value):
        self.calls.append(("set_sensor", value))
        if self.fail:
            raise self.fail
        if self.applies:
            self.sensor = value

    def set_artmode(self, *args):
        self.calls.append(("set_artmode",))

    def close(self):
        self.calls.append(("close",))


def make_tv(tmp_path, monkeypatch, art, power="on", info_error=None):
    device = Television(Config(tv={"ip": IP, "mac": "AA:BB:CC:DD:EE:FF"}).tv, tmp_path)
    private_write(device.token, TOKEN)
    opened = []

    def info():
        if info_error:
            raise info_error
        return {"device": {"PowerState": power}}

    def open_art(**_):
        opened.append(True)
        return art

    timeouts = []

    def remote(**kwargs):
        timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(rest_device_info=info, art=open_art)

    monkeypatch.setattr(device, "remote", remote)
    device.timeouts = timeouts
    monkeypatch.setattr(television.time, "sleep", lambda seconds: None)
    device.opened = opened
    return device


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


SETS = {"set_brightness", "set_sensor", "set_artmode"}


@pytest.mark.parametrize("power,mode,info_error", [
    ("off", "on", None),
    ("standby", "on", None),
    ("on", "off", None),  # a TV programme is playing
    ("on", "", None),  # unknown Art Mode reply
    ("on", OSError("no reply"), None),
    ("on", "on", OSError("REST failed")),  # unknown power state
])
def test_display_actions_refuse_unless_on_in_art_mode(tmp_path, monkeypatch, power, mode, info_error):
    art = Art(mode=mode)
    device = make_tv(tmp_path, monkeypatch, art, power=power, info_error=info_error)
    for action in (device.display_settings, lambda: device.set_brightness(3), lambda: device.set_light_sensor(True)):
        with pytest.raises(DisplayError) as error:
            action()
        assert error.value.code == "not-art-mode"
    assert not [call for call in art.calls if call[0] in SETS]
    if power in {"off", "standby"} or info_error:
        assert not device.opened  # no Art Mode connection at all


def test_unpaired_tv_is_refused_without_contact(tmp_path):
    device = Television(Config(tv={"ip": IP}).tv, tmp_path)
    with pytest.raises(DisplayError, match="unpaired"):
        device.set_brightness(3)


def test_blank_power_state_with_art_mode_is_allowed(tmp_path, monkeypatch):
    art = Art()
    device = make_tv(tmp_path, monkeypatch, art, power="")
    assert device.set_brightness(3).reported == 3
    assert ("close",) in art.calls


def test_display_settings_reads_only_and_keeps_four_text_fields(tmp_path, monkeypatch):
    art = Art()
    settings = make_tv(tmp_path, monkeypatch, art).display_settings()
    assert settings["brightness"] == 5 and (settings["min"], settings["max"]) == (1, 10)
    assert settings["sensor"] == "off" and settings["color_temperature"] == 2
    assert settings["items"][0] == {"item": "brightness", "value": "5", "min": "1", "max": "10"}
    assert all(set(item) == {"item", "value", "min", "max"} for item in settings["items"])
    assert not [call for call in art.calls if call[0] in SETS]


@pytest.mark.parametrize("value,limits,low,high", [
    (11, ("1", "10"), 1, 10), (0, ("1", "10"), 1, 10), (51, None, 0, 50), (-1, None, 0, 50), (True, None, 0, 50), ("3", None, 0, 50),
])
def test_brightness_outside_tv_range_is_rejected_before_any_set(tmp_path, monkeypatch, value, limits, low, high):
    art = Art(limits=limits)
    with pytest.raises(DisplayError) as error:
        make_tv(tmp_path, monkeypatch, art).set_brightness(value)
    assert (error.value.code, error.value.low, error.value.high) == ("out-of-range", low, high)
    assert not [call for call in art.calls if call[0] in SETS]


def test_brightness_readback_reports_match_and_mismatch(tmp_path, monkeypatch):
    art = Art()
    result = make_tv(tmp_path, monkeypatch, art).set_brightness(3)
    assert (result.requested, result.reported, result.before, result.acknowledged) == (3, 3, 5, True)
    assert art.calls.count(("set_brightness", 3)) == 1  # no fallback commands
    art = Art(applies=False)
    result = make_tv(tmp_path, monkeypatch, art).set_brightness(3)
    assert (result.requested, result.reported, result.before) == (3, 5, 5)
    assert [call for call in art.calls if call[0] in SETS] == [("set_brightness", 3)]


def test_lost_reply_still_reads_back_and_tv_error_is_rejected(tmp_path, monkeypatch):
    art = Art(fail=TimeoutError("socket to " + IP))
    result = make_tv(tmp_path, monkeypatch, art).set_brightness(3)
    assert not result.acknowledged and result.reported == 5
    art = Art(fail=ResponseError("`set_brightness` request failed with error number -1"))
    with pytest.raises(DisplayError, match="rejected"):
        make_tv(tmp_path, monkeypatch, art).set_brightness(3)


def test_light_sensor_reads_back(tmp_path, monkeypatch):
    art = Art()
    result = make_tv(tmp_path, monkeypatch, art).set_light_sensor(True)
    assert (result.requested, result.reported, result.before) == ("on", "on", "off")
    assert ("set_sensor", "on") in art.calls
    with pytest.raises(DisplayError, match="invalid"):
        make_tv(tmp_path, monkeypatch, Art()).set_light_sensor("yes")


def test_demo_display_keeps_state_between_requests(tmp_path):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(), id="main")
    controller = Controller(configs, Library(tmp_path), demo=True)
    assert controller.display_settings()["brightness"] == 5
    assert controller.set_brightness(3).reported == 3
    assert controller.set_light_sensor(True).reported == "on"
    settings = controller.display_settings()
    assert settings["brightness"] == 3 and settings["sensor"] == "on"
    with pytest.raises(DisplayError):
        DemoTelevision().set_brightness(11)


def test_change_is_logged_with_numbers_only(tmp_path, monkeypatch, caplog):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(tv={"ip": IP}), id="main")
    art = Art(fail=TimeoutError("ws://" + IP + "/?token=secret"))
    device = make_tv(tmp_path, monkeypatch, art)
    controller = Controller(configs, Library(tmp_path), tv_factory=lambda cfg: device)
    with caplog.at_level(logging.INFO):
        controller.set_brightness(3)
    assert "TV main: Art Mode brightness 5 -> 3 (TV reports 5)" in caplog.text
    assert IP not in caplog.text and "token" not in caplog.text
    assert controller.library.state("art_display")["brightness"] == 5


def test_display_action_holds_the_display_lock(tmp_path, monkeypatch):
    from art_frames.controller import Busy

    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(), id="main")
    controller = Controller(configs, Library(tmp_path), demo=True)
    controller.lock.acquire()
    try:
        with pytest.raises(Busy):
            controller.set_brightness(3)
    finally:
        controller.lock.release()
    assert controller.display_settings()["brightness"] == 5  # the failed attempt changed nothing


ROUTES = ["/screen/display/refresh", "/screen/display/brightness", "/screen/display/light-sensor"]


@pytest.mark.parametrize("route", ROUTES)
def test_display_routes_require_auth_and_csrf(app, monkeypatch, route):
    calls = []
    controller = app.extensions["controller"]
    for name in ("display_settings", "set_brightness", "set_light_sensor"):
        monkeypatch.setattr(controller, name, lambda *args, _name=name: calls.append(_name))
    client = app.test_client()
    data = {"brightness": "3", "sensor": "on"}
    assert client.post(route, data=data).status_code == 401
    assert client.post(route, headers=auth(), data=data).status_code == 400
    assert not calls


def readback(requested, reported, acknowledged=True):
    return television.Readback(requested, reported, 5, acknowledged, {"brightness": reported})


@pytest.mark.parametrize("result,text", [
    (readback(3, 3), "Done. Brightness is now 3."),
    (readback(3, 5), "Not done: the TV accepted the request but still reports 5."),
    (readback(3, 5, acknowledged=False), "Not done: the TV did not confirm the request. It reports 5."),
    (readback(3, 3, acknowledged=False), "Done. The TV did not reply, but it now reports 3."),
    (readback(3, None), "Not done: the TV did not report its brightness setting afterward."),
])
def test_brightness_route_flashes_what_the_tv_reports(app, monkeypatch, result, text):
    monkeypatch.setattr(app.extensions["controller"], "set_brightness", lambda value: result)
    client = app.test_client()
    response = client.post("/screen/display/brightness", headers=auth(), data={"csrf": csrf(client), "brightness": "3"}, follow_redirects=True)
    assert text in response.text


@pytest.mark.parametrize("raw", ["abc", "3.5", "", "1e2", "٣", "12345"])
def test_brightness_route_rejects_non_integers(app, monkeypatch, raw):
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "set_brightness", calls.append)
    client = app.test_client()
    response = client.post("/screen/display/brightness", headers=auth(), data={"csrf": csrf(client), "brightness": raw}, follow_redirects=True)
    assert "whole number" in response.text and not calls


def test_routes_refuse_plainly_and_hide_library_errors(app, monkeypatch, caplog):
    controller = app.extensions["controller"]
    client = app.test_client()
    token = csrf(client)

    def refuse(*args):
        raise DisplayError("not-art-mode")
    monkeypatch.setattr(controller, "set_light_sensor", refuse)
    response = client.post("/screen/display/light-sensor", headers=auth(), data={"csrf": token, "sensor": "on"}, follow_redirects=True)
    assert "Not done: the TV is not on in Art Mode. Art Frames did not wake it" in response.text
    response = client.post("/screen/display/light-sensor", headers=auth(), data={"csrf": token, "sensor": "maybe"}, follow_redirects=True)
    assert "Not done: choose On or Off." in response.text

    def private(*args):
        raise RuntimeError("ws://" + IP + "/?token=secret")
    monkeypatch.setattr(controller, "display_settings", private)
    with caplog.at_level(logging.INFO):
        response = client.post("/screen/display/refresh", headers=auth(), data={"csrf": token}, follow_redirects=True)
    assert "The TV did not answer" in response.text
    assert IP not in response.text + caplog.text and "secret" not in response.text + caplog.text


def test_page_shows_stored_reading_and_settings_table(tmp_path):
    client = create_app(tmp_path, demo=True, testing=True).test_client()
    page = client.get("/tv/main/picture").text
    assert "Brightness" in page and "Not read yet" in page
    response = client.post("/screen/display/refresh", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Done. The TV reported its Art Mode settings." in response.text
    assert "All Art Mode settings" in response.text and "brightness_sensor_setting" in response.text
    assert 'name="brightness" min="0" max="10"' in response.text
    response = client.post("/screen/display/brightness", data={"csrf": csrf(client), "brightness": "11"}, follow_redirects=True)
    assert "Not done: choose a whole number from 0 to 10." in response.text


def test_connection_failure_stops_readback_and_uses_short_timeout(tmp_path, monkeypatch):
    sleeps = []
    art = Art(fail=TimeoutError("no reply"))
    art.read_error = ConnectionResetError("gone")
    device = make_tv(tmp_path, monkeypatch, art)
    monkeypatch.setattr(television.time, "sleep", sleeps.append)
    result = device.set_brightness(3)
    assert device.timeouts == [television.DISPLAY_TIMEOUT]
    assert (result.reported, result.acknowledged, result.before) == (None, False, 5)
    assert art.calls.count(("settings",)) == 2 and not sleeps  # one read before, one failed read after
    assert [call for call in art.calls if call[0] in SETS] == [("set_brightness", 3)]


def test_art_mode_is_checked_again_just_before_the_set(tmp_path, monkeypatch):
    art = Art()
    art.modes = ["on", "off"]  # Art Mode at the start, a programme by the time of the set
    with pytest.raises(DisplayError, match="not-art-mode"):
        make_tv(tmp_path, monkeypatch, art).set_light_sensor(True)
    assert not [call for call in art.calls if call[0] in SETS]


def test_huge_or_odd_numbers_from_the_tv_are_dropped():
    assert television._number(10 ** 4000) is None and television._number(-10000) is None
    assert television._number(9999) == 9999 and television._number(" 7 ") == 7
    assert television._number(True) is None and television._number("1" * 5) is None


def test_light_sensor_message_uses_on_off_labels_in_the_panel(app, monkeypatch):
    result = television.Readback("off", "off", "on", True, {"sensor": "off"})
    monkeypatch.setattr(app.extensions["controller"], "set_light_sensor", lambda on: result)
    client = app.test_client()
    page = client.post("/screen/display/light-sensor", headers=auth(), data={"csrf": csrf(client), "sensor": "off"}, follow_redirects=True).text
    assert "Done. Light sensor is now Off." in page
    # The result shows inside the panel, not only at the top of the page.
    assert page.index('id="art-display"') < page.index("Done. Light sensor is now Off.")
    assert page.count("Done. Light sensor is now Off.") == 1


def test_settings_items_from_the_tv_are_escaped(app):
    library = app.extensions["library"]
    identity = app.extensions["configs"].load().tv.identity
    library.set_state("art_display", {
        **television.parse_display_settings({"data": json.dumps([{"item": "<script>x</script>", "value": "<b>1</b>"}])}),
        "device": identity, "checked_at": "2026-10-31T08:00:00+00:00",
    })
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt;" in page
    assert "<b>1</b>" not in page and "&lt;b&gt;1&lt;/b&gt;" in page


@pytest.mark.parametrize("route", ROUTES)
def test_display_routes_reject_unknown_tv(app, route):
    client = app.test_client()
    token = csrf(client)
    assert client.post(route + "?tv=missing", headers=auth(), data={"csrf": token, "brightness": "3", "sensor": "on"}).status_code == 404


def test_page_says_not_read_yet_before_first_refresh(app):
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    # One line in Brightness instead of a row per value; the three TV details still say "Not read yet".
    assert "No settings read yet</h2>" in page and "Turn the TV on in Art Mode, then read its settings." in page
    assert page.count("Not read yet") == 3 and "Not reported" not in page
    assert "range appears after Read from TV" in page and "0 to 50" not in page
    assert "<dt>Color temperature</dt>" not in page  # it shows only in Art Mode options


def test_malformed_settings_payload_reads_as_not_reported(tmp_path, monkeypatch):
    class Malformed(Art):
        def get_artmode_settings(self):
            raise AttributeError("'int' object has no attribute 'get'")  # the library's own parse failed

        def get_brightness(self):
            raise ResponseError("unsupported")

        def get_color_temperature(self):
            raise ResponseError("unsupported")

    settings = make_tv(tmp_path, monkeypatch, Malformed()).display_settings()
    assert settings["brightness"] is None and settings["items"] == []


def test_about_keeps_only_model_and_versions(tmp_path, monkeypatch):
    art = Art()
    art.get_api_version = lambda: "4.3.4.0"
    device = make_tv(tmp_path, monkeypatch, art)
    private = {"ip": IP, "wifiMac": "AA:BB:CC:DD:EE:FF", "ssid": "home-network", "duid": "uuid:secret", "name": "Living room"}
    info = {"device": {"PowerState": "on", "modelName": "QN65LS03DAFXZA", "model": "24_PONTUSM_FTV",
                       "firmwareVersion": "Unknown", "OS": "Tizen", **private}}
    real_remote = device.remote
    monkeypatch.setattr(device, "remote", lambda **kw: SimpleNamespace(**{**vars(real_remote(**kw)), "rest_device_info": lambda: info, "close": lambda: None}))
    about = device.display_settings()["about"]
    assert about == {"model_name": "QN65LS03DAFXZA", "model_code": "24_PONTUSM_FTV",
                     "firmware": "Unknown", "os": "Tizen", "art_api": "4.3.4.0"}
    assert not any(value in str(about) for value in private.values())


def test_about_survives_a_brightness_change(tmp_path, monkeypatch):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(tv={"ip": IP}), id="main")
    controller = Controller(configs, Library(tmp_path), demo=True)
    controller.display_settings()
    assert controller.library.state("art_display")["about"]["model_name"] == "Demo Frame"
    controller.set_brightness(3)
    assert controller.library.state("art_display")["about"]["model_name"] == "Demo Frame"


def test_failed_about_read_keeps_the_last_model(tmp_path):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(tv={"ip": IP}), id="main")
    controller = Controller(configs, Library(tmp_path), demo=True)
    controller.display_settings()
    controller.tv_factory = lambda cfg: SimpleNamespace(display_settings=lambda: {"brightness": 5, "about": {"model_name": "", "art_api": ""}})
    controller.display_settings()
    assert controller.library.state("art_display")["about"]["model_name"] == "Demo Frame"
