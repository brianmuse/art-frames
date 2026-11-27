"""Gaps and flicker in the presence readings: the empty-room clock, the night's Sleep After, the pause on a
brightness change, the curve while the room is empty, and the log lines for each.

Every TV and sensor here is a fake: no test contacts a TV or a sensor.
"""
# ruff: noqa: F811

import logging
from datetime import UTC, datetime

from test_away_off import empty_until_blank, room, sleeps  # noqa: F401  (room is a fixture)
from test_presence_power import BASE, HOUR, PowerTV, at, blanked, control, tv  # noqa: F401  (tv is a fixture)
from test_sensors import Clock, CountingTV, changes, light, readings_for, rig  # noqa: F401  (rig is a fixture)

from art_frames import sensor_control, sensors
from art_frames.config import Sensor
from art_frames.television import DisplayError

# 1. A short unknown gap keeps the empty-room clock; a long one starts it again.


def test_a_short_unknown_gap_keeps_the_off_clock(room):
    empty_until_blank(room)  # empty from 60 s
    # The sensor reports no value from 29 to 32 min: shorter than UNKNOWN_GAP.
    for minute in (29, 30, 31):
        assert at(room, 60 + minute * 60, None) == "blanked"
    assert sleeps(room) == []  # the gap itself never fires the off step
    assert at(room, 60 + 32 * 60, False) == "tv_off"  # the clock kept counting through the gap
    assert sleeps(room) == ["5"]


def test_a_short_link_drop_keeps_the_off_clock(room):
    empty_until_blank(room)
    room["link"].readings.lost()  # the link drops: every reading is unknown
    for seconds in (20 * 60, 22 * 60):
        room["clock"].now = BASE + 60 + seconds
        room["status"]()
        assert room["run"]() == "stale"
    room["link"].readings.start("api")  # it comes back
    assert at(room, 60 + 30 * 60, False) == "tv_off"


def test_a_long_unknown_gap_starts_the_off_clock_again(room):
    empty_until_blank(room)
    for minute in range(10, 17):  # 6 min unknown: longer than UNKNOWN_GAP
        at(room, 60 + minute * 60, None)
    assert at(room, 60 + 17 * 60, False) == "blanked"  # empty again, counted from now
    assert at(room, 60 + 31 * 60, False) == "blanked" and sleeps(room) == []
    assert at(room, 60 + 47 * 60, False) == "tv_off"


def test_an_unknown_gap_starts_no_dim(tv):
    tv["enable"](away_after=2, blank_after=5)
    at(tv, 0, True)
    assert at(tv, 60, False) not in {"away", "blanked"}
    for seconds in (120, 180, 240):  # past the dim time, but presence unknown
        assert at(tv, 60 + seconds, None) not in {"away", "blanked"}
    assert control(tv).get("away") is not True
    assert at(tv, 60 + 245, False) == "away"  # the next "empty" reading dims at once


# 2. After the set hours, Sleep After waits for the room to settle.


def test_flicker_after_the_hours_writes_no_sleep_after_until_the_room_settles(tv):
    at(tv, 15 * HOUR - 60, True)
    moment = 15 * HOUR
    for empty, there in ((20, 10), (37, 15), (9, 20), (30, 5), (25, 30)):
        assert at(tv, moment, False) != "night"
        assert at(tv, moment + empty, False) != "night"
        moment += empty
        assert at(tv, moment, True) != "day"
        moment += there
    assert sleeps(tv) == []
    at(tv, moment, False)
    assert at(tv, moment + 100, False) != "night" and sleeps(tv) == []
    assert at(tv, moment + 120, False) == "night" and sleeps(tv) == ["5"]
    assert at(tv, moment + 125, True) == "day" and sleeps(tv) == ["5", "off"]  # someone back: off at once


# 3. A brightness change on the TV: only a fresh reading pauses control.


def stored_reading(rig, level):
    """An Art Mode reading in the library, newer than the last level Art Frames set."""
    library = rig["screen"].library
    moment = datetime.fromtimestamp(rig["clock"].now + 1, UTC).isoformat()
    library.set_state("art_display", {**library.state("art_display"), "brightness": level, "checked_at": moment})


