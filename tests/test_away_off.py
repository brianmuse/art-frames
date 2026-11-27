"""The TV off step for an empty room (Sensor.off_after), coming back after it, and the wording of a sensor blank.

The off step sends no power key: it sets the TV's own Sleep After to its shortest value, and the TV turns
itself off. Every TV here is a fake (DemoTelevision subclasses): no test contacts a TV or a sensor.
"""
# ruff: noqa: F811

import logging

import pytest
from pydantic import ValidationError
from test_presence_power import BASE, HOUR, PowerTV, at, blanked, control, tv  # noqa: F401  (tv is a fixture)
from test_sensors import rig  # noqa: F401  (a fixture)
from test_web import auth, csrf

from art_frames import room_presets, sensor_control, sensor_web
from art_frames.config import Sensor
from art_frames.controller import DELIVERY

OFF = {"awake": False}  # the TV status after it turned itself off: standby


class KeyTV(PowerTV):
    """A PowerTV that records any power-off key."""

    def screen_off(self):
        self.calls.append(("power-off-key", None))
        return super().screen_off()


@pytest.fixture
def room(tv, monkeypatch):
    """The tv rig with a KeyTV: blank after 5 min, off after the default 30 min, someone there at 08:00.

    Controller.screen_off fails the test: the off step must never send a power key."""
    tv["screen"].controller.tv_factory = lambda cfg: KeyTV(tv["display"], tv["calls"])
    monkeypatch.setattr(tv["screen"].controller, "screen_off", lambda: pytest.fail("a power-off key was sent"))
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    return tv


def sleeps(rig):
    """The Sleep After values Art Frames set, in order."""
    return [value for name, value in rig["calls"] if name == "motion_timer"]


def keys(rig):
    return [call for call in rig["calls"] if call[0] in {"power-off-key", "key"}]


def empty_until_blank(rig):
    """The room is empty from 60 s: dimmed at once, blank 5 min later."""
    assert at(rig, 60, False) == "away"
    assert at(rig, 60 + 5 * 60, False) == "blanked"


def sleep_set(rig):
    """Empty for 30 min: the shortest Sleep After."""
    empty_until_blank(rig)
    assert at(rig, 60 + 30 * 60, False) == "tv_off"
    assert rig["display"]["motion_timer"] == "5"


def turned_off(rig):
    """Sleep After set, and 5 min later the TV is in standby by itself."""
    sleep_set(rig)
    rig["display"]["power"] = "standby"
    assert at(rig, 60 + 36 * 60, False, **OFF) == "standby"
    assert control(rig)["tv_off"] is True and control(rig)["off_at"] == BASE + 60 + 36 * 60


def no_answer(rig):
    """A TV status with no answer at all: a TV that is fully off."""
    rig["screen"].library.set_state("tv_status", {"awake": False, "playing": False, "paired": True, "known": False,
                                                  "answered": False})


# The setting.


def test_off_after_defaults_to_30_minutes_and_never_is_none():
    assert Sensor().off_after == 30
    assert Sensor(off_after=None).off_after is None
    assert Sensor(off_after="never").off_after is None  # a TOML file cannot hold null
    assert Sensor(off_after=120).off_after == 120


@pytest.mark.parametrize("value", [0, -5, 241, "soon"])
def test_off_after_needs_a_time_from_1_minute_to_4_hours(value):
    assert Sensor(off_after=1).off_after == 1 and Sensor(off_after=240).off_after == 240
    with pytest.raises(ValidationError):
        Sensor(off_after=value)


def test_a_saved_never_survives_a_reload(room):
    room["enable"](off_after=None)
    assert room["screen"].configs.load().sensor.off_after is None


@pytest.mark.parametrize("blank, off", [(5, 30), (10, 30), (15, 30), (30, 60), (60, 120), (120, 240), (240, None)])
def test_older_settings_take_the_first_off_time_after_the_blank(blank, off):
    assert Sensor(blank_after=blank).off_after == off
    assert Sensor.model_validate({"blank_after": blank}).off_after == off
    assert Sensor(blank_after=blank, off_after=None).off_after is None  # a saved value wins


