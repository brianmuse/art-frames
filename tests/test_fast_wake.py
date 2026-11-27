"""A faster presence wake: the hub acts on an arrival at once, the TV gets one probe before the key, the
check for Art Mode after the key runs often at first, and each wake logs where its time went.

Every TV here is a fake: no test contacts a TV or a sensor.
"""
# ruff: noqa: F811

import logging
import re
import threading
import time
from types import SimpleNamespace

from test_presence_power import BASE, at, standby
from test_screen_power import IP, FakeTV, make
from test_sensors import rig  # noqa: F401  (rig is a fixture)
from test_wake_brightness import tv  # noqa: F401  (tv is a fixture)

from art_frames import sensor_control

# The hub acts on an arrival at once.


def test_only_a_turn_to_present_counts_as_an_arrival(tv):
    readings, arrivals = tv["link"].readings, []
    readings.on_arrival = lambda object_id: arrivals.append((object_id, readings.clock()))
    readings.update("binary_sensor", "presence", False)
    tv["clock"].now = BASE + 7
    readings.update("binary_sensor", "presence", True)
    readings.update("binary_sensor", "presence", True)  # still there: no new arrival
    readings.update("sensor", "ambient_light", 3.0)
    assert arrivals == [("presence", BASE + 7)] and readings.arrival("presence") == BASE + 7


def test_an_arrival_wakes_the_hub_without_waiting_a_tick(tv, monkeypatch):
    monkeypatch.setattr(sensor_control, "TICK", 60)  # far longer than the test waits
    hub = sensor_control.SensorHub(tv["app"].extensions["displays"], discovery=object(),
                                   link_factory=lambda *args: tv["link"])
    hub.reconcile()
    ticked = threading.Event()
    monkeypatch.setattr(hub, "tick", lambda now=None: ticked.set())
    tv["link"].readings.update("binary_sensor", "presence", False)
    hub.start()
    try:
        assert not ticked.wait(0.3)  # no arrival: the loop waits for TICK
        tv["link"].readings.update("binary_sensor", "presence", True)
        assert ticked.wait(2)  # at once, not after 60 s
    finally:
        hub.stop()


# One probe before the key.


class TracedTV(FakeTV):
    """A FakeTV that records each REST read, each art channel and each key, in order."""

    def __init__(self, states, clock=None):
        super().__init__(states)
        self.events, self.clock = [], clock

    def remote(self, **kwargs):
        device = super().remote(**kwargs)
        info, art, key = device.rest_device_info, device.art, device.send_key

        def traced(name, call):
            def run(*args, **kwargs):
                self.events.append((name, self.clock.now if self.clock else None))
                return call(*args, **kwargs)
            return run
        return SimpleNamespace(**{**vars(device), "rest_device_info": traced("info", info),
                                  "art": traced("art", art), "send_key": traced("key", key)})


def traced(tmp_path, monkeypatch, states):
    device, _, clock, wakes = make(tmp_path, monkeypatch, states)
    fake = TracedTV(states, clock)
    monkeypatch.setattr(device, "remote", fake.remote)
    return device, fake, clock, wakes


def test_an_answering_tv_gets_one_probe_and_the_recheck_before_the_key(tmp_path, monkeypatch):
    states = [("standby", "on")] * 3 + [("on", "on")]
    device, fake, clock, wakes = traced(tmp_path, monkeypatch, states)
    result = device.presence_wake()
    assert result.result == "confirmed" and not wakes
    names = [name for name, _ in fake.events]
    before_key = names[:names.index("key")]
    # The probe reads the power state only (no art channel), then the key's connection reads it again.
    assert before_key == ["info", "info"] and names.count("key") == 1
    # The times on the fake clock: nothing waits before the key, and the checks after it run every 0.5 s.
    assert result.key_at == 0.0 and result.confirmed_at == 1.0
    assert [at for name, at in fake.events if name == "info"] == [0.0, 0.0, 0.5, 1.0, 1.0]
    # The checks read the power state alone; the art channel opens once, when the power reads on.
    assert [at for name, at in fake.events if name == "art"] == [1.0]


