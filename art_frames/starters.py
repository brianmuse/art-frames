"""Starter plans: a few ready-made schedules for one TV, for the catalog layouts (morning, news, photos).

A plan sets the TV's layout blocks for one day tab (or every day), plus a few layout values. Artwork shows
where no block is. It sets a value only when the installed layout declares that key, and a plan whose layouts
are not installed does not show. Nothing here writes; web.py applies the result and keeps an undo copy.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

MORNING, NEWS, PHOTOS = "morning", "news", "photos"
WEEKEND = ("sat", "sun")


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    line: str
    needs: tuple[str, ...]  # the layouts the plan uses
    blocks: tuple[tuple[str, str, str], ...] = ()  # (app, start, end); Artwork shows where no block is
    wake: str | None = None  # the turn-on time it sets, if the TV has an address and a MAC
    options: dict = field(default_factory=dict)  # {app: {display key: bool}} on this TV
    settings: dict = field(default_factory=dict)  # {app: {key: value}}, shared by every TV
    art: bool = False  # turns on the daily artwork
    refresh: int | None = None  # the household refresh it needs at most, in minutes
    album: bool = False  # asks for the Photos album link when it is missing
    weekdays_only: bool = False  # the turn-on time only Monday to Friday; Every day tab only


MORNING_HOURS = ((MORNING, "06:00", "09:00"),)
PLANS = (
    Plan("art", "Art all day", "The daily artwork, with no layouts.", (), art=True),
    Plan("morning", "Morning info, art the rest of the day",
         "Your calendar and the weather from 6 to 9 AM. Art the rest of the day.", (MORNING,), MORNING_HOURS,
         wake="06:30", art=True),
    Plan("news", "News in the morning, art after", "Headlines from 6 to 9 AM. Art the rest of the day.",
         (NEWS,), ((NEWS, "06:00", "09:00"),), options={NEWS: {"show_art": True}}, art=True),
    Plan("evening-photos", "Art by day, photos at night", "Your photos from 6 to 10 PM. Art the rest of the day.",
         (PHOTOS,), ((PHOTOS, "18:00", "22:00"),), album=True, art=True),
    Plan("photo-frame", "Photo frame", "Your photos all day, a new one every 15 minutes.", (PHOTOS,),
         ((PHOTOS, "00:00", "24:00"),), options={PHOTOS: {"show_caption": True, "show_clock": False}},
         settings={PHOTOS: {"photo_interval": "15 minutes", "order": "Shuffle", "fit": "contain"}},
         refresh=15, album=True),
    Plan("weekdays", "Weekday mornings only",
         "Turns on at 6:30 AM on weekdays, with your calendar from 6 to 9 AM. No turn-on time on weekends.",
         (MORNING,), MORNING_HOURS, wake="06:30", weekdays_only=True, art=True),
)


def find(key):
    return next((plan for plan in PLANS if plan.key == key), None)


def offered(installed, day="every"):
    """The plans these installed layouts can run. Weekday mornings only shows on the Every day tab."""
    ids = {item.id for item in installed}
    return [plan for plan in PLANS if set(plan.needs) <= ids and (day == "every" or not plan.weekdays_only)]


def declared(plan, installed):
    """The plan's option and setting values that the installed layouts declare: ({app: {key: value}}, same)."""
    by_id = {item.id: item for item in installed}
    options, settings = {}, {}
    for app_id, values in plan.options.items():
        keys = {item.key for item in by_id[app_id].manifest.display_settings} if app_id in by_id else set()
        if kept := {key: value for key, value in values.items() if key in keys}:
            options[app_id] = kept
    for app_id, values in plan.settings.items():
        known = {item.key: item for item in by_id[app_id].manifest.settings} if app_id in by_id else {}
        # Like presets, a plan never sets a secret or a list.
        kept = {key: value for key, value in values.items()
                if key in known and known[key].type not in ("secret", "list")
                and (known[key].type != "choice" or value in known[key].choices)}
        if kept:
            settings[app_id] = kept
    return options, settings


def schedule_after(plan, saved, day, can_wake):
    """The TV's saved schedule dict after the plan. `saved` is the full schedule (parse_schedule().model_dump())."""
    schedule = copy.deepcopy(saved)
    week = schedule["week"]
    blocks = [{"app": app, "start": start, "end": end} for app, start, end in plan.blocks]
    current = week["default"] if day == "every" else (week.get("days") or {}).get(day) or week["default"]
    wake = plan.wake if plan.wake and can_wake else current.get("wake")
    if day == "every":
        week["default"] = {"blocks": blocks, "wake": wake}
    else:
        week.setdefault("days", {})[day] = {"blocks": blocks, "wake": wake}
    if plan.weekdays_only and plan.wake and can_wake:
        days = week.setdefault("days", {})
        for weekday in ("mon", "tue", "wed", "thu", "fri"):
            days[weekday] = None  # weekdays follow every day's plan
        for weekend in WEEKEND:
            days[weekend] = {"blocks": [], "wake": None}  # Artwork all day
    return schedule
