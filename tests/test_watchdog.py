"""The scheduler watchdog: a stuck pass logs every thread's stack and ends the process once."""

import logging
import threading

from art_frames import displays as displays_module
from art_frames.controller import Controller
from art_frames.displays import WATCHDOG_EXIT, WATCHDOG_LIMIT, Displays


def make(configs, library):
    clock = [1000.0]
    exits = []
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True,
                        clock=lambda: clock[0], exit_process=exits.append)
    return displays, clock, exits


def test_stuck_pass_logs_thread_stacks_and_exits_once(configs, library, caplog):
    displays, clock, exits = make(configs, library)
    entered, release = threading.Event(), threading.Event()

    def hang():
        entered.set()
        release.wait(10)
    displays.get("main").controller.run_wake = hang
    worker = threading.Thread(target=displays.tick, name="art-frames-displays", daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        assert displays.check_stuck() is False  # just started
        clock[0] += WATCHDOG_LIMIT - 1
        assert displays.check_stuck() is False
        clock[0] += 2
        with caplog.at_level(logging.ERROR, logger="art_frames.displays"):
            assert displays.check_stuck() is True
        assert exits == [WATCHDOG_EXIT]
        text = caplog.text
        assert "TV scheduler is stuck" in text and "the wake check of TV main" in text
        assert "Thread art-frames-displays:" in text and "in hang" in text
    finally:
        release.set()
        worker.join(5)
    assert displays.step_started is None  # the pass ended: nothing left to watch


def test_healthy_passes_never_exit(configs, library):
    displays, clock, exits = make(configs, library)
    for _ in range(5):
        displays.tick()
        clock[0] += WATCHDOG_LIMIT * 2  # a long quiet time between passes is normal
        assert displays.check_stuck() is False
    assert exits == []


def test_no_exit_while_stopping(configs, library):
    displays, clock, exits = make(configs, library)
    displays.step, displays.step_started = "the update of TV main", clock[0]
    clock[0] += WATCHDOG_LIMIT * 3
    displays.stopping.set()
    assert displays.check_stuck() is False
    assert exits == []


def test_watchdog_thread_checks_and_stops(configs, library, monkeypatch):
    monkeypatch.setattr(displays_module, "WATCHDOG_CHECK", 0.01)
    displays, clock, exits = make(configs, library)
    checked = threading.Event()
    displays.check_stuck = lambda: checked.set() or False
    displays.tick = lambda: None
    displays.start()
    try:
        assert checked.wait(5)
        assert displays.watchdog_thread.name == "art-frames-watchdog"
    finally:
        displays.stop()
    assert not displays.watchdog_thread.is_alive()
    assert exits == []


def test_thread_stacks_show_places_not_values():
    secret = "do-not-log-this-value"  # noqa: F841 - a local variable that must not appear
    text = displays_module.thread_stacks()
    assert "test_thread_stacks_show_places_not_values" in text
    assert "do-not-log-this-value" not in text
