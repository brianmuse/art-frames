"""The Sensor page of one TV (/tv/<tv>/sensor) and the Sensor card on TV settings.

The page finds ESPHome sensors, connects one, and sets how sensor control changes the TV.
It never shows the encryption key: the key field is always empty, and the page says only that a key is set.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    get_flashed_messages,
    redirect,
    render_template,
    request,
    url_for,
)
from pydantic import ValidationError

from . import room_presets, sensor_control, sensor_curve, sensors
from .config import (
    DARK_BELOW_CHOICES,
    MAX_CURVE_POINTS,
    OFF_AFTER_CHOICES,
    CardError,
    Sensor,
    StaleForm,
    first_off_after,
    without_mac,
)
from .controller import Busy
from .sensor_discovery import SearchBusy
from .television import SLEEP_AFTER_MINUTES, DisplayError, brightness_range
from .timewords import ago, how_long
from .web import duration, save_card

pages = Blueprint("sensors", __name__)
# Choices for the page's selects, in minutes.
AWAY_CHOICES = (0, 1, 5, 10, 15, 30, 60)
NEW_AWAY_AFTER = 5  # minutes: a new sensor's "Dim after the room is empty"
BLANK_CHOICES = (5, 10, 15, 30, 60, 120)
OFFSET_CHOICES = tuple(
    (step, "As set" if step == 0 else f"{abs(step)} step{'s' if abs(step) != 1 else ''} "
     f"{'brighter' if step > 0 else 'darker'}")
    for step in range(-sensor_curve.OFFSET_STEPS, sensor_curve.OFFSET_STEPS + 1))
TRANSPORTS = {"api": "Native API", "web": "Web server (fallback)"}
# What Art Frames adds to the Samsung line while a request waits.
SAMSUNG_RESULTS = {
    "not-confirmed": "The TV did not confirm every change. Art Frames tries again later.",
    "busy": "The TV was busy. Art Frames tries again in a moment.",
    "failed": "The TV did not answer. Art Frames tries again later.",
    "unpaired": "Pair this TV first.",
}
TV_ERRORS = {"not-art-mode": "Turn the TV on in Art Mode first.", "unpaired": "Pair this TV first.",
             "busy": "The TV is busy. Try again in a moment."}
STALE = ("The sensor settings changed after the page opened. Here is the latest version. "
         "Make your change again if you still need it.")
RANGE_UNKNOWN = "This TV's range shows after Art Frames reads it in Art Mode."
FIND_FAILED = ("Art Frames cannot search this network. In Docker's bridge mode, the search finds nothing: "
               "run Art Frames on the host network (compose.static.host.yaml). Enter the sensor's address "
               "instead.")
# A search result counts for "the same sensor" this long: later, its address may belong to another device.
FOUND_FRESH = 10 * 60
FIND_EMPTY = "No sensors answered. Check power and network, then try again, or enter its address."
# A sentence for each field's range error. The model's own checks (ValueError) bring their own sentence.
# The shortest Sleep After (Samsung's documented list): the TV turns itself off this long after Art Frames sets it.
SLEEP_AFTER = min(SLEEP_AFTER_MINUTES)
FIELD_ERRORS = {
    "host": "Use the sensor's private IP address or its .local name.",
    "port": "Use a port from 1 to 65535.",
    "lux": "Use more than 0 lux for each point.",
    "level": "Use a brightness in this TV's range.",
    "offset": "Pick an adjustment from the list.",
    "away_after": "Pick a time from the list.",
    "blank_after": "Pick a time from the list.",
    "off_after": "Pick a time from the list.",
    "dark_below": "Pick a light level from the list.",
    "hours_start": "Use a time such as 07:00.",
    "hours_end": "Use a time such as 23:00.",
}


def hub():
    return current_app.extensions["sensors"]


def plain(error):
    """One plain sentence for a sensor ValidationError: the model's own sentence, or the field's. Never a field path."""
    item = error.errors(include_input=False, include_url=False)[0]
    if item["type"] == "value_error":
        return item["msg"].removeprefix("Value error, ").rstrip(".") + "."
    field = next((str(part) for part in reversed(item["loc"]) if str(part) in FIELD_ERRORS), None)
    return FIELD_ERRORS[field] if field else "Check the values and try again."


def says_more(text, title):
    """True when `text` has a word that `title` does not, so a subtitle adds something."""
    words = set(re.findall(r"[0-9a-z]+", (text or "").casefold()))
    return bool(words - set(re.findall(r"[0-9a-z]+", (title or "").casefold())))


def device_id(name):
    """A short hash of a device's mDNS name with its MAC suffix, such as "akamatis-smart-device-005301", or
    "" without one. It tells two sensors of one model apart without showing the suffix."""
    name = (name or "").lower().removesuffix(".local")
    return hashlib.sha256(name.encode()).hexdigest()[:16] if name else ""


def same_sensor(item, host):
    """True when a found sensor is the one saved at `host`: the same address, or the .local name it answers to."""
    if item.get("host") == host:
        return True
    return host.lower().endswith(".local") and bool(item.get("device_id")) and item["device_id"] == device_id(host)


def shown_host(host):
    """The sensor's address for pages: a .local name without ESPHome's MAC suffix. The saved address keeps
    it, because the name must still resolve."""
    if host.lower().endswith(".local"):
        return without_mac(host[:-6]) + host[-6:]
    return host


def lux_text(lux):
    """Room light in plain words: one decimal under 10 lux, such as "3.1 lux" or "120 lux"."""
    return f"{lux:.1f} lux" if lux < 10 else f"{lux:.0f} lux"


def curve_line(screen, cfg, link, now):
    """The live line, such as "Room light 120 lux now → brightness 5", or "" without a light reading."""
    lux, _ = sensor_control.light_now(link, cfg.sensor, now)
    if lux is None:
        return ""
    low, high = brightness_range(sensor_control.art_display(screen, cfg))
    level = sensor_curve.target(lux, cfg.sensor.curve, low, high)
    return f"Room light {lux_text(lux)} now → {'brightness' if cfg.sensor.enabled else 'would set brightness'} {level}"


def control_text(state, now):
    """What sensor control did last, such as "Last change 2 minutes ago.", or "".

    It never names the level it sent: the live line names the level for the light now, and two numbers
    that disagree confuse people.
    """
    why = room_word(state)
    if state.get("tv_off"):
        off_at = state.get("off_at")
        when = ago(off_at, now) if isinstance(off_at, int | float) else ""
        return f"TV turned off because the room is {why}, {when}." if when else f"TV turned off because the room is {why}."
    if state.get("night") and not state.get("blanked"):
        return "The TV turns itself off for the night when the room is empty."
    if sensor_curve.paused(state, now) or not state.get("sent_at") or state.get("sent") is None:
        return ""
    when = ago(state["sent_at"], now)
    if state.get("blanked"):
        return f"Screen blank because the room is {why}, {when}."
    if state.get("away"):
        return f"Dimmed to {state['sent']} because the room is {why}, {when}."
    return f"Last change {when}."


def room_word(state):
    """Why the away steps hold: "dark" while the room is dark (Sensor.dark_below), else "empty"."""
    return "dark" if state.get("dark") else "empty"


def held(state):
    """True when sensor control holds the brightness: the TV did not confirm the last changes."""
    return int(state.get("unconfirmed") or 0) >= sensor_control.MAX_UNCONFIRMED


def control_word(state, cfg, status, now):
    """What sensor control does now, in a few words, for the Now page; "" when it is off.

    "Following room light", "Dimmed: room empty", "Screen blank: room empty", "Off for the night",
    "TV off: room empty", "TV off", "Waiting: someone is watching TV", "Waiting for the TV to answer",
    "Sensor paused until 21:40" or "Holding the brightness". Sensor control leaves the TV alone while someone watches TV or the TV does
    not answer, so those states never say "Following room light". They come first; a pause is then a
    second note, such as "TV off · sensor paused until 21:40".
    """
    if not cfg.sensor.enabled:
        return ""
    pause = ""
    if sensor_curve.paused(state, now):
        until = datetime.fromtimestamp(state["paused_until"], ZoneInfo(cfg.timezone)).strftime("%H:%M")
        pause = f"sensor paused until {until}"
    gate = sensor_control.status_gate(status)
    why = room_word(state)
    off = f"TV off: room {why}" if state.get("tv_off") else "Off for the night" if state.get("night") else "TV off"
    word = {"standby": off,
            "watching": "Waiting: someone is watching TV", "unknown": "Waiting for the TV to answer"}.get(gate)
    if word:
        return f"{word} · {pause}" if pause else word
    if pause:
        return pause[:1].upper() + pause[1:]
    if held(state):
        return "Holding the brightness"
    if state.get("blanked"):
        return f"Screen blank: room {why}"
    if state.get("away"):
        return f"Dimmed: room {why}"
    return "Following room light"


def presence_text(presence, since, now):
    """"Someone here", "Room empty" or "No reading", with how long when Art Frames knows it, such as
    "Room empty for 12 min"."""
    if presence is None:
        return "No reading"
    word = "Someone here" if presence else "Room empty"
    span = how_long(now - since) if since is not None else ""
    return f"{word} for {span}" if span else word


def live(screen, cfg, now=None):
    """What the sensor reports now, for the page and live.json. Never holds the key or a MAC address."""
    now = time.time() if now is None else now
    sensor = cfg.sensor
    link = hub().link(screen.id)
    readings = link.readings if link else None
    snap = readings.snapshot() if readings else {"entities": [], "device": {}, "transport": ""}
    project = snap["device"].get("project_name", "")
    presence_id = sensors.pick_presence(snap["entities"], project, sensor.presence_entity)
    light_id = sensors.pick_light(snap["entities"], project, sensor.light_entity)
    presence = readings.value("binary_sensor", presence_id, now) if readings else None
    light = readings.value("sensor", light_id, now) if readings else None
    friendly = snap["device"].get("friendly_name") or sensor.name
    product = sensors.product_name(project, friendly) if project or friendly else "Sensor"
    memory = hub().memory.get(screen.id)
    since = None if memory is None else memory.present_since if presence else memory.absent_since
    state = screen.library.state(sensor_control.CONTROL) or {}
    return {
        "check": sensor_control.health_check(link, sensor, now),
        "product": product,
        # The page's subtitle, only when it says more than the title.
        "friendly_name": friendly if says_more(friendly, product) else "",
        "transport": TRANSPORTS.get(snap["transport"], ""),
        "presence": presence_text(presence, since, now),
        "light": "No reading" if light is None else lux_text(light),
        "lux": light,
        "curve": curve_line(screen, cfg, link, now),
        "control": control_text(state, now),
        "word": control_word(state, cfg, screen.library.state("tv_status") or {}, now),
        "brightness": brightness_text(screen, cfg, now, state),
        "presence_id": presence_id, "light_id": light_id,
        "entities": snap["entities"],
    }


def brightness_text(screen, cfg, now=None, state=None):
    """The TV's brightness from the last stored Art Mode reading, such as "Brightness 5 · 3 minutes ago", or "".

    It adds "(changed on the TV)" when the reading is not the level sensor control set, outside a pause.
    """
    now = time.time() if now is None else now
    display = sensor_control.art_display(screen, cfg)
    level = sensor_control.as_level(display.get("brightness"))
    if level is None:
        return ""
    when = ago(display.get("checked_at"), now)
    text = f"Brightness {level} · {when}" if when else f"Brightness {level}"
    sent = sensor_control.as_level((state or {}).get("sent"))
    if cfg.sensor.enabled and sent is not None and sent != level and not sensor_curve.paused(state, now):
        text += " (changed on the TV)"
    return text


def room_view(screen, cfg, now=None):
    """The Room block of the Now page, from stored state only: it never contacts the TV or the sensor.

    {"sensor": bool, "presence", "light", "word", "brightness", "resume", "why"}; an empty value hides its
    row. "resume" shows Resume now while sensor control is paused or holds the brightness; "why" says why.
    """
    now = time.time() if now is None else now
    if not cfg.sensor.host:
        return {"sensor": False, "presence": "", "light": "", "word": "", "resume": False, "why": "",
                "offer": False, "held": False, "brightness": brightness_text(screen, cfg, now)}
    info = live(screen, cfg, now)
    state = screen.library.state(sensor_control.CONTROL) or {}
    _, _, resume = control_status(state, cfg, now)
    paused = sensor_curve.paused(state, now)
    why = ""
    if resume:
        why = "You changed the brightness on the TV." if paused else "The TV did not confirm the last brightness changes."
    # "offer": a paused level that the Sensor page offers to add as a point. "held": the hold links to Health.
    return {"sensor": True, "resume": resume, "why": why,
            "offer": bool(resume and paused and suggestion(state, cfg)), "held": bool(resume and not paused),
            **{key: info[key] for key in ("presence", "light", "word", "brightness")}}


def now_status(screen, cfg, status, demo, now=None):
    """The Now page's status sentence and the note on its picture, from stored state only.

    {"sentence": "Screen blank: room empty since 21:40", "overlay": {"tone", "title", "text"} or None}.
    tone "dark" covers a blank or off screen, "dim" a dimmed one, and "note" leaves the picture as it is.
    A plain Art Mode sentence is "" so the page can name what the TV shows instead.
    """
    from .web import tv_state  # web imports this module's blueprint late; a top import would loop

    now = time.time() if now is None else now
    zone = ZoneInfo(cfg.timezone)

    def clock(stamp):
        return datetime.fromtimestamp(stamp, zone).strftime("%H:%M")

    gate = sensor_control.status_gate(status)
    if demo and gate == "unknown":
        gate = None  # the demo TV has no status: show it in Art Mode
    off = {"tone": "dark", "title": "TV off", "text": "Art Frames shows this when the TV is on in Art Mode."}
    watching = {"tone": "dim", "title": "Watching TV",
                "text": "Art Frames leaves the TV alone until it is back in Art Mode."}
    if not (cfg.sensor.host and cfg.sensor.enabled):
        if gate == "standby":
            return {"sentence": "TV off", "overlay": off}
        if gate == "watching":
            return {"sentence": "Someone is watching TV", "overlay": watching}
        if gate == "unknown":
            return {"sentence": tv_state(status, False)["word"], "overlay": None}
        return {"sentence": "", "overlay": None}
    state = screen.library.state(sensor_control.CONTROL) or {}
    memory = hub().memory.get(screen.id)
    since = f" since {clock(memory.absent_since)}" if memory is not None and memory.absent_since else ""
    # The TV's own state comes first. A pause is then a second note: "TV off · sensor paused until 00:00".
    pause = f"sensor paused until {clock(state['paused_until'])}" if sensor_curve.paused(state, now) else ""

    def said(sentence, overlay):
        return {"sentence": f"{sentence} · {pause}" if pause else sentence, "overlay": overlay}

    if gate == "standby":
        wake_at, off_at = state.get("wake_at"), state.get("off_at")
        if (state.get("tv_off") and memory is not None and memory.present_since is not None
                and isinstance(wake_at, int | float) and isinstance(off_at, int | float)
                and wake_at > off_at and wake_at >= memory.present_since
                and (sensor_control.wake_check(screen.library, cfg, now) or {}).get("tone") == "problem"):
            # Someone came in after the off, and that screen-on try failed: the Health row says why and when it
            # tries again.
            text = "Someone is here, but the TV did not turn on. See Health."
            return said(text, {"tone": "dark", "title": "TV off", "text": text})
        if state.get("tv_off") and state.get("dark"):
            inside = sensor_control.in_hours(cfg.sensor, datetime.fromtimestamp(now, zone))
            text = ("It turns on when the light is back and someone is there." if inside
                    else f"It turns on when the light is back and someone comes in after {cfg.sensor.hours_start}.")
            return said(f"TV off: room dark. {text}", {"tone": "dark", "title": "TV off", "text": text})
        if state.get("tv_off"):
            inside = sensor_control.in_hours(cfg.sensor, datetime.fromtimestamp(now, zone))
            text = ("It turns on when someone comes in." if inside
                    else f"It turns on when someone comes in after {cfg.sensor.hours_start}.")
            return said(f"TV off: room empty{since}. {text}", {"tone": "dark", "title": "TV off", "text": text})
        if state.get("night"):
            text = f"It turns on when someone comes in after {cfg.sensor.hours_start}."
            return said(f"Off for the night. {text}", {"tone": "dark", "title": "Off for the night", "text": text})
        return said("TV off", off)
    if gate == "watching":
        return said("Someone is watching TV. Art Frames waits.", watching)
    if gate == "unknown":
        return said("Waiting for the TV to answer", None)
    if pause:
        # The Room block says why and offers Resume now, so the picture has no note.
        return {"sentence": pause[:1].upper() + pause[1:], "overlay": None}
    if held(state):
        return {"sentence": "Holding the brightness: the TV did not confirm the last changes", "overlay": None}
    if state.get("dark") and (state.get("blanked") or state.get("away")):
        title = "Screen blank" if state.get("blanked") else "Dimmed"
        return {"sentence": f"{title}: room dark",
                "overlay": {"tone": "dark" if state.get("blanked") else "dim", "title": title,
                            "text": "Room dark. It comes back when the light is back and someone is there."}}
    if state.get("blanked"):
        return {"sentence": f"Screen blank: room empty{since}",
                "overlay": {"tone": "dark", "title": "Screen blank",
                            "text": f"Room empty{since}. It comes back when someone walks in."}}
    if state.get("away"):
        return {"sentence": f"Dimmed: room empty{since}",
                "overlay": {"tone": "dim", "title": "Dimmed",
                            "text": f"Room empty{since}. It brightens when someone walks in."}}
    level = sensor_control.as_level(state.get("sent"))
    return {"sentence": f"brightness {level} from room light" if level is not None else "following room light",
            "overlay": None}


def samsung_text(library, tv):
    """One statement about the TV's own light sensor and Sleep After, or "" when Art Frames never changed them."""
    state = library.state(sensor_control.SAMSUNG) or {}
    action = state.get("want") or state.get("action")
    if not action:
        return ""
    waiting = state.get("want") is not None
    if action == "restore" and not waiting and state.get("result") in sensor_control.REFUSED:
        return "The TV refused to turn its own light sensor and Sleep After back on. Check them on the TV."
    if action == "restore" and not waiting and state.get("result") == "not-confirmed":
        return "The TV did not turn its own light sensor and Sleep After back on. Check them on the TV."
    if action == "off" and not waiting and state.get("result") in sensor_control.REFUSED:
        return "The TV refused to turn off its own light sensor and Sleep After. Turn them off on the TV."
    if action == "off" and not waiting and state.get("result") == "not-confirmed":
        return "The TV did not turn off its own light sensor and Sleep After. Check them on the TV."
    if action == "off":
        text = (f"Art Frames turns off the TV's own light sensor and Sleep After the next time {tv} is on in Art Mode."
                if waiting else f"The TV's own light sensor and Sleep After are off while the sensor controls {tv}.")
    else:
        text = (f"Art Frames turns the TV's own light sensor and Sleep After back on the next time {tv} is on in "
                "Art Mode." if waiting else "Art Frames turned the TV's own light sensor and Sleep After back on.")
    extra = SAMSUNG_RESULTS.get(state.get("result")) if waiting else None
    return f"{text} {extra}" if extra else text