def test_a_stale_stored_brightness_does_not_pause(rig, caplog):
    light(rig, 100.0)
    rig["run"]()  # reads the TV's range once
    sensor_control.merge(rig["screen"].library, sensor_control.CONTROL, sent=5, sent_at=rig["clock"].now)
    stored_reading(rig, 3)  # the TV still has 5
    del rig["calls"][:]
    with caplog.at_level(logging.INFO):
        assert rig["run"]() != "paused"
    assert ("read", None) in rig["calls"]  # it read the TV again first
    assert not sensor_control.sensor_curve.paused(control(rig), rig["clock"].now)
    assert "sensor control paused" not in caplog.text


def test_a_fresh_differing_brightness_still_pauses(rig, caplog):
    light(rig, 100.0)
    rig["run"]()
    sensor_control.merge(rig["screen"].library, sensor_control.CONTROL, sent=5, sent_at=rig["clock"].now)
    rig["display"]["brightness"] = 3  # someone used the remote
    stored_reading(rig, 3)
    with caplog.at_level(logging.INFO):
        assert rig["run"]() == "paused"
    assert "brightness changed on the TV to 3; sensor control paused" in caplog.text


# 4. The curve never brightens an empty room while the dim waits.


def test_the_curve_does_not_brighten_while_the_dim_waits(tv):
    tv["enable"](away_after=10, blank_after=15)
    tv["display"]["brightness"] = 2
    at(tv, 0, True, lux=5.0)
    del tv["calls"][:]
    for seconds in range(60, 9 * 60, 15):  # empty, and the room gets bright
        assert at(tv, seconds, False, lux=1000.0) not in {"brightness"}
    assert changes(tv) == [] and tv["display"]["brightness"] == 2


# 5. One log line for one undo.


def test_someone_back_logs_one_line(room, caplog):
    empty_until_blank(room)
    with caplog.at_level(logging.INFO):
        assert at(room, 60 + 6 * 60, True) == "back"
    assert caplog.text.count("someone is back") == 1
    assert "TV main: someone is back; blank and dim undone; brightness 7" in caplog.text


# Log lines: Sleep After failures and the sensor link.


def test_a_sleep_after_failure_logs_once_per_reason(tv, caplog):
    class Refusing(PowerTV):
        def set_option(self, field, value):
            self.calls.append((field, value))
            raise DisplayError("rejected")
    tv["screen"].controller.tv_factory = lambda cfg: Refusing(tv["display"], tv["calls"])
    at(tv, 15 * HOUR - 120, False)
    with caplog.at_level(logging.INFO):
        assert at(tv, 15 * HOUR, False) == "rejected"
        assert at(tv, 15 * HOUR + 400, False) == "rejected"
        assert at(tv, 15 * HOUR + 800, False) == "rejected"
    assert caplog.text.count("Sleep After not changed (rejected)") == 1


class Dropping:
    """A sensor client that connects and then loses the connection."""

    transport = "api"

    def run(self, readings, stopping):
        readings.start(self.transport)
        raise ConnectionError("lost")


def test_the_log_says_when_the_link_drops_and_comes_back(caplog):
    clock = Clock()
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[Dropping()], clock=clock, name="sensor-main")
    with caplog.at_level(logging.INFO):
        link.run_once()  # connects (no line: not a reconnect), then drops
        link.run_once()  # back, then drops again soon after
        clock.now += 60
        link.run_once()
        clock.now += sensors.LINK_LOG_GAP
        link.run_once()
    lines = [record.getMessage() for record in caplog.records if record.name == "art_frames.sensors"]
    assert lines == [
        "Sensor sensor-main: connection lost; reconnecting",
        "Sensor sensor-main: connection back (api)",
        "Sensor sensor-main: connection lost; reconnecting (2 more drops since the last line)",
    ]


# Review fixes: gap log lines, the TV's own brightness change, a re-enable.


