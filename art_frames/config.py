"""Validated installation defaults and atomic, private runtime overrides."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import threading
import tomllib
from collections import Counter
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def clock(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Use a time in HH:MM format")
    return value


def weekdays(value):
    if len(value) != len(set(value)) or any(day not in range(7) for day in value):
        raise ValueError("Weekdays must be unique values from 0 (Monday) to 6 (Sunday)")
    return value


class Wake(Model):
    """How long a missed wake is retried. The wake times are in the schedule (Day.wake, see wake_time)."""

    grace_minutes: int = Field(5, ge=1, le=15)


# Older saves kept one wake time here: {"enabled", "at", "days"}. TVOptions.legacy_wake moves it into the schedule.
LEGACY_WAKE = ("enabled", "at", "days")


DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEK_MINUTES = 7 * 1440
# An installed layout's ID, and its settings scope: the key for its saved values.
APP_ID = r"[a-z][a-z0-9-]{0,63}"
SCOPE = r"[a-z][a-z0-9-]{0,95}"
# A rotation shows each layout this long unless its slot says otherwise.
DEFAULT_SWAP_SECONDS = 60
# The most time rules one schedule can have.
MAX_RULES = 12


def minutes(value):
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def hhmm(value):
    """Minutes of the day as "HH:MM"."""
    value %= 1440
    return f"{value // 60:02d}:{value % 60:02d}"


def week_spans(begin, length):
    """`length` minutes from `begin` (minutes of the week, Monday 00:00 is 0), as [start, end) pairs.

    A span past Sunday night continues on Monday morning, so it comes back as two pairs.
    """
    begin %= WEEK_MINUTES
    if begin + length <= WEEK_MINUTES:
        return [(begin, begin + length)]
    return [(begin, WEEK_MINUTES), (0, begin + length - WEEK_MINUTES)]


class AppSlot(Model):
    """The layout a TV shows, or two or more layouts it rotates between."""

    apps: list[str] = Field(min_length=1, max_length=6)
    swap_seconds: int = Field(DEFAULT_SWAP_SECONDS, ge=30, le=3600)

    @model_validator(mode="before")
    @classmethod
    def stored_rotate(cls, value):
        # Older saves stored a "rotate" flag. Two or more layouts now always rotate.
        if isinstance(value, dict) and "rotate" in value:
            value = {key: item for key, item in value.items() if key != "rotate"}
        return value

    @field_validator("apps")
    @classmethod
    def app_ids(cls, value):
        if any(not re.fullmatch(APP_ID, item) for item in value) or len(set(value)) != len(value):
            raise ValueError("Choose each layout once")
        return value

    @property
    def rotate(self):
        return len(self.apps) >= 2


def window_label(start, end, days):
    """A time window's key, such as "07:00–10:00 Mon, Tue". Every day adds no day names.

    The plan key uses it, so a rotation keeps its place. People see rule_label() instead.
    """
    days = sorted(day for day in set(days) if day in range(7))
    return f"{start}–{end}" + ("" if len(days) in {0, 7} else " " + ", ".join(DAY_NAMES[day] for day in days))


def _twelve_hour(value):
    """ "07:00" as ("7:00", "AM"). A value that is not HH:MM comes back as is, with no AM or PM."""
    if value == "24:00":  # the end of a block at midnight
        return "12:00", "AM"
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value or ""):
        return (value or "?"), ""
    hour, minute = minutes(value) // 60, minutes(value) % 60
    return f"{hour % 12 or 12}:{minute:02d}", "AM" if hour < 12 else "PM"


def short_time(value):
    """ "07:00" as ("7", "AM"), "11:30" as ("11:30", "AM"). For labels that must fit a small block."""
    time, half = _twelve_hour(value)
    return time.removesuffix(":00"), half


def day_names(days):
    """Days in plain words: "Every day", "Mon to Fri", "Sat and Sun" or "Mon, Wed, Fri"."""
    days = sorted(day for day in set(days) if day in range(7))
    if len(days) == 7:
        return "Every day"
    if not days:
        return "No days"
    if len(days) >= 3 and days == list(range(days[0], days[-1] + 1)):
        return f"{DAY_NAMES[days[0]]} to {DAY_NAMES[days[-1]]}"
    names = [DAY_NAMES[day] for day in days]
    return " and ".join(names) if len(names) == 2 else ", ".join(names)


ALL_DAY = "all day"  # a block from 12:00 AM to midnight, in the middle of a sentence


def span_label(start, end):
    """A stretch of the day in plain words, such as "7:00 to 10:00 AM", "10:00 PM to 12:00 AM" or "all day"."""
    if (start, end) == ("00:00", "24:00"):
        return ALL_DAY
    (one, half), (two, other) = _twelve_hour(start), _twelve_hour(end)
    times = f"{one} to {two} {half}" if half == other else f"{one} {half} to {two} {other}"
    return " ".join(times.split())


def rule_label(start, end, days):
    """A time rule in plain words, such as "7:00 to 10:00 AM, Mon to Fri"."""
    days = day_names(days)
    return f"{span_label(start, end)}, {days.lower() if days in {'Every day', 'No days'} else days}"


class AppWindow(AppSlot):
    """A daily time window. A window that ends before it starts runs past midnight; its days are start days."""

    start: str
    end: str
    days: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1)
    _times = field_validator("start", "end")(clock)
    _days = field_validator("days")(weekdays)

    @model_validator(mode="after")
    def length(self):
        if self.start == self.end:
            raise ValueError("Pick an end time that is not the start time")
        return self

    @property
    def label(self):
        return window_label(self.start, self.end, self.days)

    @property
    def text(self):
        """The rule in plain words, for people (see rule_label)."""
        return rule_label(self.start, self.end, self.days)

    def ranges(self):
        """The window's minutes of the week (Monday 00:00 is 0), as [start, end) pairs."""
        start, end = minutes(self.start), minutes(self.end)
        length = (end - start) % 1440
        return [span for day in self.days for span in week_spans(day * 1440 + start, length)]