def samsung_settled(library):
    """True when the last request finished with the TV's own settings back on: nothing left to say once removed."""
    state = library.state(sensor_control.SAMSUNG) or {}
    return state.get("want") is None and state.get("action") == "restore" and state.get("result") == "done"


def control_status(state, cfg, now):
    """The Sensor control row and the footer note: (row, note, resume). resume shows "Resume now"."""
    if not cfg.sensor.enabled:
        return "Off", "", False
    if sensor_curve.paused(state, now):
        until = datetime.fromtimestamp(state["paused_until"], ZoneInfo(cfg.timezone)).strftime("%H:%M")
        changed = ago(state.get("paused_at") or now, now)
        return (f"Paused until {until}", f"Paused until {until}. Someone changed the brightness on the TV {changed}. "
                "Sensor control starts again then.", True)
    if held(state):
        return ("Holding the brightness", f"The TV did not confirm the last {sensor_control.MAX_UNCONFIRMED} "
                "brightness changes, so Art Frames holds the brightness. Check the TV, then resume.", True)
    return "On", "", False


@pages.app_context_processor
def sensor_context():
    def sensor_summary():
        """The Sensor card on TV settings: its status row and whether sensor control is on."""
        cfg = g.screen.configs.load()
        if not cfg.sensor.host:
            return None
        check = sensor_control.health_check(hub().link(g.screen.id), cfg.sensor, time.time())
        return {**check, "enabled": cfg.sensor.enabled}
    return {"sensor_summary": sensor_summary}


