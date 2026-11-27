"""Picture looks: exact steps for a 0-10 and a 0-50 TV, the sensor rule, and the Apply job. Fake TV only."""
# ruff: noqa: F811

import html
import re

import pytest
from test_sensors import client  # noqa: F401  (a fixture)
from test_web import auth, csrf

from art_frames import picture_looks
from art_frames.controller import Busy
from art_frames.television import DisplayError, Readback


@pytest.mark.parametrize("key, low, high, expected", [
    ("warm", 0, 10, [("light_sensor", False), ("brightness", 4), ("color_temperature", -2)]),
    ("neutral", 0, 10, [("light_sensor", False), ("brightness", 6), ("color_temperature", 0)]),
    ("bright", 0, 10, [("light_sensor", False), ("brightness", 9), ("color_temperature", 0)]),
    ("tv", 0, 10, [("light_sensor", True)]),
    ("warm", 0, 50, [("light_sensor", False), ("brightness", 20), ("color_temperature", -2)]),
    ("neutral", 0, 50, [("light_sensor", False), ("brightness", 30), ("color_temperature", 0)]),
    ("bright", 0, 50, [("light_sensor", False), ("brightness", 45), ("color_temperature", 0)]),
    ("tv", 0, 50, [("light_sensor", True)]),
])
def test_look_steps_for_each_range(key, low, high, expected):
    assert picture_looks.steps(picture_looks.BY_KEY[key], {"min": low, "max": high}, False) == expected


def test_an_unread_range_uses_zero_to_ten():
    assert picture_looks.steps(picture_looks.BY_KEY["bright"], {}, False)[1] == ("brightness", 9)


@pytest.mark.parametrize("key, expected", [
    ("warm", [("color_temperature", -2)]), ("neutral", [("color_temperature", 0)]),
    ("bright", [("color_temperature", 0)]), ("tv", []),
])
def test_with_the_sensor_on_a_look_sets_color_only(key, expected):
    assert picture_looks.steps(picture_looks.BY_KEY[key], {"min": 0, "max": 10}, True) == expected


def test_looks_mats_and_names():
    assert [(look.name, look.mats) for look in picture_looks.LOOKS] == [
        ("Warm gallery", "show"), ("Neutral", "show"), ("Bright room", "never"), ("Let the TV decide", None)]
    assert not picture_looks.usable(picture_looks.BY_KEY["tv"], True)
    assert picture_looks.usable(picture_looks.BY_KEY["tv"], False)


def test_current_matches_the_stored_reading():
    reading = {"min": 0, "max": 10, "brightness": 4, "color_temperature": -2, "sensor": "off"}
    assert picture_looks.current(reading, "show", False).key == "warm"
    assert picture_looks.current(reading, "never", False) is None  # warm uses mats
    assert picture_looks.current({**reading, "brightness": 5}, "show", False) is None
    assert picture_looks.current({**reading, "brightness": 5}, "show", True).key == "warm"  # the sensor owns it
    assert picture_looks.current({**reading, "sensor": "on"}, "show", False).key == "tv"
    assert picture_looks.current({}, "show", False) is None


# The Look card and its Apply job.


class FakeTV:
    """Records each change. off=True refuses the first one, like a TV that is not in Art Mode."""

    def __init__(self, controller, off=False):
        self.calls, self.off = [], off
        controller.set_light_sensor = lambda on: self.step("light_sensor", "on" if on else "off")
        controller.set_brightness = lambda value: self.step("brightness", value)
        controller.set_option = lambda field, value: self.step(field, value)

    def step(self, field, value):
        if self.off:
            raise DisplayError("not-art-mode")
        self.calls.append((field, value))
        return Readback(value, value, None, True, {})


def apply(browser, look):
    response = browser.post("/tv/main/picture/look", headers=auth(), data={"csrf": csrf(browser), "look": look},
                            follow_redirects=True)
    return response, html.unescape(response.get_data(as_text=True))


def read_range(app, low, high):
    cfg = app.extensions["configs"].load()
    app.extensions["library"].set_state("art_display", {
        "device": app.extensions["controller"]._identity(cfg), "min": low, "max": high, "brightness": 1,
        "color_temperature": 0, "sensor": "on", "checked_at": "2026-11-02T08:00:00+00:00"})


def test_apply_sends_one_job_and_saves_the_mats(client):
    app, browser = client
    tv = FakeTV(app.extensions["controller"])
    read_range(app, 0, 50)
    response, body = apply(browser, "bright")
    assert tv.calls == [("light_sensor", "off"), ("brightness", 45), ("color_temperature", 0)]
    assert app.extensions["configs"].load().mats == "never"
    assert "Applied Bright room. Read back from the TV: light sensor off, brightness 45, color temperature 0." in body