class LegacySchedule(Model):
    """The schedule of older saves: a default slot and time rules that never overlap. Only migration uses it."""

    default: AppSlot | None = None
    windows: list[AppWindow] = Field(default_factory=list, max_length=MAX_RULES)

    @model_validator(mode="after")
    def no_overlap(self):
        spans = sorted((begin, end, index) for index, window in enumerate(self.windows) for begin, end in window.ranges())
        reach, owner = 0, None  # the latest end so far, and its window
        for begin, end, index in spans:
            if begin < reach and index != owner:
                one, two = sorted((owner, index))
                raise ValueError(f'These time rules overlap: "{self.windows[one].text}" and '
                                 f'"{self.windows[two].text}". Change the times or days of one')
            if end > reach:
                reach, owner = end, index
        return self


DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_FULL_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The most blocks the Schedule page adds to one day.
MAX_BLOCKS = 24
# The most blocks a saved day can have. Older time rules can give more than MAX_BLOCKS: 12 rules of 6 layouts,
# each in two parts on a day (before and after midnight). The migration must never fail on this limit.
MAX_STORED_BLOCKS = MAX_RULES * 6 * 2
# New schedules: layouts that share a time take turns this often.
TURN_SECONDS = 300


def block_time(value):
    """A block's start or end: "HH:MM", or "24:00" for the end of the day."""
    return value if value == "24:00" else clock(value)


def wake_clock(value):
    return None if value is None else clock(value)


class Block(Model):
    """One layout on a day's timeline. Blocks never cross midnight. Where blocks overlap, their layouts take turns."""

    app: str = Field(pattern=f"^{APP_ID}$")
    start: str
    end: str
    _times = field_validator("start", "end")(block_time)

    @model_validator(mode="after")
    def order(self):
        if minutes(self.end) <= minutes(self.start):
            raise ValueError("Pick an end time after the start time")
        return self

    def covers(self, minute):
        return minutes(self.start) <= minute < minutes(self.end)


class Day(Model):
    """A day's plan: its layout blocks and its wake time ("HH:MM"), if any."""

    blocks: list[Block] = Field(default_factory=list, max_length=MAX_STORED_BLOCKS)
    wake: str | None = None
    _wake = field_validator("wake")(wake_clock)

    def apps_at(self, minute):
        """The layout IDs of the blocks that cover `minute` of the day, each once, in block order."""
        return list(dict.fromkeys(block.app for block in self.blocks if block.covers(minute)))

    def edges(self):
        """The minutes of the day where a block starts or ends."""
        return sorted({minutes(edge) for block in self.blocks for edge in (block.start, block.end)})


