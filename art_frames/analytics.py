"""Per-TV analytics: read-only samples, state changes and reliability events in SQLite.

Hooks in the controller and the scheduler call the record_* functions and
sample_if_due. They never raise, and writes run on one background thread, so
a web request or a scheduler pass never waits for the database.
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import math
import os
import queue
import re
import sqlite3
import statistics
import threading
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as clock_time
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from .television import MOTION_TIMER_CHOICES, OPTION_SETTERS, SLEEP_AFTER_MINUTES

log = logging.getLogger(__name__)

FILE = "analytics.sqlite"
SAMPLE_SECONDS = 15 * 60
SAMPLE_BUDGET = 10  # seconds; a sample starts no new TV read after this
KEEP_DAYS = 365
PRUNE_SECONDS = 24 * 3600
# One failure of the same stage is recorded at most this often; a stuck stage fails every pass.
FAILURE_GAP = 5 * 60
# "deep": a paired TV that answered before stops answering (deep standby, network off).
# "unknown": the TV never answered, answered without a clear state, is unpaired,
# or Art Frames itself was not running.
STATES = ("awake", "playing", "standby", "deep", "unknown")
KNOWN_STATES = ("awake", "playing", "standby")
POWER = {"on", "standby", "off"}
UNPAIRED = "unpaired"  # the power column of a sample taken while the TV was not paired
CONTENT_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
# Only these columns are ever stored. TV device info also holds the IP, MAC address,
# network name, unique IDs and the TV's name; none of them reach this file.
SAMPLE_FIELDS = ("ts", "answered", "latency_ms", "power", "art_mode", "playing",
                 "brightness", "sensor", "color_temperature", "content_id")
WAKE_TEXT = (("Wake confirmed", "confirmed"), ("Wake not confirmed", "late"),
             ("Wake was attempted", "failed"), ("Wake skipped", "skipped"))
# The only setting events stored, as "field:outcome".
SETTING_FIELDS = frozenset(OPTION_SETTERS)
SETTING_OUTCOMES = frozenset({"done", "not-done", "busy", "failed", "unpaired", "not-art-mode", "rejected",
                              "invalid", "not-reported", "out-of-range"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples (
  ts REAL NOT NULL, answered INTEGER NOT NULL, latency_ms INTEGER, power TEXT NOT NULL,
  art_mode INTEGER, playing INTEGER, brightness INTEGER, sensor INTEGER,
  color_temperature INTEGER, content_id TEXT);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);
CREATE TABLE IF NOT EXISTS transitions (ts REAL NOT NULL, state TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS transitions_ts ON transitions (ts);
CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, kind TEXT NOT NULL, outcome TEXT NOT NULL, latency_ms INTEGER);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
"""


class Store:
    """One TV's analytics file. Writes run only on the writer thread."""

    def __init__(self, path: Path, offset=0):
        self.path = path
        self.offset = offset  # seconds; staggers this TV's 15-minute slots
        self.last_state = None  # None: not read from the file yet in this process
        self.ever_known = None  # True once the TV has answered; None: not read from the file yet
        self.last_prune = 0.0
        self.failures = {}
        self.sampled_slot = None
        self.broken = False  # a damaged file is logged once, not on every write
        self.marked = False  # True once this process has stored the 'deep_since' marker
        self.earliest = None  # the earliest time this process wrote a transition, until the marker is stored

    def connect(self):
        if self.path.exists():
            os.chmod(self.path, 0o600)
        else:
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript(SCHEMA)
        now = time.time()
        db.execute("INSERT OR IGNORE INTO meta VALUES ('started', ?)", (repr(now),))
        return db

    def note(self, at):
        """Record a time this process wrote a transition for, so the marker is no later than it."""
        if not self.marked:
            self.earliest = at if self.earliest is None else min(self.earliest, at)

    def _mark(self, db):
        """Store 'deep_since' once: the earliest of now, this process's first transition and the
        first stored "deep" span. From that time on, a no-answer span is stored as "deep" or
        "unknown" as it is; only spans stored before it are resolved when read (see resolve)."""
        if self.marked:
            return
        first_deep = db.execute("SELECT MIN(ts) FROM transitions WHERE state = 'deep'").fetchone()[0]
        since = min(value for value in (time.time(), first_deep, self.earliest) if value is not None)
        db.execute("INSERT OR IGNORE INTO meta VALUES ('deep_since', ?)", (repr(since),))
        self.marked = True

    def write(self, fn):
        try:
            with closing(self.connect()) as db, db:
                fn(db)
                self._mark(db)  # after fn, which notes the times it wrote
                now = time.time()
                if now - self.last_prune >= PRUNE_SECONDS:
                    prune(db, now)
                    self.last_prune = now
        except sqlite3.DatabaseError as error:
            if not self.broken:
                log.warning("Analytics file %s could not be written (%s)", self.path.parent.name, type(error).__name__)
            self.broken = True
            return
        if self.broken:
            log.info("Analytics file %s can be written again", self.path.parent.name)
            self.broken = False


