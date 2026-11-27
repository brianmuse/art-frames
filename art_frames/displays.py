"""Shared content and a separate delivery context for each household TV."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass

from . import analytics
from .config import Config, Display, Television
from .controller import Controller

log = logging.getLogger(__name__)
# A pass can wait for wakes and eight renders; a healthy loop still finishes well inside this.
STALE_SECONDS = 30 * 60
# The rotation swap timer checks this often; a swap itself only selects an image already on the TV.
SWAP_TICK = 5
# The watchdog restarts Art Frames when one step of a pass (one TV's wake, update or sample) runs longer
# than this. Every step is bounded. A wake check is about 6 minutes at most. An update is at most about
# 26 to 28 minutes: it can run a full wake and a prepare render, then up to six layout renders (each up to
# 90 s waiting for the renderer and 35 s rendering) and a 300 s upload. A healthy step stays far below this.
WATCHDOG_LIMIT = 45 * 60
WATCHDOG_CHECK = 60  # seconds between watchdog checks
WATCHDOG_EXIT = 70  # the exit code after a stuck pass; Docker's restart policy starts Art Frames again


def thread_stacks():
    """Where each thread is now: file, line and function only, never local variables."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        lines = [f'  File "{item.filename}", line {item.lineno}, in {item.name}'
                 for item in traceback.extract_stack(frame, limit=None)]
        parts.append(f"Thread {names.get(ident, ident)}:\n" + "\n".join(lines))
    return "\n".join(parts)


class DisplayLibrary:
    # Shared catalog access only. Any state-reading method must be namespaced below.
    SHARED = frozenset({"add", "all", "get", "path_for", "presentation", "update", "root", "images", "path"})

    def __init__(self, library, display_id):
        self.library, self.display_id = library, display_id

    def __getattr__(self, name):
        if name not in self.SHARED:
            raise AttributeError(f"{name} is not shared between TVs")
        return getattr(self.library, name)

    def choose(self, day, order="shuffle"):
        return self.library.choose(day, order, display_id=self.display_id)

    def state(self, key, default=None):
        if key.startswith("art_revision:"):
            return self.library.state(key, default)
        return self.library.state(f"display:{self.display_id}:{key}", default)

    def set_state(self, key, value):
        self.library.set_state(f"display:{self.display_id}:{key}", value)

    def delete_state(self, key):
        self.library.delete_state(f"display:{self.display_id}:{key}")


class DisplayConfig:
    def __init__(self, shared, display_id):
        self.shared, self.id = shared, display_id
        self.root = shared.root / "displays" / display_id
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def load(self):
        cfg = self.shared.load()
        display = next((item for item in cfg.displays if item.id == self.id), None)
        if display is None:
            raise ValueError("TV no longer exists")
        cfg.tv, cfg.wake = display.tv.model_copy(deep=True), display.wake.model_copy(deep=True)
        cfg.site = (display.site or cfg.site).model_copy(deep=True)
        cfg.tv_name, cfg.mats = display.name, display.mats
        cfg.app_schedule = display.app_schedule.model_copy(deep=True)
        cfg.app_settings = {app: dict(values) for app, values in display.app_settings.items()}
        cfg.sensor = display.sensor.model_copy(deep=True)
        return cfg

    def mutate(self, change, *, inherit_site=None):
        """Change this TV's settings. inherit_site True links its website settings to the household's, False
        keeps its own. None keeps the link as it is: a linked TV stays linked, and a TV with its own settings
        keeps them, even when they equal the household's."""
        def update(raw):
            display = next(item for item in raw["displays"] if item["id"] == self.id)
            effective = Config.model_validate(raw).model_dump()
            effective.update(tv=display["tv"], wake=display["wake"], tv_name=display["name"],
                             mats=display["mats"], app_settings=display["app_settings"],
                             app_schedule=display["app_schedule"], sensor=display["sensor"])
            if display["site"] is not None:
                effective["site"] = display["site"]
            change(effective)
            validated = Config.model_validate({**effective, "displays": []})
            display.update(tv=validated.tv.model_dump(), wake=validated.wake.model_dump(), name=validated.tv_name,
                           mats=validated.mats, app_settings=validated.app_settings,
                           app_schedule=validated.app_schedule.model_dump(), sensor=validated.sensor.model_dump())
            follow_shared = display["site"] is None if inherit_site is None else inherit_site
            display["site"] = None if follow_shared else validated.site.model_dump()
            raw.update(timezone=validated.timezone, art=validated.art.model_dump())
        self.shared.mutate(update)
        return self.load()


@dataclass
class Screen:
    id: str
    configs: object
    library: object
    controller: Controller