class Week(Model):
    """Every day's plan ("default"), and the days with their own plan (None: the same as every day)."""

    default: Day = Field(default_factory=Day)
    days: dict[str, Day | None] = Field(default_factory=lambda: dict.fromkeys(DAY_KEYS))
    # Layouts that share a time take turns this often: where blocks overlap, and outside the blocks.
    swap_seconds: int = Field(TURN_SECONDS, ge=30, le=3600)

    @field_validator("days")
    @classmethod
    def day_keys(cls, value):
        if any(key not in DAY_KEYS for key in value):
            raise ValueError("Days are mon, tue, wed, thu, fri, sat and sun")
        return {key: value.get(key) for key in DAY_KEYS}

    def day(self, weekday):
        """The plan of a weekday (0 is Monday): its own, or every day's."""
        return self.days.get(DAY_KEYS[weekday]) or self.default

    def custom(self, weekday):
        return self.days.get(DAY_KEYS[weekday]) is not None

    def has_wake(self):
        return any(self.day(weekday).wake for weekday in range(7))


def legacy_days(windows):
    """Each weekday's blocks for older time rules, as 7 lists of block dicts. A rule past midnight
    continues on the next day, and Sunday night continues on Monday, as AppWindow.ranges() does."""
    days = [[] for _ in range(7)]
    for window in windows:
        start, end = minutes(window.start), minutes(window.end)
        for day in window.days:
            for app in window.apps:
                if end > start:
                    days[day].append({"app": app, "start": window.start, "end": window.end})
                    continue
                days[day].append({"app": app, "start": window.start, "end": "24:00"})
                if end:
                    days[(day + 1) % 7].append({"app": app, "start": "00:00", "end": window.end})
    return days


def factor_week(days, swap_seconds=TURN_SECONDS):
    """A Week dict from 7 Day dicts. The most common day (blocks and wake time) becomes every day's plan;
    on a tie, the earlier weekday. The days equal to it use it. Each other day gets its own plan."""
    days = [{"blocks": list(day["blocks"]), "wake": day.get("wake")} for day in days]
    for day in days:
        # The same order on each day, so equal days compare equal. sort() is stable: layouts that take turns keep their order.
        day["blocks"].sort(key=lambda block: (minutes(block["start"]), minutes(block["end"])))
    default = max(days, key=lambda day: (days.count(day), -days.index(day)))
    return {"default": default, "swap_seconds": swap_seconds,
            "days": {key: None if day == default else day for key, day in zip(DAY_KEYS, days, strict=True)}}


def migrate_windows(value):
    """A layout schedule dict of older saves (no "week", maybe "windows") in the new shape ("week"). Other values
    come back as is.

    Each rule keeps its days and times exactly. A rule with two or more layouts becomes one block for each
    layout, so the blocks overlap and the layouts take turns as before. Wake times already in the week stay.
    """
    if not isinstance(value, dict) or ("windows" not in value and "week" in value):
        return value
    legacy = LegacySchedule.model_validate({"default": value.get("default"), "windows": value.get("windows") or []})
    week = Week.model_validate(value.get("week") or {})
    days = [{"blocks": blocks, "wake": week.day(weekday).wake} for weekday, blocks in enumerate(legacy_days(legacy.windows))]
    # One turn speed for the whole TV: the most common one of the old rules and default that took turns.
    swaps = Counter(slot.swap_seconds for slot in [legacy.default, *legacy.windows] if slot and slot.rotate)
    swap = swaps.most_common(1)[0][0] if swaps else week.swap_seconds
    rest = {key: item for key, item in value.items() if key not in ("windows", "week")}
    return {**rest, "week": factor_week(days, swap), "turns_note": value.get("turns_note") or len(swaps) > 1}


