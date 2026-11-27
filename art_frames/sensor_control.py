"""Room sensors drive Art Mode brightness, and turn the TV on and off around the people in the room.

SensorHub runs one thread ("art-frames-sensors") beside the scheduler and the swap timer. Every TICK seconds:
1. It starts, changes or stops each TV's sensor Link (sensors.py) to match the TV's settings.
2. It turns the Samsung light sensor and Sleep After off, or back on, when a TV asks for that.
3. For each TV with an enabled sensor, it runs step().

What step() does, for a TV on in Art Mode:
- Room empty: the AWAY_ACTIONS in order. Dim at once (or after away_after minutes), then blank the screen
  after blank_after minutes (Controller.soft_blank). Someone back: undo them at once, in reverse order.
- After the set hours (Sensor.hours_start to hours_end): once the room has been empty for NIGHT_SETTLE, set
  the TV's own Sleep After to its shortest value, so the TV turns itself off. Someone there after hours:
  Sleep After off again at once, until they leave. Back inside the hours: Sleep After off. Art Frames saves
  the owner's value first, so every off path restores it.
- The TV off step (Sensor.off_after; None: never): inside the hours, once the screen is blank and the room
  has been empty for off_after minutes, set the same shortest Sleep After. Art Frames never sends a power
  key to turn the TV off: on a Frame, KEY_POWEROFF in Art Mode does nothing. The TV turns itself off when
  its own motion sensor sees no one (the frame covers it). The blank stays until then. When the TV reads
  standby after that change, it is Art Frames' own off ("tv_off", since "off_at"): the first screen-on for
  someone after it skips the WAKE_GAP wait. When the TV is on in Art Mode again (a presence wake, the
  remote, Turn on now), Sleep After goes off, the away steps undo, the saved image comes back, and the
  empty room counts from then. Near a scheduled wake, Sleep After goes off again, so it never fights the
  wake.
- Room dark (Sensor.dark_below; None: never): when the 2-minute median of the room light is below dark_below,
  the away steps run at once, even with someone there and during a pause: dim, then blank. With off_after set,
  the off step's Sleep After follows at once. While dark, nothing undoes them, no presence wake runs and the
  scheduled wake waits (Controller._wake, CurveStep.dark). The room is light again only from dark_below *
  LIGHT_MARGIN, so the screen's own glow does not flip it back and forth. An unknown light keeps the last
  state. When the light is back with someone there, the steps undo, and a TV in standby turns on as for
  someone who came in. With the room empty, the steps stay. A gap in the readings shorter than UNKNOWN_GAP
  keeps the dark room for the scheduled wake; after a longer gap it counts as light (dark_held).
A short gap in the presence readings (up to UNKNOWN_GAP: the link drops, or the sensor sends no value) keeps
the empty-room clock, but no away step and no off step starts during the gap. While the room is empty and the
dim waits, the curve may lower the brightness, but never raises it.
For a TV in standby: when someone comes in inside the hours (or is there when the hours start), turn the
screen on once (Controller.presence_wake), at most once every 10 minutes and less often after failures.

The brightness rules (the curve, the median and when to change) are pure functions in sensor_curve.py.

When the screen comes on (a presence wake or the scheduled wake), and when someone comes back to a blank or
dimmed screen, the brightness goes to the curve's level for the room light now at once (CurveStep,
room_light). That one change skips the waits of a light change; the next change waits as usual. A pause, or
an unknown room light, keeps today's behavior.

Safety rules (the same as a rotation swap, see Controller.swap_block):
- Art Frames changes the TV only when the last status reading says the TV is on in Art Mode, and that reading
  is recent. Each TV command then confirms power and Art Mode again (Television._in_art_mode). It never
  leaves a programme, and never acts in a wake window, after Screen off or while the owner's blank test holds.
- Only presence_wake turns a screen on, and only from a TV that the last reading saw in standby or not
  answering. It reads the TV again first and never sends a key to a TV that is on.
- A missing or stale reading never acts on the brightness: Art Frames holds the current level. Unknown TV
  state never acts.
- The thread never waits for the TV lock: a busy TV is skipped until the next tick.
- Before each change, Art Frames reads the TV's brightness again. When it differs from the last level
  Art Frames set, someone changed it on the TV: sensor control pauses for 2 hours, and the Sensor page offers
  that level as a point of the curve. A stored reading that differs is read again first: only a fresh reading
  pauses. A change within OWN_CHANGE_WINDOW after Art Frames changed Sleep After or the light sensor is the
  TV's own: no pause, and the curve sets its level again later.
- Presence comes first: while no one is there, the away actions win over the curve.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as day_time
from zoneinfo import ZoneInfo

from . import controller as controller_module
from . import sensor_curve, sensors
from .controller import Busy
from .rotation import screen_off as screen_off_sent
from .sensor_discovery import Discovery
from .television import DisplayError, brightness_range, shortest_sleep_after
from .timewords import ago

log = logging.getLogger(__name__)
TICK = 5
# A pass that someone's arrival starts runs at most this often (seconds).
NUDGE_GAP = 1.0
# A TV status reading older than this is unknown (the scheduler reads it every 30 s; see _record_status).
STATUS_MAX_AGE = timedelta(minutes=10)
# How long to wait before trying the Samsung settings again after the TV was not in Art Mode.
SAMSUNG_RETRY = 300
BUSY_RETRY = 30
# The TV refused the change, so trying again does not help. A refusal ends an "off" request: control runs
# anyway, and the page asks the person to turn the light sensor off on the TV.
REFUSED = {"rejected", "invalid"}
# An "off" request ends after this many tries that the TV does not confirm.
MAX_SAMSUNG_TRIES = 3
# Presence can be unknown for a short time: the sensor link drops and comes back, or the sensor reports no
# value. A gap up to this long (seconds) keeps the empty-room clock; a longer gap starts it again.
UNKNOWN_GAP = 5 * 60
# After the set hours, the room must be empty this long (seconds) before Sleep After goes to its shortest
# value. A sensor that flickers between empty and someone there then does not flip Sleep After.
NIGHT_SETTLE = 2 * 60
# A gap in the presence readings at least this long (seconds) gets a log line when the readings come back.
GAP_LOG = 60
# Seconds after Art Frames changed Sleep After or the light sensor when a new brightness level on the TV is the
# TV's own change, not a press on the remote: it starts no pause. A brightness change has its own check (the
# readback, _by_hand and _caught_up), so a remote press during it still pauses.
OWN_CHANGE_WINDOW = 30
# After a recheck of the TV's brightness failed, wait this long (seconds) before the next one.
RECHECK_RETRY = BUSY_RETRY
# Room dark (Sensor.dark_below): the room counts as light again only at this many times the dark level, so a
# light near the level, or the screen's own glow, does not flip it back and forth.
LIGHT_MARGIN = 2.0
CONTROL = "sensor_control"  # library state: what Art Frames last sent, the away state and the pause
SAMSUNG = "sensor_samsung"  # library state: the Samsung settings to turn off or restore, and the result


@dataclass
class Memory:
    """In-memory state of one TV's sensor control. Lost on restart, which only restarts the timers."""

    absent_since: float | None = None
    unknown_since: float | None = None  # when presence became unknown; None while it is known
    present_since: float | None = None  # for pages: how long someone has been there
    last_samsung_try: float = 0.0
    last_night_try: float = 0.0
    night_error: str | None = None  # the last Sleep After failure in the log: each new reason logs once
    wrote_at: float | None = None  # when Art Frames last changed Sleep After or the light sensor
    recheck_after: float = 0.0  # no brightness recheck before this time, after a failed one
    pending: sensor_curve.Pending | None = None
    # For presence_wake: the last presence and hours seen. None after a restart, so a restart never wakes.
    last_presence: bool | None = None
    in_hours: bool | None = None
    # Someone came in (or was there when the hours began): a wake may run until this time. Only this edge
    # wakes the TV, so a TV someone turned off while they stay is left off.
    wake_until: float | None = None
    arrived_at: float | None = None  # when the sensor saw someone come in (its clock), for the wake's log line
    background: bool = False  # the hub sets it: each wake runs on its own thread, so other TVs keep running
    wake_thread: threading.Thread | None = field(default=None, repr=False)
    # The away settings and hours of the last pass. A change sets `restart`: the away steps undo and count
    # again from now, so the new settings apply.
    away_settings: tuple | None = None
    restart: bool = False
    # The TV was seen off (standby, just after Screen off, or no answer at all). The next pass that sees it on
    # in Art Mode sets on_since: the TV off step then counts from then, so a TV someone just turned on stays on.
    was_off: bool = False
    on_since: float | None = None
    # The room is dark (Sensor.dark_below). None after a restart: the next pass takes CONTROL "dark", so a
    # restart without a light reading keeps a dark blank.
    dark: bool | None = None
    # When the readings were last fresh: a dark room holds through a gap shorter than UNKNOWN_GAP (dark_held).
    # The pass that restores `dark` after a restart sets it to that moment.
    fresh_at: float | None = None


def in_hours(sensor, moment):
    """True when `moment` is inside the sensor's set hours. An end before the start runs past midnight;
    the same start and end mean all day."""
    start, end = day_time.fromisoformat(sensor.hours_start), day_time.fromisoformat(sensor.hours_end)
    now = moment.time().replace(tzinfo=None)
    if start == end:
        return True
    if start < end:
        return start <= now < end
    return now >= start or now < end