class Displays:
    def __init__(self, configs, library, controller, *, demo=False, clock=time.monotonic, exit_process=os._exit):
        self.configs, self.library, self.demo = configs, library, demo
        self.screens = {"main": Screen("main", configs, library, controller)}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = self.swap_thread = self.watchdog_thread = None
        self.started = self.last_pass = None
        # The step the scheduler runs now, and when it started (self.clock()); None between passes.
        self.step, self.step_started = None, None
        # The watchdog ends the process with exit_process; tests pass their own, so it never ends the tests.
        self.clock, self.exit_process = clock, exit_process
        self.jobs = None  # web.py attaches the activity record; passes then show while they run

    def _pass(self, item, controller, kind, label, fn):
        if self.jobs is None:
            return fn()
        return self.jobs.scheduled(item["id"], item["name"], controller, kind, label, fn)

    def items(self):
        cfg = self.configs.load()
        return [{"id": "main", "name": cfg.tv_name}, *[{"id": item.id, "name": item.name} for item in cfg.displays]]

    def get(self, display_id):
        with self.lock:
            if display_id not in {item["id"] for item in self.items()}:
                raise ValueError("Unknown TV")
            if display_id not in self.screens:
                configs = DisplayConfig(self.configs, display_id)
                library = DisplayLibrary(self.library, display_id)
                self.screens[display_id] = Screen(display_id, configs, library, Controller(configs, library, demo=self.demo))
            return self.screens[display_id]

    def add(self, name, ip="", mac=""):
        display = Display(id=uuid.uuid4().hex, name=name.strip(), tv=Television(ip=ip.strip(), mac=mac.strip()))
        def update(raw):
            raw["displays"].append(display.model_dump())
        self.configs.mutate(update)
        return display.id

    def _begin(self, step):
        self.step, self.step_started = step, self.clock()

    def tick(self):
        try:
            self._tick()
        finally:
            self.step = self.step_started = None

    def _tick(self):
        controllers = []
        for item in self.items():
            try:
                controllers.append((item, self.get(item["id"]).controller))
            except ValueError:
                continue  # removed while this pass was starting
        # Wake checks are cheap and time-critical; a long render must not delay another TV's wake.
        for item, controller in controllers:
            if self.stopping.is_set():
                return
            self._begin(f"the wake check of TV {item['id']}")
            self._pass(item, controller, "wake", "Scheduled wake", controller.run_wake)
        # Sequential rendering bounds memory use on a small NAS; failures stay local.
        for item, controller in controllers:
            if self.stopping.is_set():
                return
            self._begin(f"the update of TV {item['id']}")
            self._pass(item, controller, "update", "Scheduled update", controller.run_once)
        for item, controller in controllers:  # read-only analytics sample; skipped while the TV is busy
            if not self.stopping.is_set():
                self._begin(f"the analytics sample of TV {item['id']}")
                analytics.sample_if_due(controller)
        self.last_pass = self.clock()

    @property
    def alive(self):
        """For health checks: None before start; else the thread runs and a pass finished recently."""
        if self.thread is None:
            return None
        return self.thread.is_alive() and self.clock() - (self.last_pass or self.started) < STALE_SECONDS

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as error:
                log.warning("TV scheduling could not complete (%s); retrying", type(error).__name__)
            self.stopping.wait(30)

    def check_stuck(self):
        """End the process when one scheduler step has run longer than WATCHDOG_LIMIT. Returns True then.

        It first logs where every thread is, so the log shows where the pass stopped. Docker then
        starts Art Frames again. It never acts while Art Frames stops.
        """
        step, started = self.step, self.step_started
        if started is None or self.stopping.is_set():
            return False
        seconds = self.clock() - started
        if seconds < WATCHDOG_LIMIT:
            return False
        log.error("The TV scheduler is stuck: %s has run for %d minutes. Art Frames will restart now.",
                  step, seconds // 60)
        log.error("Where each thread is now:\n%s", thread_stacks())
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        analytics.flush(timeout=2)  # save queued analytics rows; queued web jobs live in memory and are lost
        self.exit_process(WATCHDOG_EXIT)
        return True

    def _watch(self):
        while not self.stopping.wait(WATCHDOG_CHECK):
            try:
                if self.check_stuck():
                    return
            except Exception as error:
                log.warning("The scheduler watchdog could not check (%s)", type(error).__name__)

    def swap(self):
        """One swap timer tick: each TV with a due rotation shows its next layout's stored image.

        A TV that is busy (rendering, uploading, any other operation) is skipped until the next tick.
        """
        for item in self.items():
            if self.stopping.is_set():
                return
            try:
                self.get(item["id"]).controller.swap_if_due()
            except ValueError:
                continue  # removed while this tick was starting
            except Exception as error:
                log.warning("TV %s: rotation swap could not run (%s)", item["id"], type(error).__name__)

    def _swap_loop(self):
        while not self.stopping.wait(SWAP_TICK):
            try:
                self.swap()
            except Exception as error:
                log.warning("Rotation swaps could not run (%s); retrying", type(error).__name__)

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Schedulers already started")
        self.started = self.clock()
        self.thread = threading.Thread(target=self._loop, name="art-frames-displays", daemon=True)
        self.thread.start()
        # Its own thread, so a swap never waits for a render pass (and a render never waits for a swap).
        self.swap_thread = threading.Thread(target=self._swap_loop, name="art-frames-swaps", daemon=True)
        self.swap_thread.start()
        # A pass that never ends would stop every TV's schedule; this restarts Art Frames instead.
        self.watchdog_thread = threading.Thread(target=self._watch, name="art-frames-watchdog", daemon=True)
        self.watchdog_thread.start()

    def stop(self):
        self.stopping.set()
        for thread in (self.thread, self.swap_thread, self.watchdog_thread):
            if thread:
                thread.join(timeout=2)
        analytics.flush(timeout=2)  # save queued analytics rows before exit
