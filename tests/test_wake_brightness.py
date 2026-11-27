"""The one-shot curve step: when the screen comes on, or someone comes back, the brightness goes to the curve's
level for the room light now at once, without the waits of a light change.

The real case: a Frame came on in a dark room at brightness 2, the level from the night before, and
stayed there for about 3 minutes until the damping let it dim to 0.

Every TV here is a fake (DemoTelevision): no test contacts a TV or a sensor.
"""
# ruff: noqa: F811

from datetime import UTC, datetime

import pytest
from test_presence_power import BASE, HOUR, PowerTV, at, control, standby
from test_sensors import Clock, light, rig  # noqa: F401  (rig is a fixture)

from art_frames import sensor_control, sensor_curve, sensors
from art_frames.config import Sensor
from art_frames.television import Readback

DARK = 0.5  # lux: below the default curve's first point (1 lux), so the curve wants the TV's lowest level


class LevelTV(PowerTV):
    """A PowerTV that records the level of each blank undo, and turns on for a scheduled wake."""

    def undo_blank(self, content_id, brightness):
        self.calls.append(("undo-blank", brightness))
        return super(PowerTV, self).undo_blank(content_id, brightness)

    def wake(self, *, checks=6):
        self.calls.append(("scheduled-wake", None))
        self.display["power"] = "on"
        return True


@pytest.fixture
def tv(rig):
    """The rig with control on, the TV in standby at brightness 2 (last night's level) and a dark room."""
    screen = rig["screen"]
    screen.controller.tv_factory = lambda cfg: LevelTV(rig["display"], rig["calls"])
    rig["display"]["brightness"] = 2
    return rig


def levels(rig):
    return [call[1] for call in rig["calls"] if call[0] == "brightness"]


def arrive_in_the_dark(rig, lux=DARK):
    """The room is empty and dark while the TV is off, then someone comes in. Returns the result."""
    off = standby(rig)
    assert at(rig, 0, False, lux=lux, **off) == "standby"
    assert at(rig, 30, False, lux=lux, **off) == "standby"
    return at(rig, 60, True, lux=lux, **off)


# 1. A presence wake.


def test_a_presence_wake_in_a_dark_room_sets_the_curve_level_at_once(tv):
    assert arrive_in_the_dark(tv) == "woke"
    assert tv["display"]["power"] == "on"
    assert levels(tv) == [0]  # the first and only change: the curve's lowest level, not 3 minutes later
    wake = tv["calls"].index(("wake", None))
    assert tv["calls"].index(("brightness", 0)) > wake  # right after the screen came on, in the same wake
    state = control(tv)
    assert state["sent"] == 0 and state["sent_at"] == BASE + 60 and state["result"] == "brightness"


def test_the_presence_wake_uses_the_readings_from_before_the_screen_came_on(tv):
    link = tv["link"]
    assert sensor_control.room_light(link, Sensor(), BASE) is None  # no light reading yet: unknown
    clock = tv["clock"]
    for seconds, lux in ((0, 0.4), (20, 0.5), (40, 0.6)):  # dark while the TV is off
        clock.now = BASE + seconds
        link.readings.update("sensor", "ambient_light", lux)
    clock.now = BASE + 50
    link.readings.update("sensor", "ambient_light", 40.0)  # the screen's own light after the wake
    assert sensor_control.room_light(link, Sensor(), BASE + 50, until=BASE + 45) == 0.5
    assert sensor_control.room_light(link, Sensor(), BASE + 50) == pytest.approx(0.55)  # all 4 readings


def test_without_a_reading_in_the_last_2_minutes_the_newest_reading_counts(tv):
    off = standby(tv)
    at(tv, 0, False, lux=DARK, **off)  # the last light reading: a steady dark room sends no more
    clock = tv["clock"]
    clock.now = BASE + 600
    tv["status"](**off)
    tv["link"].readings.update("binary_sensor", "presence", False)
    assert tv["run"]() == "standby"
    clock.now = BASE + 610
    tv["status"](**off)
    tv["link"].readings.update("binary_sensor", "presence", True)
    assert sensor_curve.median_light(tv["link"].readings.samples("ambient_light"), clock.now) is None
    assert tv["run"]() == "woke"
    assert levels(tv) == [0]


