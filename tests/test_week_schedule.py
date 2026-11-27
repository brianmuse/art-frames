"""The day timeline schedule: the move from older time rules and wake times, and what shows when."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_app_schedule import three_apps  # noqa: F401  (a fixture)

from art_frames import apps
from art_frames.config import AppSchedule, Config, LegacySchedule, ScheduleError, parse_schedule, wake_time

MONDAY = date(2026, 10, 26)
WEEK = 7 * 1440
NEWS, PHOTOS = {"apps": ["news"]}, {"apps": ["photos"]}

# Older schedules: time rules that never overlap, on start days, some past midnight.
OLDER = [
    [{**NEWS, "start": "07:00", "end": "10:00", "days": [0, 1, 2, 3, 4]}],
    [{**NEWS, "start": "23:00", "end": "06:00", "days": [6]}],  # Sunday night into Monday
    [{"apps": ["news", "photos"], "rotate": True, "swap_seconds": 120, "start": "18:00", "end": "22:00"},
     {**PHOTOS, "start": "06:00", "end": "07:00", "days": [5]}],
    [{**PHOTOS, "start": "20:00", "end": "00:00", "days": [1, 3]}],  # ends at midnight
    [{**NEWS, "start": "00:00", "end": "23:59", "days": [2]},
     {**PHOTOS, "start": "22:30", "end": "01:15", "days": [0, 4, 6]}],
    [{**NEWS, "start": "06:45", "end": "06:00", "days": list(range(7))}],  # nearly all week
    [],
]


def older_apps(legacy, minute):
    """What an older schedule showed at `minute` of the week: the apps of the rule that covers it."""
    for window in legacy.windows:
        if any(start <= minute < end for start, end in window.ranges()):
            return list(window.apps)
    return []


@pytest.mark.parametrize("windows", OLDER)
def test_each_minute_of_the_week_shows_the_same_apps_after_the_move(windows):
    raw = {"default": {"apps": ["morning"]}, "windows": windows}
    legacy, schedule = LegacySchedule.model_validate(raw), AppSchedule.model_validate(raw)
    assert schedule.default == legacy.default
    for minute in range(WEEK):  # every 15-minute slot, and each minute between
        assert apps.apps_at(schedule.week, minute) == older_apps(legacy, minute), minute
    # Blocks never cross midnight, and the moved schedule saves and loads as it is.
    for weekday in range(7):
        assert all(block.start < block.end or block.end == "24:00" for block in schedule.week.day(weekday).blocks)
    assert AppSchedule.model_validate(schedule.model_dump()) == schedule


def test_the_move_shares_what_every_day_has_and_keeps_the_rest_per_day():
    schedule = AppSchedule.model_validate({"windows": OLDER[2]})
    week = schedule.week
    # The 6:00 PM rule is on every day: every day's plan. Saturday also has its morning rule.
    assert [(block.app, block.start, block.end) for block in week.default.blocks] == [
        ("news", "18:00", "22:00"), ("photos", "18:00", "22:00")]
    assert [key for key, day in week.days.items() if day is not None] == ["sat"]
    assert [block.app for block in week.days["sat"].blocks] == ["photos", "news", "photos"]
    # Apps that took turns overlap, and take turns as often as before.
    assert week.swap_seconds == 120


def test_the_most_common_day_becomes_every_days_plan():
    # News on weekday evenings, and a 6:30 AM turn-on time on weekdays.
    cfg = Config.model_validate({
        "tv": {"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:01"},
        "wake": {"enabled": True, "at": "06:30", "days": [0, 1, 2, 3, 4]},
        "app_schedule": {"default": {"apps": ["morning"]},
                         "windows": [{**NEWS, "start": "18:00", "end": "22:00", "days": [0, 1, 2, 3, 4]}]}})
    week = cfg.app_schedule.week
    assert [(block.app, block.start, block.end) for block in week.default.blocks] == [("news", "18:00", "22:00")]
    assert week.default.wake == "06:30"
    assert [key for key, day in week.days.items() if day is not None] == ["sat", "sun"]
    assert week.days["sat"].blocks == [] and week.days["sat"].wake is None and week.days["sun"] == week.days["sat"]
    # Two plans used on as many days: the earlier weekday's plan wins.
    week = AppSchedule.model_validate({"windows": [{**NEWS, "start": "07:00", "end": "09:00", "days": [0, 1, 2]},
                                                   {**PHOTOS, "start": "07:00", "end": "09:00", "days": [3, 4, 5]}]}).week
    assert [block.app for block in week.default.blocks] == ["news"]
    assert [key for key, day in week.days.items() if day is not None] == ["thu", "fri", "sat", "sun"]


def test_twelve_older_rules_of_six_apps_still_load():
    six = [f"app-{number}" for number in range(6)]
    windows = [{"apps": six, "start": f"{hour:02d}:00", "end": f"{(hour + 2) % 24:02d}:00"} for hour in range(1, 24, 2)]
    schedule = AppSchedule.model_validate({"windows": windows})
    legacy = LegacySchedule.model_validate({"windows": windows})
    assert len(schedule.week.default.blocks) == 13 * 6  # 11 rules, and the last one before and after midnight
    assert all(apps.apps_at(schedule.week, minute) == older_apps(legacy, minute) for minute in range(0, WEEK, 15))
    assert AppSchedule.model_validate(schedule.model_dump()) == schedule


@pytest.mark.parametrize("slots, swap, note", [
    ([120, 120, 600], 120, True),   # the most common speed, and a note: the speeds were different
    ([600], 600, False),            # one speed: no note
    ([], 300, False),               # nothing took turns: 5 minutes
])
def test_one_turn_speed_for_the_whole_tv(slots, swap, note):
    windows = [{"apps": ["news", "photos"], "swap_seconds": seconds, "start": f"{2 * index:02d}:00",
                "end": f"{2 * index + 1:02d}:00"} for index, seconds in enumerate(slots)]
    windows.append({**NEWS, "swap_seconds": 900, "start": "20:00", "end": "21:00"})  # one app: it never took turns
    schedule = AppSchedule.model_validate({"default": {"apps": ["morning"]}, "windows": windows})
    assert (schedule.week.swap_seconds, schedule.turns_note) == (swap, note)
    # The default app counts when it took turns too.
    schedule = AppSchedule.model_validate({"default": {"apps": ["morning", "news"], "swap_seconds": 1800},
                                           "windows": windows})
    assert schedule.turns_note == bool(slots)
    assert AppSchedule().week.swap_seconds == 300


@pytest.mark.parametrize("default, swap", [
    ({"apps": ["morning", "news"], "swap_seconds": 60}, 60),   # its apps took turns: keep their speed
    ({"apps": ["morning", "news"]}, 60),                       # the older default speed
    ({"apps": ["morning"], "swap_seconds": 900}, 300),         # one app never took turns: 5 minutes
])
def test_a_save_without_time_rules_keeps_its_turn_speed(default, swap):
    schedule = AppSchedule.model_validate({"default": default})
    assert (schedule.week.swap_seconds, schedule.turns_note) == (swap, False)
    assert schedule.week.default.blocks == [] and not any(schedule.week.custom(day) for day in range(7))
    # A save in the week shape stays as it is.
    assert AppSchedule.model_validate({"default": default, "week": {}}).week.swap_seconds == 300


def test_a_saved_rest_of_the_time_is_ignored_for_artwork(three_apps):  # noqa: F811
    cfg = Config.model_validate({"app_schedule": {"default": {"apps": ["morning", "news"], "swap_seconds": 60},
                                                  "week": {"swap_seconds": 600}}})
    plan = apps.plan(cfg, at(0, "08:00"), apps.installed())
    assert plan.apps == [apps.ARTWORK] and not plan.rotate and plan.key == "default"
    assert cfg.app_schedule.default.apps == ["morning", "news"]  # it still loads, and saves as it is


def test_an_older_overlap_is_still_refused():
    with pytest.raises(ScheduleError, match="overlap"):
        parse_schedule({"windows": [{**NEWS, "start": "07:00", "end": "10:00"}, {**PHOTOS, "start": "09:00", "end": "11:00"}]})


@pytest.mark.parametrize("wake", [
    {"enabled": True, "at": "06:30", "days": [0, 1, 2, 3, 4, 5, 6]},
    {"enabled": True, "at": "06:30", "days": [0, 2, 4]},
    {"enabled": True, "at": "23:45", "days": [6]},
    {"enabled": False, "at": "06:30", "days": [0, 1]},
    {"enabled": True},
])
def test_an_older_wake_time_wakes_on_the_same_days(wake):
    for windows in (OLDER[0], OLDER[2], []):
        cfg = Config.model_validate({"tv": {"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:01"}, "wake": wake,
                                     "app_schedule": {"default": {"apps": ["morning"]}, "windows": windows}})
        days = wake.get("days", range(7)) if wake["enabled"] else []
        for offset in range(14):
            day = MONDAY + timedelta(days=offset)
            assert wake_time(cfg, day) == (wake.get("at", "07:00") if day.weekday() in days else None), (wake, day)
        # The apps did not change, and the moved settings load as they are.
        legacy = LegacySchedule.model_validate({"windows": windows})
        assert all(apps.apps_at(cfg.app_schedule.week, minute) == older_apps(legacy, minute) for minute in range(0, WEEK, 15))
        assert Config.model_validate(cfg.model_dump()) == cfg
        assert set(cfg.wake.model_dump()) == {"grace_minutes"}


def at(day, clock):
    return datetime.fromisoformat(f"{(MONDAY + timedelta(days=day)).isoformat()}T{clock}:00").replace(tzinfo=ZoneInfo("UTC"))


def test_what_shows_uses_the_day_s_plan_then_every_day_s(three_apps):  # noqa: F811
    available = apps.installed()
    cfg = Config.model_validate({"app_schedule": {"default": {"apps": ["morning"]}, "week": {
        "default": {"blocks": [{"app": "news", "start": "07:00", "end": "10:00"},
                               {"app": "photos", "start": "09:00", "end": "12:00"}]},
        "days": {"sat": {"blocks": [{"app": "photos", "start": "08:00", "end": "09:00"}]}},
        "swap_seconds": 90}}})

    def shows(day, clock):
        plan = apps.plan(cfg, at(day, clock), available)
        return [item.id for item in plan.apps], plan.swap_seconds, plan.key

    # No block: Artwork. One block: its app. Two blocks: their apps take turns.
    assert shows(0, "06:00")[::2] == (["artwork"], "default")
    assert shows(0, "08:00") == (["news"], 90, "blocks:420:news")
    assert shows(0, "09:30") == (["news", "photos"], 90, "blocks:540:news,photos")
    assert shows(0, "11:59")[0] == ["photos"] and shows(0, "12:00")[0] == ["artwork"]
    # Saturday has its own plan. Sunday uses every day's.
    assert shows(5, "08:30")[0] == ["photos"] and shows(5, "09:30")[0] == ["artwork"]
    assert shows(6, "09:30")[0] == ["news", "photos"]
    # The next change, and what shows then.
    plan = apps.plan(cfg, at(0, "09:30"), available)
    assert plan.next_change == at(0, "10:00") and [item.id for item in plan.next_apps] == ["photos"]
    plan = apps.plan(cfg, at(4, "13:00"), available)  # Friday afternoon: next is Saturday 8:00 AM
    assert plan.next_change == at(5, "08:00") and [item.id for item in plan.next_apps] == ["photos"]
    assert apps.uses(cfg, apps.find("photos", available), available)


def test_a_schedule_without_blocks_never_changes(three_apps):  # noqa: F811
    available = apps.installed()
    cfg = Config.model_validate({"app_schedule": {"default": {"apps": ["news"]}}})
    plan = apps.plan(cfg, at(2, "15:00"), available)
    assert plan.apps == [apps.ARTWORK] and plan.next_change is None
    assert not apps.uses(cfg, apps.find("news", available), available)
    assert not apps.uses(cfg, apps.find("photos", available), available)


@pytest.mark.parametrize("block, text", [
    ({"app": "news", "start": "10:00", "end": "09:00"}, "Pick an end time after the start time"),
    ({"app": "news", "start": "24:00", "end": "24:00"}, None),
    ({"app": "news", "start": "09:00", "end": "25:00"}, None),
])
def test_a_wrong_block_is_refused(block, text):
    with pytest.raises(ScheduleError) as error:
        parse_schedule({"week": {"default": {"blocks": [block]}}})
    if text:
        assert text in str(error.value)


def test_a_saved_day_has_at_most_144_blocks():
    blocks = [{"app": "news", "start": f"{hour:02d}:00", "end": f"{hour:02d}:30"} for hour in range(24)] * 6
    parse_schedule({"week": {"default": {"blocks": blocks}}})
    with pytest.raises(ScheduleError, match="at most 144 blocks"):
        parse_schedule({"week": {"default": {"blocks": [*blocks, blocks[0]]}}})
    with pytest.raises(ScheduleError):
        parse_schedule({"week": {"days": {"someday": None}}})