def test_the_log_says_when_a_long_gap_starts_the_count_again_and_when_readings_come_back(room, caplog):
    empty_until_blank(room)
    with caplog.at_level(logging.INFO):
        for minute in range(10, 17):  # 6 min unknown
            at(room, 60 + minute * 60, None)
        at(room, 60 + 17 * 60, False)
    assert caplog.text.count("TV main: no presence reading for 5 min; the empty-room count starts again") == 1
    assert caplog.text.count("TV main: presence reading back after 420 s without one") == 1


def test_a_short_flicker_to_unknown_logs_nothing(room, caplog):
    empty_until_blank(room)
    with caplog.at_level(logging.INFO):
        at(room, 60 + 10 * 60, None)
        at(room, 60 + 10 * 60 + 20, False)
    assert "presence reading" not in caplog.text


class SettingDims(CountingTV):
    """A TV that lowers its own brightness when Art Frames changes a setting, like the Frame after Sleep After."""

    def set_option(self, field, value):
        self.display["brightness"] = 3
        return super().set_option(field, value)


def test_the_tv_changing_its_own_brightness_after_a_setting_change_is_no_pause(rig, caplog):
    screen = rig["screen"]
    screen.controller.tv_factory = lambda cfg: SettingDims(rig["display"], rig["calls"])
    rig["display"]["brightness"] = 7
    light(rig, 100.0)
    rig["run"]()
    sensor_control.merge(screen.library, sensor_control.CONTROL, sent=7, sent_at=rig["clock"].now)
    sensor_control.request_samsung(screen.library, "off")  # turns the light sensor and Sleep After off
    assert sensor_control.samsung_step(screen, screen.configs.load(), rig["memory"], rig["clock"].now) == "done"
    assert rig["display"]["brightness"] == 3  # the readback after the Sleep After change says 3
    rig["clock"].now += 5
    stored_reading(rig, 3)
    light(rig, 100.0)
    rig["status"]()
    with caplog.at_level(logging.INFO):
        assert rig["run"]() == "tv-changed"
    assert not sensor_control.sensor_curve.paused(control(rig), rig["clock"].now)
    assert "sensor control paused" not in caplog.text
    # The curve sets its level again on a later pass.
    readings_for(rig, 100.0, 180)
    assert rig["display"]["brightness"] == 7 and not sensor_control.sensor_curve.paused(control(rig),
                                                                                        rig["clock"].now)


def test_a_pause_logs_the_stored_and_the_fresh_level(rig, caplog):
    light(rig, 100.0)
    rig["run"]()
    sensor_control.merge(rig["screen"].library, sensor_control.CONTROL, sent=5, sent_at=rig["clock"].now)
    rig["display"]["brightness"] = 3
    stored_reading(rig, 2)
    with caplog.at_level(logging.INFO):
        assert rig["run"]() == "paused"
    assert "sensor control paused (stored reading 2, " in caplog.text and "; fresh reading 3)" in caplog.text


def test_a_failed_recheck_waits_before_the_next(rig, monkeypatch):
    light(rig, 100.0)
    rig["run"]()
    sensor_control.merge(rig["screen"].library, sensor_control.CONTROL, sent=5, sent_at=rig["clock"].now)
    stored_reading(rig, 3)

    def fails():
        raise DisplayError("not-art-mode")
    monkeypatch.setattr(rig["screen"].controller, "display_settings", fails)
    assert rig["run"]() == "not-art-mode"
    rig["clock"].now += 5
    assert rig["run"]() == "recheck-wait"
    rig["clock"].now += sensor_control.RECHECK_RETRY
    assert rig["run"]() == "not-art-mode"


def test_turning_control_off_forgets_an_unknown_gap(tv):
    tv["memory"].unknown_since = BASE - HOUR
    tv["screen"].configs.mutate(lambda raw: raw["sensor"].update(enabled=False))
    assert tv["run"]() == "off"
    assert tv["memory"].unknown_since is None
    tv["enable"]()
    assert at(tv, 15 * HOUR, None) != "night"  # the settle counts from now, not from the old gap
