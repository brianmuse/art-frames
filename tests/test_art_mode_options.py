"""Art Mode options: Sleep After, motion sensitivity, color temperature and Night Mode."""

import json
import re
from types import SimpleNamespace

import pytest
from test_art_display import IP, Art, make_tv
from test_web import PASSWORD, auth, csrf

from art_frames import analytics
from art_frames import controller as controller_module
from art_frames.config import Config
from art_frames.controller import Controller
from art_frames.library import Library
from art_frames.television import DisplayError, Readback, parse_display_settings
from art_frames.web import create_app

OPTION_SETS = {"set_motion_timer", "set_motion_sensitivity", "set_color_temperature"}


class OptionArt(Art):
    """A fake Art Mode connection that also reports the motion and Night Mode items."""

    def __init__(self, *, motion=True, night=None, applies=True, **kwargs):
        super().__init__(**kwargs)
        self.timer, self.sensitivity, self.warmth = "off", "2", "0"
        self.motion, self.night, self.option_applies = motion, night, applies

    def get_artmode_settings(self):
        self.calls.append(("settings",))
        items = [
            {"item": "brightness", "value": str(self.brightness), "min": "1", "max": "10"},
            {"item": "color_temperature", "value": self.warmth, "min": "-3", "max": "3"},
        ]
        if self.motion:
            items += [{"item": "motion_timer", "value": self.timer, "valid_values": ["off", "5", "15"]},
                      {"item": "motion_sensitivity", "value": self.sensitivity, "min": "1", "max": "3"}]
        if self.night is not None:
            items.append({"item": "night_mode", "value": self.night})
        return {"data": json.dumps(items)}

    def _set(self, name, attribute, value):
        self.calls.append((name, value))
        if self.option_applies:
            setattr(self, attribute, str(value))

    def set_motion_timer(self, value):
        self._set("set_motion_timer", "timer", value)

    def set_motion_sensitivity(self, value):
        self._set("set_motion_sensitivity", "sensitivity", value)

    def set_color_temperature(self, value):
        self._set("set_color_temperature", "warmth", value)


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(analytics, "record_setting", lambda ctl, setting, outcome: recorded.append((setting, outcome)))
    return recorded


def controller_for(tmp_path, device):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(tv={"ip": IP}), id="main")
    return Controller(configs, Library(tmp_path), tv_factory=lambda cfg: device)


def test_settings_are_read_only_in_art_mode(tmp_path, monkeypatch):
    art = OptionArt(night="on")
    device = make_tv(tmp_path, monkeypatch, art)
    controller = controller_for(tmp_path, device)
    settings = controller.display_settings()
    assert (settings["motion_timer"], settings["motion_sensitivity"], settings["night_mode"]) == ("off", 2, "on")
    assert (settings["color_temperature_min"], settings["color_temperature_max"]) == (-3, 3)
    stored = controller.library.state("art_display")
    assert stored["motion_timer"] == "off" and stored["checked_at"]
    assert not [call for call in art.calls if call[0] in OPTION_SETS]


@pytest.mark.parametrize("power,mode", [("standby", "on"), ("off", "on"), ("on", "off"), ("on", "")])
def test_standby_or_tv_mode_is_not_done_and_never_wakes(tmp_path, monkeypatch, events, power, mode):
    art = OptionArt(mode=mode)
    device = make_tv(tmp_path, monkeypatch, art, power=power)
    controller = controller_for(tmp_path, device)
    for action in (controller.display_settings, lambda: controller.set_option("motion_timer", "15")):
        with pytest.raises(DisplayError) as error:
            action()
        assert error.value.code == "not-art-mode"
    assert not [call for call in art.calls if call[0] in OPTION_SETS | {"set_artmode"}]
    if power in {"standby", "off"}:
        assert not device.opened  # the art channel stays closed in standby
    assert events == [("motion_timer", "not-art-mode")]


@pytest.mark.parametrize("field,value,sent", [
    ("motion_timer", "15", "15"), ("motion_timer", "off", "off"),
    ("motion_sensitivity", 3, "3"), ("color_temperature", -2, -2),
])
def test_set_reads_back_and_confirms(tmp_path, monkeypatch, events, field, value, sent):
    art = OptionArt()
    art.timer = "5"
    device = make_tv(tmp_path, monkeypatch, art)
    controller = controller_for(tmp_path, device)
    result = controller.set_option(field, value)
    assert result.requested == value and result.reported == value
    assert ("set_" + field, sent) in art.calls
    assert art.calls.index(("set_" + field, sent)) < len(art.calls) - 1  # a settings read follows the set
    assert controller.library.state("art_display")[field] == value
    assert events == [(field, "done")]


def test_readback_mismatch_is_not_done(tmp_path, monkeypatch, events):
    art = OptionArt(applies=False)
    device = make_tv(tmp_path, monkeypatch, art)
    controller = controller_for(tmp_path, device)
    result = controller.set_option("motion_timer", "30")
    assert result.reported == "off" and result.requested == "30"
    assert events == [("motion_timer", "not-done")]


