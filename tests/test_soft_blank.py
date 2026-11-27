"""The soft blank test: a black image at the lowest brightness, only in confirmed Art Mode, and its undo."""

import io
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from test_screen_power import Clock, card
from test_sensors import light, rig  # noqa: F401  (rig is a fixture)
from test_web import PASSWORD, auth, csrf

from art_frames import sensor_control, television
from art_frames.config import Config, private_write
from art_frames.controller import DEVICE_KEYS, Busy, Controller
from art_frames.library import Library
from art_frames.television import DemoTelevision, DisplayError, Readback, Television, black_jpeg
from art_frames.web import create_app

IP = "192.168.1.50"
CHANGES = ("select", "upload", "brightness", "set_artmode", "key")


class BlankTV:
    """A fake Frame. `powers` and `modes` are reply lists (None means no answer); the last one repeats.

    `images` are the content IDs the TV holds; selecting another ID does nothing.
    """

    def __init__(self, powers=("on",), modes=("on",), *, current="art-1", brightness=6, images=("art-1",),
                 report_brightness=True):
        self.powers, self.modes = list(powers), list(modes)
        self.current, self.brightness, self.images = current, brightness, set(images)
        self.report_brightness, self.calls, self.uploads = report_brightness, [], 0

    @staticmethod
    def _next(replies):
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if reply is None:
            raise OSError("no route to " + IP)
        return reply

    def changes(self):
        return [call for call in self.calls if call[0] in CHANGES]

    def remote(self, **kwargs):
        tv = self

        def settings():
            items = [{"item": "brightness", "value": str(tv.brightness), "min": "0", "max": "10"}] if tv.report_brightness else []
            return {"data": json.dumps(items)}

        def select(content_id, *, show):
            tv.calls.append(("select", content_id, show))
            if content_id in tv.images:
                tv.current = content_id

        def upload(image, **kwargs):
            tv.uploads += 1
            content_id = f"black-{tv.uploads}"
            tv.images.add(content_id)
            tv.calls.append(("upload", content_id, image))
            return content_id

        def set_brightness(value):
            tv.calls.append(("brightness", value))
            tv.brightness = value

        art = SimpleNamespace(
            get_artmode=lambda: tv._next(tv.modes), get_artmode_settings=settings,
            get_brightness=lambda: tv.brightness if tv.report_brightness else None,
            get_color_temperature=lambda: 0, get_current=lambda: {"content_id": tv.current},
            select_image=select, upload=upload, set_brightness=set_brightness,
            set_artmode=lambda value: tv.calls.append(("set_artmode", value)), close=lambda: None)
        return SimpleNamespace(
            rest_device_info=lambda: {"device": {"PowerState": tv._next(tv.powers)}}, art=lambda **_: art,
            close=lambda: None, send_key=lambda key, **kwargs: tv.calls.append(("key", key)),
            hold_key=lambda key, seconds: tv.calls.append(("key", key)))