# The off step: Sleep After, never a power key.


def test_inside_the_hours_the_off_step_sets_the_shortest_sleep_after_once(room, caplog):
    empty_until_blank(room)
    assert at(room, 60 + 29 * 60, False) == "blanked" and sleeps(room) == []  # not before its time
    with caplog.at_level(logging.INFO):
        assert at(room, 60 + 30 * 60, False) == "tv_off"
    assert sleeps(room) == ["5"] and room["display"]["motion_timer"] == "5"
    state = control(room)
    assert state["night"] == "5" and state["empty_sleep"] is True and not state.get("tv_off")
    assert "TV main: room empty for 30 min; Sleep After 5 min so the TV turns itself off" in caplog.text
    saved = room["screen"].library.state(sensor_control.SAMSUNG)["saved"]
    assert saved == {"motion_timer": "15"}  # the owner's value, for every off path
    # The blank stays, and nothing is sent again every 5 s.
    for seconds in (5, 10, 60, 120):
        assert at(room, 60 + 30 * 60 + seconds, False) == "blanked"
    assert sleeps(room) == ["5"] and blanked(room) and keys(room) == []


def test_standby_after_sleep_after_is_art_frames_own_off(room, caplog):
    sleep_set(room)
    room["display"]["power"] = "standby"
    with caplog.at_level(logging.INFO):
        assert at(room, 60 + 36 * 60, False, **OFF) == "standby"
    assert control(room)["tv_off"] is True and control(room)["off_at"] == BASE + 60 + 36 * 60
    assert "the TV is in standby after Sleep After; TV off because the room is empty" in caplog.text
    assert at(room, 60 + 40 * 60, False, **OFF) == "standby"
    assert control(room)["off_at"] == BASE + 60 + 36 * 60  # first seen in standby
    assert blanked(room) and keys(room) == []


def test_never_keeps_the_inside_hours_behaviour(room):
    room["enable"](blank_after=5, off_after=None)
    at(room, 30, True)
    empty_until_blank(room)
    assert at(room, 60 + 3 * HOUR, False) == "blanked"
    assert sleeps(room) == [] and room["display"]["motion_timer"] == "15" and control(room).get("night") is None


def test_outside_the_hours_is_unchanged(room, caplog):
    room["enable"](blank_after=5, hours_start="07:00", hours_end="08:20")
    at(room, 30, True)
    empty_until_blank(room)
    with caplog.at_level(logging.INFO):
        assert at(room, 20 * 60, False) == "night"  # 08:20: the hours ended before the off time
    assert sleeps(room) == ["5"] and control(room)["empty_sleep"] is False
    assert "Sleep After 5 (after the set hours)" in caplog.text
    assert at(room, 60 + 30 * 60, False) == "blanked" and sleeps(room) == ["5"]  # the off step adds nothing
    room["display"]["power"] = "standby"
    assert at(room, 60 + 40 * 60, False, **OFF) == "standby"
    assert not control(room).get("tv_off")  # off for the night, as before
    assert at(room, 60 + 41 * 60, True, **OFF) == "standby"  # no wake outside the hours
    assert ("wake", None) not in room["calls"]


@pytest.mark.parametrize("status, gate", [({"playing": True}, "watching"), ({"known": False}, "unknown"),
                                          ({"awake": False}, "standby")])
def test_no_off_step_while_watching_unknown_or_in_standby(room, status, gate):
    empty_until_blank(room)
    assert at(room, 60 + 30 * 60, False, **status) == gate
    assert at(room, 60 + 2 * HOUR, False, **status) == gate
    assert sleeps(room) == [] and not control(room).get("tv_off")
    room["display"]["power"] = "on"  # on in Art Mode again
    if gate == "standby":  # it was off: the off step waits its full time again
        assert at(room, 60 + 2 * HOUR + 5, False) == "blanked"
        assert at(room, 60 + 2 * HOUR + 30 * 60 + 10, False) == "tv_off"
    else:  # a TV that answered was not off
        assert at(room, 60 + 2 * HOUR + 5, False) == "tv_off"