def test_the_step_respects_the_darker_brighter_offset_and_the_points(tv):
    tv["enable"](curve={"points": [{"lux": 1.0, "level": 3}, {"lux": 1000.0, "level": 9}], "offset": 1})
    assert arrive_in_the_dark(tv) == "woke"
    assert levels(tv) == [4]  # the dark point (3) plus one step


def test_a_pause_blocks_the_step(tv):
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, paused_until=BASE + HOUR, paused_at=BASE)
    assert arrive_in_the_dark(tv) == "woke"
    assert levels(tv) == [] and tv["display"]["brightness"] == 2


def test_a_level_set_by_hand_before_the_wake_pauses_control(tv):
    # Art Frames set 4 last night; someone set 2 on the remote, then turned the TV off.
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, sent=4, sent_at=BASE - HOUR)
    assert arrive_in_the_dark(tv) == "woke"
    assert levels(tv) == [] and sensor_curve.paused(control(tv), BASE + 61)


def test_no_light_reading_changes_nothing(rig):
    rig["screen"].controller.tv_factory = lambda cfg: LevelTV(rig["display"], rig["calls"])
    rig["display"]["brightness"] = 2
    clock = Clock()
    clock.now = BASE
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[], clock=clock)
    link.readings.start("api", {"project_name": "Akamatis.SmartHomeDevice"},
                        rig["link"].readings.snapshot()["entities"])
    memory = sensor_control.Memory()
    screen = rig["screen"]
    off = standby(rig)
    for seconds, presence in ((0, False), (60, True)):
        clock.now = rig["clock"].now = BASE + seconds
        rig["status"](**off)
        link.readings.update("binary_sensor", "presence", presence)  # presence, but never a light reading
        result = sensor_control.step(screen, screen.configs.load(), link, memory, clock.now)
    assert result == "woke"
    assert levels(rig) == [] and rig["display"]["brightness"] == 2


def test_a_wake_in_tv_mode_changes_nothing(tv):
    tv["display"]["wake_result"] = "tv-mode"
    assert arrive_in_the_dark(tv) == "watching"
    assert levels(tv) == []


def first_change(rig, lux, seconds):
    """A reading every 5 s after the wake (at BASE + 60). Returns (seconds after the wake, the results before)."""
    results = []
    for after in range(5, seconds + 1, 5):
        result = at(rig, 60 + after, True, lux=lux)
        if result == "brightness":
            return after, results
        results.append(result)
    raise AssertionError(f"no change in {seconds} s: {results}")


def test_damping_still_applies_to_the_next_brighter_change(tv):
    assert arrive_in_the_dark(tv) == "woke"
    after, before = first_change(tv, 1000.0, 300)
    # The 2-minute median, 2 readings, 30 s and a minute since the wake's own change.
    assert after >= sensor_curve.MIN_GAP and "wait" in before
    assert levels(tv) == [0, 10]


def test_damping_still_applies_to_the_next_darker_change(tv):
    assert arrive_in_the_dark(tv, lux=1000.0) == "woke"
    assert levels(tv) == [10]
    after, before = first_change(tv, DARK, 600)
    assert after >= sensor_curve.DOWN_SECONDS and "wait" in before  # dim slowly, as before
    assert levels(tv) == [10, 0]


# 2. Someone back: the undo of the blank and of the dim.


def test_undo_blank_goes_to_the_curve_level_not_the_level_from_before(tv):
    tv["enable"](blank_after=5)
    tv["display"]["brightness"] = 5
    tv["screen"].library.set_state(sensor_control.CONTROL, {"sent": 5, "sent_at": BASE - HOUR})
    at(tv, 0, True, lux=DARK)
    at(tv, 60, False, lux=DARK)  # dim
    assert at(tv, 60 + 5 * 60, False, lux=DARK) == "blanked"
    assert tv["screen"].controller.blank_record()["brightness"] == 0  # the dim's level, saved by the blank
    at(tv, 60 + 5 * 60 + 2, False, lux=100.0)  # a lamp comes on
    at(tv, 60 + 5 * 60 + 4, False, lux=100.0)
    del tv["calls"][:]
    assert at(tv, 60 + 5 * 60 + 5, True, lux=100.0) == "back"
    assert tv["calls"][0] == ("undo-blank", 7)  # the curve's level for 100 lux, not the saved 0
    assert levels(tv) == []  # the dim's undo finds the TV at that level already
    assert control(tv)["sent"] == 7


