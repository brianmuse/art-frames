"""Per-TV command queue and an in-memory activity record for the web UI.

Each TV gets one worker thread that runs its queued commands in order. A command
waits for a running scheduler pass instead of failing as busy. Nothing here is saved:
a restart drops the queue and the record.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from .controller import CALLER

log = logging.getLogger(__name__)
# A queued command waits this long for a scheduler pass, then fails.
JOB_WAIT = 10 * 60
QUEUE_LIMIT, HISTORY = 10, 100
# The controller's stage, in plain words.
STEPS = {
    "status": "Reading the TV status",
    "wake": "Waking the TV",
    "render": "Rendering the website",
    "publish": "Sending the image to the TV",
}
WAITING = "Waiting for another TV update to finish"
# Result data a job may add to its entry. A run can also say "skipped": nothing happened (asleep, busy).
EXTRA = ("pending", "revision")
BUSY_TEXT = "Another TV update ran for too long. Try again."


class QueueFull(ValueError):
    """This TV already has QUEUE_LIMIT commands waiting."""


def _now():
    return datetime.now().astimezone()


@dataclass(eq=False)
class Job:
    tv: str
    tv_name: str
    label: str
    key: tuple
    source: str = "you"
    run: object = None
    controller: object = None
    step_text: str = "Talking to the TV"
    staged: bool = False  # the controller's _stage describes the step
    place: str = "notice"  # where the result shows: a page notice or the Art Mode panel
    id: str = field(default_factory=lambda: secrets.token_urlsafe(9))
    state: str = "queued"
    locked: bool = False
    queued_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str = ""
    category: str = ""
    extra: dict = field(default_factory=dict)
    started: float = 0.0
    duration: float | None = None

    @property
    def finished(self):
        return self.state in {"done", "skipped", "failed", "cancelled"}

    def step(self):
        if self.state != "running":
            return ""
        if not self.locked:
            return WAITING
        if self.staged:
            return STEPS.get(getattr(self.controller, "_stage", ""), self.step_text)
        return self.step_text

    def public(self):
        """Only fixed labels, the TV's own name, times and result text; never addresses or tokens."""
        stamp = lambda value: value.isoformat(timespec="seconds") if value else None  # noqa: E731
        return {
            "id": self.id, "tv": self.tv, "tv_name": self.tv_name, "label": self.label, "source": self.source,
            "state": self.state, "step": self.step(), "queued_at": stamp(self.queued_at),
            "started_at": stamp(self.started_at), "finished_at": stamp(self.finished_at),
            "duration": self.duration, "message": self.message, "category": self.category,
            "place": self.place, **{key: self.extra[key] for key in EXTRA if key in self.extra},
        }