def make(tmp_path, monkeypatch, fake):
    device = Television(Config(tv={"ip": IP}).tv, tmp_path)
    private_write(device.token, b"synthetic-token")
    monkeypatch.setattr(device, "remote", fake.remote)
    clock = Clock()
    monkeypatch.setattr(television.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(television.time, "sleep", clock.sleep)
    monkeypatch.setattr(device, "wake", lambda **kwargs: pytest.fail("The soft blank must never wake the TV"))
    monkeypatch.setattr(device, "_send_magic", lambda: pytest.fail("The soft blank must never send a wake packet"))
    return device


def controller_for(tmp_path, tv):
    configs = SimpleNamespace(root=tmp_path, load=lambda: Config(timezone="UTC", tv={"ip": IP}), id="main")
    return Controller(configs, Library(tmp_path), tv_factory=lambda cfg: tv)


IDENTITY = Config(tv={"ip": IP}).tv.identity


def state(controller, prefix):
    return controller.library.state(prefix + IDENTITY)


def test_black_image_is_a_small_black_jpeg_at_the_art_size():
    image = Image.open(io.BytesIO(black_jpeg()))
    assert image.format == "JPEG" and image.size == (3840, 2160) and image.getextrema() == ((0, 0),) * 3
    assert len(black_jpeg()) < 100_000


def test_blank_saves_the_image_and_brightness_then_shows_black_at_the_lowest_brightness(tmp_path, monkeypatch, caplog):
    fake = BlankTV()
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    with caplog.at_level(logging.INFO):
        result = controller.soft_blank()
    assert fake.changes() == [("upload", "black-1", black_jpeg()), ("select", "black-1", True), ("brightness", 0)]
    assert fake.current == "black-1" and fake.brightness == 0
    saved = state(controller, "soft_blank:")
    assert saved["content_id"] == "art-1" and saved["brightness"] == 6 and datetime.fromisoformat(saved["at"])
    assert result["shown"] and result["readback"].reported == 0 and result["saved"]["brightness"] == 6
    # The black image is Art Frames' upload, so cleanup deletes it later.
    assert state(controller, "owned_images:") == ["black-1"] and state(controller, "soft_blank_image:") == "black-1"
    assert controller.library.state("art_display")["brightness"] == 0
    assert "TV main: soft blank: black image shown, brightness 0 (TV reports 0), saved brightness 6" in caplog.text
    assert IP not in caplog.text and "black-1" not in caplog.text and "art-1" not in caplog.text


def test_a_second_blank_keeps_the_saved_values_and_reuses_the_black_image(tmp_path, monkeypatch):
    fake = BlankTV()
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    controller.soft_blank()
    first = state(controller, "soft_blank:")
    fake.calls.clear()
    fake.brightness = 3  # someone changed the brightness while blanked
    result = controller.soft_blank()
    assert state(controller, "soft_blank:") == first  # not the black image, not brightness 3
    assert result["saved"] == first
    assert fake.changes() == [("select", "black-1", True), ("brightness", 0)] and fake.uploads == 1


def test_blank_uploads_again_when_the_tv_lost_the_black_image(tmp_path, monkeypatch):
    fake = BlankTV()
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    controller.soft_blank()
    controller.library.delete_state("soft_blank:" + IDENTITY)
    fake.images.discard("black-1")  # deleted on the TV, though Art Frames still owns it
    fake.current, fake.brightness, fake.calls = "art-1", 6, []
    controller.soft_blank()
    assert [call[:2] for call in fake.changes()] == [("select", "black-1"), ("upload", "black-2"),
                                                    ("select", "black-2"), ("brightness", 0)]
    assert state(controller, "soft_blank_image:") == "black-2"
    assert state(controller, "soft_blank:")["content_id"] == "art-1"


def test_blank_does_not_reuse_a_black_image_cleanup_deleted(tmp_path, monkeypatch):
    fake = BlankTV()
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    controller.library.set_state("soft_blank_image:" + IDENTITY, "black-old")  # no longer in owned_images
    controller.soft_blank()
    assert [call[:2] for call in fake.changes()][:2] == [("upload", "black-1"), ("select", "black-1")]


@pytest.mark.parametrize("powers,modes", [
    (["standby"], ["on"]),  # a Frame in standby can still report Art Mode
    (["off"], ["on"]),
    (["on"], ["off"]),  # TV mode
    ([None], ["on"]),  # no answer
    (["on"], [None]),  # Art Mode unknown
])
def test_blank_refuses_outside_confirmed_art_mode_and_sends_nothing(tmp_path, monkeypatch, powers, modes):
    fake = BlankTV(powers, modes)
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    with pytest.raises(DisplayError, match="not-art-mode"):
        controller.soft_blank()
    assert fake.changes() == []
    assert state(controller, "soft_blank:") is None and state(controller, "owned_images:") is None


def test_blank_refuses_when_the_tv_does_not_report_brightness(tmp_path, monkeypatch):
    fake = BlankTV(report_brightness=False)
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    with pytest.raises(DisplayError, match="no-brightness"):
        controller.soft_blank()
    assert fake.changes() == [] and state(controller, "soft_blank:") is None


def test_blank_rechecks_art_mode_before_the_upload_and_select(tmp_path, monkeypatch):
    # Power on at the first check, then standby at the recheck.
    fake = BlankTV(["on", "standby"])
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    with pytest.raises(DisplayError, match="not-art-mode"):
        controller.soft_blank()
    assert fake.changes() == [] and state(controller, "soft_blank:") is None


def test_blank_rechecks_art_mode_before_the_brightness(tmp_path, monkeypatch):
    # First check, before the upload, before saving, before the select: on. Before the brightness: standby.
    fake = BlankTV(["on", "on", "on", "on", "standby"])
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    with pytest.raises(DisplayError, match="not-art-mode"):
        controller.soft_blank()
    assert [call[0] for call in fake.changes()] == ["upload", "select"]
    # The black image is on screen, so the saved values stay for Undo blank.
    assert state(controller, "soft_blank:")["brightness"] == 6


def test_blank_without_a_confirmed_black_image_leaves_the_brightness(tmp_path, monkeypatch):
    fake = BlankTV()
    fake_select = fake.remote

    def never_shows(**kwargs):
        connection = fake_select(**kwargs)
        art = connection.art()
        art.select_image = lambda content_id, *, show: fake.calls.append(("select", content_id, show))
        connection.art = lambda **_: art
        return connection
    device = make(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(device, "remote", never_shows)
    controller = controller_for(tmp_path, device)
    result = controller.soft_blank()
    assert not result["shown"] and result["readback"] is None
    assert ("brightness", 0) not in fake.changes() and state(controller, "soft_blank:")["brightness"] == 6


def blanked(tmp_path, monkeypatch, **kwargs):
    fake = BlankTV(**kwargs)
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    controller.soft_blank()
    fake.calls.clear()
    return fake, controller


def test_undo_restores_the_image_and_brightness_and_clears_the_record(tmp_path, monkeypatch, caplog):
    fake, controller = blanked(tmp_path, monkeypatch)
    controller.library.set_state("display_key:" + IDENTITY, "key-1")
    with caplog.at_level(logging.INFO):
        result = controller.undo_blank()
    assert fake.changes() == [("select", "art-1", True), ("brightness", 6)]
    assert fake.current == "art-1" and fake.brightness == 6
    assert result["restored"] is True and result["brightness"] == 6 and result["readback"].reported == 6
    assert state(controller, "soft_blank:") is None
    assert state(controller, "display_key:") == "key-1"  # the image is back: no new render needed
    assert "TV main: soft blank undone: image restored, brightness 6 (TV reports 6)" in caplog.text


def test_undo_when_the_saved_image_is_gone_lets_the_schedule_send_the_next_image(tmp_path, monkeypatch):
    fake, controller = blanked(tmp_path, monkeypatch)
    fake.images.discard("art-1")
    controller.library.set_state("display_key:" + IDENTITY, "key-1")
    result = controller.undo_blank()
    assert result["restored"] is False and fake.brightness == 6
    assert state(controller, "soft_blank:") is None and state(controller, "display_key:") is None


def test_undo_without_a_saved_image_restores_the_brightness(tmp_path, monkeypatch):
    fake, controller = blanked(tmp_path, monkeypatch)
    controller.library.set_state("soft_blank:" + IDENTITY, {"content_id": None, "brightness": 4, "at": "2026-11-01T07:00:00+00:00"})
    result = controller.undo_blank()
    assert fake.changes() == [("brightness", 4)] and result["restored"] is None
    assert state(controller, "soft_blank:") is None


@pytest.mark.parametrize("powers,modes", [(["standby"], ["on"]), (["on"], ["off"]), ([None], ["on"]), (["on"], [None])])
def test_undo_refuses_outside_art_mode_and_keeps_the_record(tmp_path, monkeypatch, powers, modes):
    fake, controller = blanked(tmp_path, monkeypatch)
    saved = state(controller, "soft_blank:")
    fake.powers, fake.modes = list(powers), list(modes)
    with pytest.raises(DisplayError, match="not-art-mode"):
        controller.undo_blank()
    assert fake.changes() == [] and state(controller, "soft_blank:") == saved


def test_undo_keeps_the_record_when_the_tv_leaves_art_mode_midway(tmp_path, monkeypatch):
    fake, controller = blanked(tmp_path, monkeypatch)
    fake.powers = ["on", "on", "standby"]  # first check, before the select, then standby before the brightness
    with pytest.raises(DisplayError, match="not-art-mode"):
        controller.undo_blank()
    assert fake.changes() == [("select", "art-1", True)]
    assert state(controller, "soft_blank:")["brightness"] == 6  # a later undo restores the brightness


def test_undo_refuses_when_the_screen_is_not_blanked(tmp_path, monkeypatch):
    fake = BlankTV()
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    with pytest.raises(DisplayError, match="not-blanked"):
        controller.undo_blank()
    assert fake.calls == []


def test_blank_actions_hold_the_display_lock(tmp_path):
    controller = controller_for(tmp_path, DemoTelevision())
    controller.lock.acquire()
    try:
        for action in (controller.soft_blank, controller.undo_blank):
            with pytest.raises(Busy):
                action()
    finally:
        controller.lock.release()


def test_blank_records_move_and_clear_with_the_device():
    assert "soft_blank:" in DEVICE_KEYS and "soft_blank_image:" in DEVICE_KEYS


def test_demo_blank_and_undo(tmp_path):
    tv = DemoTelevision()
    controller = controller_for(tmp_path, tv)
    assert controller.soft_blank()["readback"].reported == 0
    assert controller.blank_record()["brightness"] == 5
    assert controller.undo_blank()["readback"].reported == 5 and controller.blank_record() is None
    tv.screen_off()
    with pytest.raises(DisplayError, match="not-art-mode"):
        tv.soft_blank(b"", None, lambda *args: None, lambda _: None)


# The Picture page


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


def post(app, route):
    client = app.test_client()
    return client.post("/screen/display/" + route, headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True).text


@pytest.mark.parametrize("route", ["blank", "undo-blank"])
def test_blank_routes_require_auth_and_csrf(app, monkeypatch, route):
    calls = []
    for name in ("soft_blank", "undo_blank"):
        monkeypatch.setattr(app.extensions["controller"], name, lambda _name=name: calls.append(_name))
    client = app.test_client()
    assert client.post("/screen/display/" + route).status_code == 401
    assert client.post("/screen/display/" + route, headers=auth()).status_code == 400
    assert not calls


def readback(requested, reported):
    return Readback(requested, reported, None, True, {})


@pytest.mark.parametrize("name,route,result,text", [
    ("soft_blank", "blank", {"shown": True, "readback": readback(0, 0), "saved": {"brightness": 6}},
     "Screen blanked: black image at brightness 0. Saved image and brightness 6."),
    ("soft_blank", "blank", {"shown": False, "readback": None, "saved": {"brightness": 6}},
     "The TV did not confirm the black image, so the brightness was not changed. Saved image and brightness 6. "
     "Use Undo blank to show them again."),
    ("soft_blank", "blank", {"shown": True, "readback": readback(0, 2), "saved": {"brightness": 6}},
     "Black image shown, but the TV reports brightness 2, not 0. Saved image and brightness 6."),
    ("undo_blank", "undo-blank", {"restored": True, "readback": readback(6, 6), "brightness": 6},
     "Blank undone: image and brightness 6 restored."),
    ("undo_blank", "undo-blank", {"restored": False, "readback": readback(6, 6), "brightness": 6},
     "Blank undone: brightness 6 restored. The TV did not show the saved image. The next update sends the current image."),
    ("undo_blank", "undo-blank", {"restored": True, "readback": readback(6, None), "brightness": 6},
     "Blank undone, but the TV reports brightness nothing, not 6."),
])
def test_blank_routes_report_plain_sentences_in_the_panel(app, monkeypatch, name, route, result, text):
    monkeypatch.setattr(app.extensions["controller"], name, lambda: result)
    page = post(app, route)
    assert page.count(text) == 1 and text in card(page, "soft-blank") and text not in card(page, "screen-power")


@pytest.mark.parametrize("name,route,code,text", [
    ("soft_blank", "blank", "not-art-mode", "Not done: the TV is not on in Art Mode. Art Frames did not wake it or change anything."),
    ("soft_blank", "blank", "no-brightness", "Not done: the TV does not report its brightness, so Art Frames could not restore it. Nothing was sent."),
    ("undo_blank", "undo-blank", "not-art-mode", "Not done: the TV is not on in Art Mode. Art Frames did not wake it or change anything."),
    ("undo_blank", "undo-blank", "not-blanked", "Not done: the screen is not blanked. Nothing was sent."),
])
def test_blank_routes_explain_refusals(app, monkeypatch, name, route, code, text):
    def refuse():
        raise DisplayError(code)
    monkeypatch.setattr(app.extensions["controller"], name, refuse)
    assert text in card(post(app, route), "soft-blank")


def test_picture_page_shows_when_the_screen_was_blanked(app):
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    panel = card(page, "soft-blank")
    assert "Blank the screen</button>" in panel and "Undo blank</button>" in panel and "Screen blanked since" not in panel
    library, configs = app.extensions["library"], app.extensions["configs"]
    moment = datetime(2026, 11, 1, 7, 5, tzinfo=ZoneInfo(configs.load().timezone))
    library.set_state("soft_blank:" + configs.load().tv.identity, {"content_id": "art-1", "brightness": 6, "at": moment.isoformat()})
    page = app.test_client().get("/tv/main/picture", headers=auth()).text
    assert "Screen blanked since 07:05." in card(page, "soft-blank")


def test_now_page_names_the_blank_pause(app):
    library, configs = app.extensions["library"], app.extensions["configs"]
    identity = configs.load().tv.identity
    library.set_state("tv_status", {"paired": True, "known": True, "awake": True, "playing": False})
    library.set_state("soft_blank:" + identity, {"content_id": "art-1", "brightness": 6, "at": "2026-11-01T07:05:00+00:00"})
    assert app.extensions["controller"].rotation_status().paused == "blanked"


# Sensor control (test_sensors.py's rig: a demo TV and a fake sensor)


def test_sensor_control_waits_while_the_screen_is_blanked(rig):  # noqa: F811
    library = rig["screen"].library
    library.set_state("soft_blank:" + rig["screen"].configs.load().tv.identity,
                      {"content_id": None, "brightness": 5, "at": "2026-11-01T07:05:00+00:00"})
    light(rig, 1000.0)
    assert rig["run"]() == "blanked" and not [call for call in rig["calls"] if call[0] == "brightness"]


def test_blank_and_undo_are_no_change_by_hand_for_the_sensor(rig):  # noqa: F811
    app, library = rig["app"], rig["screen"].library
    browser = app.test_client()
    browser.post("/screen/display/blank", headers=auth(), data={"csrf": csrf(browser)}, follow_redirects=True)
    assert rig["display"]["brightness"] == 0 and library.state(sensor_control.CONTROL)["sent"] == 0
    browser.post("/screen/display/undo-blank", headers=auth(), data={"csrf": csrf(browser)}, follow_redirects=True)
    assert rig["display"]["brightness"] == 5 and library.state(sensor_control.CONTROL)["sent"] == 5
    light(rig, 1000.0)
    assert rig["run"]() != "paused"  # the restored level is Art Frames' own, not a remote press


def test_a_blank_that_changed_no_brightness_leaves_the_sensor_alone(app, monkeypatch):
    library = app.extensions["library"]
    library.set_state(sensor_control.CONTROL, {"sent": 7})
    monkeypatch.setattr(app.extensions["controller"], "soft_blank",
                        lambda: {"shown": False, "readback": None, "saved": {"brightness": 7}})
    post(app, "blank")
    assert library.state(sensor_control.CONTROL)["sent"] == 7


def test_a_reused_black_image_rechecks_before_saving(tmp_path, monkeypatch):
    fake = BlankTV()
    controller = controller_for(tmp_path, make(tmp_path, monkeypatch, fake))
    controller.soft_blank()
    controller.undo_blank()
    assert state(controller, "soft_blank:") is None
    # The second blank reuses the black image; the TV goes to standby at the recheck.
    fake.powers = ["on", "standby"]
    before = len(fake.changes())
    with pytest.raises(DisplayError, match="not-art-mode"):
        controller.soft_blank()
    assert len(fake.changes()) == before and state(controller, "soft_blank:") is None