def page_values(sensor):
    """The cards' values from the saved settings."""
    curve = sensor.curve
    return {"enabled": sensor.enabled, "away_after": sensor.away_after, "blank_after": sensor.blank_after,
            "off_after": off_value(sensor.off_after), "dark_below": dark_value(sensor.dark_below),
            "hours_start": sensor.hours_start,
            "hours_end": sensor.hours_end, "offset": curve.offset,
            "points": [{"lux": f"{point.lux:g}", "level": str(point.level)} for point in curve.points],
            "new_lux": "", "new_level": ""}


def off_value(minutes):
    """The off_after select's value: "never" for None, else the minutes as text."""
    return "never" if minutes is None else str(minutes)


def dark_value(lux):
    """The dark_below select's value: "never" for None, else the lux as short text ("5", "0.5")."""
    return "never" if lux is None else f"{lux:g}"


def dark_choices(saved):
    """The "Blank when the room is darker than" select: Never, then each light level. A saved level that is not
    in the list (such as one from the config file) stays in it, so a save never changes it without a pick."""
    levels = sorted({*map(float, DARK_BELOW_CHOICES), *([saved] if saved else [])})
    return [("never", "Never"), *((dark_value(lux), lux_words(lux)) for lux in levels)]


def lux_words(lux):
    """A light level of a setting, such as "1 lux" or "0.5 lux". Live readings use lux_text."""
    return f"{lux:g} lux"