def light_now(link, sensor, now):
    """(the median light of the last 2 minutes, the time of the newest light reading), or (None, None)."""
    if link is None or not link.readings.fresh(now):
        return None, None
    readings = link.readings
    snap = readings.snapshot()
    light_id = sensors.pick_light(snap["entities"], snap["device"].get("project_name", ""), sensor.light_entity)
    if not light_id:
        return None, None
    samples = readings.samples(light_id)
    lux = sensor_curve.median_light(samples, now)
    if lux is None:
        return None, None
    return lux, max(at for at, value in samples if value is not None and at <= now)


def dark_room(sensor, lux, was_dark):
    """True when the room is dark for Sensor.dark_below, from the 2-minute median `lux`.

    Dark below dark_below; light again only from dark_below * LIGHT_MARGIN. An unknown light (None) keeps
    `was_dark`. dark_below None: never dark.
    """
    if sensor.dark_below is None:
        return False
    if lux is None:
        return bool(was_dark)
    return lux < sensor.dark_below * (LIGHT_MARGIN if was_dark else 1.0)


def dark_held(memory, fresh, now):
    """True when the room counts as dark for the scheduled wake and the wake window: memory.dark is set, and the
    readings are fresh or were fresh less than UNKNOWN_GAP ago. A short link drop never lets the wake show
    artwork in a dark room; after a longer gap the room counts as light, so a broken sensor never keeps the TV
    off for good. A blank already on the screen stays either way (see step)."""
    if not memory.dark:
        return False
    return fresh or memory.fresh_at is not None and now - memory.fresh_at < UNKNOWN_GAP


def _track_dark(screen, sensor, link, memory, now):
    """Update memory.dark for this pass. Returns True when the room just became light again.

    CONTROL "dark" changes only when the state changes, for the pages and for a restart. One log line each.
    """
    if memory.dark is None:
        memory.dark = bool((screen.library.state(CONTROL) or {}).get("dark"))
        memory.fresh_at = now
    was = memory.dark
    lux = light_now(link, sensor, now)[0]
    memory.dark = dark_room(sensor, lux, was)
    if memory.dark == was:
        return False
    merge(screen.library, CONTROL, dark=memory.dark)
    if memory.dark:
        log.info("TV %s: room dark (%s lux, below %g); the screen blanks and stays off until the light is back",
                 screen.id, round(lux, 1), sensor.dark_below)
    elif lux is not None:
        log.info("TV %s: room light again (%s lux)", screen.id, round(lux, 1))
    else:
        log.info("TV %s: the room dark rule is off", screen.id)
    return not memory.dark


def room_light(link, sensor, now, until=None):
    """The room light for a one-shot step (a wake, someone back), or None when it is unknown.

    The median of the readings of the last 2 minutes up to `until` (default: now), so readings taken after the
    screen came on do not count. Without such readings: the newest single reading. A sensor sends a reading
    when the light changes, so in a steady room the newest reading is still the light now. A stale link is
    unknown.
    """
    if link is None or not link.readings.fresh(now):
        return None
    readings = link.readings
    snap = readings.snapshot()
    light_id = sensors.pick_light(snap["entities"], snap["device"].get("project_name", ""), sensor.light_entity)
    if not light_id:
        return None
    end = now if until is None else min(until, now)
    # Only readings up to `end`: after a wake, the screen's own light must not count, not even as the newest.
    samples = [(at, lux) for at, lux in readings.samples(light_id) if lux is not None and at <= end]
    lux = sensor_curve.median_light(samples, end)
    if lux is None and samples:
        lux = max(samples, key=lambda sample: sample[0])[1]
    return lux


def status_gate(status):
    """The last status reading as a gate: None for on in Art Mode, else "unknown", "watching" or "standby"."""
    if not (status.get("paired") and status.get("known")):
        return "unknown"
    if status.get("playing"):
        return "watching"
    if not status.get("awake"):
        return "standby"
    return None


def tv_gate(screen, cfg, now, *, own_blank=False, past_wake=False):
    """Why Art Frames must not change this TV now, or None when the TV is on in Art Mode.

    With own_blank=True, a blank that sensor control owns (room empty, its record's owner "sensor") does not
    block: the other checks of swap_block still do. With past_wake=True, a scheduled wake that is due ("wake")
    does not block either: for undoing what sensor control did. It uses stored state only and never contacts
    the TV.
    """
    ctl = screen.controller
    moment = datetime.fromtimestamp(now, ZoneInfo(cfg.timezone))
    stamp = screen.library.state("tv_status_at")
    try:
        if not stamp or moment - datetime.fromisoformat(stamp) > STATUS_MAX_AGE:
            return "unknown"
    except ValueError:
        return "unknown"
    device = ctl._identity(cfg)
    gate = ctl.swap_block(cfg, device, moment, swapping=False)
    if not (gate == "blanked" or gate == "wake" and past_wake):
        return gate
    record = ctl.blank_record(device)
    if record is not None and not (own_blank and record.get("owner") == "sensor"):
        return "blanked"  # the owner's test blank always holds
    if screen_off_sent(screen.library, device, moment):
        return "screen-off"
    return status_gate(screen.library.state("tv_status") or {})


def art_display(screen, cfg):
    """The last stored Art Mode reading of this TV, or {} when it is for another TV."""
    display = screen.library.state("art_display") or {}
    return display if display.get("device") == screen.controller._identity(cfg) else {}


