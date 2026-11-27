"""The room dark rule (Sensor.dark_below): no artwork while the room lights are off.

The screen blanks at once in a dark room, even with someone there; nothing turns it on while the room stays
dark; and it comes back when the light is back and someone is there. Every TV here is a fake
(DemoTelevision subclasses): no test contacts a TV, a sensor or a NAS.
"""
# ruff: noqa: F811

import logging
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_away_off import away_form, keys, sleeps
from test_presence_power import BASE, PowerTV, at, blanked, control, standby, tv  # noqa: F401  (tv is a fixture)
from test_sensors import rig  # noqa: F401  (a fixture)
from test_wake_brightness import LevelTV
from test_web import auth

from art_frames import sensor_control, sensor_web
from art_frames.config import Sensor

LIGHT = 100.0
DARK = 0.5


@pytest.fixture
def room(tv):
    """The tv rig with the dark rule at 2 lux, blank after 15 min, off after 30 min, someone there at 08:00."""
    tv["enable"](dark_below=2.0)
    assert at(tv, 0, True) in {"wait", "same", "brightness"}
    return tv


def passes(rig, start, seconds, presence, lux, every=15, **status):
    """A reading and a pass every `every` seconds from BASE + start, for `seconds`. Returns the results."""
    return [at(rig, start + offset, presence, lux=lux, **status) for offset in range(every, seconds + 1, every)]


def go_dark(rig, start=0, presence=True, seconds=180, **status):
    """The lights go off at BASE + start. Returns the results."""
    return passes(rig, start, seconds, presence, DARK, **status)


# The setting.


def test_the_rule_is_on_by_default_and_never_survives_a_save():
    assert Sensor().dark_below == 5.0
    assert Sensor.model_validate({"blank_after": 15}).dark_below == 5.0  # an older file
    # The first build wrote null for its old default (off): it loads as the default level now.
    assert Sensor.model_validate({"dark_below": None}).dark_below == 5.0
    assert Sensor(dark_below="never").dark_below is None
    assert Sensor(dark_below=5).dark_below == 5.0
    # The owner's Never is saved as "never", so it loads as Never again, not as the default.
    off = Sensor(dark_below="never")
    assert off.model_dump()["dark_below"] == "never"
    assert Sensor.model_validate(off.model_dump()).dark_below is None
    assert Sensor.model_validate_json(off.model_dump_json()).dark_below is None


@pytest.mark.parametrize("value", [0, -1, 1001, "dim"])
def test_the_dark_level_is_more_than_0_lux(value):
    with pytest.raises(ValidationError):
        Sensor(dark_below=value)


def test_dark_room_has_a_margin_and_keeps_its_state_without_a_reading():
    sensor = Sensor(dark_below=2.0)
    assert sensor_control.dark_room(sensor, 1.9, False) is True
    assert sensor_control.dark_room(sensor, 2.0, False) is False
    assert sensor_control.dark_room(sensor, 3.9, True) is True  # light again only from 4 lux
    assert sensor_control.dark_room(sensor, 4.0, True) is False
    assert sensor_control.dark_room(sensor, None, True) is True
    assert sensor_control.dark_room(sensor, None, False) is False
    assert sensor_control.dark_room(Sensor(dark_below="never"), 0.0, True) is False  # the rule is off


# The blank, with someone there.


def test_a_dark_room_blanks_at_once_with_someone_there(room, caplog):
    with caplog.at_level(logging.INFO):
        results = go_dark(room)
    assert "away" in results and "blanked" in results
    assert blanked(room) and control(room)["dark"] is True
    assert "TV main: room dark (0.5 lux, below 2); the screen blanks" in caplog.text
    assert "TV main: room dark; screen blank" in caplog.text
    assert caplog.text.count("room dark (") == 1  # one line for the change


def test_off_rule_never_blanks_a_dark_room_with_someone_there(tv):
    at(tv, 0, True)
    go_dark(tv)
    assert not blanked(tv) and not control(tv).get("dark")


def test_the_dark_blank_does_not_wait_for_a_pause(room):
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, paused_until=BASE + 3600,
                         paused_at=BASE)
    go_dark(room)
    assert blanked(room)


def test_nothing_undoes_the_blank_while_the_room_is_dark(room):
    go_dark(room)
    del room["calls"][:]
    results = passes(room, 180, 600, True, DARK)
    assert blanked(room) and "back" not in results
    assert ("undo-blank", None) not in room["calls"]


def test_unknown_light_keeps_the_room_dark(room):
    go_dark(room)
    # No light reading for 10 min: the 2-minute median is unknown.
    for offset in range(195, 800, 15):
        room["clock"].now = BASE + offset
        room["status"]()
        room["link"].readings.update("binary_sensor", "presence", True)
        room["run"]()
    assert blanked(room) and room["memory"].dark is True


