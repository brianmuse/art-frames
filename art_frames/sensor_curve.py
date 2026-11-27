"""Pure brightness rules for sensor control: the light curve, the smoother and the change decision.

Nothing here reads the TV, the sensor or the clock. Callers pass the time in. sensor_control.py uses these
functions, and tests/test_sensor_curve.py tests them with tables.

1. target() maps room light to a brightness on a log scale, because people see brightness on a log scale.
2. median_light() takes the median of the last 2 minutes of light readings. One bright flash does not count.
3. decide() says when to change: brighten after about 30 s, dim after about 3 min, at most once a minute,
   and only after 2 readings in a row agree.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, replace

WINDOW = 120  # the median uses the light readings of the last 2 minutes
CONFIRM = 2  # this many readings in a row must want the same change
UP_SECONDS = 30  # brighten quickly
DOWN_SECONDS = 180  # dim slowly
MIN_GAP = 60  # never more than one change a minute
PAUSE_SECONDS = 2 * 3600  # after someone changes the brightness on the TV
OFFSET_STEPS = 3  # the Darker/Brighter adjustment goes from -3 to +3 steps


DEFAULT_DARK_LUX, DEFAULT_BRIGHT_LUX = 1.0, 1000.0  # the default curve: the TV's lowest to its highest
SAME_POINT = 0.1  # two lux values closer than this on log10(lux) (about 25 %) count as one point


def anchors(curve, low, high):
    """The (lux, level) pairs the curve runs through, in order of light, with levels inside (low, high).

    Two or more saved points: just those. No point: the TV's lowest brightness at 1 lux to its highest at
    1000 lux. One point: that point, plus the default ends on the sides where it leaves room.
    """
    points = [(point.lux, max(low, min(point.level, high))) for point in curve.points]
    if len(points) >= 2:
        return points
    if not points:
        return [(DEFAULT_DARK_LUX, low), (DEFAULT_BRIGHT_LUX, high)]
    lux, level = points[0]
    ends = [(DEFAULT_DARK_LUX, min(low, level))] if lux > DEFAULT_DARK_LUX else []
    ends += [(lux, level)]
    ends += [(DEFAULT_BRIGHT_LUX, max(high, level))] if lux < DEFAULT_BRIGHT_LUX else []
    return ends


def dark_level(curve, low, high):
    """The curve's lowest brightness: the away dim uses it."""
    return min(level for _, level in anchors(curve, low, high))


def target(lux, curve, low, high, step=1):
    """The brightness for a light reading, rounded to the TV's step and inside its range.

    Between two points, the brightness follows log10(lux). Below the first point and above the last, it
    stays at that point's brightness. Lux at or below 0 counts as the first point. The offset adds whole
    steps on top of the curve.
    """
    points = anchors(curve, low, high)
    if lux is None or lux <= points[0][0]:
        level = points[0][1]
    elif lux >= points[-1][0]:
        level = points[-1][1]
    else:
        (lux_a, level_a), (lux_b, level_b) = next(
            (points[index], points[index + 1]) for index in range(len(points) - 1) if lux <= points[index + 1][0])
        share = (math.log10(lux) - math.log10(lux_a)) / (math.log10(lux_b) - math.log10(lux_a))
        level = level_a + (level_b - level_a) * share
    level = round(level / step) * step + curve.offset * step
    return max(low, min(level, high))


def same_point(lux_a, lux_b):
    """True when two light levels are close enough to be one point of the curve."""
    return abs(math.log10(lux_a) - math.log10(lux_b)) < SAME_POINT


def with_point(points, lux, level):
    """The points as dicts, with (lux, level) added, or replacing a point at about the same light."""
    kept = [{"lux": point["lux"], "level": point["level"]} for point in points if not same_point(point["lux"], lux)]
    return sorted([*kept, {"lux": lux, "level": level}], key=lambda point: point["lux"])


def median_light(samples, now, window=WINDOW):
    """The median light reading of the last `window` seconds, or None when there is none.

    samples are (at, lux) pairs. None values (the sensor reports no value) are left out.
    """
    values = [lux for at, lux in samples if lux is not None and now - window <= at <= now]
    return statistics.median(values) if values else None


@dataclass(frozen=True)
class Pending:
    """A change that the readings want, but that has not waited long enough yet."""

    direction: int  # +1 brighter, -1 darker
    since: float  # when the first reading wanted it
    count: int  # how many readings in a row wanted it
    sample_at: float  # the time of the last reading counted


def decide(want, current, pending, sample_at, now, last_change_at, step=1):
    """Whether to change the brightness now. Returns (verdict, pending).

    want is the curve's level and current the TV's level. sample_at is the time of the newest light
    reading, so a reading counts once however often this runs. The verdicts:
    - "same": the TV is within one step of the curve. Nothing is pending.
    - "wait": a change is pending. It needs more readings, more time, or a minute since the last change.
    - "change": send `want` now.
    """
    if current is None or abs(want - current) < step:
        return "same", None
    direction = 1 if want > current else -1
    if pending is None or pending.direction != direction:
        pending = Pending(direction, now, 1, sample_at)
    elif sample_at > pending.sample_at:
        pending = replace(pending, count=pending.count + 1, sample_at=sample_at)
    hold = UP_SECONDS if direction > 0 else DOWN_SECONDS
    if pending.count < CONFIRM or now - pending.since < hold:
        return "wait", pending
    if last_change_at is not None and now - last_change_at < MIN_GAP:
        return "wait", pending
    return "change", pending


def changed_by_hand(reported, sent):
    """True when the TV reports another level than the last one Art Frames set: someone used the remote."""
    return isinstance(reported, int) and isinstance(sent, int) and reported != sent


def paused(state, now):
    """True while sensor control is paused after a change on the TV."""
    return (state.get("paused_until") or 0) > now
