"""One TV's rotation state: the image stored on the TV for each layout in use, the layout it shows,
the swap timer, a hold that keeps one layout on screen, and each layout's render failures.

The controller owns the TV and makes every TV call. This class only keeps the state, like
AddressRecovery keeps the address search. The web pages read it through
Controller.rotation_status(), which never writes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta

from . import apps

# Per-TV state keys; each ends with the TV's device identity (see Controller._device).
KEYS = ("app_images:", "rotation:", "swaps:", "app_health:")
# A hold "until the next change" lasts at most this long, also without a schedule change.
MAX_HOLD = timedelta(hours=4)
# A swap waits this long after Screen off, and until a newer TV status shows what the screen does.
SCREEN_OFF_SECONDS = 60


@dataclass
class Record:
    """What a rotation shows. `slot` is the plan key the record belongs to; `at` starts the swap interval.

    `hold_app` keeps one layout on screen until `hold_until`; without `hold_until`, until resumed.
    """

    slot: str | None = None
    app: str | None = None
    at: str | None = None
    hold_app: str | None = None
    hold_until: str | None = None


@dataclass(frozen=True)
class Hold:
    """Keep one layout on screen until `until`, or until resumed (None)."""

    app: str
    until: datetime | None = None


class Rotation:
    def __init__(self, library, device):
        self.library, self.device = library, device

    def _get(self, prefix, default=None):
        return self.library.state(prefix + self.device) or default

    def _set(self, prefix, value):
        self.library.set_state(prefix + self.device, value)

    def _record(self):
        saved = self._get("rotation:", {})
        return Record(**{item.name: saved.get(item.name) for item in fields(Record)})

    def _save(self, record):
        self._set("rotation:", asdict(record))

    # The images stored on the TV

    def images(self, keep_ids=None):
        """The image stored on the TV for each layout in use: {app id: {content_id, image_sha256, key, at}}.

        With `keep_ids`, entries of other layouts are dropped, so their images are deleted at the
        next cleanup. The TV keeps at most one image per layout in use.
        """
        images = self._get("app_images:", {})
        if keep_ids is not None:
            kept = {app_id: entry for app_id, entry in images.items() if app_id in keep_ids}
            if kept != images:
                self._set("app_images:", kept)
            images = kept
        return images

    def store(self, app, content_id, digest, key, now):
        images = self.images()
        images[app.id] = {"content_id": content_id, "image_sha256": digest, "key": key, "at": now.isoformat()}
        self._set("app_images:", images)

    def drop(self, app_id):
        """Forget a layout's image; the next render pass uploads it again."""
        images = self.images()
        images.pop(app_id, None)
        self._set("app_images:", images)

    def refresh_all(self):
        """Mark every stored image out of date, so the next render pass renders each layout again."""
        images = self.images()
        if any(entry.get("key") for entry in images.values()):
            self._set("app_images:", {app_id: {**entry, "key": None} for app_id, entry in images.items()})

    def keep(self, *, app_id=None, content_id=None):
        """The stored images that stay on the TV: every layout's in use but `app_id`'s, and never `content_id`.

        An upload passes the layout it replaces; a cleanup passes the image the TV shows now.
        """
        return [entry["content_id"] for item, entry in self.images().items()
                if item != app_id and entry.get("content_id") != content_id]

    def ready(self, plan, owned, keys):
        """The layouts whose stored image is on the TV and matches the layout's current revision in `keys`."""
        images = self.images()
        return [app for app in plan.apps
                if (images.get(app.id) or {}).get("content_id") in owned
                and images[app.id].get("key") is not None and images[app.id].get("key") == keys.get(app.id)]

    # Which layout shows, and the swap timer

    def showing(self, plan):
        """The layout ID this rotation shows in the plan's slot, or None."""
        record = self._record()
        return record.app if record.slot == plan.key else None

    def due(self, plan, now):
        record = self._record()
        if record.slot != plan.key or not record.at:
            return True
        return (now - datetime.fromisoformat(record.at)).total_seconds() >= plan.swap_seconds

    def record_shown(self, plan, app_id, now):
        """Record the layout the rotation shows and start its swap interval."""
        record = self._record()
        record.slot, record.app, record.at = plan.key, app_id, now.isoformat()
        self._save(record)

    def restart_timer(self, plan, now):
        """Start a new swap interval for the layout shown now: one swap try per interval."""
        self.record_shown(plan, self.showing(plan), now)

    def record_swap(self, now, outcome):
        """Swaps are routine: count them per day for the Overview instead of adding activity lines."""
        day = now.date().isoformat()
        counts = self._get("swaps:", {})
        if counts.get("day") != day:
            counts = {"day": day, "done": 0, "skipped": 0, "failed": 0}
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == "failed":
            counts["failed_at"] = now.isoformat()
        self._set("swaps:", counts)

    # Holds

    def _hold(self):
        """The saved hold, also one that ended, or None."""
        record = self._record()
        if not record.hold_app:
            return None
        return Hold(record.hold_app, datetime.fromisoformat(record.hold_until) if record.hold_until else None)

    def active_hold(self, now, available=None):
        """The hold in effect now, or None. A hold ends at its time, or when its layout is no longer installed."""
        hold = self._hold()
        if hold is None or hold.until is not None and now >= hold.until:
            return None
        return hold if apps.find(hold.app, available) is not None else None

    def end_hold(self, now, available=None):
        """Clear a saved hold that is no longer in effect. Returns True when one was cleared."""
        if self._hold() is None or self.active_hold(now, available) is not None:
            return False
        self.resume()
        return True

    def set_hold(self, hold):
        """Keep `hold.app` on screen until `hold.until`, or until resumed."""
        record = self._record()
        record.hold_app, record.hold_until = hold.app, hold.until.isoformat() if hold.until else None
        self._save(record)

    def resume(self):
        """End the hold. The TV still shows the held layout, so forget which rotation layout it showed:
        the next swap shows the schedule's layout at once, also when another layout waits on a backoff."""
        record = self._record()
        if record.hold_app:
            self._save(Record())

    def apply_hold(self, plan, now, available=None):
        """The plan with an active hold applied: only the held layout, in its own slot. Never writes.

        The held layout still renders on its own revision; swaps stop, and a slot change or a
        rotation's render pass does not change the layout. A hold of a layout that is no longer
        installed is ignored.
        """
        hold = self.active_hold(now, available)
        if hold is None or not plan.apps:
            return plan
        return held_plan(plan, apps.find(hold.app, available))

    # Render failures, per layout

    def failing(self, app_id, now):
        """Whether the layout waits after a render failure before the next try."""
        entry = self._get("app_health:", {}).get(app_id) or {}
        return bool(entry.get("next_at")) and now < datetime.fromisoformat(entry["next_at"])

    def record_failure(self, app_id, message, now, refresh_minutes):
        """Record a failed render and back off this layout only. Returns True when the message is new."""
        health = self._get("app_health:", {})
        entry = health.get(app_id) or {}
        failures = entry.get("failures", 0) + 1
        minutes = min(2 ** min(failures - 1, 11), refresh_minutes)
        new = entry.get("error") != message
        health[app_id] = {**entry, "failures": failures, "next_at": (now + timedelta(minutes=minutes)).isoformat(),
                          "error": message, "error_at": now.isoformat()}
        self._set("app_health:", health)
        return new

    def record_success(self, app_id, now):
        """Record a render that worked. The last error stays for the UI, with its time."""
        health = self._get("app_health:", {})
        entry = health.get(app_id) or {}
        health[app_id] = {**entry, "failures": 0, "next_at": None, "ok_at": now.isoformat()}
        self._set("app_health:", health)

    def record_test(self, app_id, now, error=None):
        """Record Health's "Test <app>": one preview render, with `error` when it failed.

        A test is not the TV's render path: it never changes the backoff (next_at, failures) or
        the last success and error, so a passed test never hides a problem on the TV.
        """
        health = self._get("app_health:", {})
        entry = health.get(app_id) or {}
        health[app_id] = {**entry, "test_at": now.isoformat(), "test_error": error}
        self._set("app_health:", health)

    def health(self):
        """{app id: AppHealth}, for each layout that rendered, failed or was tested on this TV."""
        return {app_id: AppHealth(_time(entry.get("ok_at")), entry.get("error"), _time(entry.get("error_at")),
                                  _time(entry.get("test_at")),
                                  entry.get("test_error") if isinstance(entry.get("test_error"), str) else None)
                for app_id, entry in self._get("app_health:", {}).items() if isinstance(entry, dict)}

    # The Overview

    def status(self, plan, now, available=None, *, blocked=None):
        """What the TV's schedule does now, from stored state only. Never writes or contacts the TV.

        `plan` is apps.plan() for now. `blocked` is the controller's swap_block() reason, or None.
        """
        held = self.active_hold(now, available) if plan.apps else None
        record = self._record()
        if held is not None:
            current, next_switch = held.app, held.until
        elif plan.rotate:
            current = record.app if record.slot == plan.key else None
            start = datetime.fromisoformat(record.at) if record.slot == plan.key and record.at else now
            next_switch = max(start + timedelta(seconds=plan.swap_seconds), now)
            if plan.next_change is not None:
                next_switch = min(next_switch, plan.next_change)
        else:
            current, next_switch = (plan.app.id if plan.app else None), plan.next_change
        if held is not None:
            paused = "hold"
        elif blocked is not None and blocked != "swapping":  # a running swap is no pause
            paused = blocked
        else:
            paused = "error" if self.library.state("scheduler_error") else None
        swaps = self._get("swaps:", {})
        return RotationStatus(
            rotate=plan.rotate, current_app=current, next_switch=next_switch, next_change=plan.next_change,
            paused=paused, hold=held, swaps=swaps if swaps.get("day") == now.date().isoformat() else {},
            apps=self.health(),
        )