def test_only_no_answer_at_all_counts_as_off(room):
    empty_until_blank(room)
    assert at(room, 60 + 10 * 60, False, known=False) == "unknown"
    assert room["memory"].was_off is False  # the TV answered, but its state was unclear
    room["clock"].now = BASE + 60 + 11 * 60
    no_answer(room)
    sensor_control.step(room["screen"], room["screen"].configs.load(), room["link"], room["memory"],
                        room["clock"].now)
    assert room["memory"].was_off is True
    # On in Art Mode again: the off step counts its full time from then.
    assert at(room, 60 + HOUR, False) == "blanked" and sleeps(room) == []
    assert at(room, 60 + HOUR + 29 * 60, False) == "blanked" and sleeps(room) == []
    assert at(room, 60 + HOUR + 30 * 60, False) == "tv_off"


def test_an_old_status_reading_never_starts_the_off_step(room):
    empty_until_blank(room)
    old = BASE + 60 + 30 * 60 - 11 * 60  # older than STATUS_MAX_AGE
    assert at(room, 60 + 30 * 60, False, at=old) == "unknown" and sleeps(room) == []
    assert at(room, 60 + 30 * 60 + 5, False) == "tv_off"  # a fresh reading; the TV answered, so not "off"


def test_no_off_step_near_a_scheduled_wake(room, monkeypatch):
    # From 10 minutes before a wake to the end of its grace window, the gate says "wake": the off step waits.
    empty_until_blank(room)
    window = {"open": True}
    real = room["screen"].controller._wake_window
    monkeypatch.setattr(room["screen"].controller, "_wake_window",
                        lambda cfg, now: (window["open"], now, False) if window["open"] else real(cfg, now))
    assert at(room, 60 + 30 * 60, False) == "wake" and sleeps(room) == []
    window["open"] = False  # the grace window ended: the scheduled wake never runs again that day
    assert at(room, 60 + 46 * 60, False) == "tv_off"


def test_a_scheduled_wake_turns_sleep_after_off_again(room, monkeypatch):
    # The off step set Sleep After; then a scheduled wake comes near. Sleep After goes off, so the TV does not
    # turn itself off while the wake turns it on.
    sleep_set(room)
    window = {"open": True}
    real = room["screen"].controller._wake_window
    monkeypatch.setattr(room["screen"].controller, "_wake_window",
                        lambda cfg, now: (window["open"], now, False) if window["open"] else real(cfg, now))
    assert at(room, 60 + 32 * 60, False) == "day"
    assert sleeps(room) == ["5", "off"] and control(room)["empty_sleep"] is False
    assert at(room, 60 + 33 * 60, False) == "wake" and sleeps(room) == ["5", "off"]  # once


# Coming back.


def test_someone_back_turns_sleep_after_off_and_turns_the_tv_on(room):
    turned_off(room)
    assert at(room, 60 + 40 * 60, True, **OFF) == "woke"
    assert room["display"]["power"] == "on" and ("wake", None) in room["calls"]
    assert at(room, 60 + 40 * 60 + 5, True) == "day"
    assert room["display"]["motion_timer"] == "off" and sleeps(room) == ["5", "off"]
    assert at(room, 60 + 40 * 60 + 10, True) == "back"
    assert not blanked(room)  # the saved image is back, not the black one
    state = control(room)
    assert state["tv_off"] is False and state["blanked"] is False and state["away"] is False
    assert state["night"] is None and state["empty_sleep"] is False
    assert room["display"]["brightness"] == state["sent"] and state["sent"] > 0  # the curve's level
    assert keys(room) == []


def test_someone_back_before_the_tv_sleeps_turns_sleep_after_off(room):
    sleep_set(room)
    assert at(room, 60 + 32 * 60, True) == "day"
    assert room["display"]["motion_timer"] == "off"
    assert at(room, 60 + 32 * 60 + 5, True) == "back" and not blanked(room)
    assert ("wake", None) not in room["calls"]