class Jobs:
    """mode "thread" runs each TV's queue on a daemon thread; "inline" runs a job as it is added;
    "manual" waits for drain(). Tests use the last two."""

    def __init__(self, *, mode="thread"):
        self.mode = mode
        self.cond = threading.Condition()
        self.queues, self.running, self.workers = {}, {}, {}
        self.passes = {}  # scheduler passes that hold a TV now
        self.history = deque(maxlen=HISTORY)
        self.describe = None  # web.py sets this: (kind, result, controller, failed) -> (text, category[, extra]) | None

    def submit(self, job):
        """Queue a job and return (job, added).

        A repeat of the last queued command is returned as is, but takes the new job's place, so
        its result shows where the latest click was. A command that differs from it is added even
        when an earlier entry matches, so on, off, on still ends on. A running job is never
        reused: it may have read the TV or the preview before this click.
        """
        with self.cond:
            queue = self.queues.setdefault(job.tv, deque())
            if queue and queue[-1].key == job.key:
                queue[-1].place = job.place
                return queue[-1], False
            if len(queue) >= QUEUE_LIMIT:
                raise QueueFull(f"This TV already has {QUEUE_LIMIT} commands waiting. Wait for one to finish, then try again.")
            queue.append(job)
            self.cond.notify_all()
            if self.mode == "thread" and job.tv not in self.workers:
                worker = threading.Thread(target=self._work, args=(job.tv,), name=f"art-frames-jobs-{job.tv[:8]}", daemon=True)
                self.workers[job.tv] = worker
                worker.start()
        if self.mode == "inline":
            self.drain(job.tv)
        return job, True

    def cancel(self, job_id):
        """Cancel a queued job. Returns False for a job that started, finished or is unknown."""
        with self.cond:
            for queue in self.queues.values():
                for job in queue:
                    if job.id == job_id:
                        queue.remove(job)
                        job.state, job.finished_at, job.category = "cancelled", _now(), "info"
                        job.message = "Cancelled before it started."
                        self.history.appendleft(job)
                        self.cond.notify_all()
                        return True
        return False

    def get(self, job_id):
        with self.cond:
            for job in [*self.running.values(), *self.passes.values(), *self.history]:
                if job.id == job_id:
                    return job
            for queue in self.queues.values():
                for job in queue:
                    if job.id == job_id:
                        return job
        return None

    def wait(self, job_id, timeout):
        """Block until a job finishes (for tests); returns whether it did."""
        with self.cond:
            return self.cond.wait_for(lambda: (job := self.get(job_id)) is not None and job.finished, timeout)

    def active(self):
        with self.cond:
            return len(self.running) + sum(len(queue) for queue in self.queues.values())  # your commands only

    def snapshot(self):
        with self.cond:
            running = sorted([*self.running.values(), *self.passes.values()], key=lambda job: job.started)
            queued = sorted((job for queue in self.queues.values() for job in queue), key=lambda job: job.queued_at)
            return {
                "running": [job.public() for job in running],
                "queued": [job.public() for job in queued],
                "recent": [job.public() for job in self.history],
            }

    def drain(self, tv=None):
        """Run queued jobs in this thread until the queue (one TV's, or every TV's) is empty."""
        while True:
            with self.cond:
                tvs = [tv] if tv else list(self.queues)
                ready = next((item for item in tvs if self.queues.get(item) and item not in self.running), None)
                if ready is None:
                    return
                job = self._take(ready)
            self._run(job)

    def _take(self, tv):
        job = self.queues[tv].popleft()
        job.state, job.started_at, job.started = "running", _now(), monotonic()
        self.running[tv] = job
        return job

    def _work(self, tv):
        while True:
            with self.cond:
                if not self.queues.get(tv):
                    del self.workers[tv]  # a later submit starts a new worker
                    return
                job = self._take(tv)
            self._run(job)

    def _run(self, job):
        def locked():
            job.locked = True
        CALLER.wait, CALLER.locked, CALLER.timed_out = JOB_WAIT, locked, False
        try:
            text, category, *extra = job.run()
        except Exception as error:
            log.warning("TV %s: queued command failed: %s", job.tv, type(error).__name__)
            text, category, extra = "That command could not be completed. Try again.", "error", []
        finally:
            timed_out = vars(CALLER).pop("timed_out", False)
            vars(CALLER).pop("wait", None), vars(CALLER).pop("locked", None)
        if timed_out:
            text, category = BUSY_TEXT, "error"
        extra = dict(extra[0]) if extra else {}
        skipped = extra.pop("skipped", False)
        with self.cond:
            job.message, job.category, job.extra = text, category, extra
            self._finish(job, "failed" if category == "error" else "skipped" if skipped else "done")
            self.running.pop(job.tv, None)
            self.cond.notify_all()

    def _finish(self, job, state):
        job.state, job.finished_at = state, _now()
        job.duration = round(monotonic() - job.started, 1) if job.started else None
        job.run = job.controller = None  # keep no TV objects in the record
        self.history.appendleft(job)

    def scheduled(self, tv, tv_name, controller, kind, label, fn):
        """Run one scheduler pass. It shows as running once it holds the TV, and stays in the
        record only when it did something notable."""
        entry = Job(tv, tv_name, label, ("schedule", kind), source="schedule", controller=controller, staged=True,
                    state="running")

        def locked():
            with self.cond:
                entry.locked, entry.started_at, entry.started = True, _now(), monotonic()
                self.passes[entry.id] = entry
        before = getattr(controller, "_reported", None)
        CALLER.locked = locked
        try:
            result = fn()
        finally:
            vars(CALLER).pop("locked", None)
            with self.cond:
                self.passes.pop(entry.id, None)
        reported = getattr(controller, "_reported", None)
        failed = result == "failed" and reported is not None and reported != before
        try:
            note = self.describe(kind, result, controller, failed) if self.describe and entry.locked else None
        except Exception as error:  # one TV's record must never stop another TV's pass
            log.warning("TV %s: scheduler pass could not be recorded: %s", tv, type(error).__name__)
            note = None
        if note:
            with self.cond:
                entry.message, entry.category, *extra = note
                skipped = bool(extra and extra[0].get("skipped"))
                self._finish(entry, "failed" if note[1] == "error" else "skipped" if skipped else "done")
                self.cond.notify_all()
        return result
