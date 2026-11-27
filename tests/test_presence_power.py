"""Presence on/off for a TV with a sensor: set hours, blank after N minutes, the night's Sleep After, turning
the screen on for someone who comes in, and every off path restoring what Art Frames changed.

Every TV here is a fake (DemoTelevision or test_screen_power.FakeTV): no test contacts a TV or a sensor.
"""
# ruff: noqa: F811

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_screen_power import make
from test_sensors import (  # noqa: F401  (rig and client are fixtures)
    CountingTV,
    client,
    control_form,
    light,
    readings_for,
    rig,
    switch,
)
from test_web import auth, csrf

from art_frames import sensor_control, sensor_curve, sensor_web, television, timewords
from art_frames.config import Sensor
from art_frames.television import DisplayError, ScreenOn, parse_display_settings, shortest_sleep_after

BASE = 1_800_000_000.0  # 08:00 UTC, inside the default hours (07:00-23:00); the rig's TV uses UTC
HOUR = 3600


class PowerTV(CountingTV):
    """A demo TV that also records the blank, the undo and the screen-on for someone in the room.

    display["wake_result"] makes presence_wake report that result without turning the screen on.
    """

    def soft_blank(self, *args):
        self.calls.append(("blank", None))
        return super().soft_blank(*args)

    def undo_blank(self, *args):
        self.calls.append(("undo-blank", None))
        return super().undo_blank(*args)

    def presence_wake(self, **kwargs):
        self.calls.append(("wake", None))
        if self.display.get("wake_result"):
            return ScreenOn("wake signal", 40, self.display["wake_result"])
        return super().presence_wake()


@pytest.fixture
def tv(rig):
    """The rig with a PowerTV, control on in the default hours, and someone in the room at 08:00."""
    screen = rig["screen"]
    screen.controller.tv_factory = lambda cfg: PowerTV(rig["display"], rig["calls"])
    light(rig, 100.0, presence=True)
    return rig


def at(rig, seconds, presence, lux=100.0, **status):
    """Move the clock to BASE + seconds, give a reading and a fresh TV status, and run one pass."""
    rig["clock"].now = BASE + seconds
    rig["status"](**status)
    light(rig, lux, presence=presence)
    return rig["run"]()


def control(rig):
    return rig["screen"].library.state(sensor_control.CONTROL) or {}


def blanked(rig):
    return rig["screen"].controller.blank_record() is not None


def samsung(rig):
    screen = rig["screen"]
    return sensor_control.samsung_step(screen, screen.configs.load(), rig["memory"], rig["clock"].now)


# The set hours.


@pytest.mark.parametrize("start, end, clock, inside", [
    ("07:00", "23:00", "06:59", False),
    ("07:00", "23:00", "07:00", True),
    ("07:00", "23:00", "22:59", True),
    ("07:00", "23:00", "23:00", False),
    # Hours that cross midnight.
    ("22:00", "06:00", "21:59", False),
    ("22:00", "06:00", "22:00", True),
    ("22:00", "06:00", "23:30", True),
    ("22:00", "06:00", "00:00", True),
    ("22:00", "06:00", "05:59", True),
    ("22:00", "06:00", "06:00", False),
    ("22:00", "06:00", "12:00", False),
    # The same start and end: all day.
    ("08:00", "08:00", "03:00", True),
])
def test_set_hours(start, end, clock, inside):
    sensor = Sensor(hours_start=start, hours_end=end)
    hour, minute = map(int, clock.split(":"))
    assert sensor_control.in_hours(sensor, datetime(2027, 1, 15, hour, minute, tzinfo=UTC)) is inside


def test_hours_need_a_clock_time():
    with pytest.raises(ValueError, match="HH:MM"):
        Sensor(hours_start="7am")


# Blank after N minutes, and undo when someone comes back.


def test_dims_at_once_then_blanks_after_n_minutes(tv):
    tv["enable"](blank_after=15)
    assert at(tv, 0, True) == "wait"  # someone there: the curve runs
    assert at(tv, 60, False) == "away"  # dim at once
    assert tv["display"]["brightness"] == 0 and not blanked(tv)
    assert at(tv, 60 + 14 * 60, False) == "away"  # 14 minutes: not yet
    assert ("blank", None) not in tv["calls"]
    assert at(tv, 60 + 15 * 60, False) == "blanked"
    assert blanked(tv) and control(tv)["blanked"] is True
    assert at(tv, 60 + 20 * 60, False) == "blanked"  # it holds: nothing is sent again
    assert tv["calls"].count(("blank", None)) == 1


def test_the_dim_waits_for_its_own_delay_and_the_blank_waits_for_the_dim(tv):
    tv["enable"](away_after=10, blank_after=5)
    at(tv, 0, True)
    assert at(tv, 60, False) not in {"away", "blanked"}
    assert at(tv, 60 + 6 * 60, False) not in {"away", "blanked"}  # the blank waits for the dim
    assert at(tv, 60 + 10 * 60, False) == "away"
    assert at(tv, 60 + 10 * 60 + 5, False) == "blanked"