def test_light_just_above_the_dark_level_keeps_it_dark(room):
    go_dark(room)
    passes(room, 180, 300, True, 3.0)  # above 2 lux, but under 2 x 2 lux
    assert blanked(room) and control(room)["dark"] is True


def test_the_light_back_with_someone_there_undoes_the_blank(room, caplog):
    go_dark(room)
    with caplog.at_level(logging.INFO):
        results = passes(room, 180, 300, True, LIGHT)
    assert "back" in results and not blanked(room)
    assert control(room)["dark"] is False and control(room)["away"] is False
    assert "TV main: room light again" in caplog.text


def test_the_light_back_with_the_room_empty_keeps_the_blank(room):
    go_dark(room, presence=False)
    results = passes(room, 180, 300, False, LIGHT)
    assert blanked(room) and "back" not in results and control(room)["dark"] is False


def test_a_restart_keeps_the_dark_state_without_a_light_reading(room):
    go_dark(room)
    room["memory"] = sensor_control.Memory()
    room["run"] = lambda: sensor_control.step(room["screen"], room["screen"].configs.load(), room["link"],
                                              room["memory"], room["clock"].now)
    room["clock"].now = BASE + 1000  # the light readings are older than 2 minutes
    room["status"]()
    room["link"].readings.update("binary_sensor", "presence", True)
    room["run"]()
    assert room["memory"].dark is True and blanked(room)


# The TV off step.


def test_a_dark_room_sets_the_off_steps_sleep_after_with_someone_there(room, caplog):
    with caplog.at_level(logging.INFO):
        results = go_dark(room)
    assert "tv_off" in results and sleeps(room) == ["5"]
    assert control(room)["empty_sleep"] is True and keys(room) == []
    assert "TV main: room dark; Sleep After 5 min so the TV turns itself off" in caplog.text


def test_without_the_off_step_a_dark_room_only_blanks(room):
    room["enable"](off_after=None)
    go_dark(room)
    assert blanked(room) and sleeps(room) == []


# Turning the screen on.


def test_no_presence_wake_in_a_dark_room(room):
    go_dark(room, presence=False)
    off = standby(room)
    passes(room, 180, 120, False, DARK, **off)
    results = passes(room, 300, 300, True, DARK, **off)  # someone comes in, the lights stay off
    assert "woke" not in results and ("wake", None) not in room["calls"]
    assert room["display"]["power"] == "standby"


def test_turning_the_light_on_with_someone_there_turns_the_screen_on(room):
    go_dark(room, presence=False)
    off = standby(room)
    passes(room, 180, 300, True, DARK, **off)  # someone there in the dark: no wake
    assert ("wake", None) not in room["calls"]
    results = passes(room, 480, 180, True, LIGHT, **off)
    assert "woke" in results and room["display"]["power"] == "on"


def scheduled_in_the_dark(rig):
    """The scheduled wake at 08:00 + 5 min, the TV in standby and the room dark. Returns the controller."""
    def change(raw):
        # Documentation addresses: the fake TV never uses them.
        raw["tv"].update(ip="192.0.2.10", mac="02:00:00:00:00:01")
        raw["wake"].update(enabled=True, at="08:05", grace_minutes=5)
    rig["screen"].configs.mutate(change)
    rig["screen"].controller.tv_factory = lambda cfg: LevelTV(rig["display"], rig["calls"])
    off = standby(rig)
    go_dark(rig, presence=False, **off)  # a pass of sensor control sets Controller.curve_step
    del rig["calls"][:]
    return rig["screen"].controller


def test_the_scheduled_wake_waits_in_a_dark_room(room):
    ctl = scheduled_in_the_dark(room)
    assert room["memory"].dark is True and ctl.curve_step.dark() is True
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 10, UTC))
    assert ("scheduled-wake", None) not in room["calls"] and room["display"]["power"] == "standby"
    library, device = room["screen"].library, ctl._identity(room["screen"].configs.load())
    assert library.state("wake_result") == "Wake skipped because the room is dark"
    assert library.state("wake_missed:" + device) is None
    assert library.state("wake_day:" + device) is None  # not done: the next pass checks again


def test_light_back_inside_the_wake_window_runs_the_scheduled_wake(room, caplog):
    ctl = scheduled_in_the_dark(room)
    with caplog.at_level(logging.INFO):
        ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 10, UTC))
        ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 30, UTC))
    assert caplog.text.count("Wake skipped because the room is dark") == 1  # once per day
    passes(room, 330, 150, False, LIGHT, **standby(room))  # the light is back; the room stays empty
    assert ctl.curve_step.dark() is False
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 8 * 60, UTC))
    assert ("scheduled-wake", None) in room["calls"] and room["display"]["power"] == "on"