_stores: dict[str, Store] = {}
_stores_lock = threading.Lock()
_jobs: queue.Queue = queue.Queue(maxsize=1000)
_writer = None


def slot_offset(name):
    """A stable offset per TV, so TVs do not all sample in the same scheduler pass."""
    return int(hashlib.sha256(str(name).encode()).hexdigest(), 16) % SAMPLE_SECONDS


def store(root, name="main") -> Store:
    path = Path(root) / FILE
    with _stores_lock:
        return _stores.setdefault(str(path), Store(path, slot_offset(name)))


def _work():
    while True:
        job = _jobs.get()
        try:
            job()
        except Exception as error:
            log.warning("Analytics could not be saved (%s)", type(error).__name__)
        finally:
            _jobs.task_done()


def _submit(job):
    global _writer
    with _stores_lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_work, name="art-frames-analytics", daemon=True)
            _writer.start()
    try:
        _jobs.put_nowait(job)
    except queue.Full:
        pass  # analytics never slows the TV down; drop the row


def flush(timeout=None):
    """Wait for queued writes, at most timeout seconds. Returns True when the queue is empty."""
    deadline = None if timeout is None else time.monotonic() + timeout
    with _jobs.all_tasks_done:
        while _jobs.unfinished_tasks:
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            _jobs.all_tasks_done.wait(left)
    return True


def prune(db, now):
    """Keep one year. The last state change before the cutoff stays, so the state at the cutoff is known."""
    cutoff = now - KEEP_DAYS * 86400
    db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM transitions WHERE ts < (SELECT MAX(ts) FROM transitions WHERE ts < ?)", (cutoff,))


def _store(controller):
    return store(controller.configs.root, getattr(controller.configs, "id", "main"))


# --- Hooks. Each one is a single line at its call site and never raises. ---

def state_of(status):
    """The state for one status read. A paired TV that does not answer is "deep";
    _transition turns that into "unknown" until the TV has answered once. A TV that
    answers but gives no clear state stays "unknown": it is on the network."""
    if status.get("playing"):
        return "playing"
    if status.get("awake"):
        return "awake"
    if not status.get("paired"):
        return "unknown"
    if status.get("known"):
        return "standby"
    return "unknown" if status.get("answered") else "deep"


def _first_known(db):
    """When this file first shows the TV answered, or None."""
    marks = ", ".join("?" * len(KNOWN_STATES))
    values = [db.execute("SELECT MIN(CAST(value AS REAL)) FROM meta WHERE key = 'known'").fetchone()[0],
              db.execute(f"SELECT MIN(ts) FROM transitions WHERE state IN ({marks})", KNOWN_STATES).fetchone()[0],
              db.execute("SELECT MIN(ts) FROM samples WHERE answered = 1").fetchone()[0]]
    values = [value for value in values if value is not None]
    return min(values) if values else None


def _ever_known(db):
    """True when this file shows the TV answered at least once."""
    return _first_known(db) is not None


def _mark_known(target, db, at):
    if not target.ever_known:
        db.execute("INSERT OR IGNORE INTO meta VALUES ('known', ?)", (repr(at),))
        target.ever_known = True


def record_status(controller, status, *, at=None):
    """Called from Controller._record_status about every 30 s; stores only changes."""
    try:
        target, state, at = _store(controller), state_of(status), time.time() if at is None else at
        _submit(lambda: target.write(lambda db: _transition(target, db, state, at)))
    except Exception as error:
        log.warning("Analytics status hook failed (%s)", type(error).__name__)


def _transition(target, db, state, at):
    target.note(at)
    if target.ever_known is None:
        target.ever_known = _ever_known(db)
    if state in KNOWN_STATES:
        _mark_known(target, db, at)
    elif state == "deep" and not target.ever_known:
        state = "unknown"  # a TV that never answered is unknown, not asleep
    if target.last_state is None:
        row = db.execute("SELECT ts, state FROM transitions ORDER BY ts DESC LIMIT 1").fetchone()
        target.last_state = row[1] if row else ""
        # After a restart, the time the host was down is unknown, not the last state.
        seen = db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        heartbeat = max(row[0] if row else 0, seen or 0)
        if row and target.last_state != "unknown" and at - heartbeat > SAMPLE_SECONDS + 300:
            target.note(heartbeat + SAMPLE_SECONDS)  # this row is in the new format too
            db.execute("INSERT INTO transitions VALUES (?, 'unknown')", (heartbeat + SAMPLE_SECONDS,))
            target.last_state = "unknown"
    if state != target.last_state:
        db.execute("INSERT INTO transitions VALUES (?, ?)", (at, state))
        target.last_state = state


def _event(controller, kind, outcome, latency_ms=None, at=None):
    target, at = _store(controller), time.time() if at is None else at
    _submit(lambda: target.write(lambda db: db.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                                                       (at, kind, outcome, latency_ms))))