def test_the_first_wake_after_the_off_skips_the_wake_gap(room):
    # A screen-on try 2 minutes before the TV turned itself off must not block the person who comes back.
    sleep_set(room)
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, wake_at=BASE + 60 + 34 * 60,
                         wake_failures=1)
    room["display"]["power"] = "standby"
    assert at(room, 60 + 36 * 60, False, **OFF) == "standby"
    assert at(room, 60 + 36 * 60 + 30, True, **OFF) == "woke"


def test_later_wakes_after_the_off_still_wait(room):
    turned_off(room)
    room["display"]["wake_result"] = "not-confirmed"  # the screen does not come on
    assert at(room, 60 + 40 * 60, True, **OFF) == "wake-failed"
    assert at(room, 60 + 41 * 60, False, **OFF) == "standby"
    assert at(room, 60 + 42 * 60, True, **OFF) == "wake-wait"  # the gap holds for the second try


def test_turn_on_now_while_the_room_is_empty_starts_the_timer_again(room):
    turned_off(room)
    room["display"]["power"] = "on"  # Turn on now (or the remote) with no one there
    assert at(room, 60 + 50 * 60, False) == "day"  # Sleep After off: the off step waits again
    assert control(room)["tv_off"] is False and room["display"]["motion_timer"] == "off"
    assert at(room, 60 + 50 * 60 + 5, False) == "restart" and not blanked(room)
    assert at(room, 60 + 50 * 60 + 10, False) == "away"  # the steps count again from now
    assert at(room, 60 + 79 * 60, False) == "blanked" and sleeps(room) == ["5", "off"]
    assert at(room, 60 + 80 * 60 + 5, False) == "tv_off" and sleeps(room) == ["5", "off", "5"]


def test_a_tv_that_just_came_on_gets_the_full_time_before_it_turns_off(room):
    # The TV went to standby by itself, then someone turned it on from the app: the room has been empty for an
    # hour, but the off step counts from when the TV came on.
    empty_until_blank(room)
    room["display"]["power"] = "standby"
    assert at(room, 60 + 20 * 60, False, **OFF) == "standby"
    room["display"]["power"] = "on"
    assert at(room, 60 + HOUR, False) == "blanked" and sleeps(room) == []
    assert at(room, 60 + HOUR + 29 * 60, False) == "blanked" and sleeps(room) == []
    assert at(room, 60 + HOUR + 30 * 60, False) == "tv_off"


def test_new_off_setting_restarts_the_steps(room):
    empty_until_blank(room)
    room["enable"](blank_after=5, off_after=60)
    assert at(room, 60 + 6 * 60, False) == "restart"


def test_someone_in_just_after_the_tv_slept_still_gets_the_wake(room):
    # The TV went to standby by itself, but the stored status still says on in Art Mode (up to a minute old).
    # Someone comes in: that pass must not drop the wake, or the next pass that reads standby never wakes it.
    sleep_set(room)
    room["display"]["power"] = "standby"
    at(room, 60 + 36 * 60, True)  # the old status: awake
    assert room["memory"].wake_until is not None
    assert at(room, 60 + 36 * 60 + 5, True, **OFF) == "woke"
    assert room["display"]["power"] == "on" and keys(room) == []
    # Back in Art Mode with someone there: Sleep After goes off at once, so the TV does not sleep on them.
    at(room, 60 + 36 * 60 + 15, True)
    assert sleeps(room)[-1] == "off"


def test_without_the_off_step_a_tv_on_in_art_mode_drops_the_wake(room):
    room["enable"](blank_after=5, off_after=None)
    at(room, 30, True)
    empty_until_blank(room)
    at(room, 60 + 36 * 60, True)
    assert room["memory"].wake_until is None  # as before: on in Art Mode, nothing to wake