def test_someone_back_undoes_the_blank_then_the_dim_at_once(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    del tv["calls"][:]
    assert at(tv, 60 + 5 * 60 + 5, True) == "back"
    assert tv["calls"][0] == ("undo-blank", None)  # reverse order: the blank first, then the dim
    # The blank's undo comes back at the curve's level: 100 lux on the default curve (1 to 1000 lux) over 0-10.
    # The dim's undo then finds the TV at that level and sends nothing more.
    assert tv["display"]["brightness"] == 7 and ("brightness", 7) not in tv["calls"]
    assert not blanked(tv)
    state = control(tv)
    assert state["away"] is False and state["blanked"] is False and state["sent"] == 7
    assert at(tv, 60 + 5 * 60 + 20, True) not in {"paused"}  # its own levels start no pause


def test_the_owner_blank_test_still_holds_sensor_control(tv):
    tv["screen"].controller.soft_blank()  # Picture, Tests: Blank the screen
    del tv["calls"][:]
    assert at(tv, 60, False) == "blanked"
    assert at(tv, 60 + 30 * 60, True) == "blanked"
    assert tv["calls"] == []  # Art Frames never undoes the owner's blank


def test_new_away_settings_undo_the_steps_and_count_again(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    browser = tv["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(), data={
        "csrf": csrf(browser), "sensor_version": sensor_web.version(tv["screen"].configs.load().sensor),
        "away_after": "0", "blank_after": "30", "hours_start": "07:00", "hours_end": "22:00"})
    assert response.status_code == 302 and response.location.endswith("#away")
    sensor = tv["screen"].configs.load().sensor
    assert (sensor.blank_after, sensor.hours_end) == (30, "22:00")
    assert at(tv, 60 + 6 * 60, False) == "restart"
    assert not blanked(tv)
    assert at(tv, 60 + 6 * 60 + 5, False) == "away"  # counts again from now: dim at once
    assert at(tv, 60 + 20 * 60, False) == "away"  # the new 30 minutes are not over


# Sleep After for the night.


def test_the_end_of_the_hours_sets_the_shortest_sleep_after_and_keeps_the_blank(tv):
    tv["enable"](blank_after=5, off_after=None)  # the TV off step: see test_away_off.py
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    assert at(tv, 15 * HOUR, False) == "night"  # 23:00
    assert tv["display"]["motion_timer"] == "5"  # the shortest value the TV lists
    assert control(tv)["night"] == "5"
    saved = tv["screen"].library.state(sensor_control.SAMSUNG)["saved"]
    assert saved == {"motion_timer": "15"}  # the owner's value, for every off path
    assert blanked(tv)  # a dark screen until the TV turns itself off
    assert at(tv, 15 * HOUR + 5, False) == "blanked"  # nothing more to do


def test_sleep_after_is_off_while_someone_is_there_after_the_hours(tv):
    at(tv, 15 * HOUR - 120, False)  # empty for 2 min when the hours end
    assert at(tv, 15 * HOUR, False) == "night"
    assert at(tv, 15 * HOUR + 60, True) == "day"  # someone turned the TV on: it must not drop them
    assert tv["display"]["motion_timer"] == "off"
    assert at(tv, 15 * HOUR + 120, False) != "night"  # they left: Sleep After waits NIGHT_SETTLE
    assert at(tv, 15 * HOUR + 240, False) == "night"
    assert tv["display"]["motion_timer"] == "5"


def test_inside_the_hours_sleep_after_goes_off_again(tv):
    at(tv, 15 * HOUR - 120, False)
    assert at(tv, 15 * HOUR, False) == "night"
    assert at(tv, 23 * HOUR, False) == "day"  # 07:00 the next day, the TV still on
    assert tv["display"]["motion_timer"] == "off" and control(tv)["night"] is None


def test_unknown_presence_after_the_hours_still_lets_the_tv_turn_itself_off(tv):
    assert at(tv, 15 * HOUR, None) != "night"  # it waits NIGHT_SETTLE, as for an empty room
    assert at(tv, 15 * HOUR + 120, None) == "night"


def test_the_shortest_sleep_after_comes_from_the_tv():
    payload = {"data": '[{"item": "motion_timer", "value": "off", "valid_values": ["off", "30", "15", "240"]}]'}
    settings = parse_display_settings(payload)
    assert settings["motion_timer_choices"] == ("off", "15", "30", "240")
    assert shortest_sleep_after(settings) == "15"
    # A TV that lists none: Samsung's documented shortest value; the readback confirms it.
    assert shortest_sleep_after(parse_display_settings({"data": '[{"item": "motion_timer", "value": "off"}]'})) == "5"


def test_a_tv_that_refuses_the_night_value_is_tried_again_later(tv):
    class Refusing(PowerTV):
        def set_option(self, field, value):
            self.calls.append((field, value))
            raise DisplayError("rejected")
    tv["screen"].controller.tv_factory = lambda cfg: Refusing(tv["display"], tv["calls"])
    at(tv, 15 * HOUR - 120, False)  # empty for NIGHT_SETTLE when the hours end
    assert at(tv, 15 * HOUR, False) == "rejected"
    assert control(tv).get("night") is None
    assert at(tv, 15 * HOUR + 60, False) != "rejected"  # not every tick
    assert at(tv, 15 * HOUR + 400, False) == "rejected"  # after SAMSUNG_RETRY


# Turning the screen on for someone who comes in.


def standby(rig):
    rig["display"]["power"] = "standby"
    return {"awake": False}


def test_someone_coming_in_turns_the_screen_on_from_standby(tv):
    off = standby(tv)
    assert at(tv, 0, False, **off) == "standby"
    assert tv["calls"] == []  # routine passes never contact a TV in standby
    assert at(tv, 60, True, **off) == "woke"
    assert ("wake", None) in tv["calls"] and tv["display"]["power"] == "on"
    assert control(tv)["wake_failures"] == 0 and control(tv)["wake_result"] == "confirmed"


def test_the_start_of_the_hours_turns_the_screen_on_for_someone_there(tv):
    off = standby(tv)
    assert at(tv, -2 * HOUR, True, **off) == "standby"  # 06:00: someone there, outside the hours
    assert ("wake", None) not in tv["calls"]
    assert at(tv, -HOUR, True, **off) == "woke"  # 07:00


def test_someone_who_stays_does_not_wake_a_tv_they_turned_off(tv):
    assert at(tv, 0, True) != "woke"
    off = standby(tv)
    for minute in range(1, 30, 5):
        assert at(tv, minute * 60, True, **off) == "standby"
    assert ("wake", None) not in tv["calls"]


def test_a_restart_never_wakes_the_tv(tv):
    off = standby(tv)
    assert at(tv, 0, True, **off) == "standby"  # no presence seen before: no arrival
    assert ("wake", None) not in tv["calls"]


def test_no_wake_after_the_hours(tv):
    off = standby(tv)
    at(tv, 15 * HOUR, False, **off)
    assert at(tv, 15 * HOUR + 60, True, **off) == "standby"
    assert ("wake", None) not in tv["calls"]


def test_failed_wakes_back_off_and_show_on_health(tv):
    off = standby(tv)
    tv["display"]["wake_result"] = "not-confirmed"
    at(tv, 0, False, **off)
    assert at(tv, 60, True, **off) == "wake-failed"
    assert at(tv, 70, True, **off) == "wake-wait"
    assert at(tv, 60 + 15 * 60, True, **off) == "standby"  # the same visit: no new try
    at(tv, 60 + 16 * 60, False, **off)
    assert at(tv, 60 + 17 * 60, True, **off) == "wake-wait"  # after one failure: 20 minutes
    assert tv["calls"].count(("wake", None)) == 1
    at(tv, 60 + 20 * 60, False, **off)
    assert at(tv, 60 + 21 * 60, True, **off) == "wake-failed"
    at(tv, 60 + 55 * 60, False, **off)
    assert at(tv, 60 + 56 * 60, True, **off) == "wake-wait"  # after two: 40 minutes
    assert tv["calls"].count(("wake", None)) == 2
    cfg = tv["screen"].configs.load()
    row = sensor_control.wake_check(tv["screen"].library, cfg, tv["clock"].now)
    assert row["tone"] == "problem" and "did not wake for someone" in row["title"]
    assert row["detail"].endswith(" If someone comes in after 09:02, Art Frames tries again.")
    body = tv["app"].test_client().get("/tv/main/health", headers=auth()).get_data(as_text=True)
    assert "Frame 1 did not turn on for someone" in body
    del tv["display"]["wake_result"]
    at(tv, 60 + 61 * 60, False, **off)
    assert at(tv, 60 + 62 * 60, True, **off) == "woke"
    assert sensor_control.wake_check(tv["screen"].library, cfg, tv["clock"].now) is None


def test_one_wake_every_ten_minutes_at_most(tv):
    off = standby(tv)
    at(tv, 0, False, **off)
    assert at(tv, 60, True, **off) == "woke"
    tv["display"]["power"] = "standby"  # turned off with the remote
    at(tv, 120, False, **off)
    assert at(tv, 180, True, **off) == "wake-wait"  # came back in two minutes later
    assert tv["calls"].count(("wake", None)) == 1
    at(tv, 60 + 9 * 60, False, **off)
    assert at(tv, 60 + 10 * 60, True, **off) == "woke"


def test_a_tv_in_tv_mode_after_the_wake_is_left_alone(tv):
    off = standby(tv)
    tv["display"]["wake_result"] = "tv-mode"
    at(tv, 0, False, **off)
    assert at(tv, 60, True, **off) == "watching"
    assert control(tv)["wake_failures"] == 0
    assert at(tv, 60 + 30 * 60, True, **off) == "standby"  # no new try for the same visit
    cfg = tv["screen"].configs.load()
    row = sensor_control.wake_check(tv["screen"].library, cfg, tv["clock"].now)
    assert row["tone"] == "info" and row["title"] == "Frame 1 came on in TV mode, not Art Mode"
    assert row["detail"] == ("Art Frames leaves the TV alone in TV mode. To show art, switch the TV to Art Mode. "
                             "This note clears then.")
    body = tv["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Frame 1 came on in TV mode, not Art Mode." in body
    tv["display"]["power"] = "on"
    at(tv, 60 + 31 * 60, True)  # in Art Mode again
    assert sensor_control.wake_check(tv["screen"].library, cfg, tv["clock"].now) is None


@pytest.mark.parametrize("status", [{"awake": True, "playing": True}, {"known": False}])
def test_watching_or_unknown_does_nothing(tv, status):
    tv["enable"](blank_after=5)
    for seconds, presence in ((0, False), (60, True), (120, False), (HOUR, False), (15 * HOUR, False),
                              (15 * HOUR + 60, True)):
        assert at(tv, seconds, presence, **status) in {"watching", "unknown"}
    assert tv["calls"] == []  # no read, no key, no blank, no Sleep After


def test_a_tv_that_answers_but_reports_no_state_is_never_woken(tv):
    tv["screen"].library.set_state("tv_status", {})
    at(tv, 0, False, known=False)
    assert at(tv, 60, True, known=False) == "unknown"  # it answered: unknown, not off the network
    assert tv["calls"] == []


def test_a_tv_off_the_network_gets_the_wake_signal(tv):
    standby(tv)
    silent = {"known": False}

    def quiet(**status):
        tv["screen"].library.set_state("tv_status", {"awake": False, "playing": False, "paired": True,
                                                     "known": False, "answered": False})
    at(tv, 0, False, **silent)
    quiet()
    tv["clock"].now = BASE + 60
    light(tv, 100.0, presence=True)
    assert tv["run"]() == "woke"


def test_standby_never_selects_artwork_or_changes_brightness(tv):
    off = standby(tv)
    for minute in range(0, 120, 10):
        at(tv, minute * 60, False, **off)
    at(tv, 15 * HOUR, False, **off)
    assert tv["calls"] == []


# Television.presence_wake: the real TV path, with a fake network TV.


def bursts(device, monkeypatch):
    sent = []
    monkeypatch.setattr(device, "_send_magic", lambda: sent.append(1) or 1)
    return sent


def test_an_answering_tv_in_standby_gets_one_short_power_key(tmp_path, monkeypatch):
    device, fake, _, wakes = make(tmp_path, monkeypatch, [("standby", "on")] * 3 + [("on", "on")])
    sent = bursts(device, monkeypatch)
    result = device.presence_wake()
    assert result.result == "confirmed" and result.method == "power key"
    assert fake.keys == [("click", "KEY_POWER")] and not sent and not wakes


def test_a_tv_that_is_on_gets_no_key(tmp_path, monkeypatch):
    for state in (("on", "on"), ("on", "off"), ("", "on")):
        device, fake, _, _ = make(tmp_path, monkeypatch, [state])
        sent = bursts(device, monkeypatch)
        with pytest.raises(DisplayError, match="screen-is-on"):
            device.presence_wake()
        assert not fake.keys and not sent


def test_the_wake_signal_tries_three_times_over_about_40_seconds(tmp_path, monkeypatch):
    device, fake, clock, _ = make(tmp_path, monkeypatch, [None])
    sent = bursts(device, monkeypatch)
    result = device.presence_wake()
    assert result.result == "not-confirmed" and len(sent) == 3
    assert 36 <= result.seconds <= 45 and not fake.keys


def test_the_wake_signal_stops_once_art_mode_is_confirmed(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [None, None, ("on", "on")])
    sent = bursts(device, monkeypatch)
    assert device.presence_wake().result == "confirmed"
    assert len(sent) == 1 and not fake.keys


def test_the_wake_signal_stops_when_the_tv_comes_up_in_tv_mode(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [None, ("on", "off")])
    sent = bursts(device, monkeypatch)
    assert device.presence_wake().result == "tv-mode"
    assert len(sent) == 1 and not fake.keys  # someone watches: never a key


def test_a_tv_that_answers_in_standby_after_the_signal_gets_one_key(tmp_path, monkeypatch):
    # Standby replies every second from 2 s: the key waits until they span STANDBY_DWELL (4 s), at 6 s. Then
    # the probe, the recheck on the key's connection, and Art Mode.
    states = [None, *[("standby", "on")] * 7, ("on", "on")]
    device, fake, _, _ = make(tmp_path, monkeypatch, states)
    sent = bursts(device, monkeypatch)
    assert device.presence_wake().result == "confirmed"
    assert len(sent) == 1 and fake.keys == [("click", "KEY_POWER")]


def test_the_wake_signal_needs_the_mac(tmp_path, monkeypatch):
    device, _, _, _ = make(tmp_path, monkeypatch, [None])
    monkeypatch.setattr(device.cfg, "mac", "")
    with pytest.raises(DisplayError, match="missing-mac"):
        device.presence_wake()


def test_presence_wake_logs_no_mac(tmp_path, monkeypatch, caplog):
    device, _, _, _ = make(tmp_path, monkeypatch, [None])
    bursts(device, monkeypatch)
    device.presence_wake()
    assert "AA:BB" not in caplog.text and "aabb" not in caplog.text.lower()


def test_the_demo_tv_turns_on_and_reports_standby():
    display = {"power": "standby"}
    demo = television.DemoTelevision(display)
    assert demo.status()["awake"] is False
    assert demo.presence_wake().result == "confirmed" and demo.status()["awake"] is True
    with pytest.raises(DisplayError, match="screen-is-on"):
        demo.presence_wake()


# Every off path restores what Art Frames changed.


def night_with_blank(rig):
    """Dim, blank and the night's Sleep After, all held."""
    rig["enable"](blank_after=5)
    at(rig, 0, True)
    at(rig, 60, False)
    assert at(rig, 60 + 5 * 60, False) == "blanked"
    assert at(rig, 15 * HOUR, False) == "night"
    assert rig["display"]["brightness"] == 0 and blanked(rig)


def turn_off(rig, how):
    browser = rig["app"].test_client()
    if how == "switch":
        switch(rig["app"], browser, "off")
    elif how == "remove":
        browser.post("/tv/main/sensor/remove", headers=auth(), data={"csrf": csrf(browser)})
    elif how == "another sensor":
        browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": "192.168.1.231"})
    else:  # an edited settings file
        rig["screen"].configs.mutate(lambda raw: raw["sensor"].update(enabled=False))
        assert rig["run"]() == "off"


@pytest.mark.parametrize("how", ["switch", "remove", "another sensor", "settings file"])
def test_every_off_path_restores_the_blank_the_dim_and_sleep_after(tv, how):
    night_with_blank(tv)
    turn_off(tv, how)
    state = tv["screen"].library.state(sensor_control.SAMSUNG)
    assert state["want"] == "restore" and state["undo_blank"] is True and state["undo_away"] == 5
    del tv["calls"][:]
    tv["clock"].now += HOUR
    tv["status"]()
    assert samsung(tv) == "done"
    assert tv["calls"][0] == ("undo-blank", None)  # reverse order: the blank, then the dim
    assert not blanked(tv) and tv["display"]["brightness"] == 5
    assert tv["display"]["motion_timer"] == "15"  # the owner's value
    state = tv["screen"].library.state(sensor_control.SAMSUNG)
    assert state["saved"] is None and state["undo_blank"] is None


def test_an_off_path_restore_waits_for_art_mode(tv):
    night_with_blank(tv)
    turn_off(tv, "switch")
    tv["display"]["power"] = "standby"  # the night's Sleep After worked
    tv["clock"].now += HOUR
    tv["status"](awake=False)
    del tv["calls"][:]
    assert samsung(tv) == "waiting" and tv["calls"] == []


def test_a_restore_that_finds_no_blank_still_finishes(tv):
    night_with_blank(tv)
    turn_off(tv, "switch")
    tv["screen"].controller.undo_blank()  # the owner pressed Undo blank meanwhile
    tv["clock"].now += HOUR
    tv["status"]()
    assert samsung(tv) == "done" and tv["display"]["motion_timer"] == "15"


# Pages.


def test_the_sensor_page_says_what_happens(tv):
    browser = tv["app"].test_client()
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert ("It sets the brightness from the room light, blanks the screen when the room is empty, and turns the "
            "screen on when someone comes in.") in body
    assert "Art Frames leaves the TV alone while someone watches TV.</p>" in body
    for line in ("The TV turns on when someone comes in, from 07:00 to 23:00.",
                 "When the room is empty, it dims at once, blanks after 15 min and turns off about 5 min after 30 min.",
                 "After 23:00, it turns off 5 min after the room is empty."):
        assert f"<li>{line}</li>" in body
    assert "When the room is empty or dark</h2>" in body and "<legend>Hours</legend>" in body
    assert "Dim after the room is empty" in body and "Blank after the room is empty" in body
    assert "Use the same time twice to keep the TV ready all day." in body
    assert 'name="hours_start" type="time" value="07:00"' in body
    assert "away-steps" not in body and "motion timer" not in body and "no one" not in body.lower()


def test_the_same_hours_twice_mean_all_day(tv):
    tv["enable"](hours_start="08:00", hours_end="08:00", away_after=5)
    lines = sensor_web.what_it_does(tv["screen"].configs.load().sensor)
    assert lines == ["The TV turns on when someone comes in, at any time.",
                     "When the room is empty, it dims after 5 min, blanks after 15 min and turns off about 5 min after "
                     "30 min."]
    assert sensor_control.in_hours(tv["screen"].configs.load().sensor, datetime(2027, 1, 15, 3, 0, tzinfo=UTC))


@pytest.mark.parametrize("status, state, word", [
    ({"awake": True}, {"sent": 3}, "Following room light"),
    ({"awake": True}, {"sent": 0, "away": True}, "Dimmed: room empty"),
    ({"awake": True}, {"sent": 0, "away": True, "blanked": True}, "Screen blank: room empty"),
    ({"awake": False}, {"night": True}, "Off for the night"),
    ({"awake": False}, {}, "TV off"),
    ({"awake": True}, {"paused_until": BASE + HOUR}, "Sensor paused until 09:00"),
    # The TV's own state comes first; the pause is a second note.
    ({"awake": False}, {"paused_until": BASE + HOUR}, "TV off · sensor paused until 09:00"),
    ({"awake": True, "playing": True}, {"paused_until": BASE + HOUR},
     "Waiting: someone is watching TV · sensor paused until 09:00"),
    # On hold, sensor control does not follow the room light.
    ({"awake": True}, {"sent": 3, "unconfirmed": 3}, "Holding the brightness"),
    # Sensor control leaves the TV alone here, so the word never says it follows the room light.
    ({"awake": True, "playing": True}, {"sent": 3}, "Waiting: someone is watching TV"),
    ({"awake": True, "known": False}, {"sent": 3}, "Waiting for the TV to answer"),
    ({"awake": False, "paired": False}, {"sent": 3}, "Waiting for the TV to answer"),
])
def test_the_control_words(tv, status, state, word):
    cfg = tv["screen"].configs.load()
    assert sensor_web.control_word(state, cfg, {"paired": True, "known": True, **status}, BASE) == word


def test_the_control_words_after_a_blank_and_a_night(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    at(tv, 60 + 5 * 60, False)
    cfg = tv["screen"].configs.load()
    awake = {"paired": True, "known": True, "awake": True}
    assert sensor_web.control_word(control(tv), cfg, awake, tv["clock"].now) == "Screen blank: room empty"
    at(tv, 15 * HOUR, False)
    standby_status = {"paired": True, "known": True, "awake": False}
    assert sensor_web.control_word(control(tv), cfg, standby_status, tv["clock"].now) == "Off for the night"


@pytest.mark.parametrize("seconds, words", [
    (None, ""), (0, ""), (59, ""), (60, "1 min"), (12 * 60 + 40, "12 min"), (59 * 60, "59 min"),
    (HOUR, "1 hour"), (2 * HOUR + 4 * 60, "2 hours"), (7 * HOUR + 34 * 60, "7 h 30 min"), (HOUR + 5 * 60, "1 h 5 min"),
])
def test_how_long(seconds, words):
    assert timewords.how_long(seconds) == words


@pytest.mark.parametrize("presence, since, words", [
    (None, None, "No reading"),
    (True, None, "Someone here"),
    (True, BASE - 30, "Someone here"),
    (True, BASE - 7 * HOUR - 31 * 60, "Someone here for 7 h 30 min"),
    (False, BASE - 2 * HOUR, "Room empty for 2 hours"),
    (False, BASE - 12 * 60, "Room empty for 12 min"),
    (False, BASE - 20, "Room empty"),
])
def test_presence_words(presence, since, words):
    assert sensor_web.presence_text(presence, since, BASE) == words


def away_form(rig, browser, **fields):
    return {"csrf": csrf(browser), "sensor_version": sensor_web.version(rig["screen"].configs.load().sensor),
            "away_after": "0", "blank_after": "15", "hours_start": "07:00", "hours_end": "23:00", **fields}


def test_a_bad_hours_field_shows_one_sentence(tv):
    browser = tv["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(), data=away_form(tv, browser, hours_start="7"))
    assert response.status_code == 400 and "Use a time in HH:MM format." in response.get_data(as_text=True)


def test_blank_later_than_the_dim(tv):
    browser = tv["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(),
                            data=away_form(tv, browser, away_after="15", blank_after="10"))
    assert response.status_code == 400 and "Blank later than you dim." in response.get_data(as_text=True)
    response = browser.post("/tv/main/sensor/away", headers=auth(),
                            data=away_form(tv, browser, away_after="15", blank_after="15"))
    assert response.status_code == 400
    response = browser.post("/tv/main/sensor/away", headers=auth(),
                            data=away_form(tv, browser, away_after="0", blank_after="5"))
    assert response.status_code == 302
    sensor = tv["screen"].configs.load().sensor
    assert (sensor.away_after, sensor.blank_after) == (0, 5)


# The brightness points on the Sensor page.


@pytest.fixture
def page_clock(tv, monkeypatch):
    """The pages read the rig's clock, so its readings are fresh for them."""
    monkeypatch.setattr(sensor_web, "time", SimpleNamespace(time=lambda: tv["clock"].now))


def page_rig(rig):
    """The rig's sensor link and memory in the hub, so the pages see them, and one pass that stores the TV's
    range (0 to 10, like the real Frame)."""
    hub = rig["app"].extensions["sensors"]
    hub.links["main"] = ("test", rig["link"])
    hub.memory["main"] = rig["memory"]
    light(rig, 100.0, presence=True)
    rig["run"]()
    return rig["app"].test_client()


def points(rig):
    return [(point.lux, point.level) for point in rig["screen"].configs.load().sensor.curve.points]


def post_points(rig, browser, **fields):
    return browser.post("/tv/main/sensor/control", headers=auth(), data=control_form(rig["app"], browser, **fields))


def test_typed_points_save_in_order(tv):
    browser = page_rig(tv)
    response = post_points(tv, browser, count="1", lux_0="200", level_0="8", new_lux="5", new_level="2")
    assert response.status_code == 302
    assert points(tv) == [(5.0, 2), (200.0, 8)]


def test_a_half_filled_point_is_refused(tv):
    browser = page_rig(tv)
    response = post_points(tv, browser, new_lux="5", new_level="")
    assert response.status_code == 400
    assert "Enter both the light and the brightness of each point." in response.get_data(as_text=True)
    assert points(tv) == []


def test_point_fields_use_the_tv_range(tv):
    browser = page_rig(tv)
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert 'name="new_level" type="number" min="0" max="10"' in body and 'max="100"' not in body
    assert "Brightness (0–10)" in body and 'placeholder="0–10"' in body and 'placeholder="New: lux"' in body
    assert "Add a point, then Save points." in body
    response = post_points(tv, browser, new_lux="5", new_level="40")
    assert response.status_code == 400
    assert "Use a brightness from 0 to 10. That is this TV&#39;s range." in response.get_data(as_text=True)
    assert points(tv) == []
    assert sensor_web.FIELD_ERRORS["level"] == "Use a brightness in this TV's range."


def test_typed_levels_wait_for_the_tv_range(tv):
    browser = tv["app"].test_client()  # no pass yet: Art Frames has not read the TV's range
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "<th scope=\"col\">Brightness</th>" in body and 'placeholder="Number"' in body
    assert 'name="new_level" type="number" value=""' in body  # no min or max from the 0-50 fallback
    response = post_points(tv, browser, new_lux="5", new_level="30")
    assert response.status_code == 400
    assert "This TV&#39;s range shows after Art Frames reads it in Art Mode." in response.get_data(as_text=True)
    assert points(tv) == []


def test_the_empty_curve_says_what_to_do(tv):
    body = page_rig(tv).get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert ("No points yet. Art Frames uses the TV's lowest brightness at 1 lux and its highest at 1000 lux. "
            "Pick a room above, or set the TV by hand at night and in the day and add a point each time.") in body


def test_enter_saves_the_points(tv):
    """The first submit button in the card's form is Save, so Enter in a field never removes a point."""
    browser = page_rig(tv)
    post_points(tv, browser, new_lux="5", new_level="2")
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    form = body[body.index('action="/tv/main/sensor/control#control"'):]
    form = form[:form.index("</form>")]
    first = form[form.index("<button"):]
    assert first.startswith('<button class="sr-only" type="submit" tabindex="-1" aria-hidden="true">Save</button>')
    assert form.index("sr-only") < form.index('name="remove"') and form.index("sr-only") < form.index('name="here"')
    assert ">Save</button>" in form


def test_at_most_8_points(tv, page_clock):
    browser = page_rig(tv)
    typed = {f"lux_{index}": str(2 ** index) for index in range(8)}
    typed.update({f"level_{index}": str(index + 1) for index in range(8)})
    post_points(tv, browser, count="8", **typed)
    assert len(points(tv)) == 8
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert 'name="new_lux"' not in body and "Add a point, then Save." not in body  # no row for a 9th point
    response = post_points(tv, browser, count="8", new_lux="1000", new_level="10", **typed)
    assert response.status_code == 400 and "Use at most 8 points. Remove one first." in response.get_data(as_text=True)
    tv["display"]["brightness"] = 9
    light(tv, 3000.0)
    response = post_points(tv, browser, count="8", here="1", **typed)
    assert response.status_code == 400 and "The curve has 8 points. Remove one first." in response.get_data(as_text=True)


def test_add_a_point_from_the_tv(tv, page_clock):
    browser = page_rig(tv)
    post_points(tv, browser, new_lux="5", new_level="1")
    tv["display"]["brightness"] = 2  # set on the TV by hand
    tv["clock"].now += 150  # past the 2-minute light window
    light(tv, 102.0)
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "<strong>Add a point from the TV</strong>" in body and ">Add point for 102 lux</button>" in body
    assert ("Set the brightness on the TV first. Art Frames reads it when you click (5 at the last check) and pairs it "
            "with the room light now (102 lux).") in body
    # A typed edit in the same form is saved with the new point.
    response = post_points(tv, browser, count="1", lux_0="5", level_0="0", here="1")
    assert response.status_code == 302
    assert points(tv) == [(5.0, 0), (102.0, 2)]
    assert "Added 102 lux at brightness 2." in browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert control(tv)["sent"] == 2  # set by hand on purpose: no pause
    # About the same light again replaces the point.
    tv["display"]["brightness"] = 3
    tv["clock"].now += 150
    light(tv, 110.0)
    post_points(tv, browser, count="2", lux_0="5", level_0="0", lux_1="102", level_1="2", here="1")
    assert points(tv) == [(5.0, 0), (110.0, 3)]


def test_add_a_point_from_the_tv_needs_art_mode(tv, page_clock):
    browser = page_rig(tv)
    tv["display"]["power"] = "standby"  # the demo TV refuses Art Mode reads unless it is on
    response = post_points(tv, browser, here="1", new_lux="5", new_level="2")
    assert response.status_code == 400 and "Turn the TV on in Art Mode first." in response.get_data(as_text=True)
    body = response.get_data(as_text=True)
    assert 'value="5" placeholder="New: lux"' in body  # the typed row stays in the form
    assert points(tv) == []


def test_remove_keeps_typed_edits(tv):
    browser = page_rig(tv)
    post_points(tv, browser, count="1", lux_0="5", level_0="2", new_lux="200", new_level="8")
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert body.count('type="submit" name="remove" value=') == 2
    response = post_points(tv, browser, count="2", lux_0="5", level_0="2", lux_1="200", level_1="9", remove="0")
    assert response.status_code == 302 and points(tv) == [(200.0, 9)]
    assert "Removed the point at 5 lux." in browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)


def test_an_old_form_is_refused(tv):
    browser = page_rig(tv)
    old = control_form(tv["app"], browser, new_lux="5", new_level="2")
    tv["enable"](blank_after=30)  # someone else saves first
    response = browser.post("/tv/main/sensor/control", headers=auth(), data=old, follow_redirects=True)
    assert "The sensor settings changed after the page opened." in response.get_data(as_text=True)
    assert points(tv) == []


def hand_change(rig):
    """Art Frames sets a level, then someone sets 2 on the TV as the room gets darker: the next change finds 2,
    so the pause starts and the offer shows."""
    readings_for(rig, 1000.0, 45, presence=True)
    assert rig["calls"][-1][0] == "brightness"
    rig["display"]["brightness"] = 2
    results = readings_for(rig, 102.0, 300, presence=True)
    assert "paused" in results


def test_a_change_by_hand_offers_a_point(tv, page_clock):
    browser = page_rig(tv)
    hand_change(tv)
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    status = body[body.index('id="sensor-status"'):body.index('id="room"')]
    assert "You set the brightness to 2 on the TV at 102 lux. The sensor is paused until" in status
    assert 'form="curve-form" name="offer" value="add">Add as a point</button>' in status
    assert 'form="resume-form">Resume now</button>' in status and 'form="curve-offer">Keep the pause</button>' in status
    response = post_points(tv, browser, offer="add")
    assert response.status_code == 302 and points(tv) == [(102.0, 2)]
    state = control(tv)
    assert not sensor_curve.paused(state, tv["clock"].now) and state["sent"] == 2 and state.get("suggest") is None
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Added 102 lux at brightness 2. Sensor control is back on." in body
    assert response.location.endswith("#sensor-status") and "Add as a point" not in body


def test_not_now_forgets_the_offer_and_keeps_the_pause(tv, page_clock):
    browser = page_rig(tv)
    hand_change(tv)
    response = browser.post("/tv/main/sensor/suggestion", headers=auth(), data={"csrf": csrf(browser)})
    assert response.status_code == 302 and points(tv) == []
    assert sensor_curve.paused(control(tv), tv["clock"].now)
    body = browser.get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Not added. Sensor control stays paused until" in body and "Add as a point" not in body
    assert response.location.endswith("#sensor-status")


# The Room block of the Now page, and live.json.


def test_the_now_page_shows_the_room(tv, page_clock):
    browser = page_rig(tv)
    at(tv, 5 * 60, True)
    body = browser.get("/tv/main", headers=auth()).get_data(as_text=True)
    assert '<p class="now-label">Room</p>' in body and '<a href="/tv/main/sensor">Sensor settings</a>' in body
    assert 'data-sensor-live data-url="/tv/main/sensor/live.json"' in body
    assert 'data-sensor-field="word">Following room light</p>' in body
    assert "Someone here for 5 min" in body
    assert ' · <span data-sensor-field="light">100 lux</span>' in body
    assert body.count("Brightness ") == 1 or 'data-sensor-field="brightness">Brightness ' in body
    assert "(changed on the TV)" not in body


def test_the_now_page_says_when_the_brightness_changed_on_the_tv(tv, page_clock):
    browser = page_rig(tv)
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, sent=4)
    level = tv["screen"].library.state("art_display")["brightness"]
    assert level != 4
    body = browser.get("/tv/main", headers=auth()).get_data(as_text=True)
    row = body[body.index('data-sensor-field="brightness">'):]
    assert row.startswith(f'data-sensor-field="brightness">Brightness {level} · ')
    assert row[:row.index("</p>")].endswith(" (changed on the TV)")


def test_the_now_page_hides_the_room_without_a_sensor_or_a_reading(tv):
    tv["screen"].configs.mutate(lambda raw: raw.update(sensor=Sensor().model_dump()))
    body = tv["app"].test_client().get("/tv/main", headers=auth()).get_data(as_text=True)
    assert ">Room</a>" not in body


def test_live_json_has_the_room(tv, page_clock):
    browser = page_rig(tv)
    data = browser.get("/tv/main/sensor/live.json", headers=auth()).get_json()
    assert data["lux"] == 100.0 and data["light"] == "100 lux" and data["presence"].startswith("Someone here")
    assert data["word"] == "Following room light" and data["brightness"].startswith("Brightness ")
    assert not any("mac" in key.lower() for key in data)


# Review round 1 (TV safety): each test here was a probe that showed the bug.


def test_turning_the_tv_off_with_the_remote_stays_off(tv):
    at(tv, 0, False)
    at(tv, 5, True)  # someone comes in; the TV is on in Art Mode: no wake needed
    for i in range(1, 20):
        at(tv, 5 + i * 180, True)  # an hour of normal control
    del tv["calls"][:]
    assert at(tv, HOUR + 10, True, awake=False) == "standby"  # they press power on the remote
    assert ("wake", None) not in tv["calls"]


def test_turning_the_tv_off_after_watching_stays_off(tv):
    at(tv, 0, False)
    at(tv, 5, True, awake=True, playing=True)  # watching TV, within the two minutes after coming in
    at(tv, 600, True, awake=True, playing=True)
    del tv["calls"][:]
    assert at(tv, 1200, True, awake=False) == "standby"  # they turn it off and stay a moment
    assert ("wake", None) not in tv["calls"]


def test_a_wake_waits_at_most_two_minutes_after_someone_comes_in(tv):
    off = standby(tv)
    at(tv, 0, False, **off)
    tv["screen"].library.set_state(sensor_control.CONTROL, {"wake_at": BASE - 9 * 60})  # one try a minute ago
    assert at(tv, 5, True, **off) == "wake-wait"
    assert at(tv, 10 + sensor_control.WAKE_EDGE, True, **off) == "standby"  # past the edge: no try
    assert ("wake", None) not in tv["calls"]


def test_control_on_again_is_no_arrival(tv):
    off = standby(tv)
    at(tv, 0, False, **off)
    browser = tv["app"].test_client()
    switch(tv["app"], browser, "off")
    assert at(tv, 60, True, **off) == "off"  # someone comes in while control is off
    switch(tv["app"], browser, "on")
    assert at(tv, 120, True, **off) in {"standby", "samsung"}
    cfg = tv["screen"].configs.load()
    at(tv, 180, False, **off)
    assert sensor_control.step(tv["screen"], cfg, None, tv["memory"], BASE + 240) == "off"  # no sensor link
    assert at(tv, 300, True, **off) in {"standby", "samsung"}
    assert ("wake", None) not in tv["calls"]


def test_control_off_and_on_before_the_restore_keeps_the_blank_owned(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    browser = tv["app"].test_client()
    tv["status"](awake=False)  # the TV is in standby: the restore waits
    switch(tv["app"], browser, "off")
    assert samsung(tv) == "waiting"
    switch(tv["app"], browser, "on")
    assert control(tv)["blanked"] is True and control(tv)["away"] is True  # the new session owns them
    results = []
    for i in range(5):  # morning: someone there, the TV on in Art Mode
        tv["clock"].now = BASE + 2 * HOUR + i * 400
        tv["status"]()
        results.append((samsung(tv), at(tv, 2 * HOUR + i * 400, True)))
    assert not blanked(tv) and not control(tv).get("blanked")
    assert tv["screen"].library.state(sensor_control.SAMSUNG)["want"] != "off"
    assert any(code == "back" for _, code in results), results


def scheduled_wake(rig, at_time="08:30"):
    rig["screen"].configs.mutate(lambda raw: (raw["tv"].update(ip="192.0.2.10", mac="00:11:22:33:44:55"),
                                              raw.setdefault("wake", {}).update(enabled=True, at=at_time,
                                                                                 grace_minutes=15)))


def test_someone_there_undoes_the_blank_inside_the_wake_window(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    scheduled_wake(tv)
    results = [at(tv, 22 * 60 + i * 60, True) for i in range(20)]  # 08:22 .. 08:41, someone there
    assert not blanked(tv) and "back" in results
    assert set(results) <= {"back", "wake"}  # nothing else runs in the window: no curve, no artwork


def test_the_wake_window_keeps_the_owner_test_blank(tv):
    tv["screen"].controller.soft_blank()
    scheduled_wake(tv)
    assert at(tv, 22 * 60, True) == "wake" and blanked(tv)


def test_the_wake_window_turns_sleep_after_off_for_someone_there(tv):
    tv["screen"].library.set_state(sensor_control.CONTROL, {"night": "5"})
    tv["display"]["motion_timer"] = "5"
    scheduled_wake(tv)
    assert at(tv, 22 * 60, True) == "day"
    assert tv["display"]["motion_timer"] == "off"


def test_the_scheduled_wake_undoes_the_sensor_blank_first(configs, library, monkeypatch):
    from test_controller import at as clock_at
    from test_controller import setup

    controller, screen = setup(configs, library, monkeypatch)
    undone = []
    screen.undo_blank = lambda content_id, brightness: undone.append((content_id, brightness)) or {
        "restored": None, "readback": SimpleNamespace(settings={"brightness": brightness}, reported=brightness)}
    cfg = configs.load()
    library.set_state("soft_blank:" + cfg.tv.identity,
                      {"content_id": None, "brightness": 4, "at": "2026-10-26T06:00:00+00:00", "owner": "sensor"})
    library.set_state(sensor_control.CONTROL, {"blanked": True, "away": True})
    assert controller.tick(clock_at("07:00")) == "published"
    assert undone == [(None, 4)] and controller.blank_record() is None
    assert library.state(sensor_control.CONTROL)["blanked"] is False
    assert library.state(sensor_control.CONTROL)["sent"] == 4


def test_the_scheduled_wake_keeps_the_owner_test_blank(configs, library, monkeypatch):
    from test_controller import at as clock_at
    from test_controller import setup

    controller, screen = setup(configs, library, monkeypatch)
    cfg = configs.load()
    library.set_state("soft_blank:" + cfg.tv.identity,
                      {"content_id": None, "brightness": 4, "at": "2026-10-26T06:00:00+00:00", "owner": "test"})
    assert controller.tick(clock_at("07:00")) == "blanked"
    assert screen.publications == [] and controller.blank_record() is not None


def test_a_wake_runs_on_its_own_thread(tv):
    off = standby(tv)
    tv["memory"].background = True
    at(tv, 0, False, **off)
    assert at(tv, 60, True, **off) == "waking"
    tv["memory"].wake_thread.join(5)
    assert tv["display"]["power"] == "on" and control(tv)["wake_result"] == "confirmed"
    assert tv["memory"].wake_until is None


def test_the_hub_skips_a_tv_while_its_wake_runs(tv, monkeypatch):
    import threading

    hub = sensor_control.SensorHub(tv["app"].extensions["displays"], discovery=object(),
                                   link_factory=lambda *args: SimpleNamespace(stop=lambda: None))
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    hub.memory["main"] = sensor_control.Memory(background=True, wake_thread=thread)
    ran = []
    monkeypatch.setattr(sensor_control, "samsung_step", lambda *args: ran.append("samsung"))
    monkeypatch.setattr(sensor_control, "step", lambda *args: ran.append("step") or "same")
    hub.tick(BASE)
    assert hub.results["main"] == "waking" and ran == []
    release.set()
    thread.join(5)
    hub.tick(BASE + 5)
    assert ran == ["samsung", "step"] and hub.results["main"] == "same"
    assert hub.memory["main"].wake_thread is None


def test_presence_wake_never_falls_back_to_the_wake_signal_after_the_key(tmp_path, monkeypatch):
    # No answer, then standby for 4 s (one key due), then no answer again for the key's probe.
    device, fake, _, wakes = make(tmp_path, monkeypatch, [None, *[("standby", "on")] * 5, None])
    sent = bursts(device, monkeypatch)
    result = device.presence_wake()
    assert result.result == "not-confirmed" and not wakes and not fake.keys and len(sent) == 1


def test_presence_wake_ends_within_its_limit(tmp_path, monkeypatch):
    # Standby twice after the last burst: the key waits only for the rest of the limit.
    states = [None] * 19 + [("standby", "on")]
    device, fake, _, _ = make(tmp_path, monkeypatch, states)
    bursts(device, monkeypatch)
    result = device.presence_wake()
    assert fake.keys == [("click", "KEY_POWER")] and result.result == "not-confirmed"
    assert result.seconds <= television.PRESENCE_WAKE_LIMIT + 3


def test_an_unknown_sleep_after_is_left_as_it_is(tv):
    tv["display"]["motion_timer"] = "90"  # a value Art Frames does not know
    at(tv, 15 * HOUR - 120, False)
    assert at(tv, 15 * HOUR, False) == "unknown"
    assert tv["display"]["motion_timer"] == "90"
    assert "motion_timer" not in ((tv["screen"].library.state(sensor_control.SAMSUNG) or {}).get("saved") or {})
    assert control(tv).get("night") is None


# Who owns a blank: sensor control, or the owner's Tests button.


def test_sensor_control_never_undoes_the_owner_test_blank(tv):
    tv["screen"].controller.soft_blank()
    tv["screen"].library.set_state(sensor_control.CONTROL, {"blanked": True})  # an old session's flag
    assert at(tv, 0, True) == "blanked"
    assert blanked(tv) and ("undo-blank", None) not in tv["calls"]


def test_a_test_blank_over_the_sensor_blank_takes_it_over(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    assert tv["screen"].controller.blank_record()["owner"] == "sensor"
    tv["screen"].controller.soft_blank()
    assert tv["screen"].controller.blank_record()["owner"] == "test"
    assert at(tv, 60 + 6 * 60, True) == "blanked" and blanked(tv)  # someone back: the test blank holds


def test_undo_blank_on_the_tests_card_ends_the_sensor_blank(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    result = tv["screen"].controller.undo_blank()
    assert result["owner"] == "sensor" and not blanked(tv)
    with pytest.raises(DisplayError, match="not-blanked"):
        tv["screen"].controller.undo_blank("sensor")


def test_an_old_blank_record_belongs_to_the_owner(tv):
    library = tv["screen"].library
    device = tv["screen"].controller._identity(tv["screen"].configs.load())
    library.set_state("soft_blank:" + device, {"content_id": None, "brightness": 3, "at": "2027-01-15T08:00:00+00:00"})
    with pytest.raises(DisplayError, match="not-blanked"):
        tv["screen"].controller.undo_blank("sensor")
    assert blanked(tv)


# Older settings files.


def test_an_old_custom_lux_without_a_level_keeps_its_point():
    from art_frames.config import BrightnessCurve

    curve = BrightnessCurve.model_validate({"dark_lux": 3.0, "dark_level": None,
                                            "bright_lux": 800.0, "bright_level": None})
    assert [(point.lux, point.level) for point in curve.points] == [(3.0, 0), (800.0, 100)]
    # The old default: the TV's top from 500 lux. It stays there; only new setups use 1000 lux.
    default = BrightnessCurve.model_validate({"dark_lux": 1.0, "dark_level": None,
                                              "bright_lux": 500.0, "bright_level": None})
    assert [(point.lux, point.level) for point in default.points] == [(500.0, 100)]
    assert sensor_curve.target(500.0, default, 0, 10) == 10 and sensor_curve.target(1.0, default, 0, 10) == 0
    assert BrightnessCurve().points == []


# Review round 2 (TV safety).


def test_turning_the_tv_off_soon_after_coming_in_stays_off(tv):
    at(tv, 0, False)
    at(tv, 5, True)  # someone comes in; the TV is on in Art Mode
    del tv["calls"][:]
    assert at(tv, 65, True, awake=False) == "standby"  # off with the remote, inside the two minutes
    assert ("wake", None) not in tv["calls"]


def test_presence_wake_sends_no_signal_when_the_tv_stops_answering_after_standby(tmp_path, monkeypatch):
    device, fake, _, wakes = make(tmp_path, monkeypatch, [("standby", "on"), None])
    sent = bursts(device, monkeypatch)
    result = device.presence_wake()
    assert result.result == "not-confirmed" and not sent and not wakes and not fake.keys


def test_a_stopped_wake_sends_no_more_bursts(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [None])
    sent = bursts(device, monkeypatch)
    with pytest.raises(DisplayError, match="stopped"):
        device.presence_wake(stop=lambda: len(sent) >= 1)
    assert len(sent) == 1 and not fake.keys


def test_a_stopped_wake_sends_no_key(tmp_path, monkeypatch):
    device, fake, _, _ = make(tmp_path, monkeypatch, [("standby", "on")])
    sent = bursts(device, monkeypatch)
    with pytest.raises(DisplayError, match="stopped"):
        device.presence_wake(stop=lambda: True)
    assert not fake.keys and not sent


@pytest.mark.parametrize("change", ["off", "removed", "another sensor"])
def test_a_wake_stops_when_sensor_control_changes(tv, change):
    screen = tv["screen"]
    stopped = sensor_control.wake_stopped(screen, screen.configs.load().sensor)
    assert stopped() is False

    def mutate(raw):
        if change == "off":
            raw["sensor"]["enabled"] = False
        elif change == "removed":
            raw["sensor"].update(enabled=False, host="")
        else:
            raw["sensor"]["host"] = "192.168.1.231"
    screen.configs.mutate(mutate)
    assert stopped() is True


def test_a_stopped_wake_counts_as_no_try(tv, monkeypatch):
    off = standby(tv)
    at(tv, 0, False, **off)

    def stop_now(stop=None):
        raise DisplayError("stopped")
    monkeypatch.setattr(tv["screen"].controller, "presence_wake", stop_now)
    assert at(tv, 60, True, **off) == "stopped"
    assert not control(tv).get("wake_failures") and tv["display"]["power"] == "standby"
    assert sensor_control.wake_check(tv["screen"].library, tv["screen"].configs.load(), tv["clock"].now) is None


def test_undo_blank_on_the_tests_card_counts_the_empty_room_again(tv):
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    browser = tv["app"].test_client()
    response = browser.post("/screen/display/undo-blank", headers=auth(), data={"csrf": csrf(browser)},
                            follow_redirects=True)
    assert response.status_code == 200 and not blanked(tv) and control(tv)["blanked"] is False
    assert at(tv, 60 + 5 * 60 + 5, False) != "blanked" and not blanked(tv)  # not at once
    assert at(tv, 60 + 9 * 60, False) != "blanked" and not blanked(tv)
    assert at(tv, 60 + 10 * 60 + 10, False) == "blanked"  # 5 minutes after the undo


def failing_undo(screen):
    def refuse(content_id, brightness):
        raise DisplayError("not-art-mode")
    screen.undo_blank = refuse


def sensor_blank(configs, library):
    cfg = configs.load()
    library.set_state("soft_blank:" + cfg.tv.identity,
                      {"content_id": None, "brightness": 4, "at": "2026-10-26T06:00:00+00:00", "owner": "sensor"})
    library.set_state(sensor_control.CONTROL, {"blanked": True, "away": True})


def test_a_failed_undo_at_the_wake_keeps_the_blank_and_tries_again(configs, library, monkeypatch):
    from test_controller import at as clock_at
    from test_controller import setup

    controller, screen = setup(configs, library, monkeypatch)
    sensor_blank(configs, library)
    failing_undo(screen)
    assert controller.tick(clock_at("07:00")) == "room-empty"  # no error out of the wake pass
    assert controller.blank_record() is not None and library.state("render_backoff") is None
    assert controller._handoff is not None  # the wake's handoff is kept for the next pass
    del screen.undo_blank
    screen.undo_blank = lambda content_id, brightness: {
        "restored": None, "readback": SimpleNamespace(settings={"brightness": brightness}, reported=brightness)}
    controller._handoff["at"] -= 60  # the handoff expires: the later pass in the wake window retries alone
    assert controller.tick(clock_at("07:03")) == "published"
    assert controller.blank_record() is None and screen.publications == [b"planner"]


def test_the_scheduled_wake_counts_the_empty_room_again(tv, configs, library, monkeypatch):
    from test_controller import at as clock_at
    from test_controller import setup

    controller, screen = setup(configs, library, monkeypatch)
    sensor_blank(configs, library)
    screen.undo_blank = lambda content_id, brightness: {
        "restored": None, "readback": SimpleNamespace(settings={"brightness": brightness}, reported=brightness)}
    assert controller.tick(clock_at("07:00")) == "published"
    assert library.state(sensor_control.CONTROL)["empty_again"] is True
    # Sensor control's next pass restarts the count, so the blank waits its full time again.
    tv["enable"](blank_after=5)
    at(tv, 0, True)
    at(tv, 60, False)
    assert at(tv, 60 + 5 * 60, False) == "blanked"
    tv["screen"].controller.undo_blank()
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, blanked=False, empty_again=True)
    assert at(tv, 60 + 6 * 60, False) != "blanked"
    assert control(tv)["empty_again"] is False
    assert at(tv, 60 + 11 * 60, False) == "blanked"


def test_the_retry_time_outside_the_hours_is_the_start_of_the_hours(tv):
    cfg = tv["screen"].configs.load()
    late = BASE + 14 * HOUR + 50 * 60  # 22:50; the next try would be 23:10, after the hours
    tv["screen"].library.set_state(sensor_control.CONTROL, {"wake_at": late, "wake_failures": 1,
                                                            "wake_result": "not-confirmed"})
    row = sensor_control.wake_check(tv["screen"].library, cfg, late + 60)
    assert row["detail"].endswith(" If someone comes in after 07:00, Art Frames tries again.")


def test_a_stopped_wake_lets_the_next_arrival_wake_the_tv(tv, monkeypatch):
    off = standby(tv)
    at(tv, 0, False, **off)
    tv["screen"].library.set_state(sensor_control.CONTROL, {"wake_at": None})
    controller = tv["screen"].controller
    real = controller.presence_wake

    def stop_now(stop=None):
        raise DisplayError("stopped")
    monkeypatch.setattr(controller, "presence_wake", stop_now)
    assert at(tv, 60, True, **off) == "stopped"
    assert control(tv).get("wake_at") is None  # the stopped wake is not a try
    monkeypatch.setattr(controller, "presence_wake", real)
    at(tv, 120, False, **off)
    assert at(tv, 180, True, **off) == "woke"  # well inside WAKE_GAP of the stopped wake


def test_a_wake_stops_when_the_settings_cannot_be_read(tv, monkeypatch):
    screen = tv["screen"]
    stopped = sensor_control.wake_stopped(screen, screen.configs.load().sensor)

    def broken():
        raise OSError("disk")
    monkeypatch.setattr(screen.configs, "load", broken)
    assert stopped() is True