def record_wake(controller, text, *, at=None):
    """Called from Controller._wake_result with its fixed result text."""
    try:
        outcome = next((code for prefix, code in WAKE_TEXT if text.startswith(prefix)), None)
        if outcome:
            _event(controller, "wake", outcome, at=at)
    except Exception as error:
        log.warning("Analytics wake hook failed (%s)", type(error).__name__)


def record_delivery(controller, result, seconds, *, at=None):
    """Called from Controller._deliver with the time the upload and selection took."""
    try:
        if result in {"published", "unconfirmed", "changed"}:
            _event(controller, "delivery", result, max(0, round(seconds * 1000)), at=at)
    except Exception as error:
        log.warning("Analytics delivery hook failed (%s)", type(error).__name__)


def record_setting(controller, setting, outcome, *, at=None):
    """Called from Controller.set_option: one event per Art Mode option change, as "setting:outcome"."""
    try:
        if setting in SETTING_FIELDS and outcome in SETTING_OUTCOMES:
            _event(controller, "setting", f"{setting}:{outcome}", at=at)
    except Exception as error:
        log.warning("Analytics setting hook failed (%s)", type(error).__name__)


def record_failure(controller, stage, *, at=None):
    """Called from Controller._report with the failed stage; the cause stays in the log only."""
    try:
        if stage not in {"status", "wake", "render", "publish"}:
            return
        target, at = _store(controller), time.time() if at is None else at
        if at - target.failures.get(stage, -FAILURE_GAP) < FAILURE_GAP:
            return
        target.failures[stage] = at
        _event(controller, "failure", stage, at=at)
    except Exception as error:
        log.warning("Analytics failure hook failed (%s)", type(error).__name__)


# --- Sampling ---

def _flag(value):
    return {"on": 1, "off": 0}.get(value)


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and -9999 <= value <= 9999 else None


def measure(tv, *, clock=time.monotonic):
    """One read-only reading through tv.sample(), or tv.status() for a TV without it.

    Television.sample keeps the art channel closed in standby, uses a short timeout
    per reply and starts no new read after SAMPLE_BUDGET seconds.
    """
    started = clock()
    try:
        if hasattr(tv, "sample"):
            reading = tv.sample(budget=SAMPLE_BUDGET)
        else:
            status = tv.status()
            known = bool(status.get("known"))
            reading = {"answers": known, "power": ("on" if status.get("awake") or status.get("playing") else "standby") if known else None,
                       "art_mode": ("on" if status.get("awake") else "off") if known else None,
                       "paired": status.get("paired", True)}
    except Exception:
        reading = {}
    latency = max(0, round((clock() - started) * 1000))
    answered = bool(reading.get("answers"))
    power = reading.get("power") if answered and reading.get("power") in POWER else "unknown"
    if reading.get("paired") is False:
        power = UNPAIRED  # a silent sample of an unpaired TV is no sign of deep standby
    art = _flag(reading.get("art_mode")) if answered else None
    if power in {"standby", "off"}:
        art, playing = 0, 0
    else:
        playing = 1 if power == "on" and art == 0 else 0 if art == 1 else None
    row = dict.fromkeys(SAMPLE_FIELDS)
    row.update(answered=int(answered), latency_ms=latency, power=power, art_mode=art, playing=playing)
    if art == 1:  # brightness and content exist only in Art Mode
        content = reading.get("content_id")
        row.update(brightness=_whole(reading.get("brightness")), sensor=_flag(reading.get("sensor")),
                   color_temperature=_whole(reading.get("color_temperature")),
                   content_id=content if isinstance(content, str) and CONTENT_ID.fullmatch(content) else None)
    return row