def off_choices(saved):
    """The "Turn the TV off" select: Never, then every time. The Blank select in the same form can change, so
    the save refuses an off time that is not later than the blank. The saved time stays in the list (also one
    from the config file that is not a choice), so a save never changes it without a pick."""
    times = sorted({*OFF_AFTER_CHOICES, *([saved] if saved else [])})
    return [("never", "Never"), *((off_value(value), duration(value * 60)) for value in times)]


def away_words(minutes):
    """A dim or blank time for the page: "At once", "5 min", "1 hour"."""
    return "At once" if not minutes else duration(minutes * 60)


def diff_words(minutes):
    """A time in the change line: "0 min" reads better than "At once" after "Dim after"."""
    return duration(minutes * 60) if minutes else "0 min"


def what_it_does(sensor):
    """What sensor control does with the saved settings, in short lines, under the Room card."""
    if sensor.hours_start == sensor.hours_end:
        lines = ["The TV turns on when someone comes in, at any time."]
    else:
        lines = [f"The TV turns on when someone comes in, from {sensor.hours_start} to {sensor.hours_end}."]
    dim = "dims at once" if not sensor.away_after else f"dims after {away_words(sensor.away_after)}"
    blank = f"blanks after {away_words(sensor.blank_after)}"
    if sensor.off_after is None:
        lines.append(f"When the room is empty, it {dim} and {blank}.")
    else:
        # The TV turns itself off with its own Sleep After timer, so it goes off that much later.
        lines.append(f"When the room is empty, it {dim}, {blank} and turns off about {SLEEP_AFTER} min after "
                     f"{away_words(sensor.off_after)}.")
    if sensor.hours_start != sensor.hours_end:
        lines.append(f"After {sensor.hours_end}, it turns off {SLEEP_AFTER} min after the room is empty.")
    if sensor.dark_below is not None:
        off = "" if sensor.off_after is None else f" and turns off about {SLEEP_AFTER} min later"
        lines.append(f"When the room is darker than {lux_words(sensor.dark_below)}, it blanks at once{off}, "
                     "and does not turn on until the light is back.")
    return lines