def test_a_tv_that_wakes_into_tv_mode_is_not_off_any_more(room):
    turned_off(room)
    assert at(room, 60 + 40 * 60, False, playing=True) == "watching"
    state = control(room)
    assert state["tv_off"] is False and state["off_at"] is None
    assert not sensor_web.control_text(state, room["clock"].now).startswith("TV turned off")
    room["display"]["power"] = "on"
    assert at(room, 60 + 50 * 60, False) == "day"  # back in Art Mode: Sleep After off, then the steps undo
    assert at(room, 60 + 50 * 60 + 5, False) == "restart" and not blanked(room)


def test_a_sensor_gap_keeps_the_off_steps_sleep_after(room):
    sleep_set(room)
    for seconds in (5, 60, 120):
        at(room, 60 + 30 * 60 + seconds, None)
    assert sleeps(room) == ["5"] and control(room)["night"] == "5"
    assert at(room, 60 + 33 * 60, False) == "blanked" and sleeps(room) == ["5"]


def test_a_restart_of_art_frames_keeps_the_off_steps_sleep_after(room):
    sleep_set(room)
    room["memory"].__dict__.update(sensor_control.Memory().__dict__)  # a new process: nothing in memory
    for seconds in (5, 60, 120):
        at(room, 60 + 30 * 60 + seconds, False)
    assert sleeps(room) == ["5"] and control(room)["night"] == "5"
    assert at(room, 60 + 33 * 60, True) == "day" and sleeps(room) == ["5", "off"]  # someone there: off


def test_never_after_the_off_step_turns_sleep_after_off(room):
    sleep_set(room)
    room["enable"](blank_after=5, off_after=None)
    at(room, 60 + 31 * 60, False)
    assert sleeps(room) == ["5", "off"] and control(room)["night"] is None


# The Now page.


def now_page(rig):
    rig["app"].extensions["sensors"].memory["main"] = rig["memory"]
    cfg = rig["screen"].configs.load()
    status = {"paired": True, "known": True, "awake": False, "at": rig["clock"].now}
    with rig["app"].app_context():
        return sensor_web.now_status(rig["screen"], cfg, status, False, now=rig["clock"].now)


def test_the_now_page_says_when_the_tv_did_not_turn_on_for_someone(room):
    turned_off(room)
    room["display"]["wake_result"] = "not-confirmed"
    assert at(room, 60 + 40 * 60, True, **OFF) == "wake-failed"
    result = now_page(room)
    assert result["sentence"] == "Someone is here, but the TV did not turn on. See Health."
    assert result["overlay"]["tone"] == "dark"


def test_the_now_page_ignores_a_failed_wake_from_before_the_off(room):
    turned_off(room)
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, wake_at=BASE + 60 + 20 * 60,
                         wake_failures=1, wake_result="not-confirmed")
    room["memory"].present_since = BASE + 60 + 10 * 60
    assert now_page(room)["sentence"].startswith("TV off: room empty")


def test_the_now_page_says_the_tv_is_off_for_the_empty_room(room):
    turned_off(room)
    assert now_page(room)["sentence"].startswith("TV off: room empty")


# The Sensor page.


def away_form(rig, browser, **fields):
    return {"csrf": csrf(browser), "sensor_version": sensor_web.version(rig["screen"].configs.load().sensor),
            "away_after": "0", "blank_after": "15", "off_after": "30", "hours_start": "07:00",
            "hours_end": "23:00", **fields}


def test_the_sensor_page_offers_turn_the_tv_off(room):
    browser = room["app"].test_client()
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Turn the TV off after the room is empty" in body and '<select name="off_after">' in body
    assert ("The TV turns itself off about 5 min later, with its own Sleep After timer. The sensor turns it back "
            "on when someone comes in, during the set hours.") in body
    assert "After 15 min" not in body
    for value, text in (("never", "Never"), ("15", "15 min"), ("30", "30 min"), ("60", "1 hour"),
                        ("120", "2 hours"), ("240", "4 hours")):
        assert f'<option value="{value}"' in body and f">{text}</option>" in body
    assert '<option value="30" selected>' in body
    assert "turns off about 5 min after 30 min" in body  # What it does