def test_the_window_ending_in_the_dark_skips_the_day(room):
    ctl = scheduled_in_the_dark(room)
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 10, UTC))
    passes(room, 180, 600, False, LIGHT, **standby(room))
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 11 * 60, UTC))  # the 5-minute grace window ended
    assert ("scheduled-wake", None) not in room["calls"]


class DarkStep:
    """A stand-in for sensor control's CurveStep: the room is dark while `dark` is True."""

    def __init__(self, dark=True):
        self.is_dark = dark

    def dark(self):
        return self.is_dark

    def level(self, *args):
        return None


def test_no_pre_wake_upload_in_a_dark_room(configs, library, caplog):
    from test_prewake import at as clock_at
    from test_prewake import setup

    caplog.set_level(logging.INFO, logger="art_frames")
    controller, frame, _ = setup(configs, library)
    controller.curve_step = DarkStep()
    controller.tick(clock_at("06:58"))
    assert frame.calls == []  # the black image stays the TV's current artwork
    assert "did not upload the 07:00 image before the wake: the room is dark" in caplog.text


def test_stale_readings_are_not_dark_for_the_wake_window(room, monkeypatch):
    # The off step's Sleep After holds in a dark room; then the readings stop for longer than UNKNOWN_GAP.
    # Like the scheduled wake, the window counts the room as light: Sleep After goes off so the wake can run.
    # The blank stays.
    go_dark(room)
    assert sleeps(room) == ["5"]
    in_the_wake_window(room, monkeypatch)
    room["clock"].now = BASE + 180 + sensor_control.UNKNOWN_GAP + 30
    room["status"]()
    assert room["run"]() == "day" and sleeps(room) == ["5", "off"]
    assert room["memory"].dark is True and blanked(room)


def test_a_short_gap_keeps_the_dark_wake_window(room, monkeypatch):
    # The link drops for 150 s: the readings are stale, but the room stays dark and Sleep After holds.
    go_dark(room)
    in_the_wake_window(room, monkeypatch)
    room["clock"].now = BASE + 180 + sensor_control.sensors.STALE_SECONDS + 30
    room["status"]()
    room["run"]()
    assert sleeps(room) == ["5"] and blanked(room)


def test_a_short_gap_in_the_readings_keeps_the_wake_waiting(room):
    ctl = scheduled_in_the_dark(room)
    room["clock"].now = BASE + 180 + sensor_control.sensors.STALE_SECONDS + 30
    assert not room["link"].readings.fresh(room["clock"].now)
    assert ctl.curve_step.dark() is True
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 10, UTC))
    assert ("scheduled-wake", None) not in room["calls"] and room["display"]["power"] == "standby"


def test_a_restart_in_a_gap_keeps_the_dark_room(room, monkeypatch):
    # After a restart, the first pass takes the dark room from CONTROL and counts that moment as fresh.
    ctl = scheduled_in_the_dark(room)
    room["memory"].dark = room["memory"].fresh_at = None
    room["clock"].now = BASE + 180 + sensor_control.sensors.STALE_SECONDS + 30
    in_the_wake_window(room, monkeypatch)
    room["status"](**standby(room))
    room["run"]()
    assert room["memory"].dark is True and ctl.curve_step.dark() is True


def test_a_dark_wake_window_reads_the_tv_once(room):
    # Each scheduler pass in a dark window would read the TV and log a wake; only the first one does.
    ctl = scheduled_in_the_dark(room)
    made = []
    factory = ctl.tv_factory
    ctl.tv_factory = lambda cfg: made.append(1) or factory(cfg)
    for second in (10, 25, 40, 55):
        ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + second, UTC))
    assert len(made) == 1
    assert ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 6 * 60, UTC)) is None  # run_wake: no "woke"


def test_a_dark_day_is_not_a_late_wake(room):
    # The first try failed (wake_missed), then the window ends in the dark. The TV comes on that evening.
    ctl = scheduled_in_the_dark(room)
    cfg = room["screen"].configs.load()
    device = ctl._device(cfg)
    library = room["screen"].library
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 10, UTC))
    today = library.state("wake_dark:" + device)
    assert today
    library.set_state("wake_missed:" + device, today)
    ctl._late_wake = lambda *args: pytest.fail("a dark day showed as a late wake")
    ctl._arrived(cfg, datetime.fromisoformat(today + "T20:00:00+00:00"), device, {"known": True, "awake": True})