def sample_if_due(controller, *, now=None):
    """Take one sample per 15-minute slot. A busy TV lock skips this pass; the next pass retries.

    Each TV's slots start at its own offset, so TVs sample in different passes.
    Returns the stored row, "busy", or None when no sample is due. Never raises.
    """
    try:
        now = time.time() if now is None else now
        target = _store(controller)
        slot = int((now - target.offset) // SAMPLE_SECONDS)
        if target.sampled_slot is None:
            target.sampled_slot = _last_slot(target)
        if target.sampled_slot >= slot:
            return None

        def read():
            cfg = controller.configs.load()
            return measure(controller.tv_factory(cfg))
        row = controller._exclusive(read, wait=0)
        if row == "busy":
            return "busy"
        row["ts"] = now
        target.sampled_slot = slot
        values = tuple(row[key] for key in SAMPLE_FIELDS)

        def save(db):
            db.execute(f"INSERT INTO samples ({', '.join(SAMPLE_FIELDS)}) VALUES ({', '.join('?' * len(SAMPLE_FIELDS))})", values)
            if row["answered"]:
                _mark_known(target, db, now)
        _submit(lambda: target.write(save))
        return row
    except Exception as error:
        log.warning("Analytics sample failed (%s)", type(error).__name__)
        return None


def _read_only(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _last_slot(target):
    if not target.path.exists():
        return -1
    try:
        with closing(_read_only(target.path)) as db:
            last = db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    except sqlite3.DatabaseError:
        return -1
    return -1 if last is None else int((last - target.offset) // SAMPLE_SECONDS)


# --- Reading and summarising ---

def intervals(transitions, start, end):
    """(state, from, to) for each change, clipped to [start, end). The last state stays open until end."""
    spans = []
    for index, (at, state) in enumerate(transitions):
        until = transitions[index + 1][0] if index + 1 < len(transitions) else end
        low, high = max(at, start), min(until, end)
        if high > low:
            spans.append((state, low, high))
    return spans


def resolve(transitions, unanswered, first_known, deep_since=math.inf):
    """Files written before the "deep" state existed store every no-answer span as "unknown".
    Show such a span (one that starts before deep_since) as "deep" when the TV answered before it
    and a sample of the paired TV inside it got no answer. A span with no sample at all stays
    "unknown": Art Frames itself was not running then. Later spans stay as stored.
    Repeated states merge into one."""
    marks = sorted(unanswered)
    resolved = []
    for index, (at, state) in enumerate(transitions):
        if state == "unknown" and first_known is not None and first_known <= at < deep_since:
            until = transitions[index + 1][0] if index + 1 < len(transitions) else math.inf
            low = bisect.bisect_left(marks, at)
            if low < len(marks) and marks[low] < until:
                state = "deep"
        if not resolved or resolved[-1][1] != state:
            resolved.append((at, state))
    return resolved


def state_time(transitions, start, end, tz):
    """Seconds per state for each local day and each (weekday, hour) cell, split at local hours."""
    days = defaultdict(lambda: dict.fromkeys(STATES, 0.0))
    cells = defaultdict(lambda: dict.fromkeys(STATES, 0.0))
    for state, low, high in intervals(transitions, start, end):
        if state not in STATES:
            continue
        moment = low
        while moment < high:
            local = datetime.fromtimestamp(moment, tz)
            into_hour = local.minute * 60 + local.second + local.microsecond / 1e6
            step = min(high, moment - into_hour + 3600)
            days[local.date()][state] += step - moment
            cells[(local.weekday(), local.hour)][state] += step - moment
            moment = step
    return days, cells


def percentile(values, share):
    """Nearest-rank percentile of a non-empty list."""
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered), math.ceil(share * len(ordered))) - 1)]


FAILURE_WORDS = {
    "status": "Art Frames could not check the TV.",
    "wake": "The scheduled wake did not finish.",
    "render": "The website could not be turned into an image.",
    "publish": "The TV did not accept the new image.",
}
PROBLEM_WORDS = {
    ("wake", "failed"): "The TV did not confirm Art Mode after the scheduled wake.",
    ("delivery", "unconfirmed"): "The TV got the new image but did not confirm it shows it.",
}
PROBLEMS_SQL = "(kind = 'failure' OR (kind = 'wake' AND outcome = 'failed') OR (kind = 'delivery' AND outcome = 'unconfirmed'))"


def _problem(kind, outcome):
    return FAILURE_WORDS.get(outcome) if kind == "failure" else PROBLEM_WORDS.get((kind, outcome))


def _nth_latency(db, start, now, index):
    row = db.execute("SELECT latency_ms FROM events WHERE kind = 'delivery' AND outcome IN ('published', 'unconfirmed') "
                     "AND latency_ms IS NOT NULL AND ts >= ? AND ts <= ? ORDER BY latency_ms LIMIT 1 OFFSET ?",
                     (start, now, index)).fetchone()
    return row[0] if row else None