def rooms_view(sensor, picked, low, high, first=False, known=True):
    """The Room card: its radio cards, the matching preset, and the preset a Custom setting is based on.

    picked is the stored ROOM_STATE. "Custom" shows only after someone picked a room. On a first setup,
    Living room starts selected. "stale": the picked room was set for another range (before Art Frames
    read the TV), so the page offers to apply it for this TV. Only a range read from the TV (known) can
    make a pick stale: an unknown range is SAFE_RANGE, not the TV's.
    """
    stale = room_presets.stale(sensor, picked, low, high) if known else None
    match = room_presets.current(sensor, low, high) or stale
    base = None if match else room_presets.BY_KEY.get((picked or {}).get("room"))
    start = None if match or not first else room_presets.BY_KEY["living"]
    cards = [{"key": room.key, "name": room.name, "line": room.line, "spark": room_presets.sparkline(room),
              "checked": room is (match or start),
              "diff": "" if match is room else room_presets.changes(sensor, room, low, high, diff_words)}
             for room in room_presets.ROOMS]
    return {"cards": cards, "match": match, "base": base, "stale": stale}


def tv_range(screen, cfg):
    """(lowest, highest, known): the TV's brightness range from the last stored reading."""
    display = sensor_control.art_display(screen, cfg)
    low, high = brightness_range(display)
    return low, high, display.get("min") is not None and display.get("max") is not None


def suggestion(state, cfg):
    """A level someone set on the TV, to offer as a point: {"lux", "level"}, or None."""
    suggest = state.get("suggest")
    if not isinstance(suggest, dict):
        return None
    lux, level = suggest.get("lux"), sensor_control.as_level(suggest.get("level"))
    if not isinstance(lux, int | float) or lux <= 0 or level is None:
        return None
    if any(sensor_curve.same_point(point.lux, lux) and point.level == level for point in cfg.sensor.curve.points):
        return None  # the curve has it already
    return {"lux": lux, "level": level}


def flashed(*cards):
    """Whether this request has a note for one of the cards (web.panel_notes reads the same notes)."""
    places = {f"card-{card}" for card in cards}
    return any(category.partition(":")[0] in places for category, _ in get_flashed_messages(with_categories=True))


def page(edit=None, code=200):
    cfg = g.screen.configs.load()
    sensor, now = cfg.sensor, time.time()
    discovery = hub().discovery
    info = live(g.screen, cfg, now)
    state = g.screen.library.state(sensor_control.CONTROL) or {}
    key_set = bool(sensor.key.get_secret_value())
    values = page_values(sensor)
    low, high, range_known = tv_range(g.screen, cfg)
    if range_known:
        # An older file's "end of the range" loads as 0 or 100 (config.OLD_CURVE_FIELDS): show the TV's end.
        values["points"] = [{**point, "level": str(max(low, min(int(point["level"]), high)))}
                            for point in values["points"]]
    cards = {
        "connection": {"host": sensor.host, "port": sensor.port},
        "readings": {"presence_entity": sensor.presence_entity, "light_entity": sensor.light_entity},
        "control": values, "away": values,
    }
    errors = {}
    if edit:
        cards[edit["card"]] = edit["values"]
        errors[edit["card"]] = edit["error"]
    check = info["check"] or {}
    key_problem = check.get("error") in sensor_control.KEY_ERRORS and check.get("tone") != "ok"
    if key_problem:
        errors.setdefault("connection", sensors.LINK_ERRORS[check["error"]])
    status_row, status_note, can_resume = control_status(state, cfg, now)
    display = sensor_control.art_display(g.screen, cfg)
    preset_range = (low, high) if range_known else room_presets.SAFE_RANGE
    picked = g.screen.library.state(room_presets.ROOM_STATE) or {}
    room_chosen = picked.get("room") in room_presets.BY_KEY
    lux_now = info["lux"]
    chart = room_presets.chart(sensor_curve.anchors(sensor.curve, low, high), low, high, sensor.curve.offset,
                               lux_now, None if lux_now is None else sensor_curve.target(lux_now, sensor.curve, low, high))
    return render_template(
        "sensor.html", sensor=sensor, cards=cards, errors=errors, live=info,
        found=discovery.found,
        key_set=key_set, key_problem=key_problem,
        presence_choices=sensors.presence_choices(info["entities"]), light_choices=sensors.light_choices(info["entities"]),
        offset_choices=OFFSET_CHOICES,
        away_choices=[(value, "At once" if not value else duration(value * 60)) for value in AWAY_CHOICES],
        blank_choices=[(value, duration(value * 60)) for value in BLANK_CHOICES],
        off_choices=off_choices(sensor.off_after), sleep_after=SLEEP_AFTER,
        dark_choices=dark_choices(sensor.dark_below),
        what_it_does=what_it_does(sensor),
        rooms=rooms_view(sensor, picked, *preset_range, first=not sensor.enabled and not room_chosen,
                         known=range_known),
        room_chosen=room_chosen,
        chart=chart, points_open=bool(errors.get("control") or flashed("control")),
        level_range=(low, high), range_known=range_known, max_points=MAX_CURVE_POINTS,
        here={"lux": info["lux"], "level": sensor_control.as_level(display.get("brightness")),
              "lux_text": "" if info["lux"] is None else lux_text(info["lux"])},
        suggest=suggestion(state, cfg),
        wake_problem=sensor_control.wake_check(g.screen.library, cfg, now),
        # Open for this request's errors and notes only: the search results are shared by every page.
        advanced_open=bool(errors.get("connection") or errors.get("readings")
                           or flashed("connection", "readings", "find")),
        address_summary=f"{shown_host(sensor.host)} · {'key set' if key_set else 'no key'}",
        key_error=check.get("error") if key_problem else "", shown_host=shown_host,
        is_saved=lambda item: same_sensor(item, sensor.host),
        control_row=status_row, control_note=status_note, can_resume=can_resume,
        samsung_text=samsung_text(g.screen.library, cfg.tv_name),
        samsung_settled=samsung_settled(g.screen.library),
        sensor_version=version(sensor),
    ), code