def with_wake(week, at, weekdays):
    """A Week dict with the older wake setting: `at` on `weekdays`, no wake on the other days."""
    days = [{**week.day(weekday).model_dump(), "wake": at if weekday in weekdays else None} for weekday in range(7)]
    return factor_week(days, week.swap_seconds)


class AppSchedule(Model):
    """When a TV shows its installed layouts. `week` holds the blocks and the wake time of each day. Outside the blocks
    the TV shows the built-in Artwork (see apps.ARTWORK). `default` is the older "rest of the time" layout: old saves still
    load, but nothing uses it."""

    default: AppSlot | None = None
    week: Week = Field(default_factory=Week)
    # The migration joined different turn speeds into one. The Schedule page says so once.
    turns_note: bool = False

    @model_validator(mode="before")
    @classmethod
    def legacy_windows(cls, value):
        return migrate_windows(value)


def wake_time(cfg, day):
    """The wake time on a date, "HH:MM", or None. Every read of the wake time goes through here."""
    return cfg.app_schedule.week.day(day.weekday()).wake


# ESPHome can add the last 6 hex digits of the MAC address to a device's name (name_add_mac_suffix).
MAC_SUFFIX = re.compile(r"[-_ ][0-9a-fA-F]{6}$")


def without_mac(name):
    """A device name without ESPHome's MAC suffix: "akamatis-smart-device-005301" -> "akamatis-smart-device"."""
    return MAC_SUFFIX.sub("", name or "")


class CardError(ValueError):
    """A setting the person can fix. Its text is a plain sentence that any page can show as is."""


class StaleForm(Exception):
    """A form drawn for older settings. `anchor` is the card where the note shows; `message` is the note."""

    def __init__(self, anchor, message=None):
        super().__init__(anchor)
        self.anchor, self.message = anchor, message


class ScheduleError(CardError):
    """A schedule the person can fix, such as two time rules that overlap."""


# Plain sentences for a schedule field's validation error, after the rule's name.
SCHEDULE_FIELDS = {
    "swap_seconds": "switch every 30 seconds to 1 hour.",
    "start": "pick a start time.",
    "end": "pick an end time.",
    "days": "pick at least one day.",
    "apps": "pick each layout once, and at most six layouts.",
}
# The schedule's default rule, in people's words.
USUALLY = "Usually shows"


def parse_schedule(raw):
    """Validate a layout schedule. Raise ScheduleError with a plain sentence.

    An older schedule's time rule is named by rule_label(), and the default slot "Usually shows". It never
    shows a field path or pydantic's "Value error," prefix.
    """
    from pydantic import ValidationError

    try:
        if isinstance(raw, dict) and "windows" in raw:
            LegacySchedule.model_validate({"default": raw.get("default"), "windows": raw["windows"]})
        return AppSchedule.model_validate(raw)
    except ValidationError as error:
        item = error.errors(include_input=False, include_url=False)[0]
    loc = list(item["loc"])
    text = item["msg"].removeprefix("Value error, ").rstrip(".") + "."
    if loc[:1] == ["windows"] and len(loc) == 1:
        raise ScheduleError(f"Use at most {MAX_RULES} time rules.")
    if loc[:1] == ["windows"]:
        window = raw["windows"][loc[1]]
        name = "Time rule " + rule_label(window.get("start", ""), window.get("end", ""), window.get("days", []))
    elif loc[:1] == ["default"]:
        name = USUALLY
    elif loc[-1:] == ["blocks"]:
        raise ScheduleError(f"Use at most {MAX_STORED_BLOCKS} blocks on a day.")
    elif loc[-1:] == ["swap_seconds"]:
        raise ScheduleError(f"Take turns: {SCHEDULE_FIELDS['swap_seconds']}")
    else:  # a block, or the whole schedule, such as two older rules that overlap
        raise ScheduleError(text)
    field = next((part for part in loc[1:] if part in SCHEDULE_FIELDS), None)
    raise ScheduleError(f"{name}: {SCHEDULE_FIELDS[field] if field else text[0].lower() + text[1:]}")


