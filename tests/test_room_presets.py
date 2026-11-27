"""Room presets: exact levels for a 0-10 and a 0-50 TV, the change line, and the drawings. No device here."""

import pytest

from art_frames import room_presets
from art_frames.config import Sensor
from art_frames.web import duration


def words(minutes):
    return duration(minutes * 60) if minutes else "0 min"


@pytest.mark.parametrize("key, low, high, levels", [
    ("living", 0, 10, [2, 4, 6, 10]),
    ("bedroom", 0, 10, [0, 2, 5, 8]),
    ("theater", 0, 10, [0, 1, 3, 6]),
    ("living", 0, 50, [10, 20, 30, 50]),
    ("bedroom", 0, 50, [0, 10, 25, 40]),
    ("theater", 0, 50, [0, 5, 15, 30]),
])
def test_preset_levels_are_a_share_of_the_tv_range(key, low, high, levels):
    assert [point["level"] for point in room_presets.points(room_presets.BY_KEY[key], low, high)] == levels


@pytest.mark.parametrize("key, lux, away, blank, start, end", [
    ("living", [1.0, 20.0, 150.0, 800.0], 5, 30, "07:00", "23:00"),
    ("bedroom", [1.0, 10.0, 100.0, 500.0], 1, 10, "07:00", "21:30"),
    ("theater", [0.5, 5.0, 50.0, 300.0], 1, 5, "08:00", "23:00"),
])
def test_preset_light_points_times_and_hours(key, lux, away, blank, start, end):
    preset = room_presets.settings(room_presets.BY_KEY[key], 0, 10)
    assert [point["lux"] for point in preset["points"]] == lux
    assert (preset["away_after"], preset["blank_after"], preset["hours_start"], preset["hours_end"]) == (
        away, blank, start, end)
    Sensor.model_validate({"curve": {"points": preset["points"]}, **{k: v for k, v in preset.items() if k != "points"}})


def test_the_room_order_names_and_lines():
    assert [(room.key, room.name, room.line) for room in room_presets.ROOMS] == [
        ("living", "Living room", "Bright by day, soft at night. Blank after 30 min, off after 1 hour."),
        ("bedroom", "Bedroom", "Very dim at night. Blank after 10 min, off after 30 min. Does not turn on after 21:30."),
        ("theater", "Dark home theater", "Never bright. Blank after 5 min, off after 30 min."),
    ]


def test_the_safe_range_is_zero_to_ten():
    assert room_presets.SAFE_RANGE == (0, 10)


def sensor_with(room, low, high, **fields):
    preset = room_presets.settings(room_presets.BY_KEY[room], low, high)
    return Sensor.model_validate({"curve": {"points": preset.pop("points")}, **preset, **fields})


def test_current_finds_the_matching_preset_for_this_range():
    sensor = sensor_with("bedroom", 0, 50)
    assert room_presets.current(sensor, 0, 50).key == "bedroom"
    assert room_presets.current(sensor, 0, 10) is None  # other levels on a 0-10 TV
    assert room_presets.current(sensor_with("bedroom", 0, 50, blank_after=15), 0, 50) is None


def test_the_change_line():
    living = room_presets.BY_KEY["living"]
    sensor = Sensor(away_after=1, blank_after=30, off_after=60, hours_start="07:00", hours_end="23:00")
    assert room_presets.changes(sensor, living, 0, 10, words) == (
        "This changes 4 brightness points, and Dim after 1 min → 5 min.")
    assert room_presets.changes(Sensor(), living, 0, 10, words) == (
        "This changes 4 brightness points, Dim after 0 min → 5 min, Blank after 15 min → 30 min, "
        "and TV off after 30 min → 1 hour.")
    assert room_presets.changes(sensor_with("living", 0, 10), living, 0, 10, words) == (
        "No change. These are the saved settings.")
    theater = room_presets.BY_KEY["theater"]
    assert room_presets.changes(sensor_with("living", 0, 10), theater, 0, 10, words) == (
        "This changes 4 brightness points, Dim after 5 min → 1 min, Blank after 30 min → 5 min, "
        "TV off after 1 hour → 30 min, and hours 08:00–23:00.")
    one_off = sensor_with("theater", 0, 10, hours_start="09:00")
    assert room_presets.changes(one_off, theater, 0, 10, words) == "This changes hours 08:00–23:00."


def test_the_sparkline_is_flat_beyond_the_ends():
    line = room_presets.sparkline(room_presets.BY_KEY["living"]).split()
    assert line[0] == "0.0,22.4" and line[1].endswith(",22.4")
    assert line[-1] == "100.0,0.0" and line[-2].endswith(",0.0")


def test_the_chart_marks_the_points_and_now():
    chart = room_presets.chart([(1.0, 2), (800.0, 10)], 0, 10, offset=0, lux_now=100.0, level_now=7)
    assert [dot["text"] for dot in chart["dots"]] == ["1 lux → 2", "800 lux → 10"]
    assert chart["now"] == {"x": 180.0, "y": 30.0, "text": "Now: 100 lux → 7"}
    assert [tick["text"] for tick in chart["ticks"]] == ["0.1", "1", "10", "100", "1k", "10k"]
    assert room_presets.chart([(1.0, 2)], 0, 10)["now"] is None
    assert [dot["text"] for dot in room_presets.chart([(1.0, 9)], 0, 10, offset=2)["dots"]] == ["1 lux → 10"]