def test_the_recheck_before_the_key_still_stops_a_tv_that_came_on(tmp_path, monkeypatch):
    device, fake, _, _ = traced(tmp_path, monkeypatch, [("standby", "on"), ("on", "on")])
    result = None
    try:
        result = device.presence_wake()
    except Exception as error:
        assert getattr(error, "code", None) == "screen-is-on"
    assert result is None and "key" not in [name for name, _ in fake.events]


# The timing log line.


def test_each_presence_wake_logs_where_the_time_went(tv, caplog):
    caplog.set_level(logging.INFO, logger="art_frames")
    off = standby(tv)
    at(tv, 0, False, lux=0.5, **off)
    tv["clock"].now = BASE + 60
    tv["link"].readings.update("binary_sensor", "presence", True)  # the sensor event arrives
    tv["clock"].now = BASE + 62  # the pass runs 2 s later
    tv["status"](**off)
    assert tv["run"]() == "woke"
    line = re.search(r"screen on for someone: arrival seen \+0\.0 s, wake started \+(\S+) s, key sent \+(\S+) s, "
                     r"Art Mode confirmed \+(\S+) s", caplog.text)
    assert line, caplog.text
    started, key, confirmed = (float(value) for value in line.groups())
    assert 2.0 <= started <= key <= confirmed < 3.0
    assert "AA:BB" not in caplog.text and IP not in caplog.text


def test_the_recheck_on_the_keys_connection_that_fails_sends_no_key(tmp_path, monkeypatch):
    # The probe sees standby; the recheck on the key's connection gets no answer (it raises).
    device, fake, _, wakes = traced(tmp_path, monkeypatch, [("standby", "on"), None])
    sent = []
    monkeypatch.setattr(device, "_send_magic", lambda: sent.append(1) or 1)
    result = device.presence_wake()
    assert result.result == "not-confirmed" and result.key_at is None
    assert [name for name, _ in fake.events] == ["info", "info"] and not fake.keys and not sent and not wakes


def test_a_tv_that_reaches_art_mode_by_itself_after_the_signal_gets_no_key(tmp_path, monkeypatch):
    # No answer, the wake signal, standby at 1 s and 2 s, then Art Mode at 3 s: shorter than STANDBY_DWELL.
    device, fake, clock, _ = traced(tmp_path, monkeypatch, [None, ("standby", "on"), ("standby", "on"), ("on", "on")])
    sent = []
    monkeypatch.setattr(device, "_send_magic", lambda: sent.append(1) or 1)
    result = device.presence_wake()
    assert result.result == "confirmed" and result.method == "wake signal"
    assert not fake.keys and len(sent) == 1 and clock.now == 3


def hub_for(tv, monkeypatch):
    """A started hub with the rig's link, TICK far longer than the tests wait, and tick() recording its times."""
    monkeypatch.setattr(sensor_control, "TICK", 60)
    hub = sensor_control.SensorHub(tv["app"].extensions["displays"], discovery=object(),
                                   link_factory=lambda *args: tv["link"])
    hub.reconcile()
    ticks, ticked = [], threading.Event()
    monkeypatch.setattr(hub, "tick", lambda now=None: ticks.append(time.monotonic()) or ticked.set())
    readings = tv["link"].readings
    for object_id in ("presence", "moving_target"):
        readings.update("binary_sensor", object_id, False)
    hub.start()
    return hub, ticks, ticked


def test_only_the_presence_sensor_that_control_uses_wakes_the_hub(tv, monkeypatch):
    hub, _, ticked = hub_for(tv, monkeypatch)
    try:
        tv["link"].readings.update("binary_sensor", "moving_target", True)  # another binary sensor
        assert not ticked.wait(0.4)
        tv["link"].readings.update("binary_sensor", "presence", True)
        assert ticked.wait(2)
    finally:
        hub.stop()


def test_arrivals_start_at_most_one_pass_a_second(tv, monkeypatch):
    hub, ticks, ticked = hub_for(tv, monkeypatch)
    readings = tv["link"].readings
    try:
        for _ in range(5):  # a flickering presence sensor: five arrivals in half a second
            readings.update("binary_sensor", "presence", True)
            readings.update("binary_sensor", "presence", False)
            time.sleep(0.1)
        assert ticked.wait(2)
        time.sleep(1.2)
    finally:
        hub.stop()
    assert 1 <= len(ticks) <= 2
    assert all(later - earlier >= 0.95 for earlier, later in zip(ticks, ticks[1:], strict=False))