class Site(Model):
    # Empty path selects the bundled Astro starter. Any static build can replace it.
    path: str = ""
    entry: str = "index.html"
    refresh_minutes: int = Field(15, ge=1, le=1440)
    ready_selector: str = Field("", max_length=200)
    providers: list[Literal["art"]] = Field(default_factory=list)
    data: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("entry", "path")
    @classmethod
    def relative_entry(cls, value):
        if Path(value).is_absolute() or ".." in Path(value).parts or "\\" in value:
            raise ValueError("Site entry must be a relative path within the built website")
        return value


class Art(Model):
    enabled: bool = True
    order: Literal["shuffle", "sequential"] = "shuffle"
    change_at: str = "00:00"
    sources: list[Literal["met", "artic", "cleveland"]] = Field(
        default_factory=lambda: ["cleveland", "met", "artic"]
    )
    _time = field_validator("change_at")(clock)


class Television(Model):
    ip: str = ""
    mac: str = ""
    port: Literal[8001, 8002] = 8002
    timeout_seconds: int = Field(8, ge=2, le=30)
    broadcast: str = "255.255.255.255"

    @property
    def identity(self):
        # The MAC names the TV across DHCP address changes; without one, the address is all we know.
        if self.mac:
            return hashlib.sha256(f"mac:{self.mac}:{self.port}".encode()).hexdigest()[:20]
        return self.address_identity

    @property
    def address_identity(self):
        """The pre-MAC identity; state stored under it migrates to `identity`."""
        return hashlib.sha256(f"{self.ip}:{self.port}".encode()).hexdigest()[:20]

    @field_validator("ip")
    @classmethod
    def local_ip(cls, value):
        if value:
            addr = ipaddress.ip_address(value)
            if not addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast:
                raise ValueError("TV address must be a private LAN IP")
        return value

    @field_validator("mac")
    @classmethod
    def mac_address(cls, value):
        if value and not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
            raise ValueError("Use a MAC address such as AA:BB:CC:DD:EE:FF")
        return value.upper()

    @field_validator("broadcast")
    @classmethod
    def broadcast_address(cls, value):
        addr = ipaddress.IPv4Address(value)
        if value != "255.255.255.255" and not addr.is_private:
            raise ValueError("Wake broadcast must stay on the private network")
        return value


class CurvePoint(Model):
    """One point of the brightness curve: at `lux` room light, the TV uses brightness `level`."""

    lux: float = Field(gt=0, le=100_000)
    level: int = Field(ge=0, le=100)  # the Sensor page checks it against the TV's own range


MAX_CURVE_POINTS = 8
# The two settings of older files: (lux field, level field, the lux of the new default curve's end, level for
# "the TV's end"). A level of None used the end of the TV's range: 0 and 100 load as that end, because
# sensor_curve clamps every level to the TV's range. The old bright default was 500 lux, and the new default
# curve ends at 1000 lux, so an old 500 lux loads as a point and the TV stays at its top from 500 lux.
OLD_CURVE_FIELDS = (("dark_lux", "dark_level", 1.0, 0), ("bright_lux", "bright_level", 1000.0, 100))


class BrightnessCurve(Model):
    """How room light (lux) sets Art Mode brightness: a short list of points (lux -> brightness).

    Between two points, the brightness follows log10(lux) (see sensor_curve.target). The points are kept in
    order of light. offset moves the whole curve by whole steps (Darker or Brighter).

    Older files kept two settings (dark_lux/dark_level and bright_lux/bright_level). A saved level loads as a
    point. A level of None used the end of the TV's range: it loads as a point at that end (0 or 100, clamped
    to the TV's range), unless the new default curve has the same end (1 lux).
    """

    points: list[CurvePoint] = Field(default_factory=list)
    offset: int = Field(0, ge=-3, le=3)

    @model_validator(mode="before")
    @classmethod
    def from_two_settings(cls, data):
        names = [name for lux, level, _, _ in OLD_CURVE_FIELDS for name in (lux, level)]
        if not isinstance(data, dict) or not any(name in data for name in names):
            return data
        data = dict(data)
        old = {name: data.pop(name, None) for name in names}
        if "points" not in data:
            points = []
            for lux, level, default_lux, end in OLD_CURVE_FIELDS:
                if old[lux] is None:
                    continue
                if old[level] is not None:
                    points.append({"lux": old[lux], "level": old[level]})
                elif old[lux] != default_lux:
                    points.append({"lux": old[lux], "level": end})
            data["points"] = points
        return data

    @model_validator(mode="after")
    def ordered(self):
        if len(self.points) > MAX_CURVE_POINTS:
            raise ValueError(f"Use at most {MAX_CURVE_POINTS} points. Remove one first")
        if len({point.lux for point in self.points}) != len(self.points):
            raise ValueError("Use each light level once")
        self.points.sort(key=lambda point: point.lux)
        return self


