"""Table tests for the pure brightness rules: the curve, the median and the change decision."""

import math

import pytest

from art_frames import sensor_curve
from art_frames.config import BrightnessCurve
from art_frames.sensor_curve import Pending, decide

MIDDLE = math.sqrt(500)  # halfway between 1 and 500 lx on a log scale
HALF = math.sqrt(1000)  # halfway along the default curve, 1 to 1000 lx


def pts(*pairs):
    return {"points": [{"lux": lux, "level": level} for lux, level in pairs]}


FOUR = pts((1, 1), (10, 2), (100, 3), (1000, 10))


@pytest.mark.parametrize("lux, fields, tv_range, step, want", [
    # The default curve (1 to 1000 lx) over the TV's whole range.
    (1, {}, (0, 50), 1, 0),
    (1000, {}, (0, 50), 1, 50),
    (500, {}, (0, 50), 1, 45),
    (HALF, {}, (0, 50), 1, 25),
    (0, {}, (0, 50), 1, 0),  # 0 lx counts as the first point
    (-3, {}, (0, 50), 1, 0),
    (None, {}, (0, 50), 1, 0),
    (0.2, {}, (0, 50), 1, 0),  # below the first point
    (90_000, {}, (0, 50), 1, 50),  # above the last point
    # Two points.
    (1, pts((1, 2), (500, 12)), (0, 50), 1, 2),
    (500, pts((1, 2), (500, 12)), (0, 50), 1, 12),
    (MIDDLE, pts((1, 2), (500, 12)), (0, 50), 1, 7),
    # Points outside the TV's range are clamped to it.
    (500, pts((500, 60)), (1, 10), 1, 10),
    (1, pts((1, 0)), (1, 10), 1, 1),
    # Flat below the first point and above the last.
    (10, pts((10, 0), (1000, 20)), (0, 20), 1, 0),
    (100, pts((10, 0), (1000, 20)), (0, 20), 1, 10),
    (2, pts((10, 4), (1000, 20)), (0, 20), 1, 4),
    (5000, pts((10, 4), (1000, 20)), (0, 20), 1, 20),
    # Many points: each span follows log10(lux) between its own two points.
    (3, FOUR, (0, 10), 1, 1),  # 1.48
    (50, FOUR, (0, 10), 1, 3),  # 2.70
    (316, FOUR, (0, 10), 1, 6),  # 6.50 rounds to even
    (102, pts((1, 1), (10, 2), (100, 2), (1000, 10)), (0, 10), 1, 2),  # the owner's room at 102 lux
    # One point: the default ends fill the sides it leaves room on.
    (102, pts((102, 2)), (0, 10), 1, 2),
    (1, pts((102, 2)), (0, 10), 1, 0),
    (1000, pts((102, 2)), (0, 10), 1, 10),
    # The offset adds whole steps, inside the range.
    (HALF, {"offset": 1}, (0, 50), 1, 26),
    (HALF, {"offset": -3}, (0, 50), 1, 22),
    (1, {"offset": -3}, (0, 50), 1, 0),
    (1000, {"offset": 3}, (0, 50), 1, 50),
    # The TV's step.
    (HALF, {}, (0, 50), 5, 25),
    (HALF, {"offset": 1}, (0, 50), 5, 30),
    (10, {}, (0, 50), 5, 15),  # 16.7 rounds to 15
])
def test_target(lux, fields, tv_range, step, want):
    assert sensor_curve.target(lux, BrightnessCurve(**fields), *tv_range, step=step) == want


def test_the_dark_level_is_the_lowest_point():
    assert sensor_curve.dark_level(BrightnessCurve(), 1, 10) == 1
    assert sensor_curve.dark_level(BrightnessCurve(**pts((5, 3), (500, 8))), 1, 10) == 3
    assert sensor_curve.dark_level(BrightnessCurve(**pts((102, 2))), 0, 10) == 0


def test_older_two_settings_load_as_points():
    curve = BrightnessCurve(dark_lux=2.5, dark_level=1, bright_lux=400, bright_level=8, offset=-1)
    assert [(point.lux, point.level) for point in curve.points] == [(2.5, 1), (400, 8)] and curve.offset == -1
    # A level of None used the end of the TV's range. The default curve covers the dark end at 1 lux; the old
    # bright default (500 lux) stays a point, because new setups reach the top only at 1000 lux.
    old_default = BrightnessCurve(dark_lux=1, dark_level=None, bright_lux=500, bright_level=None)
    assert [(point.lux, point.level) for point in old_default.points] == [(500, 100)]
    one = BrightnessCurve(dark_lux=1, dark_level=None, bright_lux=300, bright_level=6)
    assert [(point.lux, point.level) for point in one.points] == [(300, 6)]


def test_points_are_kept_in_order_of_light():
    curve = BrightnessCurve(**pts((500, 9), (1, 1), (50, 4)))
    assert [point.lux for point in curve.points] == [1, 50, 500]