def stored_time(value):
    """A stored ISO time as an aware datetime, or None when it is missing or unreadable.

    A time without a zone is read as UTC, so it compares with the other stored times.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


_time = stored_time


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


@dataclass(frozen=True)
class AppHealth:
    """One layout's last render on this TV. The last error stays after a later success, with its time."""

    last_success: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    test_at: datetime | None = None  # Health's last "Test <app>", and its error when it failed
    test_error: str | None = None


@dataclass(frozen=True)
class RotationStatus:
    """What a TV's schedule does now (see Rotation.status).

    - rotate: the schedule rotates between layouts now.
    - current_app: the layout ID on screen by Art Frames' records, or None.
    - next_switch: when the layout on screen changes next (a swap, a hold's end or a schedule
      change), or None.
    - next_change: the next schedule change, or None.
    - paused: None, or why the schedule does not change the screen: "hold", "wake",
      "screen-off", "watching", "standby", "unknown" or "error".
    - hold: the Hold in effect, or None.
    - swaps: today's swap counts {"done", "skipped", "failed"}, or {}.
    - apps: {app id: AppHealth}.
    """

    rotate: bool
    current_app: str | None
    next_switch: datetime | None
    next_change: datetime | None
    paused: str | None
    hold: Hold | None
    swaps: dict
    apps: dict

    def to_json(self):
        """The status with ISO times, for a JSON response."""
        return {
            "rotate": self.rotate, "current_app": self.current_app, "next_switch": _iso(self.next_switch),
            "next_change": _iso(self.next_change), "paused": self.paused,
            "hold": {"app": self.hold.app, "until": _iso(self.hold.until)} if self.hold else None,
            "swaps": dict(self.swaps),
            "apps": {app_id: {key: _iso(value) for key, value in asdict(health).items()}
                     for app_id, health in self.apps.items()},
        }


def held_plan(plan, app):
    """A plan that shows only `app`, in a slot of its own, with the schedule's next change."""
    return apps.Plan([app], plan.swap_seconds, key=f"hold:{app.id}", next_change=plan.next_change,
                     next_apps=plan.next_apps)


def hold_until_next_change(plan, now):
    """When a hold "until the next change" ends: the next schedule change, at most MAX_HOLD from now."""
    cap = now + MAX_HOLD
    return min(plan.next_change, cap) if plan.next_change is not None else cap


def screen_off(library, device, now):
    """Whether Screen off was sent less than SCREEN_OFF_SECONDS ago, or no TV status was read since."""
    off = stored_time(library.state("screen_off_at:" + device))
    if off is None:  # none, or an unreadable record: it never blocks the schedule or a page
        return False
    status_at = stored_time(library.state("tv_status_at"))
    fresh = status_at is not None and status_at > off
    try:
        return (now - off).total_seconds() < SCREEN_OFF_SECONDS or not fresh
    except TypeError:  # a `now` without a zone
        return False