# A noise PSK: 32 bytes in base64, as ESPHome's api: encryption: key: shows it.
SENSOR_KEY = r"[A-Za-z0-9+/]{43}="
# The Sensor page's "Turn the TV off" times, in minutes (and Never).
OFF_AFTER_CHOICES = (15, 30, 60, 120, 240)
# The Sensor page's "Blank when the room is darker than" values, in lux (and Never).
DARK_BELOW_CHOICES = (1, 2, 5, 10, 20, 50)


def first_off_after(blank_after):
    """The off time for settings saved before off_after existed: the first choice later than the blank and at
    least 30 min (the default and the presets' shortest), else None (never). A blank of 5 to 15 min gets 30 min;
    a 30-minute blank gets 1 hour."""
    return next((minutes for minutes in OFF_AFTER_CHOICES if minutes > blank_after and minutes >= 30), None)


DARK_BELOW_DEFAULT = 5.0  # lux: under a lamp-lit room, above the glow of the artwork at its lowest brightness


def _never(value):
    return isinstance(value, str) and value.strip().lower() == "never"


class Sensor(Model):
    """An ESPHome presence and light sensor next to this TV (see sensors.py)."""

    enabled: bool = False  # TV changes need an explicit opt-in
    host: str = Field("", max_length=253)
    port: int = Field(6053, ge=1, le=65535)  # the native API; the web server fallback uses web_port
    web_port: int = Field(80, ge=1, le=65535)
    key: SecretStr = SecretStr("")  # the API encryption key; never shown on a page or in the log
    name: str = Field("", max_length=80)  # the sensor's friendly name, for pages
    presence_entity: str = Field("", max_length=64)  # an object_id; "" picks one by device class
    light_entity: str = Field("", max_length=64)
    # While no one is there: dim after away_after minutes (0: at once), then blank the screen after
    # blank_after minutes, then turn the TV off after off_after minutes (None: never). All count from when
    # the room became empty (see sensor_control.AWAY_ACTIONS).
    away_after: int = Field(0, ge=0, le=240)
    blank_after: int = Field(15, ge=1, le=240)
    off_after: int | None = Field(30, ge=1, le=240)
    # Kept so older files load: dim and blank always run, in that order.
    away_action: Literal["dim"] = "dim"
    # Art Frames turns the TV on for someone only inside these hours. After them, the TV turns itself off
    # (its Sleep After). An end before the start runs past midnight; the same start and end mean all day.
    hours_start: str = "07:00"
    hours_end: str = "23:00"
    # Room dark: when the room light is below this many lux, Art Frames blanks the screen at once, even with
    # someone there, and does not turn the TV on until the light is back (see sensor_control). None: never.
    # Blank the screen while the room light is below this many lux (sensor_control.dark_room); None: never.
    # On by default. The settings file keeps "never" for an owner's Never; a null loads as the default, because
    # the first build of this setting wrote null for its old default (off).
    dark_below: float | None = Field(DARK_BELOW_DEFAULT, gt=0, le=1000)
    curve: BrightnessCurve = Field(default_factory=BrightnessCurve)

    @field_serializer("key")
    def saved_key(self, value):
        # A dump is for the private settings file. repr() and str() of the model still mask the key,
        # and no page or log line receives a dump.
        return value.get_secret_value()

    @field_validator("key")
    @classmethod
    def key_format(cls, value):
        if value.get_secret_value() and not re.fullmatch(SENSOR_KEY, value.get_secret_value()):
            raise ValueError("Use the sensor's 44-character encryption key, or leave it empty")
        return value

    @field_validator("host")
    @classmethod
    def local_host(cls, value):
        value = value.strip()
        if not value:
            return value
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62})\.local", value.lower()):
                return value.lower()
            raise ValueError("Use the sensor's private IP address or its .local name") from None
        mapped = getattr(addr, "ipv4_mapped", None)
        for item in (addr, mapped) if mapped else (addr,):
            if (not item.is_private or item.is_loopback or item.is_link_local or item.is_multicast
                    or item.is_unspecified):
                raise ValueError("Use the sensor's private IP address or its .local name")
        return value

    @field_validator("presence_entity", "light_entity")
    @classmethod
    def object_id(cls, value):
        if value and not re.fullmatch(r"[a-z0-9_]{1,64}", value):
            raise ValueError("Pick one of the listed readings")
        return value

    @model_validator(mode="before")
    @classmethod
    def older_off_after(cls, data):
        # Saved before the TV off step: pick an off time after the saved blank, so the steps keep their order,
        # a save of the Away card works, and a saved room preset still matches.
        if not isinstance(data, dict) or "off_after" in data:
            return data
        blank = data.get("blank_after", 15)
        if isinstance(blank, bool) or not isinstance(blank, int):
            return data  # the field's own check reports a bad blank time
        return {**data, "off_after": first_off_after(blank)}

    @field_validator("off_after", mode="before")
    @classmethod
    def never_off(cls, value):
        # A config file cannot hold null: off_after = "never" means the same as None.
        return None if _never(value) else value

    @field_validator("dark_below", mode="before")
    @classmethod
    def never_dark(cls, value):
        # "never" turns the rule off. A null (or a missing value) is the default level.
        if _never(value):
            return None
        return DARK_BELOW_DEFAULT if value is None else value

    @field_serializer("dark_below")
    def saved_dark(self, value):
        return "never" if value is None else value

    @field_validator("hours_start", "hours_end")
    @classmethod
    def hours_clock(cls, value):
        return clock(value)

    @model_validator(mode="after")
    def needs_host(self):
        if self.enabled and not self.host:
            raise ValueError("Add the sensor's address before you turn it on")
        return self