def _read(path, start, now):
    """Rows and aggregates for one range, from a read-only connection."""
    data = {"started": None, "transitions": [], "readings": [], "samples": 0, "answered": 0, "wakes": [],
            "deliveries": {}, "delivery_failures": 0, "latency_median": None, "latency_p90": None, "problems": [],
            "events": [], "sleep_set_at": None}
    if not path.exists():
        return data
    with closing(_read_only(path)) as db:
        try:
            row = db.execute("SELECT value FROM meta WHERE key = 'started'").fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return data  # the writer has not created the tables yet
            raise
        data["started"] = float(row[0]) if row else None
        span = (start, now)
        data["transitions"] = (
            db.execute("SELECT ts, state FROM transitions WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)).fetchall()
            + db.execute("SELECT ts, state FROM transitions WHERE ts >= ? AND ts <= ? ORDER BY ts", span).fetchall())
        row = db.execute("SELECT value FROM meta WHERE key = 'deep_since'").fetchone()
        deep_since = float(row[0]) if row else math.inf
        # Samples are taken every 15 minutes, so the in-range ones are enough to judge each span.
        silent = [at for (at,) in db.execute("SELECT ts FROM samples WHERE answered = 0 AND power != ? "
                                             "AND ts >= ? AND ts <= ? ORDER BY ts", (UNPAIRED, *span))]
        data["transitions"] = resolve(data["transitions"], silent, _first_known(db), deep_since)
        data["readings"] = db.execute("SELECT ts, brightness, sensor FROM samples WHERE ts >= ? AND ts <= ? "
                                      "AND (brightness IS NOT NULL OR sensor IS NOT NULL) ORDER BY ts", span).fetchall()
        data["samples"], data["answered"] = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(answered), 0) FROM samples WHERE ts >= ? AND ts <= ?", span).fetchone()
        # A retry in the wake window adds a row per pass, so this stays a few rows a day.
        data["wakes"] = db.execute("SELECT ts, outcome FROM events WHERE kind = 'wake' AND outcome != 'skipped' "
                                   "AND ts >= ? AND ts <= ?", span).fetchall()
        data["events"] = db.execute("SELECT ts, kind, outcome FROM events WHERE kind IN ('wake', 'delivery') "
                                    "AND outcome != 'skipped' AND ts >= ? AND ts <= ? ORDER BY ts", span).fetchall()
        data["deliveries"] = dict(db.execute("SELECT outcome, COUNT(*) FROM events WHERE kind = 'delivery' "
                                             "AND ts >= ? AND ts <= ? GROUP BY outcome", span).fetchall())
        data["delivery_failures"] = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'failure' "
                                               "AND outcome IN ('render', 'publish') AND ts >= ? AND ts <= ?", span).fetchone()[0]
        count = data["deliveries"].get("published", 0) + data["deliveries"].get("unconfirmed", 0)
        if count:
            middle = [_nth_latency(db, start, now, index) for index in {(count - 1) // 2, count // 2}]
            middle = [value for value in middle if value is not None]
            data["latency_median"] = sum(middle) / len(middle) if middle else None
            data["latency_p90"] = _nth_latency(db, start, now, max(0, math.ceil(.9 * count) - 1))
        data["problems"] = db.execute(f"SELECT ts, kind, outcome FROM events WHERE {PROBLEMS_SQL} "
                                      "AND ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 5", span).fetchall()
        data["sleep_set_at"] = db.execute("SELECT MAX(ts) FROM events WHERE kind = 'setting' "
                                          "AND outcome = 'motion_timer:done' AND ts <= ?", (now,)).fetchone()[0]
    return data


_unreadable = set()


def _read_once(root, start, now):
    """_read, or None for a damaged file. A damaged file is logged once until it reads again."""
    path = Path(root) / FILE
    try:
        data = _read(path, start, now)
    except sqlite3.DatabaseError as error:
        if str(path) not in _unreadable:
            log.warning("Analytics file %s could not be read (%s)", Path(root).name, type(error).__name__)
        _unreadable.add(str(path))
        return None
    _unreadable.discard(str(path))
    return data


def report(root, timezone, days, *, now=None):
    """Everything the Analytics page shows, in the TV's timezone. Never writes."""
    tz = ZoneInfo(timezone)
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, tz).date()
    dates = [today - timedelta(days=days - 1 - index) for index in range(days)]
    start = datetime.combine(dates[0], clock_time(0), tzinfo=tz).timestamp()
    data = _read_once(root, start, now)
    if data is None:
        return {"unreadable": True, "timezone": timezone, "days": days}
    started = datetime.fromtimestamp(data["started"], tz) if data["started"] else None
    by_day, cells = state_time(data["transitions"], start, now, tz)
    daily = []
    for day in dates:
        collected = started is not None and day >= started.date()
        hours = {state: by_day[day][state] / 3600 if day in by_day else 0.0 for state in STATES}
        daily.append({"date": day, "collected": collected, **hours})
    tracked = sum(sum(day[state] for state in STATES) for day in daily)  # hours
    totals = {state: sum(day[state] for day in daily) for state in STATES}
    brightness = [(at, value) for at, value, _ in data["readings"] if value is not None]
    sensor = [(at, value) for at, _, value in data["readings"] if value is not None]
    wake_days = defaultdict(set)
    for at, outcome in data["wakes"]:
        wake_days[datetime.fromtimestamp(at, tz).date()].add(outcome)
    by_date = defaultdict(list)
    for at, value in brightness:
        by_date[datetime.fromtimestamp(at, tz).date()].append(value)
    return {
        "unreadable": False, "timezone": timezone, "days": days, "dates": dates, "start": start, "now": now,
        "started": started,
        "empty": not data["samples"] and tracked == 0,
        "daily": daily,
        "tracked_hours": tracked,
        # Average per 24 hours of tracked time, so a part-tracked day does not count as a full day.
        "per_day": {state: (totals[state] / tracked * 24 if tracked else None) for state in STATES},
        "cells": {key: {"art": value["awake"], "tracked": sum(value.values())} for key, value in cells.items()},
        "samples": data["samples"],
        "answered": data["answered"],
        "brightness": brightness,
        "brightness_median": statistics.median(value for _, value in brightness) if brightness else None,
        "brightness_days": [{"date": day, "low": min(values), "median": statistics.median(values), "high": max(values)}
                            for day, values in sorted(by_date.items())],
        "sensor": sensor,
        "wakes": len(wake_days),
        "wakes_confirmed": sum(1 for outcomes in wake_days.values() if "confirmed" in outcomes),
        "wakes_late": sum(1 for outcomes in wake_days.values() if "late" in outcomes and "confirmed" not in outcomes),
        "deliveries": {outcome: data["deliveries"].get(outcome, 0) for outcome in ("published", "unconfirmed", "changed")},
        "delivery_failures": data["delivery_failures"],
        "latency_median": data["latency_median"],
        "latency_p90": data["latency_p90"],
        "problems": [(datetime.fromtimestamp(at, tz), _problem(kind, outcome)) for at, kind, outcome in data["problems"]],
    }



# --- Suggested fixes: guesses about TV settings from stored history. Never contacts the TV. ---

DIAGNOSE_DAYS = 14
MIN_DAYS = 3  # days of data a detector needs before it says anything
MIN_REPEATS = 3  # a pattern must repeat this often
MIN_SHARE = .5  # and fit at least this share of the cases
HIGH_REPEATS = 5  # "Likely" needs this many repeats
HIGH_SHARE = .7  # and this share of the cases
WAKE_WORKS_SHARE = .8  # the morning wake works when it worked on this share of mornings
NIGHT_ENDS_HOUR = 6  # a night counts when there is data between midnight and this local hour
DEEP_MINUTES = 30  # a shorter no-answer span is a network blip, not deep standby
MORNING_SECONDS = 2 * 3600  # after the TV answers again, look this long for the wake or an image
# Art Mode lengths that point to a Sleep After timer, in minutes: Art Frames' choices
# (television.MOTION_TIMER_CHOICES) and two more that some models offer.
DETECTABLE_SLEEP_MINUTES = tuple(sorted(SLEEP_AFTER_MINUTES + (10, 180)))
SLEEP_TOLERANCE_MINUTES = 1.5  # an Art Mode span matches a timer length within this many minutes
SLEEP_TOLERANCE_SHARE = .08  # or within this share of the length, whichever is larger
MIN_READINGS = 12  # brightness samples, about 3 hours of Art Mode
# A checklist, not a fix: many Frame TVs leave the network in standby even with these set right.
NETWORK_CHECK = "Check these settings. If they already match, nothing needs fixing."
NETWORK_STEPS = (
    "Power On with Mobile: On. On the TV, open Settings → General → Network → Expert Settings.",
    "Auto Power Off: Off. Open Settings → General → Power and Energy Saving. "
    "On older models, the menu is Settings → General → Eco Solution.",
)
NETWORK_NOTE = "Art Frames cannot read or change these settings. Menu names differ a little between models."
# When the morning wake fails, matching TV settings do not mean that nothing needs fixing.
WAKE_CHECK = ("If these match, check the MAC address and wake broadcast address in this TV's Art Frames settings. "
              "The MAC must be for the TV's active connection (Wi-Fi or Ethernet).")
OFF_NETWORK = ("Your TV stops answering on the network a while after it goes to standby. "
               "Many Frame TVs do this to save power, even with the right settings.")


@dataclass(frozen=True)
class Finding:
    id: str
    title: str
    kind: str  # "fix": a setting to change; "info": how the TV is set, no fix needed
    confidence: str  # "high" or "medium"
    summary: str  # what Art Frames saw, in plain words
    evidence: str  # the "Why we think this" line
    steps_intro: str = ""
    steps: tuple[str, ...] = ()
    note: str = ""
    # {"label", "anchor"}: a link to a part of the TV's Picture page; {"label", "page": "settings", "anchor"}: a
    # section of its TV settings page (the anchor is optional).
    action: dict | None = field(default=None, hash=False, compare=False)


def _confidence(hits, total):
    """"high" for a pattern that repeats often and fits most cases, else "medium"."""
    return "high" if hits >= HIGH_REPEATS and total and hits / total >= HIGH_SHARE else "medium"


def _spans(transitions):
    """One dict per state span. "until" is None for the open last span."""
    spans = []
    for index, (at, state) in enumerate(transitions):
        closed = index + 1 < len(transitions)
        spans.append({"state": state, "at": at, "until": transitions[index + 1][0] if closed else None,
                      "before": transitions[index - 1][1] if index else None,
                      "since": transitions[index - 1][0] if index else None,
                      "after": transitions[index + 1][1] if closed else None})
    return spans


def _night(at, tz):
    """The evening date a moment belongs to: noon to noon."""
    return (datetime.fromtimestamp(at, tz) - timedelta(hours=12)).date()


def _plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def _duration(minutes):
    return _plural(minutes // 60, "hour") if minutes >= 60 and minutes % 60 == 0 else _plural(minutes, "minute")


def _deep_standby(spans, events, start, now, tz):
    nights = set()  # nights with data between midnight and NIGHT_ENDS_HOUR
    for span in spans:
        if span["state"] == "unknown":
            continue
        moment, until = max(span["at"], start), min(span["until"] or now, now)
        while moment < until:
            if datetime.fromtimestamp(moment, tz).hour < NIGHT_ENDS_HOUR:
                nights.add(_night(moment, tz))
            moment += 1800
    drops = [span for span in spans if span["state"] == "deep" and span["before"] in KNOWN_STATES and span["at"] >= start
             and min(span["until"] or now, now) - span["at"] >= DEEP_MINUTES * 60]
    dropped = {}
    for span in drops:
        dropped.setdefault(_night(span["at"], tz), span)
    if len(nights) < MIN_DAYS or len(dropped) < MIN_REPEATS or len(dropped) / len(nights) < MIN_SHARE:
        return None
    seen = f"Seen on {len(dropped)} of the last {_plural(len(nights), 'night')}."
    waits = [round((span["at"] - span["since"]) / 60) for span in dropped.values() if span["before"] == "standby"]
    if len(waits) * 2 >= len(dropped):
        seen += (f" Each time, the TV answered in standby for about {_duration(round(statistics.median(waits)))}, "
                 "then stopped answering.")
    else:
        seen += " Each time, the TV stopped answering soon after it turned off."
    attempted = worked = 0
    for span in dropped.values():
        if span["until"] is None:
            continue
        morning = [(kind, outcome) for at, kind, outcome in events if span["at"] <= at <= span["until"] + MORNING_SECONDS]
        if morning:
            attempted += 1
            worked += any(item in {("wake", "confirmed"), ("delivery", "published")} for item in morning)
    confidence = _confidence(len(dropped), len(nights))
    if attempted >= 2 and worked / attempted >= WAKE_WORKS_SHARE:
        return Finding(
            id="deep-standby-normal", kind="info", confidence=confidence,
            title="The TV leaves the network overnight. This is normal.",
            summary=(f"{OFF_NETWORK} The morning wake still works: Wake-on-LAN still reaches the TV, "
                     "so Art Frames can turn it on in the morning."),
            evidence=(f"{seen} The next morning, Art Frames woke the TV or sent an image on "
                      f"{worked} of {_plural(attempted, 'morning')}."),
            steps_intro=NETWORK_CHECK, steps=NETWORK_STEPS, note=NETWORK_NOTE)
    summary = f"{OFF_NETWORK} While the TV is off the network, Art Frames cannot check it or send it a new image."
    if attempted and worked < attempted:
        seen += f" The morning wake or image did not work on {attempted - worked} of {_plural(attempted, 'morning')}."
        return Finding(
            id="deep-standby", kind="fix", confidence=confidence, title="The TV leaves the network in standby",
            summary=summary, evidence=seen, steps_intro="Check these settings.", steps=NETWORK_STEPS,
            note=f"{NETWORK_NOTE} {WAKE_CHECK}",
            # WAKE_CHECK names the MAC and wake broadcast addresses, which the Connection section holds.
            action={"label": "Open TV settings", "page": "settings", "anchor": "connection"})
    return Finding(
        id="deep-standby", kind="fix", confidence=confidence, title="The TV leaves the network in standby",
        summary=summary, evidence=seen, steps_intro=NETWORK_CHECK, steps=NETWORK_STEPS, note=NETWORK_NOTE)


def _sleep_after(spans, start, tz, *, timer=None, since=None):
    """timer: the Sleep After value of the last stored read, None when not read or not reported.
    since: the last time Art Frames changed Sleep After; Art Mode spans ending before it do not count."""
    if timer == "off":
        return None  # the TV reports the timer off; older spans are from before the change
    endings = [((span["until"] - span["at"]) / 60, span["until"]) for span in spans
               if span["state"] == "awake" and span["until"] is not None and span["until"] >= start
               and (since is None or span["until"] > since)
               and span["before"] not in (None, "unknown") and span["after"] in ("standby", "deep")]
    if len(endings) < MIN_REPEATS:
        return None
    best = None
    for preset in DETECTABLE_SLEEP_MINUTES:
        tolerance = max(SLEEP_TOLERANCE_MINUTES, preset * SLEEP_TOLERANCE_SHARE)
        hits = [(minutes, until) for minutes, until in endings if abs(minutes - preset) <= tolerance]
        if best is None or len(hits) > len(best[1]):
            best = (preset, hits)
    preset, hits = best
    days = {datetime.fromtimestamp(until, tz).date() for _, until in hits}
    if len(hits) < MIN_REPEATS or len(days) < 2 or len(hits) / len(endings) < MIN_SHARE:
        return None
    label = _duration(preset)
    summary = (f"Art Mode keeps turning off about {label} after it starts. "
               f"The Art Mode Sleep After timer is probably set to {label}.")
    if preset not in SLEEP_AFTER_MINUTES:
        closest = min(SLEEP_AFTER_MINUTES, key=lambda minutes: (abs(minutes - preset), minutes))
        summary += f" Art Frames cannot set {label}; its closest Sleep After choice is {_duration(closest)}."
    low, high = round(min(minutes for minutes, _ in hits)), round(max(minutes for minutes, _ in hits))
    lasted = f"{low} minutes" if low == high else f"{low} to {high} minutes"
    return Finding(
        id="sleep-after", kind="fix", confidence=_confidence(len(hits), len(endings)),
        title=f"Art Mode turns off after {label}",
        summary=summary,
        evidence=(f"Seen {len(hits)} of the {len(endings)} times Art Mode turned off in the last {DIAGNOSE_DAYS} days, "
                  f"on {_plural(len(days), 'different day')}. Each time, Art Mode lasted {lasted}."),
        steps_intro="To keep Art Mode on:",
        steps=("Put the TV in Art Mode.", "Open Art Mode Options. On some models, open Settings → Art Mode instead.",
               "Set Sleep After to Off."),
        note=("Motion Detection and Night Mode can also turn Art Mode off. "
              "Motion Detection turns it off when nobody moves nearby. Night Mode turns it off when the room is dark. "
              "Turn them off in the same menu to keep Art Mode on."),
        # The TV page shows a Sleep After control only after a read that reports it.
        action={"label": "Set Sleep After to Off", "anchor": "art-mode-options"} if timer is not None else None)


def _brightness_sensor(readings, start, tz):
    rows = [(at, value, sensor) for at, value, sensor in readings if at >= start and value is not None]
    days = {datetime.fromtimestamp(at, tz).date() for at, _, _ in rows}
    if len(rows) < MIN_READINGS or len(days) < MIN_DAYS:
        return None
    values = [value for _, value, _ in rows]
    changes = [at for (_, before, _), (at, value, _) in pairwise(rows) if value != before]
    reported = next((sensor for at, _, sensor in reversed(readings) if sensor is not None and at >= start), None)
    count = f"{len(rows)} Art Mode samples on {len(days)} days"
    seen = (f"Brightness stayed at {values[0]} in all {count}." if len(set(values)) == 1
            else f"Brightness changed {_plural(len(changes), 'time')} in {count}, from {min(values)} to {max(values)}.")
    if reported is not None:
        on, confidence = bool(reported), "high"
        evidence = f"The TV reported the sensor {'on' if on else 'off'} in its last sample. {seen}"
    elif len(set(values)) == 1:
        on, evidence, confidence = False, seen, "medium"
    elif len(changes) >= MIN_REPEATS and len({datetime.fromtimestamp(at, tz).date() for at in changes}) >= 2:
        on, evidence, confidence = True, seen, "medium"
    else:
        return None
    verb = "is" if reported is not None else "looks"
    return Finding(
        id="brightness-sensor", kind="info", confidence=confidence,
        title=f"The brightness sensor {verb} {'on' if on else 'off'}",
        summary=("The TV changes Art Mode brightness by itself to match the room light." if on
                 else "The TV keeps Art Mode at one brightness. It does not change it for the room light."),
        evidence=evidence,
        steps_intro=f"To turn the brightness sensor {'off' if on else 'on'}:",
        steps=("Put the TV in Art Mode.", "Open Art Mode Options.", f"Set Brightness Sensor to {'Off' if on else 'On'}."),
        note=("On some models, the setting is in Settings → General → Eco Solution → Brightness Optimization. "
              "You can also set the light sensor in the Brightness card on this TV's Picture page."))


def diagnose(transitions, readings, events, now, tz, *, days=DIAGNOSE_DAYS, display=None, sleep_set_at=None):
    """Suggested fixes from stored history only. An empty list means no pattern or not enough data.

    transitions: (ts, state) in time order; the first may be from before the window.
    readings: (ts, brightness, sensor) Art Mode samples. events: (ts, kind, outcome) wakes and deliveries.
    display: the last stored Art Mode reading of this TV (the "art_display" record), or None.
    sleep_set_at: when Art Frames last changed Sleep After and the TV confirmed it, or None.
    """
    start = now - days * 86400
    spans = _spans(transitions)
    tracked = sum(min(span["until"] or now, now) - max(span["at"], start) for span in spans
                  if span["state"] != "unknown" and (span["until"] or now) > start)
    if tracked < MIN_DAYS * 86400:
        return []
    timer = (display or {}).get("motion_timer")
    timer = timer if timer in MOTION_TIMER_CHOICES or timer == "unknown" else None
    found = [_deep_standby(spans, events, start, now, tz),
             _sleep_after(spans, start, tz, timer=timer, since=sleep_set_at),
             _brightness_sensor(readings, start, tz)]
    return sorted((item for item in found if item), key=lambda item: item.kind != "fix")


def findings(root, timezone, *, now=None, display=None):
    """Suggested fixes for one TV from its analytics file and its last stored Art Mode reading.

    Never writes, never raises, never contacts the TV. A damaged file is logged once.
    """
    try:
        now = time.time() if now is None else now
        data = _read_once(root, now - DIAGNOSE_DAYS * 86400, now)
        if data is None:
            return []
        return diagnose(data["transitions"], data["readings"], data["events"], now, ZoneInfo(timezone),
                        display=display, sleep_set_at=data["sleep_set_at"])
    except Exception as error:
        log.warning("Suggested fixes could not be worked out (%s)", type(error).__name__)
        return []