def test_undo_dim_uses_the_newest_reading_when_none_is_in_the_last_2_minutes(tv):
    tv["enable"](blank_after=60)
    tv["display"]["brightness"] = 10
    at(tv, 0, True, lux=1000.0)
    assert at(tv, 60, False, lux=1000.0) == "away"  # dimmed to 0, before_away 10
    at(tv, 120, False, lux=30.0)  # dusk: the last light reading
    clock = tv["clock"]
    clock.now = BASE + 600
    tv["status"]()
    tv["link"].readings.update("binary_sensor", "presence", True)
    del tv["calls"][:]
    assert tv["run"]() == "back"
    want = sensor_curve.target(30.0, tv["screen"].configs.load().sensor.curve, 0, 10)
    assert levels(tv) == [want] and want != 10


def test_a_pause_keeps_the_level_from_before_the_blank(tv):
    tv["enable"](blank_after=5)
    tv["display"]["brightness"] = 5
    at(tv, 0, True, lux=DARK)
    at(tv, 60, False, lux=DARK)
    assert at(tv, 60 + 5 * 60, False, lux=DARK) == "blanked"
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, paused_until=BASE + 10 * HOUR)
    del tv["calls"][:]
    assert at(tv, 60 + 5 * 60 + 5, True, lux=100.0) == "back"
    assert tv["calls"][0] == ("undo-blank", 0)  # the saved level from before the blank
    assert levels(tv) == [5]  # the dim's undo: the level from before the dim


# 3. The scheduled wake.


def scheduled(rig):
    """Turn the scheduled wake on at 08:00 (BASE), with the TV in standby and a dark room seen by the sensor."""
    def change(raw):
        # Documentation addresses: the fake TV never uses them.
        raw["tv"].update(ip="192.0.2.10", mac="02:00:00:00:00:01")
        raw["wake"].update(enabled=True, at="08:00", grace_minutes=5)
    rig["screen"].configs.mutate(change)
    off = standby(rig)
    at(rig, -60, False, lux=DARK, **off)  # a pass of sensor control sets Controller.curve_step
    rig["clock"].now = BASE + 10
    del rig["calls"][:]


def test_the_scheduled_wake_gets_the_same_step(tv):
    scheduled(tv)
    tv["screen"].controller.wake_if_due(now=datetime.fromtimestamp(BASE + 10, UTC))
    assert tv["calls"][0] == ("scheduled-wake", None)
    assert levels(tv) == [0]
    assert control(tv)["sent"] == 0


def test_the_scheduled_wake_without_sensor_control_changes_nothing(tv):
    scheduled(tv)
    tv["screen"].controller.curve_step = None  # control off: the pass sets no step
    tv["screen"].controller.wake_if_due(now=datetime.fromtimestamp(BASE + 10, UTC))
    assert ("scheduled-wake", None) in tv["calls"] and levels(tv) == []


def test_the_scheduled_wake_skips_the_step_while_paused(tv):
    scheduled(tv)
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, paused_until=BASE + HOUR)
    tv["screen"].controller.wake_if_due(now=datetime.fromtimestamp(BASE + 10, UTC))
    assert levels(tv) == []


def test_the_scheduled_wake_undoes_the_sensor_blank_at_the_curve_level(tv):
    tv["display"]["power"] = "on"
    tv["display"]["brightness"] = 5
    at(tv, 0, True, lux=DARK)  # a pass sets Controller.curve_step
    ctl = tv["screen"].controller
    ctl.soft_blank(owner="sensor")
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, blanked=True)
    cfg = tv["screen"].configs.load()
    device = ctl._identity(cfg)
    held = ctl.blank_record(device)
    assert held["brightness"] == 5
    del tv["calls"][:]
    assert ctl._undo_sensor_blank(LevelTV(tv["display"], tv["calls"]), device, cfg, held) is True
    assert tv["calls"] == [("undo-blank", 0)]  # dark room: the curve's lowest level, not the saved 5
    assert control(tv)["sent"] == 0