def app_values(value):
    """Per-TV values of a layout's display settings, by layout settings scope, then setting key."""
    for app, values in value.items():
        if not re.fullmatch(SCOPE, app) or len(values) > 10 or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) for key in values
        ):
            raise ValueError("Invalid layout settings for this TV")
    return value


class TVOptions(Model):
    """Settings each TV owns. Saved settings without these fields load with the defaults."""

    mats: Literal["show", "never"] = "show"  # "never" overrides each painting's own mat
    app_schedule: AppSchedule = Field(default_factory=AppSchedule)
    app_settings: dict[str, dict[str, bool | int | str]] = Field(default_factory=dict, max_length=8)
    sensor: Sensor = Field(default_factory=Sensor)
    _apps = field_validator("app_settings")(app_values)

    @model_validator(mode="before")
    @classmethod
    def legacy_wake(cls, value):
        """Older saves kept one wake time in "wake": {"enabled", "at", "days"}. Move it into the schedule's days."""
        wake = value.get("wake") if isinstance(value, dict) else None
        if not isinstance(wake, dict) or not any(key in wake for key in LEGACY_WAKE):
            return value
        enabled = bool(wake.get("enabled", False))
        at = clock(wake.get("at", "07:00")) if enabled else None
        days = weekdays([int(day) for day in wake.get("days", range(7))])
        schedule = AppSchedule.model_validate(value.get("app_schedule") or {})
        return {**value, "wake": {key: item for key, item in wake.items() if key not in LEGACY_WAKE},
                "app_schedule": {**schedule.model_dump(), "week": with_wake(schedule.week, at, days)}}


class Display(TVOptions):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=60)
    tv: Television = Field(default_factory=Television)
    wake: Wake = Field(default_factory=Wake)
    site: Site | None = None  # None follows the shared website.

    @model_validator(mode="after")
    def valid_wake(self):
        if self.app_schedule.week.has_wake() and (not self.tv.ip or not self.tv.mac):
            raise ValueError("Scheduled wake needs both a TV IP and MAC address")
        return self