def test_with_point_replaces_a_point_at_about_the_same_light():
    points = [{"lux": 1.0, "level": 1}, {"lux": 100.0, "level": 5}]
    assert sensor_curve.with_point(points, 110.0, 2) == [{"lux": 1.0, "level": 1}, {"lux": 110.0, "level": 2}]
    assert sensor_curve.with_point(points, 300.0, 8) == [*points, {"lux": 300.0, "level": 8}]


@pytest.mark.parametrize("fields, message", [
    (pts((5, 3), (5, 4)), "each light level once"),
    (pts(*[(lux, 1) for lux in range(1, 10)]), "at most 8 points"),
    (pts((0, 3)), "greater than 0"),
    (pts((5, 101)), "less than or equal to 100"),
    ({"offset": 4}, "less than or equal to 3"),
])
def test_bad_curves_are_refused(fields, message):
    with pytest.raises(ValueError, match=message):
        BrightnessCurve(**fields)


@pytest.mark.parametrize("samples, now, want", [
    ([(0, 1.0), (60, 100.0), (100, 3.0)], 110, 3.0),  # one bright flash does not count
    ([(0, 1.0), (15, 2.0)], 20, 1.5),
    ([(-20, 500.0), (10, 1.0)], 110, 1.0),  # older than 2 minutes is left out
    ([(0, None), (15, 4.0)], 20, 4.0),  # no value is left out
    ([(0, None)], 10, None),
    ([], 10, None),
    ([(-200, 4.0)], 0, None),  # no reading for 2 minutes
    ([(50, 4.0)], 10, None),  # a reading from after `now` is left out
])
def test_median_light(samples, now, want):
    assert sensor_curve.median_light(samples, now) == want


# Each row: (want, current, sample_at, now, last_change_at) -> verdict. Rows run in order, and each row
# gets the pending change of the row before.
@pytest.mark.parametrize("rows", [
    pytest.param([((10, 10, 0, 0, None), "same")], id="at the curve"),
    pytest.param([((12, 10, 0, 0, None), "wait"),
                  ((12, 10, 0, 40, None), "wait"),  # the same reading counts once
                  ((12, 10, 15, 45, None), "change")], id="brighten after 2 readings and 30 s"),
    pytest.param([((12, 10, 0, 0, None), "wait"),
                  ((12, 10, 15, 15, None), "wait"),  # 2 readings, but only 15 s
                  ((12, 10, 30, 30, None), "change")], id="brighten after 30 s"),
    pytest.param([((8, 10, 0, 0, None), "wait"),
                  ((8, 10, 15, 15, None), "wait"),
                  ((8, 10, 165, 165, None), "wait"),
                  ((8, 10, 180, 180, None), "change")], id="dim after 3 min"),
    pytest.param([((12, 10, 0, 0, None), "wait"),
                  ((8, 10, 15, 40, None), "wait"),  # the direction changed: start again
                  ((8, 10, 30, 60, None), "wait"),
                  ((8, 10, 200, 220, None), "change")], id="a change of direction starts again"),
    pytest.param([((12, 10, 0, 0, None), "wait"),
                  ((10, 10, 15, 15, None), "same"),  # back at the curve: nothing pending
                  ((12, 10, 30, 30, None), "wait"),
                  ((12, 10, 45, 45, None), "wait"),
                  ((12, 10, 60, 60, None), "change")], id="back at the curve clears the pending change"),
    pytest.param([((12, 10, 0, 0, -10), "wait"),
                  ((12, 10, 15, 45, 0), "wait"),  # 45 s since the last change
                  ((12, 10, 30, 60, 0), "change")], id="at most once a minute"),
    pytest.param([((12, None, 0, 0, None), "same")], id="unknown TV level"),
])
def test_decide(rows):
    pending = None
    for (want, current, sample_at, now, last), verdict in rows:
        got, pending = decide(want, current, pending, sample_at, now, last)
        assert got == verdict, (want, current, sample_at, now)
        assert (pending is None) == (verdict == "same")


def test_decide_uses_the_tv_step():
    assert decide(12, 10, None, 0, 0, None, step=5) == ("same", None)
    assert decide(15, 10, None, 0, 0, None, step=5) == ("wait", Pending(1, 0, 1, 0))


@pytest.mark.parametrize("reported, sent, want", [
    (4, 10, True),
    (10, 10, False),
    (None, 10, False),  # the TV does not report its brightness
    (4, None, False),  # Art Frames never set a level
])
def test_changed_by_hand(reported, sent, want):
    assert sensor_curve.changed_by_hand(reported, sent) is want


@pytest.mark.parametrize("state, want", [
    ({}, False),
    ({"paused_until": None}, False),
    ({"paused_until": 99}, False),
    ({"paused_until": 101}, True),
])
def test_paused(state, want):
    assert sensor_curve.paused(state, 100) is want