def test_the_scheduled_wake_runs_when_the_room_is_light(room):
    ctl = scheduled_in_the_dark(room)
    passes(room, 180, 120, False, LIGHT, **standby(room))
    assert ctl.curve_step.dark() is False
    ctl.wake_if_due(now=datetime.fromtimestamp(BASE + 5 * 60 + 10, UTC))
    assert ("scheduled-wake", None) in room["calls"]


def test_a_stale_light_reading_is_not_dark_for_the_scheduled_wake(room):
    ctl = scheduled_in_the_dark(room)
    room["clock"].now = BASE + 3600  # the readings are old: the link is not fresh
    assert ctl.curve_step.dark() is False


def test_the_scheduled_wake_keeps_the_sensor_blank_in_a_dark_room(room, monkeypatch):
    go_dark(room)  # the TV is on in Art Mode, blank for the dark room
    ctl = room["screen"].controller
    assert blanked(room)
    monkeypatch.setattr(ctl, "_undo_sensor_blank", lambda *args: pytest.fail("the dark blank was undone"))
    cfg = room["screen"].configs.load()
    status = {"awake": True, "playing": False, "paired": True, "known": True, "answered": True}
    result = ctl._deliver(PowerTV(room["display"], room["calls"]), cfg, datetime.fromtimestamp(BASE + 200, UTC),
                          ctl._identity(cfg), status, force=False, scheduled=True, preview_revision=None,
                          woke=status)
    assert result == "room-empty" and blanked(room)


def test_the_wake_window_does_not_undo_a_dark_blank(room, monkeypatch):
    go_dark(room)
    ctl = room["screen"].controller
    real = ctl._wake_window
    monkeypatch.setattr(ctl, "_wake_window", lambda cfg, now: (True, now, False))
    assert at(room, 200, True, lux=DARK) == "blanked"
    assert blanked(room) and sleeps(room) == ["5"]
    monkeypatch.setattr(ctl, "_wake_window", real)


def in_the_wake_window(rig, monkeypatch):
    monkeypatch.setattr(rig["screen"].controller, "_wake_window", lambda cfg, now: (True, now, False))


def test_a_dark_room_blanks_an_unblanked_tv_inside_the_wake_window(room, monkeypatch):
    # The TV is on in Art Mode and not blank when the lights go off inside the wake window.
    in_the_wake_window(room, monkeypatch)
    assert not blanked(room)
    results = go_dark(room)
    assert "away" in results and "blanked" in results
    assert blanked(room) and control(room)["dark"] is True
    # The window still holds the off step's Sleep After and the curve.
    assert sleeps(room) == [] and results[-1] == "blanked"


def test_without_a_dark_room_the_wake_window_still_holds(room, monkeypatch):
    in_the_wake_window(room, monkeypatch)
    assert set(passes(room, 0, 180, False, LIGHT)) == {"wake"}
    assert not blanked(room)


def test_the_owners_test_blank_still_holds_in_a_dark_wake_window(room, monkeypatch):
    room["screen"].controller.soft_blank(owner="test")
    in_the_wake_window(room, monkeypatch)
    results = go_dark(room)
    assert room["memory"].dark is True and results[-1] == "blanked"  # "wake" until the room reads dark
    assert set(results) == {"wake", "blanked"}
    assert room["screen"].controller.blank_record()["owner"] == "test" and sleeps(room) == []


def test_a_busy_tv_skips_the_dark_blank_in_the_wake_window(room, monkeypatch):
    from art_frames import controller as controller_module

    in_the_wake_window(room, monkeypatch)
    monkeypatch.setattr(controller_module.CALLER, "wait", 0, raising=False)
    ctl = room["screen"].controller
    assert ctl.lock.acquire(timeout=1)
    try:
        results = go_dark(room)
    finally:
        ctl.lock.release()
    assert "busy" in results and "away" not in results and not blanked(room)


# The Sensor page.