def test_apply_with_the_tv_off_changes_nothing(client):
    app, browser = client
    tv = FakeTV(app.extensions["controller"], off=True)
    mats = app.extensions["configs"].load().mats
    response, body = apply(browser, "bright")
    assert tv.calls == [] and app.extensions["configs"].load().mats == mats
    assert "Turn the TV on in Art Mode, then apply again. Nothing changed." in body


def test_with_the_sensor_on_apply_leaves_the_brightness_alone(client):
    app, browser = client
    tv = FakeTV(app.extensions["controller"])
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", enabled=True))
    response, body = apply(browser, "warm")
    assert tv.calls == [("color_temperature", -2)]
    assert "Applied Warm gallery. Read back from the TV: color temperature -2." in body
    response, body = apply(browser, "tv")
    assert tv.calls == [("color_temperature", -2)] and "Not done: choose one of the looks." in body


def test_an_unknown_look_is_refused(client):
    app, browser = client
    tv = FakeTV(app.extensions["controller"])
    response, body = apply(browser, "disco")
    assert tv.calls == [] and "Not done: choose one of the looks." in body


def test_the_look_card_comes_first_with_four_choices(client):
    app, browser = client
    read_range(app, 0, 10)
    body = html.unescape(browser.get("/tv/main/picture", headers=auth()).get_data(as_text=True))
    titles = re.findall(r'<h2 class="card-title" id="([a-z-]+)-title">', body)
    assert titles[:3] == ["look", "mats", "art-display"] and "No settings read yet" not in body
    look = body[body.index('id="look"'):body.index('id="mats"')]
    assert look.count('name="look"') == 4 and ">Apply</button>" in look
    assert 'value="tv" checked' in look  # the stored reading has the TV light sensor on


def test_with_the_sensor_on_the_picture_page_shows_what_it_sets(client):
    app, browser = client
    read_range(app, 0, 10)
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", enabled=True,
                                                                     hours_end="22:00"))
    body = html.unescape(browser.get("/tv/main/picture", headers=auth()).get_data(as_text=True))
    assert "Set by the sensor" in body and "To change it for a while, change it on the TV." in body
    assert 'action="/screen/display/brightness"' not in body and 'action="/screen/display/light-sensor"' not in body
    assert 'name="motion_timer"' not in body
    assert "Managed by the sensor<small>Off during the sensor hours, 5 min after 22:00.</small>" in body
    look = body[body.index('id="look"'):body.index('id="mats"')]
    assert 'value="tv" disabled' in look and "Turn off sensor control first." in look
    # The sensor sets the brightness, so no look line promises one.
    for line in ("Warm color, with mats.", "True color, with mats.", "True color, no mats."):
        assert f'<span class="small">{line}</span>' in look
    assert "brightness" not in re.sub(r'<p class="card-description">.*?</p>', "", look).lower().replace(
        'name="look"', "")


def test_a_look_queued_before_sensor_control_turns_on_sets_color_only(client):
    app, browser = client
    tv = FakeTV(app.extensions["controller"])
    read_range(app, 0, 10)
    jobs = app.extensions["jobs"]
    jobs.mode = "manual"
    browser.post("/tv/main/picture/look", headers=auth(), data={"csrf": csrf(browser), "look": "bright"})
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(host="192.168.1.148", enabled=True))
    jobs.drain()
    assert tv.calls == [("color_temperature", 0)]  # no brightness, no TV light sensor
    browser.post("/tv/main/picture/look", headers=auth(), data={"csrf": csrf(browser), "look": "neutral"})
    browser.post("/tv/main/picture/look", headers=auth(), data={"csrf": csrf(browser), "look": "tv"})
    jobs.drain()
    assert tv.calls[-1] == ("color_temperature", 0) and ("light_sensor", "on") not in tv.calls


@pytest.mark.parametrize("error, why", [
    (Busy(), "another TV update is running."),
    (DisplayError("out-of-range", low=0, high=10), "it takes a whole number from 0 to 10."),
    (RuntimeError("socket closed"), "it did not answer."),
])
def test_a_look_that_stops_part_way_says_what_changed(client, error, why):
    app, browser = client
    tv = FakeTV(app.extensions["controller"])
    read_range(app, 0, 10)
    mats = app.extensions["configs"].load().mats

    def brightness(value):
        raise error
    app.extensions["controller"].set_brightness = brightness
    response, body = apply(browser, "bright")
    assert tv.calls == [("light_sensor", "off")] and app.extensions["configs"].load().mats == mats
    assert (f"Partly applied Bright room. Read back from the TV: light sensor off. Then the TV stopped: {why} "
            "The mats did not change.") in body
