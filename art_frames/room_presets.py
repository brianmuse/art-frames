"""Room presets for the Sensor page, and the small curve drawings on it.

A preset fills in normal sensor settings: the brightness points, the dim, blank and TV off times and the
hours.
It is not a mode. After any change, the page shows "Custom (based on Living room)".

Each level is a share of the TV's own range, so one preset fits a 0-10 TV and a 0-50 TV:
level = low + round(percent * (high - low) / 100). When Art Frames has not read the range yet, it uses
0-10. On a TV with a wider range, that only makes the screen darker, never brighter.

Nothing here reads the TV, the sensor or the clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ROOM_STATE = "sensor_room"  # library state: {"room": key, "range": [low, high]}, the last pick and its range
SAFE_RANGE = (0, 10)  # the range presets use before Art Frames reads the TV's own


@dataclass(frozen=True)
class Room:
    key: str
    name: str
    line: str
    shares: tuple[tuple[float, int], ...]  # (lux, percent of the TV's range)
    away_after: int  # minutes
    blank_after: int  # minutes
    off_after: int | None  # minutes, or None: never
    hours_start: str
    hours_end: str


ROOMS = (
    Room("living", "Living room", "Bright by day, soft at night. Blank after 30 min, off after 1 hour.",
         ((1, 20), (20, 40), (150, 60), (800, 100)), 5, 30, 60, "07:00", "23:00"),
    Room("bedroom", "Bedroom", "Very dim at night. Blank after 10 min, off after 30 min. Does not turn on after "
         "21:30.", ((1, 0), (10, 20), (100, 50), (500, 80)), 1, 10, 30, "07:00", "21:30"),
    # Never above 60 %: a dark basement never gets a bright screen.
    Room("theater", "Dark home theater", "Never bright. Blank after 5 min, off after 30 min.",
         ((0.5, 0), (5, 10), (50, 30), (300, 60)), 1, 5, 30, "08:00", "23:00"),
)
BY_KEY = {room.key: room for room in ROOMS}


def level(percent, low, high):
    """A share of the TV's range as a whole level. Integer math, so 0-10 and 0-50 give exact values."""
    return low + round(percent * (high - low) / 100)


def points(room, low, high):
    """The preset's brightness points for a TV with this range."""
    return [{"lux": float(lux), "level": level(percent, low, high)} for lux, percent in room.shares]


def settings(room, low, high):
    """The sensor fields a preset sets. The Darker/Brighter offset stays as it is."""
    return {"points": points(room, low, high), "away_after": room.away_after, "blank_after": room.blank_after,
            "off_after": room.off_after, "hours_start": room.hours_start, "hours_end": room.hours_end}


def saved(sensor):
    """The same fields from the saved sensor settings."""
    return {"points": [{"lux": float(point.lux), "level": point.level} for point in sensor.curve.points],
            "away_after": sensor.away_after, "blank_after": sensor.blank_after, "off_after": sensor.off_after,
            "hours_start": sensor.hours_start, "hours_end": sensor.hours_end}


def current(sensor, low, high):
    """The preset the saved settings match exactly, or None."""
    now = saved(sensor)
    return next((room for room in ROOMS if settings(room, low, high) == now), None)


def stale(sensor, picked, low, high):
    """The picked room when it was applied at another range than (low, high) and nothing changed since, or None.

    picked is the stored ROOM_STATE. A pick from before Art Frames read the TV used SAFE_RANGE; a pick
    stored without a range is read the same way.
    """
    room = BY_KEY.get((picked or {}).get("room"))
    applied = tuple((picked or {}).get("range") or SAFE_RANGE)
    if room is None or applied == (low, high) or len(applied) != 2:
        return None
    return room if settings(room, *applied) == saved(sensor) else None


def changes(sensor, room, low, high, words):
    """One line that says what picking `room` changes, such as "This changes 4 brightness points, Dim after
    1 min → 5 min, and hours 07:00–23:00." words(minutes) names a time."""
    now, new = saved(sensor), settings(room, low, high)
    parts = []
    if now["points"] != new["points"]:
        kept = {(point["lux"], point["level"]) for point in now["points"]}
        count = sum((point["lux"], point["level"]) not in kept for point in new["points"]) or len(new["points"])
        parts.append(f"{count} brightness point{'' if count == 1 else 's'}")
    for field, label in (("away_after", "Dim after"), ("blank_after", "Blank after"), ("off_after", "TV off after")):
        if now[field] != new[field]:
            parts.append(f"{label} {said(now[field], words)} → {said(new[field], words)}")
    if (now["hours_start"], now["hours_end"]) != (new["hours_start"], new["hours_end"]):
        parts.append(f"hours {new['hours_start']}–{new['hours_end']}")
    if not parts:
        return "No change. These are the saved settings."
    if len(parts) == 1:
        return f"This changes {parts[0]}."
    return f"This changes {', '.join(parts[:-1])}, and {parts[-1]}."


def said(minutes, words):
    """A time in the change line: "Never" for a step that never runs (None)."""
    return "Never" if minutes is None else words(minutes)


# Drawings. x is lux on a log scale from 0.1 to 10,000 lux; y is the level, highest at the top.
LOG_LOW, LOG_HIGH = -1.0, 4.0
TICKS = ((0.1, "0.1"), (1, "1"), (10, "10"), (100, "100"), (1000, "1k"), (10000, "10k"))


def _x(lux, width):
    share = (math.log10(max(lux, 10 ** LOG_LOW)) - LOG_LOW) / (LOG_HIGH - LOG_LOW)
    return round(max(0.0, min(share, 1.0)) * width, 1)


def _y(value, low, high, height):
    span = (high - low) or 1
    return round(height - (max(low, min(value, high)) - low) / span * height, 1)


def line(anchors, low, high, width, height):
    """SVG polyline points for a curve's (lux, level) anchors, flat beyond the first and last point."""
    ends = [(10 ** LOG_LOW, anchors[0][1]), *anchors, (10 ** LOG_HIGH, anchors[-1][1])]
    return " ".join(f"{_x(lux, width)},{_y(value, low, high, height)}" for lux, value in ends)


def sparkline(room):
    """A small drawing of the preset's curve for its radio card, as a share of any range."""
    return line(room.shares, 0, 100, 100, 28)


def chart(anchors, low, high, offset=0, lux_now=None, level_now=None):
    """The Brightness points chart: the curve line, a dot per point, a dot for now, and the lux ticks."""
    width, height = 300, 100
    moved = [(lux, max(low, min(value + offset, high))) for lux, value in anchors]
    now = None
    if lux_now is not None and level_now is not None:
        now = {"x": _x(lux_now, width), "y": _y(level_now, low, high, height),
               "text": f"Now: {lux_now:.1f} lux → {level_now}" if lux_now < 10 else
               f"Now: {lux_now:.0f} lux → {level_now}"}
    return {"width": width, "height": height, "line": line(moved, low, high, width, height),
            "dots": [{"x": _x(lux, width), "y": _y(value, low, high, height), "text": f"{lux:g} lux → {value}"}
                     for lux, value in moved],
            "now": now, "ticks": [{"x": _x(lux, width), "text": text} for lux, text in TICKS],
            "low": low, "high": high}