def test_values_outside_the_choices_are_refused_before_any_set(tmp_path, monkeypatch):
    art = OptionArt()
    device = make_tv(tmp_path, monkeypatch, art)
    for field, value, code in (("motion_timer", "10", "invalid"), ("motion_sensitivity", 4, "out-of-range"),
                               ("color_temperature", 5, "out-of-range"), ("brightness", 3, "invalid")):
        with pytest.raises(DisplayError) as error:
            device.set_option(field, value)
        assert error.value.code == code
    assert not [call for call in art.calls if call[0] in OPTION_SETS]


def test_motion_sensitivity_is_refused_when_the_tv_does_not_report_it(tmp_path, monkeypatch):
    art = OptionArt(motion=False)
    device = make_tv(tmp_path, monkeypatch, art)
    with pytest.raises(DisplayError, match="not-reported"):
        device.set_option("motion_sensitivity", 2)
    assert not [call for call in art.calls if call[0] in OPTION_SETS]


def test_unreported_items_parse_as_none():
    settings = parse_display_settings({"data": json.dumps([{"item": "brightness", "value": "3"}])})
    assert settings["motion_timer"] is None and settings["motion_sensitivity"] is None
    assert settings["night_mode"] is None and settings["color_temperature_min"] is None
    odd = parse_display_settings({"data": json.dumps([{"item": "motion_timer", "value": "<b>7</b>"}])})
    assert odd["motion_timer"] == "unknown"


def test_change_log_has_values_only(tmp_path, monkeypatch, events, caplog):
    art = OptionArt()
    device = make_tv(tmp_path, monkeypatch, art)
    with caplog.at_level("INFO"):
        controller_for(tmp_path, device).set_option("motion_timer", "60")
    assert "TV main: Art Mode motion timer off -> 60 (TV reports 60)" in caplog.text
    assert IP not in caplog.text


def test_analytics_records_setting_events(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(analytics, "_event", lambda ctl, kind, outcome, latency_ms=None, at=None: written.append((kind, outcome)))
    analytics.record_setting(None, "motion_timer", "done")
    analytics.record_setting(None, "motion timer", "done")  # not a fixed name: dropped
    analytics.record_setting(None, "brightness", "done")  # not an option field: dropped
    analytics.record_setting(None, "motion_timer", "made-up")  # not a fixed outcome: dropped
    analytics.record_setting(None, "color_temperature", "failed")
    assert written == [("setting", "motion_timer:done"), ("setting", "color_temperature:failed")]


@pytest.mark.parametrize("error,outcome", [(controller_module.Busy("busy"), "busy"),
                                           (ConnectionResetError("gone"), "failed")])
def test_busy_and_connection_errors_are_recorded_and_raised(tmp_path, monkeypatch, events, error, outcome):
    controller = controller_for(tmp_path, None)

    def fail(action):
        raise error
    monkeypatch.setattr(controller, "_display_action", fail)
    with pytest.raises(type(error)):
        controller.set_option("motion_timer", "15")
    assert events == [("motion_timer", outcome)]


# --- Web panel ---

@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


def store_reading(app, **values):
    screen = app.extensions["displays"].get("main")
    settings = {**parse_display_settings(None), "brightness": 3, "color_temperature": 1, **values}
    screen.library.set_state("art_display", {**settings, "device": screen.configs.load().tv.identity,
                                             "checked_at": "2026-11-01T08:00:00+00:00"})


def test_panel_renders_from_stored_state_without_contacting_the_tv(app, monkeypatch):
    monkeypatch.setattr(controller_module.Television, "remote", lambda *a, **k: pytest.fail("the page must not contact the TV"))
    client = app.test_client()
    page = client.get("/tv/main/picture", headers=auth()).text
    assert 'id="art-mode-options"' in page and "No settings read yet" in page and "Read from TV" in page
    assert "Set Sleep After" not in page
    store_reading(app, motion_timer="15")
    page = client.get("/tv/main/picture", headers=auth()).text
    assert "Last read Nov 1" in page and 'name="motion_timer"' in page
    assert '<option value="15" selected>15 minutes</option>' in page
    assert 'name="color_temperature" min="-5" max="5"' in page
    # Not reported by this TV: hidden.
    assert "Motion sensitivity" not in page and "Night Mode" not in page


def test_panel_shows_reported_sensitivity_and_night_mode(app):
    store_reading(app, motion_timer="off", motion_sensitivity=2, night_mode="on", color_temperature_min=-3,
                  color_temperature_max=3)
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    assert "Motion sensitivity" in page and 'name="motion_sensitivity" value="2" checked' in page
    assert "Night Mode" in page and '<span class="badge secondary">On</span>' in page
    assert [int(level) for level in re.findall(r'name="motion_sensitivity" value="(\d)"', page)] == [1, 2, 3]
    assert 'min="-3" max="3"' in page


def readback(requested, reported):
    return Readback(requested, reported, "off", True, {})


@pytest.mark.parametrize("result,text", [
    (readback("15", "15"), "Done. Sleep After is now 15 minutes."),
    (readback("15", "off"), "Not done: the TV accepted the request but still reports Off."),
    (readback("15", None), "Not done: the TV did not report its Sleep After setting afterward."),
])
def test_sleep_after_route_reports_done_or_not_done(app, monkeypatch, result, text):
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "set_option", lambda field, value: calls.append((field, value)) or result)
    client = app.test_client()
    response = client.post("/screen/display/sleep-after", headers=auth(),
                           data={"csrf": csrf(client), "motion_timer": "15"}, follow_redirects=True)
    assert text in response.text and calls == [("motion_timer", "15")]
    job = app.extensions["jobs"].snapshot()["recent"][0]
    assert job["place"] == "options" and job["state"] == ("done" if text.startswith("Done") else "skipped")