def as_level(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def tv_level(display, state):
    """The TV's brightness as Art Frames knows it: the stored reading when it is newer than the last change,
    else the level Art Frames last set."""
    reading = as_level(display.get("brightness"))
    try:
        read_at = datetime.fromisoformat(display["checked_at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        read_at = None
    sent, sent_at = as_level(state.get("sent")), state.get("sent_at")
    if reading is not None and (sent is None or sent_at is None or (read_at is not None and read_at >= sent_at)):
        return reading
    return sent if sent is not None else reading


# Library state: each read-change-write of CONTROL or SAMSUNG holds the TV's lock. The web thread and the
# hub both write these, and the hub's TV commands are slow, so each write reads the state again first.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def state_lock(library):
    """The lock of one TV's sensor state."""
    key = (str(getattr(library, "root", id(library))), getattr(library, "display_id", ""))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def merge(library, key, **fields):
    """Read the state again, change `fields` and write it, under the TV's lock. Returns the new state."""
    with state_lock(library):
        state = dict(library.state(key) or {})
        state.update(fields)
        library.set_state(key, state)
        return state


@dataclass
class Away:
    """What an away action needs to know in one pass."""

    state: dict  # CONTROL
    current: int | None  # the TV's brightness as Art Frames knows it
    low: int
    high: int
    lux: float | None
    paused: bool
    unconfirmed: int
    now: float
    room: float | None = None  # room_light(): the light for the undo steps, which skip the damping
    dark: bool = False  # the room is dark (Sensor.dark_below): the steps run at once; for the log


def _curve_now(cfg, away):
    """The curve's level for the room light now, for an undo step. None while paused or without a reading."""
    if away.paused or away.room is None:
        return None
    return sensor_curve.target(away.room, cfg.sensor.curve, away.low, away.high)


def _dim(screen, cfg, away):
    """Dim to the curve's lowest brightness. Returns (CONTROL fields, error code or None)."""
    dark = sensor_curve.dark_level(cfg.sensor.curve, away.low, away.high)
    result = None
    if away.current != dark:  # never send a level the TV already has
        result, error = _send(screen, dark)
        if error:
            return None, error
    log.info("TV %s: room %s; dimmed to %s", screen.id, "dark" if away.dark else "empty", dark)
    return {"away": True, "before_away": away.current, **_sent(dark, result, away.unconfirmed, away.now,
                                                                away.current)}, None


def _undim(screen, cfg, away):
    """Someone is back: the curve's level now, or the level from before the dim without a light reading.

    The curve's level comes at once, without the waits of a light change. A pause keeps the level from before.
    """
    want = _curve_now(cfg, away)
    if want is None:
        want = as_level(away.state.get("before_away"))
    if want is None:
        return {"away": False}, None
    result = None
    if away.current != want:  # never send a level the TV already has (the blank's undo may have set it)
        result, error = _send(screen, want)
        if error:
            return None, error
    return {"away": False, **_sent(want, result, away.unconfirmed, away.now, away.current)}, None


def _blank_level(readback):
    reported = as_level(readback.reported)
    return readback.requested if reported is None else reported


def _blank(screen, cfg, away):
    """Blank the screen (Controller.soft_blank: a black image at the lowest brightness, in Art Mode only)."""
    result, error = _tv_call(screen, screen.controller.soft_blank, None, "sensor")
    if error:
        # A blank record saved before the error still holds the schedule: undo it like a blank.
        return ({"blanked": True} if screen.controller.blank_record() is not None else None), error
    fields = {"blanked": True}
    if result["readback"] is not None:
        # Art Frames set this level, so it is no change by hand.
        fields.update(sent=_blank_level(result["readback"]), sent_at=away.now, unconfirmed=0, before=None,
                      readback=None, asked=None, got=None)
    if away.dark:
        log.info("TV %s: room dark; screen blank", screen.id)
    else:
        log.info("TV %s: room empty for %s min; screen blank", screen.id, cfg.sensor.blank_after)
    return fields, None if result["shown"] else "not-confirmed"


def _unblank(screen, cfg, away):
    """Someone is back: show the saved image again at the curve's level now, at once.

    Without a light reading, or while paused, the brightness from before the blank comes back.
    """
    result, error = _tv_call(screen, screen.controller.undo_blank, "sensor", _curve_now(cfg, away))
    if error == "not-blanked":
        return {"blanked": False}, None  # gone, or the owner's test blank now: not sensor control's to undo
    if error:
        return None, error
    level = _blank_level(result["readback"])
    return {"blanked": False, "sent": level, "sent_at": away.now, "unconfirmed": 0}, None


def _restore_blank(ctl, _value):
    try:
        ctl.undo_blank("sensor")  # never a newer test blank
    except DisplayError as error:
        if error.code != "not-blanked":
            raise


@dataclass(frozen=True)
class AwayAction:
    """One step of what the TV does while no one is there.

    The steps apply in order, each after its delay, and undo in reverse order. Someone back runs undo(); an
    off path (control off, Remove, another sensor) keeps keep(CONTROL) in SAMSUNG[undo_key] and the hub runs
    restore(controller, value) the next time the TV is on in Art Mode.
    """

    flag: str  # the CONTROL field that is True while this step holds
    delay: str  # the Sensor field with its minutes, counted from when the room became empty
    apply: Callable  # apply(screen, cfg, Away) -> (CONTROL fields or None, error code or None)
    undo: Callable  # undo(screen, cfg, Away) -> (CONTROL fields or None, error code or None)
    undo_key: str
    keep: Callable  # keep(CONTROL) -> the value restore needs, or None
    restore: Callable  # restore(controller, value)


AWAY_ACTIONS = (
    AwayAction("away", "away_after", _dim, _undim, "undo_away",
               keep=lambda control: as_level(control.get("before_away")),
               restore=lambda ctl, level: ctl.set_brightness(level)),
    AwayAction("blanked", "blank_after", _blank, _unblank, "undo_blank",
               keep=lambda control: True, restore=_restore_blank),
)
# One control session: what Art Frames set, the pause, the away state, the night and the wake. Turning control
# on, turning it off and removing the sensor clear them, so an old level never starts a pause.
# "before" is the TV's level before the last change and "readback" what the TV reported right after it.
# "asked" and "got" remember a level the TV rounds: asked for 7, it sets 6.
# "night" is the Sleep After value Art Frames set for the night. "suggest" is a level someone set on the TV,
# offered as a point of the curve. "wake_*" count the screen-on tries for someone in the room. "empty_again"
# asks the next pass to count the empty room from then: an undo of the blank outside sensor control sets it.
# "empty_sleep" is True while "night" was set inside the hours for the empty room (the TV off step). "tv_off" is
# True while the TV is in standby after that, since "off_at". "dark" is True while the room is dark
# (Sensor.dark_below).
SESSION = {"sent": None, "sent_at": None, "paused_until": None, "paused_at": None, "away": False,
           "before_away": None, "unconfirmed": 0, "before": None, "readback": None, "asked": None, "got": None,
           "blanked": False, "night": None, "suggest": None, "wake_at": None, "wake_failures": 0,
           "wake_result": None, "empty_again": False, "empty_sleep": False,
           "tv_off": False, "off_at": None, "dark": False}
# After this many changes in a row that the TV does not confirm, Art Frames holds the brightness.
MAX_UNCONFIRMED = 3
# A TV that rounds the brightness sets a level at most this far from the one asked for.
ROUNDING = 1
# At most one screen-on try for someone in the room in this many seconds. Each failure in a row doubles
# the wait, up to 8 times.
WAKE_GAP = 10 * 60
MAX_WAKE_BACKOFF = 3
WAKE_EDGE = 2 * 60  # seconds after someone comes in (or the hours begin) when Art Frames may turn the screen on


def _tv_call(screen, call, *args):
    """Run one TV command; returns (result, None) or (None, an error code)."""
    try:
        return call(*args), None
    except Busy:
        return None, "busy"
    except DisplayError as error:
        return None, error.code
    except Exception as error:
        log.warning("TV %s: a sensor TV command failed (%s)", screen.id, type(error).__name__)
        return None, "failed"


def _confirmed(result, value):
    """Whether the TV reported `value` after a change. A TV that reports nothing counts as confirmed."""
    reported = as_level(getattr(result, "reported", None))
    return reported is None or reported == value


def _sent(level, result, unconfirmed, now, before):
    """The state fields for a level Art Frames sent. A change the TV did not confirm counts toward the hold.

    `result` is the TV's readback, or None when Art Frames sent nothing because the TV had the level.
    """
    confirmed = result is None or _confirmed(result, level)
    fields = {"sent": level, "sent_at": now, "unconfirmed": 0 if confirmed else unconfirmed + 1, "before": before,
              "readback": None if result is None else as_level(getattr(result, "reported", None))}
    if confirmed:
        fields.update(asked=None, got=None)  # the TV set what Art Frames asked: no rounding to remember
    return fields


def _send(screen, value):
    """Set brightness; returns (the TV's readback, an error code or None)."""
    return _tv_call(screen, screen.controller.set_brightness, value)


def _by_hand(level, state, sent, unconfirmed):
    """True when the TV reports a level that someone set with the remote.

    While a change is unconfirmed, the TV may still report the level from before it. Then only a third
    level, neither the one Art Frames sent nor the one before it, counts as a change by hand.
    """
    if not sensor_curve.changed_by_hand(level, sent):
        return False
    if not unconfirmed:
        return True
    before = as_level(state.get("before"))
    return before is not None and level != before


def _caught_up(library, level, state, unconfirmed):
    """Clear an unconfirmed change when the TV now reports it. Returns (sent, unconfirmed, state).

    A TV that rounds the level (asked for 7, it reported 6 right after and still does) also counts as
    caught up. "asked" and "got" keep that pair, so Art Frames does not ask for 7 again. Rounding is at most
    ROUNDING step away: a level further away is a press on the remote.
    """
    sent = as_level(state.get("sent"))
    if not unconfirmed or level is None:
        return sent, unconfirmed, state
    readback, before = as_level(state.get("readback")), as_level(state.get("before"))
    if level == sent:
        state = merge(library, CONTROL, unconfirmed=0)
    elif level == readback and readback != before and sent is not None and abs(level - sent) <= ROUNDING:
        state = merge(library, CONTROL, sent=level, unconfirmed=0, asked=sent, got=level)
    else:
        return sent, unconfirmed, state
    return as_level(state.get("sent")), 0, state


def _rounded(want, level, state):
    """True when the TV rounds `want` to `level`, as seen before: no change is needed."""
    asked = as_level(state.get("asked"))
    return asked is not None and want == asked and level == as_level(state.get("got"))


def _pause(screen, reported, now, memory, lux=None, detail=""):
    """Someone changed the brightness on the TV: pause sensor control for 2 hours.

    The level and the room light now are kept as "suggest": the Sensor page offers them as a point.
    """
    memory.pending = None
    suggest = {"lux": max(round(lux, 1), 0.1), "level": reported, "at": now} if lux is not None else None
    merge(screen.library, CONTROL, paused_until=now + sensor_curve.PAUSE_SECONDS, paused_at=now, sent=reported,
          unconfirmed=0, result="paused", **({"suggest": suggest} if suggest else {}))
    log.info("TV %s: brightness changed on the TV to %s; sensor control paused%s", screen.id, reported,
             f" ({detail})" if detail else "")
    return "paused"


def _own_change(memory, now):
    """True shortly after Art Frames changed a TV setting: the TV may still change its brightness itself."""
    return memory.wrote_at is not None and 0 <= now - memory.wrote_at < OWN_CHANGE_WINDOW


def _tv_changed(screen, memory, level, now):
    """The TV changed its brightness just after Art Frames changed a setting. No pause: take the TV's level as
    the level Art Frames knows. The curve sets the wanted level again on a later pass, with its usual waits
    and checks."""
    memory.pending = None
    merge(screen.library, CONTROL, sent=level, sent_at=now, unconfirmed=0, before=None, readback=None,
          asked=None, got=None)
    log.info("TV %s: the TV set its brightness to %s %d s after Art Frames changed a setting; no pause",
             screen.id, level, now - memory.wrote_at)
    return "tv-changed"


def _stored_detail(display, stored):
    """Where the stored brightness came from, for the pause's log line."""
    try:
        age = f"{max(0.0, time.time() - datetime.fromisoformat(display['checked_at']).timestamp()):.0f} s old"
    except (KeyError, TypeError, ValueError):
        age = "age unknown"
    return f"stored reading {stored}, {age}"


@dataclass
class CurveStep:
    """The one-shot curve step after the screen comes on: the curve's level for the room light now, at once.

    step() puts one on the TV's Controller (Controller.curve_step). The Controller runs it, while it holds
    the TV, after a presence wake ("presence") or a scheduled wake ("scheduled") confirms Art Mode, and for
    the scheduled wake's undo of the sensor's blank. It skips the waits of a light change (CONFIRM,
    DOWN_SECONDS, MIN_GAP) for this one change. The change counts as a change Art Frames made, so the
    damping starts again from it.

    No step when control is off or paused, the TV's own light sensor still runs, a change is on hold, or the
    room light is unknown: then the TV keeps its level, as before.
    """

    screen: object
    link: object
    memory: Memory | None = None

    def _now(self):
        return self.link.readings.clock()

    def level(self, settings=None, reason="scheduled"):
        """The level to set, or None for no step. `settings` is a fresh Art Mode reading of the TV, if any.

        A TV level that someone set by hand pauses sensor control, like a pass of step() does.
        """
        now, library = self._now(), self.screen.library
        cfg = self.screen.configs.load()
        if not cfg.sensor.enabled or self.link is None:
            return None
        state = dict(library.state(CONTROL) or {})
        if sensor_curve.paused(state, now) or (library.state(SAMSUNG) or {}).get("want") == "off":
            return None
        if state.get("away") and reason == "scheduled":
            # The room is empty and the dim holds: keep it. Someone back undoes it at the curve's level.
            return None
        unconfirmed = int(state.get("unconfirmed") or 0)
        if unconfirmed >= MAX_UNCONFIRMED:
            return None  # on hold: the page says so, and Resume now tries again
        # A presence wake counts the readings from before the screen came on: its own light does not count.
        until = state.get("wake_at") if reason == "presence" else None
        lux = room_light(self.link, cfg.sensor, now, until if isinstance(until, int | float) else None)
        display = settings or art_display(self.screen, cfg)
        if lux is None or not display:
            return None
        reported = as_level(display.get("brightness")) if settings else None
        if reported is not None:
            sent, unconfirmed, state = _caught_up(library, reported, state, unconfirmed)
            if _by_hand(reported, state, sent, unconfirmed):
                _pause(self.screen, reported, now, self.memory or Memory(), lux)
                return None
        want = sensor_curve.target(lux, cfg.sensor.curve, *brightness_range(display))
        if reported is not None and _rounded(want, reported, state):
            return None  # the TV rounds `want` to the level it has: asking again only counts toward the hold
        return want

    def dark(self):
        """True when the room is dark (Sensor.dark_below): the scheduled wake waits.

        False when control is off or the rule is off. Readings missing for UNKNOWN_GAP or longer count as light
        (dark_held); a shorter gap keeps the dark room.
        """
        cfg = self.screen.configs.load()
        if not cfg.sensor.enabled or cfg.sensor.dark_below is None or self.link is None or self.memory is None:
            return False
        now = self._now()
        return dark_held(self.memory, self.link.readings.fresh(now), now)

    def applied(self, level, readback, before, reason="scheduled"):
        """Record the level the step set (readback None: the TV had it already). The damping counts from now."""
        now = self._now()
        state = self.screen.library.state(CONTROL) or {}
        fields = _sent(level, readback, int(state.get("unconfirmed") or 0), now, as_level(before))
        merge(self.screen.library, CONTROL, result="unconfirmed" if fields["unconfirmed"] else "brightness",
              **fields)
        if self.memory is not None:
            self.memory.pending = None
        log.info("TV %s: screen on (%s); brightness %s for the room light now", self.screen.id, reason, level)


def resume(library):
    """End a pause, or a hold, now. The next change waits for the readings as usual.

    It forgets the last level Art Frames set, so the TV's level now is no new pause.
    """
    merge(library, CONTROL, paused_until=None, paused_at=None, sent=None, unconfirmed=0)


def accept_level(library, level):
    """Take `level` as the level Art Frames set, so a brightness set by hand on purpose is no pause."""
    merge(library, CONTROL, sent=level, sent_at=time.time(), paused_until=None, paused_at=None, unconfirmed=0)


def reset_control(library):
    """Forget the control session (SESSION), such as when control turns on."""
    merge(library, CONTROL, **SESSION)


def release(library, still_off=None):
    """Control is off or the sensor is gone. Ask for the Samsung restore when Art Frames changed the TV, then
    forget the control session.

    This covers every way control ends: the switch, Remove, connecting another sensor, or a settings edit.
    The restore undoes the away actions (blank, dim) and puts the light sensor and Sleep After back.
    `still_off()` reads the settings again under the lock, so a page that just turned control on wins.
    """
    with state_lock(library):
        if still_off is not None and not still_off():
            return
        samsung = library.state(SAMSUNG) or {}
        control = library.state(CONTROL) or {}
        want = samsung.get("want")
        # An away action asks again even when a restore already waits: the dim may have ended after that
        # request, and its level from before the dim belongs in the restore.
        if (want != "restore" and (want == "off" or samsung.get("saved"))) or any(
                control.get(action.flag) for action in AWAY_ACTIONS):
            request_samsung(library, "restore")
        if any(control.get(key) for key in SESSION):
            merge(library, CONTROL, **SESSION)


def _night(screen, cfg, memory, state, presence, inside, now, empty=False, dark=False):
    """Sleep After for the night and for the TV off step. Returns a result code when it contacted the TV,
    else None.

    Inside the hours, and after them while someone is there: Sleep After off (the state Art Frames keeps while
    it controls the TV). After the hours with the room empty, or no presence reading: the TV's shortest Sleep
    After, so it turns itself off. Inside the hours with `empty` (the room has been empty for off_after
    minutes): the same shortest value. The owner's value goes into SAMSUNG "saved" first, so restore puts it
    back. It changes the TV only when the wanted state differs from the stored one.
    With `dark` (the room is dark and the screen blank, see step), the same shortest value, even with someone
    there and without the NIGHT_SETTLE wait: the dark reading is already a 2-minute median.
    """
    want_night = (presence is not True or dark) and (not inside or empty)
    have = state.get("night")
    if want_night and have is None and not dark and _quiet_for(memory, now) < NIGHT_SETTLE:
        return None  # the room must stay empty a short time first: a flicker does not flip Sleep After
    if want_night == (have is not None):
        return None
    if now - memory.last_night_try < SAMSUNG_RETRY:
        return None
    memory.last_night_try = now
    code = _set_night(screen, cfg, memory, want_night, inside, now, dark)
    if code in NIGHT_DONE:
        memory.night_error = None
    elif code != "busy" and code != memory.night_error:
        memory.night_error = code  # once per reason, not every try
        log.warning("TV %s: Sleep After not changed (%s); Art Frames tries again in %s min", screen.id, code,
                    SAMSUNG_RETRY // 60)
    return code


NIGHT_DONE = {"tv_off", "night", "day"}


def _quiet_for(memory, now):
    """Seconds the room has been empty, or else unknown. 0 while someone is there."""
    since = memory.absent_since if memory.absent_since is not None else memory.unknown_since
    return 0.0 if since is None else now - since


def _set_night(screen, cfg, memory, want_night, inside, now, dark=False):
    """Read the TV and set Sleep After for _night. Returns a result code."""
    ctl, library = screen.controller, screen.library
    settings, error = _tv_call(screen, ctl.display_settings)
    if error:
        if error == "busy":
            memory.last_night_try = now - SAMSUNG_RETRY + BUSY_RETRY
        return error
    current = settings.get("motion_timer")
    if current is None:
        return "no-sleep-after"  # the TV does not list Sleep After: it cannot turn itself off
    if current == "unknown":
        return "unknown"  # a value Art Frames cannot save, so it cannot put it back: change nothing
    value = shortest_sleep_after(settings) if want_night else "off"
    with state_lock(library):
        samsung = dict(library.state(SAMSUNG) or {})
        saved = dict(samsung.get("saved") or {})
        if "motion_timer" not in saved:
            saved["motion_timer"] = current
            library.set_state(SAMSUNG, {**samsung, "saved": saved})
    if current != value:
        result, error = _tv_call(screen, ctl.set_option, "motion_timer", value)
        memory.wrote_at = now
        if error:
            if error == "busy":
                memory.last_night_try = now - SAMSUNG_RETRY + BUSY_RETRY
            return error
        if result.reported != value:
            return "not-confirmed"
    memory.last_night_try = 0.0
    merge(library, CONTROL, night=value if want_night else None, empty_sleep=want_night and inside)
    if want_night and inside:
        if dark:
            log.info("TV %s: room dark; Sleep After %s min so the TV turns itself off", screen.id, value)
        else:
            log.info("TV %s: room empty for %s min; Sleep After %s min so the TV turns itself off", screen.id,
                     cfg.sensor.off_after, value)
        return "tv_off"
    log.info("TV %s: Sleep After %s (%s)", screen.id, value,
             "after the set hours" if want_night else "someone is there" if not inside else "inside the set hours")
    return "night" if want_night else "day"


def _empty_long(sensor, memory, state, now):
    """True when the TV off step is due: the screen is blank, and the room has been empty for off_after minutes
    (and the TV on for that long). A pause never counts. A short unknown gap keeps the clock (see step), but the
    step fires only on a reading that says the room is empty. off_after None: never."""
    if sensor.off_after is None or not state.get("blanked") or memory.absent_since is None:
        return False
    if memory.unknown_since is not None:
        return False  # an unknown gap keeps the clock, but the step waits for the sensor to say "empty"
    if sensor_curve.paused(state, now):
        return False
    waited = now - memory.absent_since
    if memory.on_since is not None:
        waited = min(waited, now - memory.on_since)  # a TV that just came on gets the full time
    return waited >= sensor.off_after * 60


def _wake(screen, cfg, memory, state, gate, now):
    """Turn the screen on for someone in the room, from standby or no answer. Returns a result code.

    At most one try every WAKE_GAP seconds; each failure in a row doubles the wait. A TV in TV mode is left
    as it is: someone watches. With memory.background, the wake runs on its own thread ("waking").
    """
    status = screen.library.state("tv_status") or {}
    standby = gate == "standby"
    silent = gate == "unknown" and status.get("paired") and not status.get("known") and status.get("answered") is False
    if not (standby or silent):
        return gate
    failures = int(state.get("wake_failures") or 0)
    wait = WAKE_GAP * 2 ** min(failures, MAX_WAKE_BACKOFF)
    # The TV turned itself off for the empty room after the last try: the first try since then never waits.
    # The person who comes back must not find the TV off because of a try from before the off.
    off_at = state.get("off_at")
    after_off = isinstance(off_at, int | float) and (state.get("wake_at") is None or off_at > state["wake_at"])
    if state.get("wake_at") is not None and now - state["wake_at"] < wait and not after_off:
        return "wake-wait"
    merge(screen.library, CONTROL, wake_at=now)
    # The arrival on the monotonic clock of the wake's timing: the sensor's clock may differ.
    lag = now - memory.arrived_at if memory.arrived_at is not None else 0.0
    arrival = time.monotonic() - max(0.0, lag)
    if memory.background:
        memory.wake_thread = threading.Thread(target=_wake_thread, args=(screen, memory, state, failures, arrival),
                                              name=f"art-frames-wake-{screen.id[:8]}", daemon=True)
        memory.wake_thread.start()
        return "waking"
    return _run_wake(screen, memory, state, failures, arrival)


def _wake_thread(screen, memory, state, failures, arrival=None):
    # Like the hub, this thread never waits for the TV lock: a busy TV is tried again on the next tick.
    controller_module.CALLER.wait = 0
    try:
        _run_wake(screen, memory, state, failures, arrival)
    except Exception as error:
        log.warning("TV %s: the screen-on for someone in the room failed (%s)", screen.id, type(error).__name__)


def wake_stopped(screen, sensor):
    """A check for a running wake: True once control is off, or the sensor is removed or changed.

    It reads the settings file each time, so a change on the Sensor page stops the wake at its next step.
    """
    signature = sensors.Link.signature(sensor)

    def stopped():
        try:
            now = screen.configs.load().sensor
        except Exception:
            return True  # the TV was removed, or its settings cannot be read: never go on
        return not now.enabled or not now.host or sensors.Link.signature(now) != signature
    return stopped


def _since(moment, arrival):
    return f"+{max(0.0, moment - arrival):.1f} s"


def _log_timing(screen, arrival, started, result):
    """One line per wake: where the time went, from the moment the sensor saw someone come in."""
    key_at = getattr(result, "key_at", None)
    confirmed_at = getattr(result, "confirmed_at", None)
    log.info("TV %s: screen on for someone: arrival seen +0.0 s, wake started %s, key sent %s, "
             "Art Mode confirmed %s", screen.id, _since(started, arrival),
             _since(key_at, arrival) if key_at is not None else "(no key)",
             _since(confirmed_at, arrival) if confirmed_at is not None else "(not confirmed)")


def _run_wake(screen, memory, state, failures, arrival=None):
    stop = wake_stopped(screen, screen.configs.load().sensor)
    started = time.monotonic()
    result, error = _tv_call(screen, screen.controller.presence_wake, stop)
    if arrival is not None and error not in {"busy", "wake-window", "stopped"}:
        _log_timing(screen, arrival, started, result)
    if error == "stopped":
        memory.wake_until = None
        # Not a try: a new arrival may wake the TV without waiting WAKE_GAP.
        merge(screen.library, CONTROL, wake_at=state.get("wake_at"))
        log.info("TV %s: the screen-on for someone in the room stopped: sensor control changed", screen.id)
        return "stopped"
    if error in {"busy", "wake-window"}:
        merge(screen.library, CONTROL, wake_at=state.get("wake_at"))  # not a try: the next tick tries again
        return "busy" if error == "busy" else "wake"
    if error == "screen-is-on":
        memory.wake_until = None  # it is on already
        return "on"
    code = error or result.result
    if code in {"confirmed", "tv-mode", "watching"}:  # TV mode or a programme: someone watches, leave it
        memory.wake_until = None
        merge(screen.library, CONTROL, wake_failures=0, wake_result=code)
        log.info("TV %s: screen on for someone in the room: %s", screen.id, code)
        return "woke" if code == "confirmed" else "watching"
    merge(screen.library, CONTROL, wake_failures=failures + 1, wake_result=code)
    log.warning("TV %s: the screen did not turn on for someone in the room (%s)", screen.id, code)
    return "wake-failed"


def _away(screen, cfg, link, state, now, dark=False):
    """(Away for this pass, None), or (None, an error code) when the TV's range could not be read."""
    display = art_display(screen, cfg)
    if not display:
        # No stored reading of this TV yet, so no range: read it once (read-only; it confirms Art Mode first).
        settings, error = _tv_call(screen, screen.controller.display_settings)
        if error:
            return None, error
        display = art_display(screen, cfg) or settings
    low, high = brightness_range(display)
    lux = light_now(link, cfg.sensor, now)[0]
    return Away(state, tv_level(display, state), low, high, lux, sensor_curve.paused(state, now),
                int(state.get("unconfirmed") or 0), now, room_light(link, cfg.sensor, now), dark), None


UNDO_WORDS = {"away": "dim", "blanked": "blank"}


def _log_undo(screen, presence, done, level):
    """One log line for one undo of the away steps."""
    if not done:
        return
    why = "someone is back" if presence is True else "away steps start again"
    log.info("TV %s: %s; %s undone; brightness %s", screen.id, why, " and ".join(done), level)


def _undo(screen, cfg, memory, away, held, presence):
    """Someone is back, or the settings changed: undo every held step at once, in reverse order, without the
    waits of a light change. Returns "back", "restart" or an error code."""
    done = []
    for action in reversed(held):
        fields, error = action.undo(screen, cfg, away)
        if fields:
            away.state = merge(screen.library, CONTROL, **fields)
            if as_level(fields.get("sent")) is not None:
                away.current = fields["sent"]  # the next undo step sees the level this one set
            if not fields.get(action.flag):
                done.append(UNDO_WORDS[action.flag])
        if error:
            _log_undo(screen, presence, done, away.current)
            return error
    _log_undo(screen, presence, done, away.current)
    code = "back" if presence is True else "restart"
    memory.restart = False
    merge(screen.library, CONTROL, result=code)
    return code


def step(screen, cfg, link, memory, now):
    """One control pass for one TV. Returns a short result code (for tests and the log)."""
    sensor, library = cfg.sensor, screen.library
    # The wakes run the one-shot curve step (CurveStep) only while control is on with a sensor.
    screen.controller.curve_step = CurveStep(screen, link, memory) if sensor.enabled and link is not None else None
    if not sensor.enabled or link is None:
        # Control on again (or a new sensor) must not read as someone who just came in: forget the last pass.
        memory.last_presence = memory.in_hours = memory.wake_until = None
    if not sensor.enabled:
        memory.absent_since = memory.unknown_since = memory.pending = memory.dark = None
        release(library, lambda: not screen.configs.load().sensor.enabled)
        return "off"
    if link is None:
        return "off"
    readings = link.readings
    fresh = readings.fresh(now)
    presence = None
    if fresh:
        snap = readings.snapshot()
        presence_id = sensors.pick_presence(snap["entities"], snap["device"].get("project_name", ""),
                                            sensor.presence_entity)
        presence = readings.value("binary_sensor", presence_id, now)
    if presence is not None and memory.unknown_since is not None and now - memory.unknown_since >= GAP_LOG:
        log.info("TV %s: presence reading back after %d s without one", screen.id, now - memory.unknown_since)
    if presence is True:
        memory.absent_since = memory.unknown_since = None
        memory.present_since = memory.present_since or now
    elif presence is False:
        memory.absent_since = memory.absent_since or now
        memory.present_since = memory.unknown_since = None
    else:
        # Unknown presence, or a gap in the readings, never starts the away timer. A short gap keeps it: the
        # link drops and comes back often, and the empty room must not count again from zero each time. The
        # gap itself starts no away step (see _empty_long and empty_for below).
        memory.unknown_since = memory.unknown_since or now
        memory.present_since = memory.pending = None
        if now - memory.unknown_since > UNKNOWN_GAP and memory.absent_since is not None:
            memory.absent_since = None
            log.info("TV %s: no presence reading for %d min; the empty-room count starts again", screen.id,
                     UNKNOWN_GAP // 60)
    # Room dark (Sensor.dark_below), from the light's 2-minute median. An unknown light keeps the last state.
    light_back = _track_dark(screen, sensor, link, memory, now)
    if fresh:
        memory.fresh_at = now
    # The same dark room as CurveStep.dark, for the wake window: it holds through a short gap (dark_held).
    dark_now = dark_held(memory, fresh, now)
    # New away settings or hours start the away steps again: undo them, and count from now.
    settings = (sensor.away_after, sensor.blank_after, sensor.off_after, sensor.hours_start, sensor.hours_end,
                sensor.dark_below)
    if memory.away_settings is not None and memory.away_settings != settings:
        memory.restart = True
        if memory.absent_since is not None:
            memory.absent_since = now
    memory.away_settings = settings
    inside = in_hours(sensor, datetime.fromtimestamp(now, ZoneInfo(cfg.timezone)))
    # Someone came in inside the hours, or is there when the hours begin: turn the screen on if it is off,
    # for WAKE_EDGE seconds after that edge only.
    if inside and presence is True and (memory.last_presence is False or memory.in_hours is False):
        memory.wake_until = now + WAKE_EDGE
        arrived = readings.arrival(presence_id) if memory.last_presence is False else None
        memory.arrived_at = arrived if isinstance(arrived, int | float) and arrived <= now else now
    if not inside or presence is not True or (memory.wake_until is not None and now > memory.wake_until):
        memory.wake_until = None
    memory.last_presence, memory.in_hours = presence, inside
    if light_back and inside and presence is True:
        # The light came on with someone there: like someone who came in, the screen may turn on.
        memory.wake_until, memory.arrived_at = now + WAKE_EDGE, now
    if memory.dark:
        memory.wake_until = None  # never turn the screen on in a dark room
    state = dict(library.state(CONTROL) or {})
    if state.get("empty_again"):
        # The owner's Undo blank, or the scheduled wake, undid the blank: the dim and the blank wait their
        # usual time again, counted from now.
        if memory.absent_since is not None:
            memory.absent_since = now
        state = merge(library, CONTROL, empty_again=False)
    gate = tv_gate(screen, cfg, now, own_blank=bool(state.get("blanked")))
    sleeping = bool(state.get("empty_sleep") and state.get("night") is not None)
    if gate in {"watching", "screen-off"} or gate is None and not sleeping:
        # On in Art Mode, someone watches, or someone just turned it off: never wake it for this visit. While
        # the off step's Sleep After is set, a TV that reads on may already be in standby (the status is up to
        # a minute old): keep the wake. _wake confirms standby before any key, and WAKE_EDGE bounds it.
        memory.wake_until = None
    if gate == "standby" and state.get("empty_sleep") and state.get("night") is not None and not state.get("tv_off"):
        # The TV turned itself off with the Sleep After the off step set: it is Art Frames' own off.
        state = merge(library, CONTROL, tv_off=True, off_at=now)
        log.info("TV %s: the TV is in standby after Sleep After; TV off because the room is %s", screen.id,
                 "dark" if memory.dark else "empty")
    if gate == "watching" and state.get("tv_off"):
        # It left standby straight into TV mode: not off any more. Back in Art Mode, the away steps undo.
        state = merge(library, CONTROL, tv_off=False, off_at=None)
        memory.restart = True
        # A Sleep After try that failed while the TV slept must not wait SAMSUNG_RETRY: turn it off now.
        memory.last_night_try = 0.0
        if memory.absent_since is not None:
            memory.absent_since = now
    status = library.state("tv_status") or {}
    if gate in {"standby", "screen-off"} or gate == "unknown" and status.get("answered") is False:
        # Off, or no answer at all (a TV that is fully off). A stale or unclear reading is not "off".
        memory.was_off = True
    elif gate is None and memory.was_off:
        memory.was_off, memory.on_since = False, now  # it just came on: the off step counts from now
    if gate is None and state.get("wake_result") == "tv-mode":
        state = merge(library, CONTROL, wake_result=None)  # it is in Art Mode now
    if gate in {"standby", "unknown"} and memory.wake_until is not None:
        return _wake(screen, cfg, memory, state, gate, now)
    if (library.state(SAMSUNG) or {}).get("want") == "off":
        # The TV's own light sensor still changes the brightness: wait until it is off.
        memory.pending = None
        return "samsung"
    held = [action for action in AWAY_ACTIONS if state.get(action.flag)]
    # In a dark room, a scheduled wake neither turns Sleep After off nor undoes the blank: the wake skips.
    if (gate == "wake" and presence is not True and inside and state.get("empty_sleep")
            and state.get("night") is not None and not dark_now):
        # A scheduled wake is near or due: Sleep After off again, so the TV does not turn itself off while the
        # wake turns it on. The TV must be on to change it; the wake turns it on first.
        if tv_gate(screen, cfg, now, own_blank=bool(state.get("blanked")), past_wake=True) is not None:
            return gate
        return _night(screen, cfg, memory, state, presence, inside, now) or gate
    if gate == "wake" and presence is True and (held or state.get("night") is not None) and not dark_now:
        # A scheduled wake is due, and someone is there: still undo the blank, the dim and the night's Sleep
        # After (Controller.undo_blank needs no power change, so the wake window allows it).
        if tv_gate(screen, cfg, now, own_blank=bool(state.get("blanked")), past_wake=True) is not None:
            return gate
        night = _night(screen, cfg, memory, state, presence, inside, now)
        if night or not held:
            return night or gate
        away, error = _away(screen, cfg, link, state, now)
        return error or _undo(screen, cfg, memory, away, held, presence)
    # A dark room: the scheduled wake waits (Controller._wake) and keeps the sensor's blank (_deliver), so the
    # wake window does not stop a new dark blank. Only the away steps run then: Sleep After and the curve
    # still wait for the window to end. Every other gate still holds; a busy TV skips the pass (_tv_call).
    in_wake = gate == "wake" and dark_now
    if in_wake:
        gate = tv_gate(screen, cfg, now, own_blank=bool(state.get("blanked")), past_wake=True)
    if gate:
        return gate
    if state.get("tv_off"):
        # The TV turned itself off for the empty room, and it is on in Art Mode again: a presence wake, the
        # remote, Turn on now or the scheduled wake. Undo the away steps now (the saved image comes back), and
        # count the empty room from now, so the off step waits its full time again.
        state = merge(library, CONTROL, tv_off=False, off_at=None)
        memory.restart = True
        # A Sleep After try that failed while the TV slept must not wait SAMSUNG_RETRY: turn it off now.
        memory.last_night_try = 0.0
        if memory.absent_since is not None:
            memory.absent_since = now
    # Once set for the empty room, the off step's Sleep After stays while no one is seen: a gap in the sensor
    # readings or a restart of Art Frames must not undo it. Someone there, a TV back on (restart) or new
    # settings do.
    kept = (sleeping and presence is not True and sensor.off_after is not None and not memory.restart
            and bool(state.get("blanked")))
    # A dark room with the screen blank: the off step's Sleep After at once, even with someone there.
    dark_off = bool(memory.dark and state.get("blanked") and sensor.off_after is not None)
    night = None if in_wake else _night(screen, cfg, memory, state, presence, inside, now,
                                        kept or dark_off or _empty_long(sensor, memory, state, now), dark=dark_off)
    if night:
        return night
    if not fresh:
        return "stale"
    curve = sensor.curve
    away, error = _away(screen, cfg, link, state, now, bool(memory.dark))
    if error:
        return error
    low, high, current, lux, paused, unconfirmed = (away.low, away.high, away.current, away.lux, away.paused,
                                                     away.unconfirmed)
    sample_at = light_now(link, sensor, now)[1]

    # Presence first: the away actions win over the curve while no one is there.
    if not held or memory.dark:
        memory.restart = False  # nothing comes back while the room is dark
    if (presence is True or memory.restart) and held and not memory.dark:
        return _undo(screen, cfg, memory, away, held, presence)
    # A pause means someone set the brightness on the TV: the away steps wait for the pause to end.
    # Only a reading that says "empty" starts a step: an unknown gap keeps the clock, but never acts on it.
    empty_for = None if paused or presence is not False or memory.absent_since is None else (
        now - memory.absent_since)
    if memory.dark:
        empty_for = float("inf")  # a dark room: every step at once, with someone there or in a pause
    for action in AWAY_ACTIONS:
        if state.get(action.flag):
            continue
        if empty_for is None or empty_for < getattr(sensor, action.delay) * 60:
            break  # the steps run in order: a later step waits for this one
        memory.pending = None
        fields, error = action.apply(screen, cfg, away)
        if fields:
            merge(library, CONTROL, result=action.flag, **fields)
        if error:
            return error
        return action.flag
    if held:
        return "blanked" if state.get("blanked") else "away"
    if in_wake:
        return "wake"  # the curve waits for the wake window to end

    if paused:
        return "paused"
    if lux is None:
        memory.pending = None  # no light reading for 2 minutes: hold the current level
        return "no-light"
    sent, unconfirmed, state = _caught_up(library, current, state, unconfirmed)
    if _by_hand(current, state, sent, unconfirmed):
        # The stored reading may be old, or come from another read (such as the one for Sleep After). Read the
        # TV again: only a fresh reading that still differs pauses control.
        if now < memory.recheck_after:
            memory.pending = None
            return "recheck-wait"
        detail = _stored_detail(art_display(screen, cfg), current)
        settings, error = _tv_call(screen, screen.controller.display_settings)
        if error:
            if error != "busy":
                memory.recheck_after = now + RECHECK_RETRY  # a failed read: not again every pass
            return error
        current = as_level(settings.get("brightness"))
        state = dict(library.state(CONTROL) or {})
        sent, unconfirmed, state = _caught_up(library, current, state, int(state.get("unconfirmed") or 0))
        if _by_hand(current, state, sent, unconfirmed):
            if _own_change(memory, now):
                return _tv_changed(screen, memory, current, now)
            return _pause(screen, current, now, memory, lux, f"{detail}; fresh reading {current}")
        low, high = brightness_range(settings)
    if unconfirmed >= MAX_UNCONFIRMED:
        memory.pending = None
        return "unconfirmed"  # hold: the page says so, and Resume now tries again
    if current is None:
        return "no-brightness"  # the TV does not report its brightness
    want = sensor_curve.target(lux, curve, low, high)
    if _rounded(want, current, state):
        memory.pending = None
        return "same"
    if memory.absent_since is not None and want > current:
        memory.pending = None  # the room is empty, waiting for the dim: never brighten it
        return "empty"
    verdict, memory.pending = sensor_curve.decide(want, current, memory.pending, sample_at, now,
                                                  state.get("sent_at"))
    if verdict != "change":
        return verdict
    # Read the TV again just before the change: someone may have used the remote since the last reading.
    settings, error = _tv_call(screen, screen.controller.display_settings)
    if error:
        return error
    reported = as_level(settings.get("brightness"))
    # Read the state again: a wake's curve step may have set a level while the TV was read.
    state = dict(library.state(CONTROL) or {})
    unconfirmed = int(state.get("unconfirmed") or 0)
    sent, unconfirmed, state = _caught_up(library, reported, state, unconfirmed)
    if _by_hand(reported, state, sent, unconfirmed):
        if _own_change(memory, now):
            return _tv_changed(screen, memory, reported, now)
        return _pause(screen, reported, now, memory, lux, f"fresh reading before a change; Art Frames set {sent}")
    want = sensor_curve.target(lux, curve, *brightness_range(settings))
    memory.pending = None
    if _rounded(want, reported, state):
        return "same"
    if memory.absent_since is not None and reported is not None and want > reported:
        return "empty"
    if reported == want:
        if unconfirmed or (sent is not None and reported != sent):
            merge(library, CONTROL, sent=reported, unconfirmed=0)  # the TV has the level now
        return "same"  # never set a level the TV already reports
    result, error = _send(screen, want)
    if error:
        return error
    fields = _sent(want, result, unconfirmed, now, reported)
    merge(library, CONTROL, lux=round(lux, 1), result="unconfirmed" if fields["unconfirmed"] else "brightness",
          **fields)
    return "brightness"


def request_samsung(library, want):
    """Ask the hub to turn the Samsung light sensor and Sleep After "off", or to "restore" them.

    "restore" also undoes the away actions (each keeps what it needs in its undo_key, so the control session
    can be forgotten). "restore" with nothing to undo clears the request: Art Frames never changed the TV.
    """
    with state_lock(library):
        state = dict(library.state(SAMSUNG) or {})
        if state.get("want") != want:
            state["tries"] = 0  # each request counts its own tries
        if want == "restore":
            control = library.state(CONTROL) or {}
            for action in AWAY_ACTIONS:
                value = action.keep(control) if control.get(action.flag) else None
                if value is not None:
                    state[action.undo_key] = value
            if not state.get("saved") and all(state.get(action.undo_key) is None for action in AWAY_ACTIONS):
                state.update(want=None)
                library.set_state(SAMSUNG, state)
                return
        else:
            # Control is on again before a restore ran: the new session takes over the dim and the blank
            # that wait for their undo, so someone back undoes them, and nothing is left without an owner.
            control = dict(library.state(CONTROL) or {})
            if state.get("undo_away") is not None:
                control.update(away=True, before_away=state["undo_away"])
            if state.get("undo_blank") is not None:
                control.update(blanked=True)
            if control != (library.state(CONTROL) or {}):
                library.set_state(CONTROL, control)
            for action in AWAY_ACTIONS:
                state[action.undo_key] = None  # control runs the away actions itself now
        state.update(want=want, result="waiting", at=time.time())
        library.set_state(SAMSUNG, state)


def _forget(library, keys):
    """Drop `keys` from the saved Samsung settings: Art Frames did not change them."""
    with state_lock(library):
        state = dict(library.state(SAMSUNG) or {})
        saved = {key: value for key, value in (state.get("saved") or {}).items() if key not in keys}
        state["saved"] = saved or None
        library.set_state(SAMSUNG, state)


def samsung_step(screen, cfg, memory, now):
    """Turn the Samsung settings off, or restore them, when asked and the TV is on in Art Mode.

    The values from before the change are saved first, so a restart in between still restores them.
    A restore first undoes the away actions, in reverse order (the blank, then the dim).
    A request made while this runs is kept: the result applies only to the request it acted on.
    """
    library = screen.library
    state = dict(library.state(SAMSUNG) or {})
    want = state.get("want")
    if want not in {"off", "restore"}:
        return None
    if now - memory.last_samsung_try < (BUSY_RETRY if state.get("result") == "busy" else SAMSUNG_RETRY):
        return "wait"
    # Sensor control's own blank (one a restore undoes, or one the session holds) does not hold this back.
    own = bool(state.get("undo_blank")) if want == "restore" else bool((library.state(CONTROL) or {}).get("blanked"))
    if tv_gate(screen, cfg, now, own_blank=own):
        return "waiting"
    memory.last_samsung_try = now
    memory.wrote_at = now  # it may change the light sensor or Sleep After
    ctl = screen.controller
    done = False
    left = []  # the settings this pass still has to turn off
    try:
        if want == "off":
            settings = ctl.display_settings()
            saved = dict(state.get("saved") or {})
            if settings.get("sensor") == "on":
                left.append("sensor")
            if settings.get("motion_timer") not in (None, "off", "unknown"):
                left.append("motion_timer")
            for key in left:
                saved.setdefault(key, settings[key])  # saved first, so a restart in between still restores
            merge(library, SAMSUNG, saved=saved or None)
            done = True
            for key in list(left):
                if key == "sensor":
                    reported = ctl.set_light_sensor(False).reported
                else:
                    reported = ctl.set_option("motion_timer", "off").reported
                left.remove(key)
                done = reported == "off" and done
        else:
            saved, done = state.get("saved") or {}, True
            for action in reversed(AWAY_ACTIONS):
                value = state.get(action.undo_key)
                if value is not None:
                    action.restore(ctl, value)
                    merge(library, SAMSUNG, **{action.undo_key: None})
                    merge(library, CONTROL, **{action.flag: False})
            if saved.get("sensor") == "on":
                done = ctl.set_light_sensor(True).reported == "on" and done
            # Sleep After is saved only when Art Frames changed it, so "off" is restored too (after a night).
            if saved.get("motion_timer") not in (None, "unknown"):
                done = ctl.set_option("motion_timer", saved["motion_timer"]).reported == saved["motion_timer"] and done
    except Busy:
        result = "busy"
    except DisplayError as error:
        result = "waiting" if error.code == "not-art-mode" else error.code
        if want == "off" and result in REFUSED and left:
            _forget(library, left)  # the TV refused them, so they are not Art Frames' to restore
    except Exception as error:
        log.warning("TV %s: the Samsung settings change failed (%s)", screen.id, type(error).__name__)
        result = "failed"
    else:
        result = "done" if done else "not-confirmed"
    with state_lock(library):
        fresh = dict(library.state(SAMSUNG) or {})
        if fresh.get("want") == want:
            tries = int(fresh.get("tries") or 0) + 1 if result == "not-confirmed" else 0
            fresh.update(result=result, at=now, action=want, tries=tries)
            if result == "done":
                fresh["want"] = None
                if want == "restore":
                    fresh["saved"] = None
            elif result in REFUSED or tries >= MAX_SAMSUNG_TRIES:
                # No endless retries. After "off", control runs, and "saved" keeps what Art Frames did change.
                # After "restore", the page asks the person to check the settings on the TV.
                fresh.update(want=None, tries=0)
                if want == "restore":
                    fresh.update(saved=None, **{action.undo_key: None for action in AWAY_ACTIONS})
                log.warning("TV %s: the TV did not %s its own light sensor (%s)", screen.id,
                            "turn off" if want == "off" else "turn back on", result)
        else:
            memory.last_samsung_try = 0.0  # a new request came in meanwhile: run it on the next tick
            if want == "restore" and result == "done" and fresh.get("want") == "off":
                fresh["saved"] = None  # restored: the next "off" saves the TV's values again
        library.set_state(SAMSUNG, fresh)
    log.info("TV %s: Samsung light sensor and Sleep After %s: %s", screen.id,
             "turned off" if want == "off" else "restored", result)
    return result


# The hub is stale when no tick finished for this long.
HUB_STALE_SECONDS = 10 * 60


class SensorHub:
    """Every TV's sensor link and control loop. web.py creates one; __main__.py starts it."""

    def __init__(self, displays, *, demo=False, link_factory=None, clock=time.time, discovery=None):
        self.displays, self.demo, self.clock = displays, demo, clock
        self.link_factory = link_factory or self._new_link
        self.links = {}  # tv id -> (signature, Link)
        self.memory = {}
        self.discovery = discovery or Discovery(demo=demo)
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = None
        self.started = self.last_pass = None
        self.results = {}  # tv id -> the last step() result, for pages
        # Set when a sensor sees someone come in: the loop runs a tick at once instead of after TICK.
        self.nudge = threading.Event()
        self.presence_entities = {}  # tv id -> the Sensor's presence_entity setting, for _arrived
        self.tick_started = None  # time.monotonic() of the loop's last tick

    def _new_link(self, tv_id, settings):
        clients = [sensors.DemoClient()] if self.demo else None
        return sensors.Link(settings, clients=clients, clock=self.clock, name=f"sensor-{tv_id[:8]}").start()

    def link(self, tv_id):
        with self.lock:
            item = self.links.get(tv_id)
            return item[1] if item else None

    def reconcile(self):
        """Start, replace or stop links to match each TV's sensor settings."""
        wanted = {}
        for item in self.displays.items():
            try:
                settings = self.displays.get(item["id"]).configs.load().sensor
            except ValueError:
                continue
            if settings.host:
                wanted[item["id"]] = settings
        with self.lock:
            self.presence_entities = {tv_id: settings.presence_entity for tv_id, settings in wanted.items()}
            for tv_id in list(self.links):
                signature, link = self.links[tv_id]
                if tv_id not in wanted or sensors.Link.signature(wanted[tv_id]) != signature:
                    link.stop()
                    del self.links[tv_id]
            for tv_id, settings in wanted.items():
                if tv_id not in self.links:
                    link = self.link_factory(tv_id, settings)
                    # The client thread only sets the event: the hub's thread contacts the TV.
                    if getattr(link, "readings", None) is not None:
                        link.readings.on_arrival = functools.partial(self._arrived, tv_id, link.readings)
                    self.links[tv_id] = (sensors.Link.signature(settings), link)

    def _arrived(self, tv_id, readings, object_id):
        """On the sensor client's thread: a binary sensor turned on. Wake the loop when it is the presence
        sensor that control uses. It only reads the readings and sets an event: it never contacts a TV."""
        snap = readings.snapshot()
        with self.lock:
            override = self.presence_entities.get(tv_id, "")
        if object_id == sensors.pick_presence(snap["entities"], snap["device"].get("project_name", ""), override):
            self.nudge.set()

    def tick(self, now=None):
        self.reconcile()
        items = self.displays.items()
        known = {item["id"] for item in items}
        for store in (self.memory, self.results):
            for tv_id in [tv_id for tv_id in store if tv_id not in known]:  # a removed TV
                del store[tv_id]
        for item in items:
            if self.stopping.is_set():
                return
            tv_id = item["id"]
            try:
                screen = self.displays.get(tv_id)
                cfg = screen.configs.load()
                moment = self.clock() if now is None else now
                memory = self.memory.setdefault(tv_id, Memory(background=True))
                if memory.wake_thread is not None and memory.wake_thread.is_alive():
                    self.results[tv_id] = "waking"  # its wake holds the TV: skip it until the wake ends
                    continue
                memory.wake_thread = None
                samsung_step(screen, cfg, memory, moment)
                self.results[tv_id] = step(screen, cfg, self.link(tv_id), memory, moment)
            except ValueError:
                continue  # removed while this tick ran
            except Exception as error:
                log.warning("TV %s: sensor control could not run (%s)", tv_id, type(error).__name__)
        self.last_pass = time.monotonic()

    @property
    def alive(self):
        """For health checks: None before start; else the thread runs and a tick finished recently."""
        if self.thread is None:
            return None
        return self.thread.is_alive() and time.monotonic() - (self.last_pass or self.started) < HUB_STALE_SECONDS

    def _loop(self):
        # This thread never waits for the TV lock: Controller._exclusive reads the wait from CALLER.
        controller_module.CALLER.wait = 0
        while not self.stopping.is_set():
            nudged = self.nudge.wait(TICK)  # TICK, or at once when someone comes in
            self.nudge.clear()
            if nudged and self.tick_started is not None:
                # At most one pass a second for arrivals: a sensor that flickers cannot flood the TV.
                self.stopping.wait(max(0.0, NUDGE_GAP - (time.monotonic() - self.tick_started)))
            if self.stopping.is_set():
                break
            self.tick_started = time.monotonic()
            try:
                self.tick()
            except Exception as error:
                log.warning("Sensor control could not run (%s); retrying", type(error).__name__)

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Sensor control already started")
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._loop, name="art-frames-sensors", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.nudge.set()
        with self.lock:
            for _, link in self.links.values():
                link.stop()
        if self.thread:
            self.thread.join(timeout=2)


# Health rows: the status_dot tone of each row tone, for templates and app.js.
DOTS = {"ok": "done", "problem": "failed", "info": "not-done"}
KEY_TITLES = {"key-needed": "Encryption key needed", "key-wrong": "Encryption key does not match",
              "key-not-used": "The sensor does not use a key"}
KEY_FIXES = {"key-needed": "Enter the api: encryption: key: from the sensor's ESPHome settings.",
             "key-wrong": "Enter the key again from the sensor's ESPHome settings.",
             "key-not-used": "Remove the saved key."}
KEY_ERRORS = set(KEY_TITLES)


def health_check(link, sensor, now):
    """The sensor's Health row, or None without a sensor. It never contacts the sensor.

    The same shape as web.health_checks() rows: {"id", "tone" ("ok", "problem" or "info"), "dot" (its
    status_dot tone), "title", "detail", "when", "fixes"}, plus "error", the LINK_ERRORS code. "when"
    says when Art Frames last heard from the sensor. A sensor that is not seen is a problem only while it
    controls the TV. Its fix, "sensor", links to the Sensor page; a key error's fix, "sensor-key", links
    to the address and key.
    """
    if not sensor.host:
        return None
    readings = link.readings if link else None
    snap = readings.snapshot() if readings else {}
    times = [at for _, at in (snap.get("values") or {}).values()]
    if snap.get("last_seen") is not None:
        times.append(snap["last_seen"])
    last = max(times) if times else None
    fresh = bool(readings and readings.fresh(now))
    code = snap.get("error", "")
    if not code and last is not None and not fresh:
        code = "lost"  # seen before, and quiet since

    def row(tone, title, detail="", fixes=()):
        return {"id": "sensor", "tone": tone, "dot": DOTS[tone], "title": title, "detail": detail,
                "when": ago(last, now) if last is not None else "", "fixes": list(fixes), "error": code}
    if fresh:
        return row("ok", "Sensor connected")
    if code in KEY_ERRORS:
        # A key error does not go away by itself: it shows at once, never as reconnecting.
        detail = KEY_FIXES[code]
        if sensor.enabled:
            detail += " Art Frames holds the brightness until the key works."
        return {**row("problem" if sensor.enabled else "info", KEY_TITLES[code], detail, ["sensor-key"]), "when": ""}
    if last is not None and now - last <= sensors.STALE_SECONDS:
        # A short drop: the link reconnects on its own, and control holds the level meanwhile.
        return row("info", "Sensor reconnecting")
    young = link is None or now - getattr(link, "created", now) <= sensors.STALE_SECONDS
    if last is None and young:
        return row("info", "Sensor connecting")  # the first tries, or the hub has not run yet
    detail = sensors.LINK_ERRORS.get(code, sensors.LINK_ERRORS["lost"])
    if sensor.enabled and last is not None:
        detail += " Art Frames does not change the TV until it hears from the sensor again."
    return row("problem" if sensor.enabled else "info", "Sensor not seen", detail, ["sensor"])


WAKE_ERRORS = {
    "not-confirmed": "Art Frames sent the wake signal, but the TV did not turn on in Art Mode.",
    "missing-mac": "Art Frames needs the TV's network card address for the wake signal. Find the TV again in TV settings.",
    "network": "Art Frames could not send the wake signal on this network.",
    "unpaired": "Pair the TV first.",
}


def wake_check(library, cfg, now):
    """A Health row when the screen did not turn on for someone in the room (a problem), or came on in TV
    mode (info), else None.

    It stays until a later try works, the TV is in Art Mode again, or sensor control turns off. Stored state
    only.
    """
    if not cfg.sensor.enabled:
        return None
    state = library.state(CONTROL) or {}
    failures = int(state.get("wake_failures") or 0)
    at = state.get("wake_at")
    when = ago(at, now) if isinstance(at, int | float) else ""
    if not failures:
        if state.get("wake_result") != "tv-mode":
            return None
        return {"id": "sensor-wake", "tone": "info", "dot": DOTS["info"],
                "title": f"{cfg.tv_name} came on in TV mode, not Art Mode",
                "detail": "Art Frames leaves the TV alone in TV mode. To show art, switch the TV to Art Mode. "
                          "This note clears then.", "when": when, "fixes": []}
    detail = WAKE_ERRORS.get(state.get("wake_result"), "The TV did not answer.")
    if isinstance(at, int | float):
        retry = datetime.fromtimestamp(at + WAKE_GAP * 2 ** min(failures, MAX_WAKE_BACKOFF), ZoneInfo(cfg.timezone))
        # Outside the hours, no wake runs: the next try is when the hours begin.
        after = f"{retry:%H:%M}" if in_hours(cfg.sensor, retry) else cfg.sensor.hours_start
        detail += f" If someone comes in after {after}, Art Frames tries again."
    return {"id": "sensor-wake", "tone": "problem", "dot": DOTS["problem"],
            "title": f"{cfg.tv_name} did not wake for someone", "detail": detail, "when": when,
            "fixes": ["sensor"]}


def hub_check(hub, enabled):
    """A Health problem row when sensor control stopped while a TV uses it, else None."""
    if not enabled or getattr(hub, "alive", None) is not False:
        return None
    return {"id": "sensor-hub", "tone": "problem", "dot": DOTS["problem"], "title": "Sensor control stopped",
            "detail": "Art Frames does not change the brightness from the sensors. Restart Art Frames.",
            "when": "", "fixes": []}