# Review fixes.


def test_the_newest_reading_fallback_ignores_the_screens_own_light(tv):
    link, clock = tv["link"], tv["clock"]
    clock.now = BASE - 600
    link.readings.update("sensor", "ambient_light", 0.4)  # a steady dark room: the last reading is old
    clock.now = BASE + 48
    link.readings.update("sensor", "ambient_light", 40.0)  # the screen's glow, after the wake began at BASE + 45
    assert sensor_control.room_light(link, Sensor(), BASE + 50, until=BASE + 45) == 0.4


class RoundingTV(LevelTV):
    """A TV that sets one level below the one asked for."""

    def set_brightness(self, value):
        before = self._settings()
        self.calls.append(("brightness", value))
        self.display["brightness"] = value - 1
        return Readback(value, value - 1, before["brightness"], True, before)


def test_a_rounding_tv_is_not_asked_again_at_each_wake(tv):
    tv["screen"].controller.tv_factory = lambda cfg: RoundingTV(tv["display"], tv["calls"])
    at(tv, 0, True, lux=1000.0)  # a pass sets Controller.curve_step
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, sent=9, sent_at=BASE, asked=10, got=9,
                         unconfirmed=0)
    tv["display"]["brightness"] = 9
    ctl = tv["screen"].controller
    for _ in range(3):
        tv["display"]["power"] = "standby"
        ctl.presence_wake()
    assert levels(tv) == []  # the TV sets 9 for 10, as seen before
    assert control(tv)["unconfirmed"] < sensor_control.MAX_UNCONFIRMED


def test_the_scheduled_wake_keeps_the_dim_in_an_empty_room(tv):
    tv["enable"](blank_after=240)
    tv["display"]["power"] = "on"
    tv["display"]["brightness"] = 8
    at(tv, -400, True, lux=1000.0)
    assert at(tv, -300, False, lux=1000.0) == "away"  # dimmed to 0
    scheduled(tv)
    tv["clock"].now = BASE + 5
    tv["link"].readings.update("sensor", "ambient_light", 1000.0)  # morning daylight, the room still empty
    tv["clock"].now = BASE + 10
    tv["screen"].controller.wake_if_due(now=datetime.fromtimestamp(BASE + 10, UTC))
    assert control(tv)["away"] is True and tv["display"]["brightness"] == 0 and levels(tv) == []


class WakeDuringReadTV(LevelTV):
    """A TV whose next settings read happens while a wake's curve step sets 8 (it runs on its own thread)."""

    armed = None

    def display_settings(self):
        if WakeDuringReadTV.armed is not None:
            library, now = WakeDuringReadTV.armed
            WakeDuringReadTV.armed = None
            self.display["brightness"] = 8
            sensor_control.merge(library, sensor_control.CONTROL, sent=8, sent_at=now, unconfirmed=0)
        return super().display_settings()


def test_a_wake_step_during_the_read_before_a_change_is_no_change_by_hand(tv):
    tv["screen"].controller.tv_factory = lambda cfg: WakeDuringReadTV(tv["display"], tv["calls"])
    tv["display"]["power"] = "on"
    tv["display"]["brightness"] = 5
    sensor_control.merge(tv["screen"].library, sensor_control.CONTROL, sent=5, sent_at=BASE - HOUR)
    results = []
    for seconds in range(0, 60, 15):
        if seconds == 30:  # the pass that changes the level reads the TV first
            WakeDuringReadTV.armed = (tv["screen"].library, BASE + seconds)
        results.append(at(tv, seconds, True, lux=1000.0))
    assert results[2] == "brightness" and "paused" not in results
    assert not sensor_curve.paused(control(tv), BASE + 60) and control(tv)["before"] == 8