@pytest.mark.parametrize("value, saved", [("never", None), ("60", 60), ("120", 120)])
def test_the_sensor_page_saves_turn_the_tv_off(room, value, saved):
    browser = room["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(), data=away_form(room, browser, off_after=value))
    assert response.status_code == 302
    assert room["screen"].configs.load().sensor.off_after == saved
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert f'<option value="{value}" selected>' in body


@pytest.mark.parametrize("value", ["15", "10"])
def test_turn_the_tv_off_later_than_the_blank(room, value):
    browser = room["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(),
                            data=away_form(room, browser, blank_after="15", off_after=value))
    assert response.status_code == 400 and "Turn the TV off later than you blank." in response.get_data(as_text=True)


def test_a_saved_time_outside_the_list_stays_selected(room):
    room["enable"](off_after=45)
    body = room["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert '<option value="45" selected>45 min</option>' in body


def test_what_it_does_names_the_off_step():
    assert "When the room is empty, it dims at once, blanks after 15 min and turns off about 5 min after 30 min." in (
        sensor_web.what_it_does(Sensor()))
    assert "When the room is empty, it dims at once and blanks after 15 min." in (
        sensor_web.what_it_does(Sensor(off_after=None)))


def test_the_pages_say_the_tv_is_off_for_the_empty_room():
    cfg = Sensor(enabled=True, host="192.168.1.148")
    state = {"tv_off": True, "off_at": BASE}
    assert sensor_web.control_text(state, BASE + 120) == "TV turned off because the room is empty, 2 minutes ago."
    word = sensor_web.control_word(state, _Cfg(cfg), {"paired": True, "known": True, "awake": False}, BASE)
    assert word == "TV off: room empty"


class _Cfg:
    def __init__(self, sensor):
        self.sensor, self.timezone = sensor, "UTC"


def test_room_presets_set_the_off_time():
    assert {room.key: room.off_after for room in room_presets.ROOMS} == {"living": 60, "bedroom": 30, "theater": 30}
    for room in room_presets.ROOMS:
        assert room.off_after > room.blank_after
        assert room_presets.settings(room, 0, 10)["off_after"] == room.off_after
    living = room_presets.BY_KEY["living"]
    line = room_presets.changes(Sensor(off_after=None), living, 0, 10, sensor_web.diff_words)
    assert "TV off after Never → 1 hour" in line


# The wording of a blank.


def test_the_delivery_says_the_room_is_empty_for_the_sensor_blank():
    text = DELIVERY["room-empty"]
    assert "room is empty" in text and "Undo blank" not in text and "test" not in text
    assert "test" in DELIVERY["blanked"]  # the owner's test blank keeps its hint


@pytest.mark.parametrize("owner, result, text", [
    ("sensor", "room-empty", "delivery: the screen is blank because the room is empty; it comes back when someone "
                             "comes in [room-empty]"),
    ("test", "blanked", "delivery: the screen is blanked for a test; Undo blank shows artwork again [blanked]"),
])
def test_a_delivery_pass_names_whose_blank_holds_the_screen(configs, library, monkeypatch, caplog, owner, result,
                                                             text):
    from test_controller import at as clock_at
    from test_controller import setup

    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    cfg = configs.load()
    library.set_state("soft_blank:" + cfg.tv.identity,
                      {"content_id": None, "brightness": 4, "at": "2026-10-26T06:00:00+00:00", "owner": owner})
    monkeypatch.setattr(controller, "tick", lambda: controller._exclusive(
        lambda: controller._tick(clock_at("12:00"), force=False)))
    with caplog.at_level(logging.INFO):
        assert controller.run_once() == result
    assert text in caplog.text
    if owner == "sensor":
        assert "for a test" not in caplog.text and "Undo blank" not in caplog.text
    assert screen.publications == []  # either blank holds the screen


# Older settings.


@pytest.mark.parametrize("blank, off", [(30, 60), (60, 120), (120, 240)])
def test_an_older_settings_file_loads_with_a_later_off_time(room, blank, off):
    import json

    path = room["screen"].configs.overrides
    data = json.loads(path.read_text())
    data["sensor"]["blank_after"] = blank
    del data["sensor"]["off_after"]
    path.write_text(json.dumps(data))
    assert room["screen"].configs.load().sensor.off_after == off


def test_an_hours_only_save_keeps_an_older_install_working(room):
    import json

    path = room["screen"].configs.overrides
    data = json.loads(path.read_text())
    data["sensor"]["blank_after"] = 30
    del data["sensor"]["off_after"]
    path.write_text(json.dumps(data))
    browser = room["app"].test_client()
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert '<option value="60" selected>' in body
    form = away_form(room, browser, blank_after="30", off_after="60", hours_start="08:00")
    assert browser.post("/tv/main/sensor/away", headers=auth(), data=form).status_code == 302
    sensor = room["screen"].configs.load().sensor
    assert (sensor.hours_start, sensor.blank_after, sensor.off_after) == ("08:00", 30, 60)


@pytest.mark.parametrize("key", ["living", "bedroom", "theater"])
def test_every_preset_still_matches_an_older_install(key):
    preset = room_presets.BY_KEY[key]
    fields = {name: value for name, value in room_presets.settings(preset, 0, 10).items() if name != "off_after"}
    points = fields.pop("points")
    old = Sensor.model_validate({**fields, "curve": {"points": points}})
    assert old.off_after == preset.off_after
    assert room_presets.current(old, 0, 10) is preset


def test_every_off_time_is_offered():
    # The Blank select in the same form can change: the save refuses a pair that does not fit.
    every = [("never", "Never"), ("15", "15 min"), ("30", "30 min"), ("60", "1 hour"), ("120", "2 hours"),
             ("240", "4 hours")]
    assert sensor_web.off_choices(60) == every and sensor_web.off_choices(None) == every
    assert ("45", "45 min") in sensor_web.off_choices(45)  # a saved time stays


def test_the_sensor_page_offers_an_off_time_for_a_shorter_blank(room):
    # A blank of 30 min: 15 and 30 are still offered, so picking a 10-minute blank can pair with them.
    room["enable"](blank_after=30, off_after=60)
    browser = room["app"].test_client()
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    body = body.split('<select name="off_after">')[1].split("</select>")[0]
    assert '<option value="15" >15 min</option>' in body and '<option value="30" >30 min</option>' in body
    assert '<option value="60" selected>1 hour</option>' in body
    form = away_form(room, browser, blank_after="10", off_after="15")
    assert browser.post("/tv/main/sensor/away", headers=auth(), data=form).status_code == 302
    sensor = room["screen"].configs.load().sensor
    assert (sensor.blank_after, sensor.off_after) == (10, 15)


def test_a_form_without_the_off_field_derives_it_from_the_new_blank(room):
    import json

    path = room["screen"].configs.overrides
    data = json.loads(path.read_text())
    data["sensor"]["blank_after"] = 15
    del data["sensor"]["off_after"]
    path.write_text(json.dumps(data))
    browser = room["app"].test_client()
    form = away_form(room, browser, blank_after="30")
    del form["off_after"]  # an older page
    assert browser.post("/tv/main/sensor/away", headers=auth(), data=form).status_code == 302
    assert room["screen"].configs.load().sensor.off_after == 60  # from the 30-minute blank, not 30


def test_a_form_without_the_off_field_keeps_a_saved_off_time(room):
    room["enable"](off_after=120)
    browser = room["app"].test_client()
    form = away_form(room, browser, blank_after="30")
    del form["off_after"]
    assert browser.post("/tv/main/sensor/away", headers=auth(), data=form).status_code == 302
    assert room["screen"].configs.load().sensor.off_after == 120


def test_an_update_says_the_screen_is_blank_for_the_empty_room(tmp_path, monkeypatch):
    from test_web import PASSWORD

    from art_frames.web import create_app

    app = create_app(tmp_path, password=PASSWORD, testing=True)
    monkeypatch.setattr(app.extensions["controller"], "tick", lambda **kwargs: "room-empty")
    client = app.test_client()
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "The room is empty, so the screen is blank. Art Frames did not send the image." in response.text
    assert "screen is dark" not in response.text and "Undo blank" not in response.text