def test_the_sensor_page_offers_the_dark_level(room):
    body = room["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert "Blank when the room is darker than" in body and '<select name="dark_below">' in body
    assert '<option value="2" selected>2 lux</option>' in body
    assert '<option value="never" ' in body and '<option value="50" ' in body
    assert "stays blank until the light is at least twice this level" in body
    assert "pick a level above the reading with the lights off and the artwork on" in body
    assert "When the room is empty or dark" in body and "The dark blank waits for the light." in body


@pytest.mark.parametrize("value, saved", [("never", None), ("5", 5.0), ("0.5", 0.5)])
def test_the_sensor_page_saves_the_dark_level(room, value, saved):
    browser = room["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(), data=away_form(room, browser, dark_below=value))
    assert response.status_code == 302
    assert room["screen"].configs.load().sensor.dark_below == saved


@pytest.mark.parametrize("value", ["dark", "0", "-1", "nan", "5000"])
def test_the_sensor_page_refuses_a_bad_dark_level(room, value):
    browser = room["app"].test_client()
    response = browser.post("/tv/main/sensor/away", headers=auth(), data=away_form(room, browser, dark_below=value))
    assert response.status_code == 400 and "Pick a light level from the list." in response.get_data(as_text=True)
    assert room["screen"].configs.load().sensor.dark_below == 2.0


def test_an_older_page_without_the_dark_field_keeps_the_saved_level(room):
    browser = room["app"].test_client()
    assert browser.post("/tv/main/sensor/away", headers=auth(), data=away_form(room, browser)).status_code == 302
    assert room["screen"].configs.load().sensor.dark_below == 2.0


def test_a_saved_level_outside_the_list_stays_selected(room):
    room["enable"](dark_below=3.0)
    body = room["app"].test_client().get("/tv/main/sensor", headers=auth()).get_data(as_text=True)
    assert '<option value="3" selected>3 lux</option>' in body


def test_what_it_does_names_the_dark_rule():
    lines = sensor_web.what_it_does(Sensor(dark_below=5))
    assert ("When the room is darker than 5 lux, it blanks at once and turns off about 5 min later, and does not "
            "turn on until the light is back.") in lines
    assert ("When the room is darker than 5 lux, it blanks at once, and does not turn on until the light is back."
            in sensor_web.what_it_does(Sensor(dark_below=5, off_after=None)))
    assert not any("darker" in line for line in sensor_web.what_it_does(Sensor(dark_below="never")))


def test_the_status_says_the_room_is_dark():
    state = {"dark": True, "blanked": True, "sent": 0, "sent_at": BASE}
    assert sensor_web.control_text(state, BASE + 60).startswith("Screen blank because the room is dark")
    assert sensor_web.room_word({}) == "empty"


def test_the_health_page_says_the_wake_was_skipped_for_the_dark_room():
    from art_frames.config import Config
    from art_frames.web import wake_check

    cfg = Config.model_validate({"tv": {"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"},
                                 "wake": {"enabled": True, "at": "06:45"}})
    check = wake_check("Wake skipped because the room is dark", cfg)
    assert (check["tone"], check["title"]) == ("info", "Last turn-on was skipped because the room was dark")


def test_an_update_says_the_screen_stays_blank_for_the_dark_room(room, monkeypatch):
    from test_web import csrf

    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, dark=True, blanked=True)
    monkeypatch.setattr(room["app"].extensions["controller"], "tick", lambda **kwargs: "room-empty")
    client = room["app"].test_client()
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert ("The room is dark, so the screen stays blank. It comes back when the light is on and someone is there."
            in response.text)
    assert "The room is empty" not in response.text


def test_the_delivery_log_says_the_room_is_dark(room, caplog):
    ctl = room["screen"].controller
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, dark=True)
    with caplog.at_level(logging.INFO):
        ctl._note("room-empty")
    assert "the room is dark, so the screen stays blank" in caplog.text and "room is empty" not in caplog.text


def test_undo_blank_in_a_dark_room_says_the_sensor_blanks_again(room):
    from test_web import csrf

    go_dark(room)
    browser = room["app"].test_client()
    response = browser.post("/screen/display/undo-blank", headers=auth(), data={"csrf": csrf(browser)},
                            follow_redirects=True)
    assert "The room is dark, so sensor control blanks the screen again." in response.text


def now_status(rig, awake, now):
    status = {"awake": awake, "playing": False, "paired": True, "known": True}
    with rig["app"].app_context():
        return sensor_web.now_status(rig["screen"], rig["screen"].configs.load(), status, False, now=now)


def test_the_now_page_names_the_hours_for_a_tv_off_in_the_dark(room):
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, tv_off=True, dark=True, blanked=True)
    inside = now_status(room, False, BASE)
    assert inside["overlay"]["text"] == "It turns on when the light is back and someone is there."
    outside = now_status(room, False, BASE + 16 * 3600)  # 00:00, after the 23:00 end
    assert outside["overlay"]["text"] == "It turns on when the light is back and someone comes in after 07:00."


def test_a_dark_dim_has_the_dim_tone(room):
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, dark=True, away=True)
    assert now_status(room, True, BASE)["overlay"]["tone"] == "dim"
    sensor_control.merge(room["screen"].library, sensor_control.CONTROL, blanked=True)
    assert now_status(room, True, BASE)["overlay"]["tone"] == "dark"