class Config(TVOptions):
    # Top-level tv, wake, tv_name, mats and app_settings belong to the main TV.
    timezone: str = "UTC"
    site: Site = Field(default_factory=Site)
    wake: Wake = Field(default_factory=Wake)
    art: Art = Field(default_factory=Art)
    tv: Television = Field(default_factory=Television)
    tv_name: str = Field("Frame 1", min_length=1, max_length=60)
    displays: list[Display] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def wake_configuration(self):
        if self.app_schedule.week.has_wake() and (not self.tv.ip or not self.tv.mac):
            raise ValueError("Scheduled wake needs both a TV IP and MAC address")
        ids = [display.id for display in self.displays]
        addresses = [tv.ip for tv in [self.tv, *(display.tv for display in self.displays)] if tv.ip]
        names = [self.tv_name.casefold(), *(display.name.casefold() for display in self.displays)]
        if len(ids) != len(set(ids)) or len(addresses) != len(set(addresses)) or len(names) != len(set(names)):
            raise ValueError("Each TV needs its own name, identity, and address")
        return self

    @field_validator("timezone")
    @classmethod
    def timezone_exists(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use an IANA timezone, such as America/New_York.") from exc
        return value


def private_write(path: Path, data: bytes) -> None:
    """Replace a file atomically; never create secret files world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)  # make the rename itself durable
            finally:
                os.close(directory)
        except OSError:
            pass  # not every platform or filesystem can sync a directory
    finally:
        Path(name).unlink(missing_ok=True)


class ConfigStore:
    def __init__(self, root: Path, config_path: Path | None = None):
        self.root = root
        self.config_path = config_path
        self.overrides = root / "settings.json"
        self._lock = threading.RLock()

    def load(self) -> Config:
        raw = {}
        if self.config_path:
            with self.config_path.open("rb") as stream:
                raw = tomllib.load(stream)
        if self.overrides.exists():
            # The UI saves a full validated snapshot. To revert to installation defaults,
            # stop the layout and remove settings.json, as documented.
            raw = json.loads(self.overrides.read_text())
        legacy = {
            key: raw.pop(key)
            for key in ("planner", "weather", "calendars", "holiday_country", "holiday_region")
            if key in raw
        }
        source = raw.get("site", {})
        if "planner" in source.get("providers", []):
            legacy["site"] = {"providers": source["providers"], "entry": source.get("entry", "")}
            source["providers"] = [item for item in source["providers"] if item != "planner"]
        if not source.get("path") and source.get("entry") == "dashboard/index.html":
            legacy.setdefault("site", {})["entry"] = source["entry"]
            source["entry"] = "index.html"
        if legacy:
            # Preserve site-owned configuration privately before the first core-only save.
            archive = self.root / "legacy-dashboard.json"
            with self._lock:
                if archive.is_symlink():
                    raise ValueError("Refusing symlinked legacy settings")
                encoded = json.dumps(legacy, indent=2).encode()
                if not archive.exists() or archive.read_bytes() != encoded:
                    private_write(archive, encoded)
        # An installed layout's entry page and artwork need come from apps.installed(), and
        # apps.configure() applies them to each render.
        return Config.model_validate(raw)

    def save(self, cfg: Config) -> None:
        with self._lock:
            self._backup_pre_week()
            private_write(self.overrides, cfg.model_dump_json(indent=2).encode())

    def _backup_pre_week(self) -> None:
        # Older builds can't read a layout schedule with "week". Before the first save in that
        # shape, keep one copy of the older file, so scripts/rollback-remote.sh can put it back.
        backup = self.root / "settings.pre-week.json"
        if backup.is_symlink():
            raise ValueError("Refusing symlinked settings backup")
        if backup.exists():
            return
        try:
            data = self.overrides.read_bytes()
            raw = json.loads(data)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        displays = raw.get("displays") if isinstance(raw.get("displays"), list) else []
        schedules = [raw.get("app_schedule"), *(item.get("app_schedule") for item in displays if isinstance(item, dict))]
        if not any(isinstance(schedule, dict) and "week" in schedule for schedule in schedules):
            private_write(backup, data)

    def mutate(self, change) -> Config:
        with self._lock:
            raw = self.load().model_dump()
            change(raw)
            cfg = Config.model_validate(raw)
            self.save(cfg)
            return cfg