def version(sensor):
    """A short hash of the saved sensor settings, without the key. A form drawn for another version is out of date."""
    data = json.dumps(sensor.model_dump(mode="json", exclude={"key"}), sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def save(card, values, change, message="Saved.", *, versioned=False, done=None, anchor=None):
    """Save one card of the Sensor page with web.save_card: a note in the card, or the page again with the error.

    change(raw) edits the saved "sensor" block. With versioned=True, a form drawn for older settings (its
    sensor_version) is refused with StaleForm: the page opens with the latest settings and a note in the card.
    `anchor` is where the page opens after a save, when it is not the card.
    """
    def store(update):
        def mutate(raw):
            sensor = raw.setdefault("sensor", Sensor().model_dump())
            if versioned and request.form.get("sensor_version", "") != version(Sensor.model_validate(sensor)):
                raise StaleForm(card, STALE)
            update(sensor)
            try:
                Sensor.model_validate(sensor)
            except ValidationError as error:
                raise CardError(plain(error)) from None
        g.screen.configs.mutate(mutate)
    return save_card(store, page, card, values, change, endpoint="sensors.sensor_page", anchor=anchor,
                     message=message, done=done)


def still_off():
    return not g.screen.configs.load().sensor.enabled


@pages.get("/tv/<tv>/sensor")
def sensor_page(tv):
    return page()


@pages.get("/tv/<tv>/sensor/live.json")
def live_json(tv):
    info = live(g.screen, g.screen.configs.load())
    return {key: info[key] for key in ("check", "product", "transport", "presence", "light", "curve", "control", "word", "brightness", "lux")}


@pages.post("/tv/<tv>/sensor/find")
def find(tv):
    try:
        found = hub().discovery.search()
    except SearchBusy as error:
        flash(str(error), "card-find:info")
    else:
        if found:
            flash(f"Found {len(found)} sensor{'s' if len(found) != 1 else ''}.", "card-find:success")
        else:
            flash(FIND_FAILED if hub().discovery.failed else FIND_EMPTY, "card-find:info")
    return redirect(url_for("sensors.sensor_page", _anchor="find"))


@pages.post("/tv/<tv>/sensor/connect")
def connect(tv):
    """Connect a sensor: a found one, or a typed address. A new sensor's readings are picked again, and
    sensor control turns off (the hub restores the Samsung settings, see sensor_control.release)."""
    form = request.form
    port = form.get("port", "").strip() or "6053"
    values = {"host": form.get("host", "").strip(), "port": int(port) if re.fullmatch(r"[0-9]{1,5}", port) else 0}
    key = form.get("key", "").strip()

    def change(raw):
        if not values["host"]:
            raise CardError("Enter the sensor's address.")
        saved_host = raw.get("host", "")
        # A found sensor saved by its .local name is the same sensor: keep its settings, update the address.
        discovery = hub().discovery
        recent = discovery.at is not None and time.time() - discovery.at < FOUND_FRESH
        same = saved_host == values["host"] or recent and any(
            same_sensor(item, saved_host) for item in discovery.found or () if item.get("host") == values["host"])
        name = sensors.without_mac(form.get("name", "").strip()[:80])
        raw.update(host=values["host"], port=values["port"], name=name or (raw.get("name", "") if same else ""))
        if key or not same or "remove_key" in form:
            raw["key"] = key  # an empty field keeps the saved key for the same sensor
        if not same:
            raw.update(presence_entity="", light_entity="", enabled=False)
            other.append(True)
        if not saved_host:
            # A first sensor dims after 5 minutes, not at once: a short trip out of the room keeps the picture.
            raw["away_after"] = NEW_AWAY_AFTER
    def done():
        sensor_control.release(g.screen.library, still_off)
        if other:
            g.screen.library.delete_state(room_presets.ROOM_STATE)  # a new sensor asks for its room again

    other = []
    return save("connection", values, change, "Connecting. The readings show here once the sensor answers.",
                anchor="sensor-status", done=done)


@pages.post("/tv/<tv>/sensor/room")
def save_room(tv):
    """Save the Room card: a preset (room, or reset), the Darker/Brighter offset, and on a first setup,
    sensor control on.

    A preset sets the points, the dim, blank and TV off times and the hours for this TV's range (0-10 until Art
    Frames reads it). The offset stays as typed. Without a preset, only the offset changes.
    """
    form = request.form
    cfg = g.screen.configs.load()
    key = form.get("reset") or form.get("room", "")
    room = room_presets.BY_KEY.get(key)
    # Only the main button turns sensor control on. Enter (the hidden Save first in the form), Save only
    # and Reset leave it as it is.
    turn_on = not cfg.sensor.enabled and form.get("enable") == "on" and "save_only" not in form and "reset" not in form
    low, high, known = tv_range(g.screen, cfg)
    preset_range = (low, high) if known else room_presets.SAFE_RANGE
    values = {**page_values(cfg.sensor), "offset": form.get("offset", "")}

    def change(raw):
        if key and room is None:
            raise CardError("Pick a room from the list.")
        offset = _number(form, "offset", message="Pick Darker or Brighter from the list.")
        if room is None:
            raw.setdefault("curve", {})["offset"] = offset
        else:
            preset = room_presets.settings(room, *preset_range)
            raw["curve"] = {"points": preset.pop("points"), "offset": offset}
            raw.update(preset)
        if turn_on:
            raw["enabled"] = True

    def done():
        library = g.screen.library
        if room is not None:
            library.set_state(room_presets.ROOM_STATE, {"room": room.key, "range": list(preset_range)})
        if turn_on:
            sensor_control.reset_control(library)
            sensor_control.request_samsung(library, "off")

    if turn_on:
        message = "Sensor control is on." + (f" The TV follows {room.name} within a minute." if room else "")
    elif not cfg.sensor.enabled:
        message = "Saved. Sensor control is still off."
    elif room is not None:
        message = f"Saved. The TV follows {room.name} within a minute."
    else:
        message = "Saved. The brightness follows the new points within a minute."
    return save("room", values, change, message, versioned=True, done=done)


@pages.post("/tv/<tv>/sensor/readings")
def save_readings(tv):
    values = {key: request.form.get(key, "").strip() for key in ("presence_entity", "light_entity")}
    return save("readings", values, lambda raw: raw.update(values), versioned=True)


def _number(form, name, kind=int, empty=False, message="Enter numbers in every light field."):
    raw = form.get(name, "").strip()
    if empty and not raw:
        return None
    try:
        return kind(raw)
    except ValueError:
        raise CardError(message) from None


@pages.post("/tv/<tv>/sensor/switch")
def switch(tv):
    """Turn sensor control on or off with one click.

    On: the hub turns the Samsung light sensor and Sleep After off, and the control session starts new.
    Off: the hub restores them and undoes the away steps. Both wait until the TV is on in Art Mode.
    """
    turn_on = request.form.get("enabled") == "on"
    values = page_values(g.screen.configs.load().sensor)
    before = {}

    def change(raw):
        before["enabled"] = bool(raw.get("enabled"))
        raw["enabled"] = turn_on

    def done():
        if turn_on == before["enabled"]:
            return
        library = g.screen.library
        if turn_on:
            sensor_control.reset_control(library)
            sensor_control.request_samsung(library, "off")
        else:
            sensor_control.release(library, still_off)
    return save("sensor-control", {**values, "enabled": turn_on}, change,
                "Sensor control is on." if turn_on else "Sensor control is off.", versioned=True, done=done)


def posted_points(form):
    """The point rows of the curve form: [(lux text, level text)], with the new row last when it has text."""
    try:
        count = min(int(form.get("count", "0")), MAX_CURVE_POINTS)
    except ValueError:
        count = 0
    rows = [(form.get(f"lux_{index}", "").strip(), form.get(f"level_{index}", "").strip()) for index in range(count)]
    new = (form.get("new_lux", "").strip(), form.get("new_level", "").strip())
    return rows, new


def typed_points(form, cfg, skip=None):
    """The curve's points as typed: [{"lux", "level"}]. Row `skip` is left out (its Remove button).

    A row with both fields empty is dropped. A new or changed level must be inside the TV's range, so it
    waits until Art Frames has read the range in Art Mode. Raises CardError with one sentence.
    """
    rows, new = posted_points(form)
    low, high, known = tv_range(g.screen, cfg)
    saved = {(point.lux, point.level) for point in cfg.sensor.curve.points}
    points = []
    for index, (lux, level) in enumerate([*rows, new]):
        if index == skip or (not lux and not level):
            continue
        if not lux or not level:
            raise CardError("Enter both the light and the brightness of each point.")
        point = {"lux": _number({"v": lux}, "v", float), "level": _number({"v": level}, "v")}
        if (point["lux"], point["level"]) not in saved:
            if not known:
                raise CardError(RANGE_UNKNOWN)
            if not low <= point["level"] <= high:
                raise CardError(f"Use a brightness from {low} to {high}. That is this TV's range.")
        points.append(point)
    return points


def tv_level_now(cfg):
    """(level, error): the TV's brightness read now, in Art Mode. The read never wakes the TV."""
    try:
        level = sensor_control.as_level(g.screen.controller.display_settings().get("brightness"))
    except Busy:
        return None, TV_ERRORS["busy"]
    except DisplayError as error:
        return None, TV_ERRORS.get(error.code, "The TV did not answer. Try again.")
    except Exception as error:
        current_app.logger.warning("The TV brightness could not be read (%s)", type(error).__name__)
        return None, "The TV did not answer. Try again."
    return (level, "") if level is not None else (None, "The TV does not report its brightness.")


@pages.post("/tv/<tv>/sensor/control")
def save_control(tv):
    """Save the curve as typed. The card's other buttons post this form too, so typed edits are never lost:

    - remove=N: save without row N.
    - here=1 ("Add a point from the TV"): read the TV's brightness now and add it at the room light now.
    - offer=add: add the level someone set on the TV, and end the pause it started.
    """
    form = request.form
    cfg = g.screen.configs.load()
    library = g.screen.library
    rows, new = posted_points(form)
    values = {**page_values(cfg.sensor), "offset": form.get("offset", ""),
              "points": [{"lux": lux, "level": level} for lux, level in rows], "new_lux": new[0], "new_level": new[1]}
    remove = form.get("remove", "")
    skip = int(remove) if re.fullmatch(r"[0-9]{1,2}", remove) and int(remove) < len(rows) else None
    added, message, done, anchor = None, "Saved. The brightness follows the new points within a minute.", None, None
    if form.get("here") == "1":
        lux, _ = sensor_control.light_now(hub().link(g.screen.id), cfg.sensor, time.time())
        if lux is None:
            return page({"card": "control", "values": values,
                         "error": "There is no light reading yet. Wait for the sensor, then try again."}, 400)
        level, text = tv_level_now(cfg)
        if text:
            return page({"card": "control", "values": values, "error": text}, 400)
        added = (max(round(lux, 1), 0.1), level)
        message = f"Added {added[0]:g} lux at brightness {level}."

        def done():
            # The level was set by hand on purpose, so it starts no pause.
            sensor_control.accept_level(library, level)
    elif form.get("offer") == "add":
        offer = suggestion(library.state(sensor_control.CONTROL) or {}, cfg)
        if offer is None:
            return page({"card": "control", "values": values, "error": "There is no change from the TV to add."}, 400)
        low, high, _ = tv_range(g.screen, cfg)
        added = (offer["lux"], max(low, min(offer["level"], high)))
        message = f"Added {added[0]:g} lux at brightness {added[1]}. Sensor control is back on."
        anchor = "sensor-status"  # the offer is in the status card

        def done():
            sensor_control.merge(library, sensor_control.CONTROL, suggest=None)
            sensor_control.accept_level(library, offer["level"])
    elif skip is not None:
        message = f"Removed the point at {rows[skip][0]} lux."

    def change(raw):
        points = typed_points(form, cfg, skip)
        if added is not None:
            if len(points) >= MAX_CURVE_POINTS and not any(
                    sensor_curve.same_point(point["lux"], added[0]) for point in points):
                raise CardError(f"The curve has {MAX_CURVE_POINTS} points. Remove one first.")
            points = sensor_curve.with_point(points, *added)
        raw["curve"] = {"points": points, "offset": _number(form, "offset")}
    return save("control", values, change, message, versioned=True, done=done, anchor=anchor)


@pages.post("/tv/<tv>/sensor/suggestion")
def ignore_suggestion(tv):
    """Not now: forget the level someone set on the TV. The pause it started goes on."""
    cfg = g.screen.configs.load()
    library = g.screen.library
    state = library.state(sensor_control.CONTROL) or {}
    sensor_control.merge(library, sensor_control.CONTROL, suggest=None)
    text = "Not added."
    if sensor_curve.paused(state, time.time()):
        until = datetime.fromtimestamp(state["paused_until"], ZoneInfo(cfg.timezone)).strftime("%H:%M")
        text += f" Sensor control stays paused until {until}."
    flash(text, "card-sensor-status:info")
    return redirect(url_for("sensors.sensor_page", _anchor="sensor-status"))


@pages.post("/tv/<tv>/sensor/away")
def save_away(tv):
    """Save the away steps (dim, blank, TV off), the room dark level and the set hours. The hub undoes the steps
    and starts them again."""
    form = request.form
    names = ("away_after", "blank_after", "off_after", "dark_below", "hours_start", "hours_end")
    kept = ("off_after", "dark_below")
    saved = page_values(g.screen.configs.load().sensor)
    # A form without the TV off or the room dark field (an older page) keeps the saved value.
    values = {**saved, **{name: form.get(name, saved[name] if name in kept else "").strip() for name in names}}

    def change(raw):
        if "dark_below" in form:
            if values["dark_below"] == "never":
                raw["dark_below"] = "never"  # None would load as the default level
            else:
                lux = _number(form, "dark_below", float, message="Pick a light level from the list.")
                if not 0 < lux <= 1000:
                    raise CardError("Pick a light level from the list.")
                raw["dark_below"] = lux
        away = _number(form, "away_after", message="Pick a time from the list.")
        blank = _number(form, "blank_after", message="Pick a time from the list.")
        if away and blank <= away:
            raise CardError("Blank later than you dim.")
        raw.update(away_after=away, blank_after=blank, hours_start=values["hours_start"],
                   hours_end=values["hours_end"])
        # A form without the TV off field (an older page) keeps the saved off time while it is still later than
        # the new blank. Else it takes the off time for the new blank, as older settings do (first_off_after).
        if "off_after" not in form:
            if raw.get("off_after") is not None and raw["off_after"] <= blank:
                raw["off_after"] = first_off_after(blank)
            return
        if values["off_after"] == "never":
            raw["off_after"] = None
            return
        off = _number(form, "off_after", message="Pick a time from the list.")
        if off <= blank:
            raise CardError("Turn the TV off later than you blank.")
        raw["off_after"] = off
    return save("away", values, change, "Saved. The room-empty timer starts again now.", versioned=True)


@pages.post("/tv/<tv>/sensor/resume")
def resume(tv):
    """End the pause. back=now returns to the Now page; else the Sensor page's status card."""
    sensor_control.resume(g.screen.library)
    if request.form.get("back") == "now":
        flash("Sensor control is on again.", "success")
        return redirect(url_for("tv_overview"))
    flash("Sensor control is on again.", "card-sensor-status:success")
    return redirect(url_for("sensors.sensor_page", _anchor="sensor-status"))


@pages.post("/tv/<tv>/sensor/remove")
def remove(tv):
    """Forget the sensor. The hub restores the Samsung settings Art Frames turned off, when the TV is in Art Mode."""
    g.screen.configs.mutate(lambda raw: raw.update(sensor=Sensor().model_dump()))
    sensor_control.release(g.screen.library)
    g.screen.library.delete_state(room_presets.ROOM_STATE)
    flash("Removed the sensor.", "card-find:success")  # the status card is gone; Find sensors shows next
    return redirect(url_for("sensors.sensor_page", _anchor="find"))