def test_routes_refuse_standby_and_bad_input_without_a_change(app, monkeypatch):
    controller = app.extensions["controller"]

    def standby(field, value):
        raise DisplayError("not-art-mode")
    monkeypatch.setattr(controller, "set_option", standby)
    client = app.test_client()
    token = csrf(client)
    response = client.post("/screen/display/color-temperature", headers=auth(),
                           data={"csrf": token, "color_temperature": "2"}, follow_redirects=True)
    assert "Not done: the TV is not on in Art Mode. Art Frames did not wake it" in response.text
    calls = []
    monkeypatch.setattr(controller, "set_option", lambda *a: calls.append(a))
    for route, data in (("/screen/display/sleep-after", {"motion_timer": "10"}),
                        ("/screen/display/motion-sensitivity", {"motion_sensitivity": "high"}),
                        ("/screen/display/color-temperature", {"color_temperature": "1.5"})):
        response = client.post(route, headers=auth(), data={"csrf": token, **data}, follow_redirects=True)
        assert "Not done:" in response.text
    assert not calls


@pytest.mark.parametrize("route", ["/screen/display/refresh", "/screen/display/sleep-after",
                                   "/screen/display/motion-sensitivity", "/screen/display/color-temperature"])
def test_option_routes_require_auth_and_csrf(app, monkeypatch, route):
    calls = []
    controller = app.extensions["controller"]
    monkeypatch.setattr(controller, "set_option", lambda *a: calls.append(a))
    monkeypatch.setattr(controller, "display_settings", lambda: calls.append("read"))
    client = app.test_client()
    data = {"motion_timer": "15", "motion_sensitivity": "2", "color_temperature": "1"}
    assert client.post(route, data=data).status_code == 401
    assert client.post(route, headers=auth(), data=data).status_code == 400
    assert not calls


def test_demo_read_then_change_round_trip(tmp_path):
    client = create_app(tmp_path, demo=True, testing=True).test_client()
    token = csrf(client)
    response = client.post("/screen/display/refresh", data={"csrf": token, "place": "options"}, follow_redirects=True)
    assert "Done. The TV reported its Art Mode settings." in response.text and "Last read" in response.text
    # The result shows in the options panel, once.
    page = response.text
    assert page.index('id="art-mode-options"') < page.index("Done. The TV reported its Art Mode settings.")
    assert page.count("Done. The TV reported its Art Mode settings.") == 1
    response = client.post("/screen/display/sleep-after", data={"csrf": token, "motion_timer": "30"}, follow_redirects=True)
    assert "Done. Sleep After is now 30 minutes." in response.text
    assert '<option value="30" selected>30 minutes</option>' in response.text
    response = client.post("/screen/display/motion-sensitivity", data={"csrf": token, "motion_sensitivity": "3"},
                           follow_redirects=True)
    assert "Done. Motion sensitivity is now 3." in response.text


def test_panel_says_how_to_load_or_change_sleep_after(app):
    screen = app.extensions["displays"].get("main")
    identity = screen.configs.load().tv.identity
    # A reading stored before Art Frames read Sleep After has no motion_timer key.
    screen.library.set_state("art_display", {"brightness": 3, "device": identity, "checked_at": "2026-11-01T08:00:00+00:00"})
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    assert "Select Read from TV to load Sleep After." in page and "Set Sleep After" not in page
    store_reading(app, motion_timer=None)
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    assert "This TV does not report Sleep After. Change it on the TV: Art Mode Options → Sleep After." in page


def test_both_read_buttons_share_one_queued_job_and_the_latest_click_picks_the_panel(app, monkeypatch):
    jobs = app.extensions["jobs"]
    jobs.mode = "manual"
    monkeypatch.setattr(app.extensions["controller"], "display_settings", lambda: {})
    client = app.test_client()
    token = csrf(client)
    first = client.post("/screen/display/refresh", headers=auth(), data={"csrf": token})
    second = client.post("/screen/display/refresh", headers=auth(), data={"csrf": token, "place": "options"})
    assert first.location.split("#")[0] == second.location.split("#")[0]  # the same job
    assert second.location.endswith("#art-mode-options")
    [queued] = jobs.snapshot()["queued"]
    assert queued["place"] == "options"
    jobs.drain()
    [done] = jobs.snapshot()["recent"]
    assert done["place"] == "options"


def test_panel_hides_controls_the_tv_did_not_report(app):
    store_reading(app, color_temperature=None)
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    assert "Last read" in page and "Set Sleep After" not in page and 'name="color_temperature"' not in page
