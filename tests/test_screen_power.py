import logging
from types import SimpleNamespace

import pytest
from test_web import PASSWORD, auth, csrf

from art_frames import television
from art_frames.config import Config, private_write
from art_frames.controller import Busy, Controller
from art_frames.library import Library
from art_frames.television import DemoTelevision, DisplayError, ScreenOn, Television, WakeError
from art_frames.web import create_app

IP = "192.168.1.50"


class FakeTV:
    """Replies come from `states`, a list of (power, art_mode) or None (no answer); the last one repeats."""

    def __init__(self, states):
        self.states, self.keys = list(states), []

    def current(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def remote(self, **kwargs):
        tv = self
        state = {}

        def info():
            state["now"] = tv.current()
            if state["now"] is None:
                raise OSError("no route to " + IP)
            return {"device": {"PowerState": state["now"][0]}}

        def art(**_):
            mode = state.get("now", tv.states[0])
            return SimpleNamespace(get_artmode=lambda: mode[1] if mode else "", close=lambda: None)

        return SimpleNamespace(
            rest_device_info=info, art=art, close=lambda: None,
            hold_key=lambda key, seconds: tv.keys.append(("hold", key, seconds)),
            send_key=lambda key, **kwargs: tv.keys.append(("click", key)),
            timeout=kwargs.get("timeout"),
        )


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(tmp_path, monkeypatch, states, wake=None):
    device = Television(Config(tv={"ip": IP, "mac": "AA:BB:CC:DD:EE:FF"}).tv, tmp_path)
    private_write(device.token, b"synthetic-token")
    fake = FakeTV(states)
    monkeypatch.setattr(device, "remote", fake.remote)
    clock = Clock()
    monkeypatch.setattr(television.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(television.time, "sleep", clock.sleep)
    wakes = []

    def fake_wake(**kwargs):
        wakes.append(True)
        clock.now += 7
        if isinstance(wake, Exception):
            raise wake
        return wake

    monkeypatch.setattr(device, "wake", fake_wake)
    return device, fake, clock, wakes


def test_screen_off_sends_power_off_key_once_only_in_art_mode(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [("on", "on"), ("standby", "on")])
    probe = device.screen_off()
    assert fake.keys == [("click", "KEY_POWEROFF")]
    assert probe == {"answers": True, "power": "standby", "art_mode": "on"}


def test_screen_off_rechecks_art_mode_just_before_the_key(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [("on", "on")])
    remote = fake.remote

    def flips(**kwargs):
        connection = remote(**kwargs)
        replies = iter(["on", "off"])  # Art Mode at the first check, TV mode at the recheck
        art = SimpleNamespace(get_artmode=lambda: next(replies), close=lambda: None)
        connection.art = lambda: art
        return connection
    monkeypatch.setattr(device, "remote", flips)
    with pytest.raises(DisplayError, match="not-art-mode"):
        device.screen_off()
    assert not fake.keys


@pytest.mark.parametrize("state", [("on", "off"), ("standby", "on"), ("off", ""), None, ("on", "")])
def test_screen_off_refuses_programme_standby_and_unknown(tmp_path, monkeypatch, state):
    device, fake, _, _ = make(tmp_path, monkeypatch, [state])
    with pytest.raises(DisplayError, match="not-art-mode"):
        device.screen_off()
    assert not fake.keys


@pytest.mark.parametrize("state", [("on", "on"), ("on", "off"), ("", "on"), ("", "")])
def test_screen_on_refuses_and_never_clicks_while_tv_answers_on(tmp_path, monkeypatch, state):
    device, fake, _, wakes = make(tmp_path, monkeypatch, [state])
    with pytest.raises(DisplayError, match="screen-is-on"):
        device.screen_on()
    assert not fake.keys and not wakes


def test_screen_on_uses_one_power_click_from_standby_and_polls(tmp_path, monkeypatch):
    states = [("standby", "on")] * 5 + [("on", "on")]  # probe, recheck, then polls
    device, fake, clock, wakes = make(tmp_path, monkeypatch, states)
    result = device.screen_on()
    assert fake.keys == [("click", "KEY_POWER")] and not wakes
    assert result == ScreenOn("power key", 2, "confirmed")  # 4 checks, every 0.5 s at first
    assert clock.now == 2.0


def test_screen_on_polling_is_bounded(tmp_path, monkeypatch):
    device, fake, clock, _ = make(tmp_path, monkeypatch, [("standby", "off")])
    result = device.screen_on()
    assert result == ScreenOn("power key", 30, "not-confirmed")
    assert fake.keys == [("click", "KEY_POWER")] and clock.now == 30


def test_screen_on_reports_tv_mode(tmp_path, monkeypatch):
    device, _, _, _ = make(tmp_path, monkeypatch, [("standby", "on"), ("standby", "on"), ("on", "off")])
    assert device.screen_on().result == "tv-mode"


@pytest.mark.parametrize("wake,result", [(True, "confirmed"), (WakeError("not-confirmed"), "not-confirmed"), (False, "watching")])
def test_screen_on_uses_wake_signal_when_tv_does_not_answer(tmp_path, monkeypatch, wake, result):
    device, fake, _, wakes = make(tmp_path, monkeypatch, [None], wake=wake)
    assert device.screen_on() == ScreenOn("wake signal", 7, result)
    assert wakes == [True] and not fake.keys


def test_screen_on_without_mac_explains_and_sends_nothing(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [None], wake=WakeError("missing-mac"))
    with pytest.raises(DisplayError, match="missing-mac"):
        device.screen_on()
    assert not fake.keys


def test_probe_is_read_only_and_drops_unknown_values(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [("<b>odd</b>", "<script>")])
    assert device.probe() == {"answers": True, "power": None, "art_mode": None}
    device, fake, _, _ = make(tmp_path, monkeypatch, [None])
    assert device.probe() == {"answers": False, "power": None, "art_mode": None}
    assert not fake.keys


def controller_for(tmp_path, tv):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(tv={"ip": IP}), id="main")
    return Controller(configs, Library(tmp_path), tv_factory=lambda cfg: tv)


def test_controller_records_off_time_and_logs_numbers_only(tmp_path, monkeypatch, caplog):
    device, _, _, _ = make(tmp_path, monkeypatch, [("on", "on")] + [("standby", "on")] * 4 + [("on", "on")])
    controller = controller_for(tmp_path, device)
    with caplog.at_level(logging.INFO):
        controller.screen_off()
        assert controller.library.state("screen_off_at:" + Config(tv={"ip": IP}).tv.identity)
        assert controller.check_screen()["since_off"] == 0
        controller.screen_on()
    assert "TV main: screen off sent (TV answers: yes, power standby)" in caplog.text
    assert "TV main: screen check: answers yes, power standby, Art Mode on, 0 s after screen off" in caplog.text
    assert "TV main: screen on by power key in " in caplog.text
    assert IP not in caplog.text and "token" not in caplog.text
    # A confirmed screen-on clears the stamp, so no "since screen off" afterwards.
    assert controller.library.state("screen_off_at:" + Config(tv={"ip": IP}).tv.identity) is None


def test_screen_actions_hold_the_display_lock(tmp_path):
    controller = controller_for(tmp_path, DemoTelevision())
    controller.lock.acquire()
    try:
        for action in (controller.screen_off, controller.check_screen, controller.screen_on):
            with pytest.raises(Busy):
                action()
    finally:
        controller.lock.release()


def test_demo_screen_off_and_on():
    tv = DemoTelevision()
    assert tv.screen_off()["power"] == "standby"
    with pytest.raises(DisplayError):
        tv.screen_off()
    with pytest.raises(DisplayError):
        tv.set_brightness(3)
    assert tv.screen_on().result == "confirmed"
    with pytest.raises(DisplayError, match="screen-is-on"):
        tv.screen_on()


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


def card(page, card_id):
    """The HTML of one Display page card, from its opening tag to its end."""
    start = page.index(f'id="{card_id}"')
    return page[start:page.index("</section>", start)]


ROUTES = ["/screen/display/screen-off", "/screen/display/check-screen", "/screen/display/screen-on"]


@pytest.mark.parametrize("route", ROUTES)
def test_screen_routes_require_auth_csrf_and_known_tv(app, monkeypatch, route):
    calls = []
    controller = app.extensions["controller"]
    for name in ("screen_off", "check_screen", "screen_on"):
        monkeypatch.setattr(controller, name, lambda _name=name: calls.append(_name))
    client = app.test_client()
    assert client.post(route).status_code == 401
    assert client.post(route, headers=auth()).status_code == 400
    assert client.post(route + "?tv=missing", headers=auth(), data={"csrf": csrf(client)}).status_code == 404
    assert not calls


@pytest.mark.parametrize("name,route,result,text", [
    ("screen_off", "screen-off", {"answers": True, "power": "standby", "art_mode": "on"}, "Power-off key sent. The TV still answers. Power: Standby. Art Mode: On."),
    ("screen_off", "screen-off", {"answers": False, "power": None, "art_mode": None}, "Power-off key sent. The TV no longer answers on the network."),
    ("check_screen", "check-screen", {"answers": True, "power": "standby", "art_mode": "off", "since_off": 12},
     "The TV answers on the network. Power: Standby. Art Mode: Off. 12 seconds since the power-off key."),
    ("screen_on", "screen-on", ScreenOn("power key", 4, "confirmed"), "The TV came back in 4 seconds using the power key."),
    ("screen_on", "screen-on", ScreenOn("wake signal", 30, "not-confirmed"), "Art Mode was not confirmed after 30 seconds using the wake signal."),
])
def test_screen_routes_report_plain_sentences_in_the_panel(app, monkeypatch, name, route, result, text):
    monkeypatch.setattr(app.extensions["controller"], name, lambda: result)
    client = app.test_client()
    page = client.post("/screen/display/" + route, headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text
    assert page.count(text) == 1 and text in card(page, "screen-power") and text not in card(page, "art-display")


def test_screen_route_refusal_and_private_errors(app, monkeypatch, caplog):
    controller = app.extensions["controller"]
    client = app.test_client()

    def on():
        raise DisplayError("screen-is-on")
    monkeypatch.setattr(controller, "screen_on", on)
    page = client.post("/screen/display/screen-on", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text
    assert "Not done: the TV answers and is not in standby. Nothing was sent." in page

    def private():
        raise RuntimeError("<script>ws://" + IP + "</script>")
    monkeypatch.setattr(controller, "screen_off", private)
    with caplog.at_level(logging.INFO):
        page = client.post("/screen/display/screen-off", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text
    assert "The TV did not answer" in page
    assert IP not in page + caplog.text and "<script>ws" not in page


def test_panel_warns_before_the_power_buttons(app):
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    warning = page.index("These buttons turn the TV's screen off and on. Use them only while testing.")
    assert warning < page.index("Try the power-off key</button>") < page.index("Turn screen on</button>")


def test_screen_on_rechecks_power_on_the_key_connection(tmp_path, monkeypatch):
    device, fake, _, wakes = make(tmp_path, monkeypatch, [("standby", "on"), ("on", "on")])
    with pytest.raises(DisplayError, match="screen-is-on"):
        device.screen_on()
    assert not fake.keys and not wakes


def test_screen_on_timeout_reports_real_elapsed_time(tmp_path, monkeypatch):
    device, fake, clock, _ = make(tmp_path, monkeypatch, [("standby", "off")])
    probe = device.probe

    def slow_probe(**kwargs):
        clock.now += 5.5  # each check takes a while on a slow network
        return probe(**kwargs)
    monkeypatch.setattr(device, "probe", slow_probe)
    result = device.screen_on()
    assert result.result == "not-confirmed" and result.seconds == round(clock.now - 5.5) == 32


@pytest.mark.parametrize("offset_minutes", [-5, 0, 3])
def test_power_tests_refused_near_scheduled_wake(tmp_path, monkeypatch, offset_minutes):
    from datetime import UTC, datetime, timedelta

    moment = datetime.now(UTC) - timedelta(minutes=offset_minutes)
    cfg = Config(timezone="UTC", tv={"ip": IP, "mac": "AA:BB:CC:DD:EE:FF"}, wake={"enabled": True, "at": moment.strftime("%H:%M"), "days": list(range(7))})
    device, fake, _, wakes = make(tmp_path, monkeypatch, [("on", "on")])
    configs = SimpleNamespace(root=tmp_path, load=lambda: cfg, id="main")
    controller = Controller(configs, Library(tmp_path), tv_factory=lambda c: device)
    for action in (controller.screen_off, controller.screen_on):
        with pytest.raises(DisplayError, match="wake-window"):
            action()
    assert not fake.keys and not wakes
    assert controller.check_screen()["answers"]  # reading stays allowed


def test_wake_window_refusal_message(app, monkeypatch):
    def refuse():
        raise DisplayError("wake-window")
    monkeypatch.setattr(app.extensions["controller"], "screen_off", refuse)
    client = app.test_client()
    page = client.post("/screen/display/screen-off", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text
    assert "Not done: wait until the morning wake is done. Nothing was changed." in page


class StandbyTV:
    """REST replies come from `powers` (None means no answer); the last one repeats. Records every art call."""

    def __init__(self, powers, *, answers=True, upload_fails=False, select_fails=False):
        self.powers, self.calls, self.keys = list(powers), [], []
        self.answers, self.upload_fails, self.select_fails = answers, upload_fails, select_fails

    def remote(self, **kwargs):
        tv = self

        def info():
            power = tv.powers.pop(0) if len(tv.powers) > 1 else tv.powers[0]
            if power is None:
                raise OSError("no route to " + IP)
            return {"device": {"PowerState": power}}

        def artmode():
            tv.calls.append("get_artmode")
            if not tv.answers:
                raise OSError("art channel timed out")
            return "on"

        def upload(image, **kwargs):
            tv.calls.append(("upload", image))
            if tv.upload_fails:
                raise OSError("upload failed")
            return "standby-id"

        def select(content_id, *, show):
            tv.calls.append(("select", content_id, show))
            if tv.select_fails:
                raise OSError("no reply")

        art = SimpleNamespace(get_artmode=artmode, upload=upload, select_image=select,
                              set_artmode=lambda value: tv.calls.append(("set_artmode", value)), close=lambda: None)
        return SimpleNamespace(rest_device_info=info, art=lambda **_: art, close=lambda: None,
                               send_key=lambda key, **kwargs: tv.keys.append(key),
                               hold_key=lambda key, seconds: tv.keys.append(key))


def standby_device(tmp_path, monkeypatch, fake):
    device = Television(Config(tv={"ip": IP, "mac": "AA:BB:CC:DD:EE:FF"}).tv, tmp_path)
    private_write(device.token, b"synthetic-token")
    monkeypatch.setattr(device, "remote", fake.remote)
    clock = Clock()
    monkeypatch.setattr(television.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(television.time, "sleep", clock.sleep)
    monkeypatch.setattr(device, "wake", lambda **kwargs: pytest.fail("The standby test must never wake the TV"))
    monkeypatch.setattr(device, "_send_magic", lambda: pytest.fail("The standby test must never send a wake packet"))
    return device


@pytest.mark.parametrize("power", ["on", "off", "", None])
def test_standby_load_refused_unless_rest_confirms_standby(tmp_path, monkeypatch, power):
    fake = StandbyTV([power])
    device = standby_device(tmp_path, monkeypatch, fake)
    with pytest.raises(DisplayError, match="not-standby"):
        device.standby_load(b"jpeg", lambda _: pytest.fail("nothing may be uploaded"))
    assert not fake.calls and not fake.keys


def test_standby_load_uploads_and_selects_without_showing(tmp_path, monkeypatch):
    fake = StandbyTV(["standby"])
    device = standby_device(tmp_path, monkeypatch, fake)
    owned = []
    result = device.standby_load(b"jpeg", owned.append)
    assert result == {"answered": True, "uploaded": True, "selected": "sent", "power_after": "standby",
                      "stayed": True, "seconds": 5.0}
    assert fake.calls == ["get_artmode", ("upload", b"jpeg"), ("select", "standby-id", False)]
    assert owned == ["standby-id"] and not fake.keys


@pytest.mark.parametrize("fake,expected,calls", [
    (StandbyTV(["standby"], answers=False), {"answered": False, "uploaded": None, "selected": None}, ["get_artmode"]),
    (StandbyTV(["standby"], upload_fails=True), {"answered": True, "uploaded": False, "selected": None},
     ["get_artmode", ("upload", b"jpeg")]),
    (StandbyTV(["standby"], select_fails=True), {"answered": True, "uploaded": True, "selected": "no-reply"},
     ["get_artmode", ("upload", b"jpeg"), ("select", "standby-id", False)]),
    # The upload turned the screen on: nothing is selected, and the result says the TV left standby.
    (StandbyTV(["standby", "on"]), {"answered": True, "uploaded": True, "selected": "skipped", "stayed": False,
                                    "power_after": "on"}, ["get_artmode", ("upload", b"jpeg")]),
    (StandbyTV(["standby", "standby", None]), {"selected": "sent", "stayed": None, "power_after": None},
     ["get_artmode", ("upload", b"jpeg"), ("select", "standby-id", False)]),
])
def test_standby_load_reports_each_outcome(tmp_path, monkeypatch, fake, expected, calls):
    device = standby_device(tmp_path, monkeypatch, fake)
    result = device.standby_load(b"jpeg", lambda _: None)
    assert {key: result[key] for key in expected} == expected
    assert fake.calls == calls and not fake.keys
    assert ("set_artmode", True) not in fake.calls


def test_controller_standby_load_uses_the_preview_stores_the_result_and_owns_the_upload(tmp_path, monkeypatch, caplog):
    fake = StandbyTV(["standby"])
    device = standby_device(tmp_path, monkeypatch, fake)
    controller = controller_for(tmp_path, device)
    with pytest.raises(DisplayError, match="no-image"):
        controller.standby_load()
    assert not fake.calls
    private_write(tmp_path / "screen-preview.jpg", b"preview-jpeg")
    with caplog.at_level(logging.INFO):
        controller.standby_load()
    identity = Config(tv={"ip": IP}).tv.identity
    stored = controller.library.state("standby_test")
    assert stored["device"] == identity and stored["stayed"] is True and stored["checked_at"]
    assert ("upload", b"preview-jpeg") in fake.calls
    assert controller.library.state("owned_images:" + identity) == ["standby-id"]
    assert "TV main: standby load test: art channel answered yes, upload worked yes, selection sent, " \
           "still in standby yes (power standby), 5.0 s" in caplog.text
    assert IP not in caplog.text and "standby-id" not in caplog.text


@pytest.mark.parametrize("offset_minutes", [-5, 0, 3])
def test_standby_load_refused_near_scheduled_wake(tmp_path, monkeypatch, offset_minutes):
    from datetime import UTC, datetime, timedelta

    moment = datetime.now(UTC) - timedelta(minutes=offset_minutes)
    cfg = Config(timezone="UTC", tv={"ip": IP, "mac": "AA:BB:CC:DD:EE:FF"}, wake={"enabled": True, "at": moment.strftime("%H:%M"), "days": list(range(7))})
    fake = StandbyTV(["standby"])
    device = standby_device(tmp_path, monkeypatch, fake)
    private_write(tmp_path / "screen-preview.jpg", b"preview-jpeg")
    controller = Controller(SimpleNamespace(root=tmp_path, load=lambda: cfg, id="main"), Library(tmp_path), tv_factory=lambda c: device)
    with pytest.raises(DisplayError, match="wake-window"):
        controller.standby_load()
    assert not fake.calls and not fake.keys


def test_demo_standby_load():
    tv = DemoTelevision()
    with pytest.raises(DisplayError, match="not-standby"):
        tv.standby_load(b"jpeg", lambda _: None)
    tv.screen_off()
    assert tv.standby_load(b"jpeg", lambda _: None)["stayed"] is True


def test_standby_route_requires_auth_and_csrf(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "standby_load", lambda: calls.append(1))
    client = app.test_client()
    assert client.post("/screen/display/standby-load").status_code == 401
    assert client.post("/screen/display/standby-load", headers=auth()).status_code == 400
    assert not calls


@pytest.mark.parametrize("result,text", [
    ({"answered": True, "uploaded": True, "selected": "sent", "power_after": "standby", "stayed": True, "seconds": 7.4},
     "Test done in 7 seconds. The art channel answered and the upload worked. The TV accepted the image as the next one to show. The TV stayed in standby."),
    ({"answered": False, "uploaded": None, "selected": None, "power_after": "standby", "stayed": True, "seconds": 8.0},
     "Test done in 8 seconds. The art channel did not answer, so nothing was uploaded. The TV stayed in standby."),
    ({"answered": True, "uploaded": False, "selected": None, "power_after": None, "stayed": None, "seconds": 9.0},
     "Test done in 9 seconds. The art channel answered, but the upload failed. The TV did not report its power afterward."),
    ({"answered": True, "uploaded": True, "selected": "skipped", "power_after": "on", "stayed": False, "seconds": 12.0},
     "Test done in 12 seconds. The art channel answered and the upload worked. The TV left standby during the upload, "
     "so the image was not selected. The TV did not stay in standby. Power: On."),
])
def test_standby_route_reports_plain_sentences_in_the_panel(app, monkeypatch, result, text):
    monkeypatch.setattr(app.extensions["controller"], "standby_load", lambda: result)
    client = app.test_client()
    page = client.post("/screen/display/standby-load", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text
    assert page.count(text) == 1 and text in card(page, "standby-test") and text not in card(page, "screen-power")


def test_standby_route_refusal_and_stored_result(app, monkeypatch):
    controller = app.extensions["controller"]

    def refuse():
        raise DisplayError("not-standby")
    monkeypatch.setattr(controller, "standby_load", refuse)
    client = app.test_client()
    page = client.post("/screen/display/standby-load", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text
    assert "Not done: the TV did not report standby. Nothing was sent." in page
    assert "Test: load the next image in standby</button>" in page
    controller.library.set_state("standby_test", {
        "answered": True, "uploaded": True, "selected": "sent", "power_after": "on", "stayed": False, "seconds": 6.0,
        "device": controller.configs.load().tv.identity, "checked_at": "2026-11-01T06:52:00+00:00"})
    page = client.get("/tv/main/picture", headers=auth()).text
    assert "<dt>Stayed in standby</dt><dd>No</dd>" in page and "<dd>6 seconds</dd>" in page
