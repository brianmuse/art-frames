"""Authenticated, server-rendered management UI; mutation forms always require CSRF."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    get_flashed_messages,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from PIL import Image
from pydantic import ValidationError
from samsungtvws.exceptions import ConnectionFailure
from werkzeug.exceptions import BadRequest, SecurityError
from werkzeug.local import LocalProxy
from werkzeug.routing import BuildError

from . import (
    __version__,
    analytics,
    analytics_web,
    apps,
    artwork,
    auth,
    names,
    picture_looks,
    rotation,
    sensor_control,
    site,
    sources,
    starters,
    timezones,
    tv_recovery,
)
from .activity import view as activity_lines
from .activity import when as activity_time
from .config import (
    DAY_FULL_NAMES,
    DAY_KEYS,
    DAY_NAMES,
    DEFAULT_SWAP_SECONDS,
    MAX_BLOCKS,
    CardError,
    ConfigStore,
    ScheduleError,
    StaleForm,
    day_names,
    minutes,
    parse_schedule,
    private_write,
    rule_label,
    short_time,
    span_label,
    wake_time,
)
from .controller import ERRORS, Busy, Controller, app_copy
from .displays import Displays
from .env import env
from .jobs import Job, Jobs, QueueFull
from .layouts import REFRESH_CHOICES, apply_presets, display_values, preset_choice
from .library import Library
from .network import FetchError
from .reach import Reach, host_allowed
from .sensor_control import SensorHub
from .sensor_control import health_check as sensor_health
from .site_service import WebsitePending
from .television import (
    MOTION_SENSITIVITY_RANGE,
    MOTION_TIMER_CHOICES,
    DisplayError,
    WakeError,
    brightness_range,
    color_temperature_range,
)
from .timewords import ago
from .tv_discovery import DiscoveryBusy, TVDiscovery, suggested_network
from .updates import Updates, fetch_latest, forced_setting

PACKAGE = Path(__file__).resolve().parent
# The descriptions of two errors that have their own page (see the 404 and 400 handlers in create_app).
UNKNOWN_TV = "This TV is not configured"
STARTER_UNDO = "starter_undo"  # library state: the values before the last starter plan, for Undo
UNDO_SECONDS = 24 * 3600  # Undo for a starter plan lasts a day
SETUP_PAIRED = "setup_paired"  # library state: the address the setup's Pair step paired
PLAN_CHOSEN = "plan_chosen"  # library state: a starter plan was applied (the Home checklist)
CHECKLIST_DISMISSED = "checklist_dismissed"  # library state: Home hides the setup list
CHECKLIST_DONE_AT = "checklist_done_at"  # library state: when every step was done; the list shows one more day
TIMEZONE_OK = "timezone_ok"  # library state: UTC is the right timezone
CHECKLIST_SEEDED = "checklist_seeded"  # library state: the first Home visit decided whether the list shows
STALE_SESSION = "Refresh the page and try again"
COOKIES_OFF = "Allow cookies for this address, then sign in."
HOST_REFUSED = ("Art Frames does not answer to this address. "
                "Use the IP address, or add this name to ART_FRAMES_HOSTS.")
BROKEN_PASSWORD = ("Art Frames cannot read its saved password: auth.json in the data folder is damaged, or "
                   "Art Frames may not read it. "
                   "Start Art Frames with ART_FRAMES_RESET_PASSWORD=1, then create a new password with the "
                   "setup code from the log. Your TVs and settings stay as they are.")
# The files signin.html loads before the browser signs in: the only static files served without a password.
SIGNIN_FILES = frozenset({"style.css", "favicon.svg", "Inter-Regular.ttf", "Inter-SemiBold.ttf"})
SESSION_DAYS = 30  # a sign-in lasts this long after the last visit
# The main TV and Config.displays (at most 7).
MAX_TVS = 8
OWN_ERRORS = (ValueError, WebsitePending, WakeError, TimeoutError, ConnectionError)
# Results that mean the TV did nothing; the activity record shows them as "Not done".
NOTHING_DONE = {"asleep", "watching", "unpaired", "unknown", "busy", "blanked", "room-empty"}
SKIPPED = {"skipped": True}
# Results where the TV may have done something but did not confirm it; also shown as "Not done", with the caveat.
UNCONFIRMED = {"unconfirmed"}
# Places on the TV's Picture page that show a job's result inside a panel: place -> panel id.
# A flashed note for a panel has the category "<place>:<category>"; see panel_notes.
PANELS = {"look": "look", "display": "art-display", "options": "art-mode-options", "power": "screen-power", "standby": "standby-test",
          "blank": "soft-blank"}
# Places that show flashed notes inside a card: the Picture panels and the TV settings page's Address recovery card.
NOTE_PLACES = {**PANELS, "recovery": "address-recovery"}
# The pages of one TV. Their URLs carry the TV in the path: /tv/<tv>, /tv/<tv>/schedule, ...
TV_PAGES = ("tv_overview", "tv_schedule", "tv_picture", "tv_health", "tv_settings", "sensors.sensor_page", "tv_setup")
# Pages outside one TV that still keep the chosen TV in ?tv=, so switching TVs stays on them.
TV_AWARE_PAGES = ("gallery", "discover", "app_page", "apps_index", "activity", "household")
# Why an Art Mode action did nothing, after "Not done: ". The TV or Art Frames refused it,
# so the job shows "Not done", not "Failed".
REFUSALS = {
    "unpaired": "pair this TV in TV settings first.",
    "not-art-mode": "the TV is not on in Art Mode. Art Frames did not wake it or change anything.",
    "rejected": "the TV rejected the request.",
    "invalid": "choose one of the listed values.",
    "not-reported": "this TV does not report that setting.",
    "screen-is-on": "the TV answers and is not in standby. Nothing was sent.",
    "wake-window": "wait until the morning wake is done. Nothing was changed.",
    "missing-mac": "the TV does not answer. Add its MAC address in TV settings to use the wake signal.",
    "not-standby": "the TV did not report standby. Nothing was sent.",
    "no-image": "make a preview first. Nothing was sent.",
    "no-brightness": "the TV does not report its brightness, so Art Frames could not restore it. Nothing was sent.",
    "not-blanked": "the screen is not blanked. Nothing was sent.",
}
# The Schedule page saves each card on its own. POST /settings no longer changes the schedule or the turn-on time.
# "Switch every" choices for a rule whose layouts take turns, in seconds. The refresh choices are layouts.REFRESH_CHOICES.
SWITCH_CHOICES = (30, 60, 120, 300, 600, 900, 1800, 3600)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# The page a form POST belongs to, so a page drawn again after an error keeps its sidebar item and TV switcher.
PAGE_OF = {
    **dict.fromkeys(("save_turns", "add_block", "save_block", "delete_block", "save_wake",
                     "delete_wake", "customize_day", "reset_day", "apply_plan", "undo_plan"), "tv_schedule"),
    "save_tv_settings": "tv_settings", "save_mats": "tv_picture", "save_tv_setup": "tv_setup", "setup_pair": "tv_setup",
    **dict.fromkeys(("save_timezone", "save_refresh", "save_artwork", "save_website", "follow_shared"), "household"),
    **dict.fromkeys(("sensors.find", "sensors.connect", "sensors.save_readings", "sensors.save_control",
                     "sensors.save_point", "sensors.switch", "sensors.save_away", "sensors.resume", "sensors.remove"),
                    "sensors.sensor_page"),
    **dict.fromkeys(("server.change_password", "server.save_updates", "server.check_updates", "server.import_settings"),
                    "server.server_page"),
}


def duration(seconds):
    """A duration in plain words: "30 sec", "1 min", "15 min", "1 hour", "3 hours"."""
    if seconds < 60:
        return f"{seconds} sec"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'' if hours == 1 else 's'}"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} sec"


def choices(values, current, scale=1):
    """DurationSelect options as (value, label): the presets, plus a saved value that is not one."""
    values = sorted({*values, current}) if current else list(values)
    return [(value, duration(value * scale)) for value in values]


def form_days(form, name):
    """The posted day checkboxes `name` as weekday numbers, Monday 0. Other values are dropped."""
    return [int(day) for day in form.getlist(name) if day in {"0", "1", "2", "3", "4", "5", "6"}]


STALE = ("This schedule changed after the page opened. Here is the latest version. "
         "Make your change again if you still need it.")


def schedule_version(schedule):
    """A short hash of the whole saved schedule, a model_dump() dict. A form drawn for another version is out of date."""
    data = json.dumps(schedule, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def names_text(names):
    """Names in plain words: "A", "A and B", "A, B and C"."""
    names = list(names)
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " and " + names[-1]


# Why the TV shows Artwork now, in people's words: no block is at this time.
NO_BLOCK = "No layout is scheduled now"


def hhmm(minute):
    """Minutes of the week or day as "HH:MM" of the day."""
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def span_text(start, end):
    """A block's times in plain words. A value that is not a time shows as is."""
    return span_label(start, end)


def first_upper(text):
    """The text with a capital first letter: "all day" becomes "All day"."""
    return text[:1].upper() + text[1:]


def dash_span(start, end):
    """A stretch of the day in short words for a list: "6:00–8:00 AM", "11:00 AM–6:00 PM"."""
    return span_label(start, end).replace(" to ", "–")


def tight_span(start, end):
    """A block's label, as short as it can be: "6–9 AM", "11:30 AM–1 PM", "10 PM–12 AM"."""
    if (start, end) == ("00:00", "24:00"):
        return "All day"
    (one, half), (two, other) = short_time(start), short_time(end)
    return f"{one}–{two} {half}" if half == other else f"{one} {half}–{two} {other}"


def full_day_names(days):
    """Weekdays (0 is Monday) in full words: "Sunday", "Saturday and Sunday", "Monday, Tuesday and Friday"."""
    names = [DAY_FULL_NAMES[day] for day in sorted(days)]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def block_values(form):
    """A block's posted layout and times, unvalidated. An end of 00:00 is midnight, the end of the day."""
    end = form.get("end", "")
    return {"app": form.get("app", ""), "start": form.get("start", ""), "end": "24:00" if end == "00:00" else end}


def check_block(values):
    """A block's times, checked before the schedule is: a plain sentence for a missing or wrong time."""
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", values["start"]):
        raise CardError("Pick a start time.")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d|24:00", values["end"]):
        raise CardError("Pick an end time.")
    if minutes(values["end"]) <= minutes(values["start"]):
        raise CardError("Pick an end time after the start time. A block ends at midnight at the latest.")


def wake_words(week, between=", "):
    """The wake times in plain words: "7:00 AM, every day", or "6:30 AM, Mon to Fri; 8:00 AM, Sat and Sun"."""
    groups = {}
    for weekday in range(7):
        at = week.day(weekday).wake
        if at:
            groups.setdefault(at, []).append(weekday)
    parts = []
    for at, weekdays in sorted(groups.items()):
        days = day_names(weekdays)
        parts.append(clock(at) + between + (days.lower() if days == "Every day" else days))
    return "; ".join(parts)


def block_places(week, app_id):
    """Where a layout's blocks are, in plain words: "7:00 to 9:00 AM every day", "6:00 to 8:00 PM on Mon"."""
    places = [f"{span_text(block.start, block.end)} every day" for block in week.default.blocks if block.app == app_id]
    for weekday, key in enumerate(DAY_KEYS):
        plan = week.days.get(key)
        if plan is not None:
            places += [f"{span_text(block.start, block.end)} on {DAY_NAMES[weekday]}" for block in plan.blocks
                       if block.app == app_id]
    return places


def shares_time(week):
    """Whether two or more layouts show at the same time on some day: blocks that overlap."""
    plans = [week.day(weekday) for weekday in range(7)]
    return any(len(plan.apps_at(edge)) > 1 for plan in plans for edge in plan.edges())


def plan_use(week, weekday):
    """Which days use a tab's plan, in plain words, for the line under the day tabs. weekday None: every day's plan."""
    own = [day for day in range(7) if week.custom(day)]
    if weekday is not None:
        return f"{DAY_FULL_NAMES[weekday]} has its own plan." if week.custom(weekday) else ""
    if not own:
        return ""  # the Every day tab needs no note when every day uses it
    used = [day for day in range(7) if day not in own]
    owners = day_names(own) + (" has its own plan." if len(own) == 1 else " have their own plans.")
    return f"Used on {day_names(used)}. {owners}" if used else f"Not used: {owners[0].lower()}{owners[1:]}"


def artwork_note(cfg, library, now):
    """Why Artwork shows a plain frame on this TV: "off", "empty" or "". Schedule, Health and Now say it."""
    if not cfg.art.enabled:
        return "off"
    return "" if artwork.todays(cfg, library, now) else "empty"


# What Health says for artwork_note(): the title, the detail and the fix button.
ARTWORK_CHECKS = {
    "empty": ("No artwork is included yet",
              "Artwork shows when no layout is scheduled. Until you add artwork, the TV shows a plain frame.",
              "add-artwork"),
    "off": ("Daily artwork is off", "Artwork shows when no layout is scheduled, so the TV shows a plain frame.",
            "artwork-on"),
}
# The short hint on Now and Home while the TV shows Artwork as a plain frame.
ARTWORK_HINTS = {"empty": "Plain frame: no artwork is included yet", "off": "Plain frame: daily artwork is off"}


def percent(minute):
    return round(minute / 1440 * 100, 4)


def day_timeline(day, available):
    """A day's timeline for schedule.html: a lane of blocks for each layout, and the strip of what the TV shows.

    `available` are the installed layouts. Lanes follow their order. Each layout keeps one tone (0-5) on every page.
    Where no block is, or where every block's layout is missing, the strip shows Artwork. Neighbours that show
    the same thing join into one segment.
    """
    names = {item.id: item.name for item in available}
    order = list(names)
    tones = {item: index % 6 for index, item in enumerate(order)}
    lanes = []
    for app_id in sorted({block.app for block in day.blocks}, key=lambda item: (order.index(item) if item in order else len(order), item)):
        blocks = [{"index": index, "start": block.start, "end": block.end, "text": span_text(block.start, block.end),
                   "label": tight_span(block.start, block.end),
                   "left": percent(minutes(block.start)), "width": percent(minutes(block.end) - minutes(block.start))}
                  for index, block in enumerate(day.blocks) if block.app == app_id]
        lanes.append({"app": app_id, "name": names.get(app_id, app_id), "tone": tones.get(app_id, 0),
                      "missing": app_id not in names, "blocks": blocks})
    edges = sorted({0, 1440, *day.edges()})
    stretches = []
    for begin, finish in zip(edges, edges[1:], strict=False):
        # What the TV shows: the installed layouts of the blocks here, else Artwork (apps.plan does the same).
        ids = [app_id for app_id in day.apps_at(begin) if app_id in names]
        if stretches and stretches[-1]["ids"] == ids:
            stretches[-1]["end"] = finish
        else:
            stretches.append({"ids": ids, "start": begin, "end": finish})
    strip = []
    for item in stretches:
        ids = item["ids"]
        shown = [names[app_id] for app_id in ids]
        kind = "artwork" if not ids else "turns" if len(ids) > 1 else "one"
        text = "Artwork" if not ids else " + ".join(shown) + " take turns" if kind == "turns" else names_text(shown)
        strip.append({"kind": kind, "text": text, "tones": [tones.get(app_id, 0) for app_id in ids],
                      "when": "All day" if item["end"] - item["start"] == 1440 else
                      dash_span(hhmm(item["start"]), "24:00" if item["end"] == 1440 else hhmm(item["end"])),
                      "left": percent(item["start"]), "width": percent(item["end"] - item["start"])})
    return {"lanes": lanes, "strip": strip}


def every(seconds):
    """ "every minute", "every 2 min", "every hour", "every 30 sec"."""
    return {60: "every minute", 3600: "every hour"}.get(seconds, "every " + duration(seconds))


def schedule_text(plan):
    """Plain words for what a plan shows: the layout, or the layouts that take turns and how often they switch."""
    if not plan.apps:
        return ""
    names = names_text(app.name for app in plan.apps)
    return f"{names} take turns, switching {every(plan.swap_seconds)}" if plan.rotate else names


def when(moment, now):
    """A time as the user's clock: "7:00 AM" today, else "Mon 7:00 AM"."""
    moment = moment.astimezone(now.tzinfo)
    text = clock(moment.strftime("%H:%M"))
    return text if moment.date() == now.date() else moment.strftime("%a ") + text


def at(moment, now):
    """A time in a sentence: "at 7:00 AM" today, else "on Mon at 7:00 AM"."""
    moment = moment.astimezone(now.tzinfo)
    text = "at " + clock(moment.strftime("%H:%M"))
    return text if moment.date() == now.date() else moment.strftime("on %a ") + text


# Plain sentences for TV and household settings errors, by the field that failed.
FIELD_ERRORS = {
    "ip": "Use the TV's local IP address, such as 192.168.1.50.",
    "mac": "Use a MAC address such as AA:BB:CC:DD:EE:FF.",
    "broadcast": "Use a private broadcast address, such as 192.168.1.255.",
    "tv_name": "Give the TV a name of 1 to 60 characters.",
    "timezone": "Pick a timezone from the list, such as America/New_York.",
    "refresh_minutes": "Refresh every 1 minute to 24 hours.",
    "change_at": "Pick a time for the new day of artwork.",
    "entry": "Use a page inside the website folder, such as index.html.",
    "path": "Use a folder inside the sites folder, such as my-site/dist.",
    "ready_selector": "Use a ready selector of up to 200 characters.",
    "at": "Pick a turn-on time.",
}
MODEL_ERRORS = {
    "Scheduled wake needs both a TV IP and MAC address":
        "A turn-on time needs the TV's IP and MAC address. Add them in TV settings, or remove the turn-on time.",
    "Each TV needs its own name, identity, and address": "Each TV needs its own name and IP address.",
}


def plain(error):
    """One plain sentence for a settings ValidationError. Never a field path or a model term."""
    item = error.errors(include_input=False, include_url=False)[0]
    text = item["msg"].removeprefix("Value error, ").rstrip(".")
    if text in MODEL_ERRORS:
        return MODEL_ERRORS[text]
    field = next((str(part) for part in reversed(item["loc"]) if str(part) in FIELD_ERRORS), None)
    return FIELD_ERRORS[field] if field else "Check the values and try again."


# Real failures of an Art Mode action: the job shows "Failed".
NO_ANSWER = "The TV did not answer. Check that it is on and in Art Mode."
FAILURES = {"network": "The TV does not answer, and the server could not send the wake signal."}


def failure(error):
    """Log Art Frames' own messages; other libraries may echo URLs, tokens or page content."""
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    own = trace is not None and Path(trace.tb_frame.f_code.co_filename).resolve().is_relative_to(PACKAGE)
    if own and type(error) in OWN_ERRORS:
        return f"{type(error).__name__}: {str(error)[:200]}"
    return type(error).__name__


def save_card(store, page, card, values, change, *, endpoint, anchor=None, message="Saved.", done=None,
              url_values=None):
    """Save one card: store(change) writes the settings, then the page opens at the card with `message`.

    An error draws the page again (400) with the posted values and one plain sentence under the card:
    a CardError's own text, a ValidationError through plain(), or a general sentence for any other
    failure, which is logged. A form drawn for an older schedule opens the latest one, with a note
    at the card the StaleForm names.
    """
    try:
        store(change)
    except StaleForm as stale:
        flash(stale.message or STALE, f"card-{stale.anchor}:error")
        return redirect(url_for(endpoint, _anchor=stale.anchor, **(url_values or {})))
    except CardError as error:
        text = str(error)
    except ValidationError as error:
        text = plain(error)
    except (ValueError, OSError) as error:
        current_app.logger.warning("The %s card could not be saved: %s", card, failure(error))
        text = "The settings could not be saved. Try again."
    else:
        if done:
            done()
        anchor = anchor() if callable(anchor) else anchor or card
        flash(message() if callable(message) else message, f"card-{anchor}:success")
        return redirect(url_for(endpoint, _anchor=anchor, **(url_values or {})))
    return page({"card": card, "values": values, "error": text}, 400)


def scheduler_alive(displays):
    alive = getattr(displays, "alive", None)
    if callable(alive):
        alive = alive()
    if alive is None:
        thread = getattr(displays, "thread", None)
        alive = thread is None or thread.is_alive()  # None: not started (tests, startup)
    return bool(alive)


def clock(value):
    """A stored 24-hour "HH:MM" as "7:00 AM"."""
    hour, minute = (int(part) for part in value.split(":"))
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def panel_notes(place):
    """This request's flashed notes for one place, as (category, text).

    place is "notice" (the top of the page), a key of NOTE_PLACES, or a card's place "card-<id>" (a
    saved card on the Schedule, Picture, TV settings and Household settings pages). Templates call it,
    so no template knows how a panel note is marked.
    """
    notes = []
    for category, message in get_flashed_messages(with_categories=True):
        where, _, rest = category.partition(":")
        if rest and (where in NOTE_PLACES or where.startswith("card-")):
            if where == place:
                notes.append((rest, message))
        elif place == "notice":
            notes.append((category, message))
    return notes


def tv_state(status, demo):
    """A TV's state word and its status-dot tone, from its stored status only."""
    if demo:
        return {"word": "Demo", "tone": "not-done"}
    if status.get("playing"):
        return {"word": "Watching TV", "tone": "running"}
    if status.get("awake"):
        return {"word": "Art Mode", "tone": "done"}
    if status.get("paired") and status.get("known"):
        return {"word": "Standby", "tone": "not-done"}
    if status.get("paired"):
        return {"word": "Status unknown", "tone": "waiting"}
    return {"word": "Not paired", "tone": "warning"}


# A TV's health checks (health_checks): the tone of each row, and how many problems a TV has.
PROBLEM, OK, INFO = "problem", "ok", "info"
WAKE_TIME = re.compile(r"at (\d\d:\d\d)")


def wake_check(text, cfg):
    """The Health row of the last scheduled wake, from Controller._wake_result's fixed text. None without a wake."""
    words = wake_words(cfg.app_schedule.week)
    if not words:
        return None
    text = text if isinstance(text, str) else ""
    times = [clock(found) for found in WAKE_TIME.findall(text or "")]
    if not text:
        return {"id": "wake", "tone": INFO, "title": f"Turns on at {words}. It has not run yet."}
    if text.startswith("Wake confirmed"):
        return {"id": "wake", "tone": OK, "title": f"Last turn-on reached Art Mode at {times[0]}" if times else "Last turn-on reached Art Mode"}
    if text.startswith("Wake not confirmed"):
        return {"id": "wake", "tone": INFO, "title": "Last turn-on was late",
                "detail": f"The TV reached Art Mode at {times[-1]}, after the turn-on time." if len(times) > 1 else ""}
    if text.startswith("Wake was attempted"):
        return {"id": "wake", "tone": PROBLEM, "title": "Last turn-on did not reach Art Mode",
                "detail": "Check the TV's MAC address, and turn on Power On with Mobile on the TV.", "fixes": ["connection"]}
    if "programme" in text:
        return {"id": "wake", "tone": INFO, "title": "Last turn-on was skipped because someone was watching TV"}
    if "room is dark" in text:
        return {"id": "wake", "tone": INFO, "title": "Last turn-on was skipped because the room was dark"}
    return {"id": "wake", "tone": INFO, "title": "Last turn-on was skipped because the TV was not paired"}


# The next steps of the "did not turn on for someone" Health row, by sensor_control's wake result code.
WAKE_STEPS = {
    "not-confirmed": ("Make sure the TV is plugged in.",
                      "On the TV, turn on Settings › General › Network › Expert Settings › Power On with Mobile.",
                      "Select Turn on now to test."),
    "missing-mac": ("Add the TV's MAC address in TV settings.",),
    "network": ("Art Frames needs host networking to send the turn-on signal. See the hosting docs.",),
    "unpaired": ("Pair the TV in TV settings.",),
}
HOSTING_DOCS = "https://github.com/brianmuse/art-frames/blob/main/docs/hosting.md"


def sensor_wake_row(row, cfg, state):
    """The sensor's "did not turn on for someone" row with real next steps and a Turn on now button.

    sensor_control.wake_check() writes the row; this keeps its detail and time, and replaces its one fix
    (the Sensor page, which cannot fix a failed turn-on). `state` is the stored sensor control state.
    Other rows pass through unchanged.
    """
    if row.get("id") != "sensor-wake" or row.get("tone") != PROBLEM:
        return row
    code = state.get("wake_result")
    steps = WAKE_STEPS.get(code, WAKE_STEPS["not-confirmed"])
    if code == "unpaired":
        fixes = ["pair"]
    elif code == "missing-mac" or not cfg.tv.mac:
        fixes = ["connection"]
    elif code == "network":
        fixes = ["wake", "docs"]
    else:
        fixes = ["wake", "connection"]
    return {**row, "title": f"{cfg.tv_name} did not turn on for someone", "steps": list(steps), "fixes": fixes,
            "docs": HOSTING_DOCS if code == "network" else ""}


def failing_apps(status_apps, apps_used):
    """The layouts whose last refresh failed on this TV, and the layouts that refreshed fine, as two lists of (layout, AppHealth)."""
    failed, fine = [], []
    for item in apps_used:
        record = status_apps.get(item.id)
        if record and record.last_error_at and (not record.last_success or record.last_error_at > record.last_success):
            failed.append((item, record))
        elif record and record.last_success:
            fine.append((item, record))
    return failed, fine


def help_parts(text):
    """A setting's help as (first sentence, the rest). The page shows the rest under "Learn more"."""
    match = re.match(r"(.+?[.!?])\s+(\S.*)", text or "", re.S)
    return (match.group(1), match.group(2)) if match else (text or "", "")


REFRESH_PRESETS = (  # Household settings → Refresh: (value, label, minutes, line under it)
    ("often", "Often", 5, "Every 5 min. For photos and clocks."),
    ("normal", "Normal", 15, "Every 15 min. Good for most layouts."),
    ("calm", "Calm", 60, "Every hour. For art and dashboards that change slowly."),
)


REFRESH_OPTIONS = [(key, label, line, {"refresh": str(minutes)}) for key, label, minutes, line in REFRESH_PRESETS]


def refresh_preset(minutes):
    """The refresh preset these minutes match, or "custom"."""
    return next((key for key, _, value, _ in REFRESH_PRESETS if value == minutes), "custom")


def refresh_saved(minutes):
    return f"Saved. Layouts refresh every {duration(minutes * 60)}, starting with the next update."


def changed_count(fields, values, configured=()):
    """How many of these layout settings differ from their default: the "N changed" of an Advanced row."""
    count = 0
    for field in fields:
        if field.type == "secret":
            count += field.key in configured
        elif field.type == "list":
            count += bool(values.get(field.key))
        else:
            count += values.get(field.key, field.default) != field.default
    return count


def choice_label(value):
    """A choice value as people read it: "cover" is "Cover", "top_stories" is "Top stories"."""
    words = str(value).replace("_", " ")
    return words[:1].upper() + words[1:]


SOURCE_LIMIT = 8_192  # .dayframe-source.json is a few hundred bytes


def app_source(chosen):
    """Where an installed layout came from, in plain words, from its .dayframe-source.json. None without one.

    Only whitelisted fields are read (see docs/cli-deployment.md). The link is kept only for GitHub.
    """
    path = chosen.manifest_path.parent / names.SOURCE_FILE
    try:
        # A link could point at any private JSON file, whose fields would then show on the page.
        if path.is_symlink() or not path.is_file() or path.stat().st_size > SOURCE_LIMIT:
            return None
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    text = lambda key: str(record.get(key) or "")[:200]  # noqa: E731
    kind, label, commit, ref = text("kind"), text("label"), text("commit")[:7], text("ref")
    if kind == "path":
        line = f"Local folder {label}" if label else "A local folder"
    else:
        line = text("repository") or label or "A Git repository"
        if ref:
            line += f" · {ref}"
    if commit:
        line += f" at {commit}"
    try:
        installed = datetime.fromisoformat(text("installed_at")).strftime("%b %-d, %Y") if text("installed_at") else ""
    except ValueError:
        installed = ""
    url = text("url")
    return {"text": line, "installed": installed,
            "url": url if url.startswith("https://github.com/") else ""}


def options_fingerprint(manifest):
    """A short hash of a layout's options on each TV: the ordered (key, type) of its display_settings.
    The On each TV form carries it, so a form drawn for other switches is refused. A new label or
    help text does not change it."""
    items = [[item.key, item.type] for item in manifest.display_settings]
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


def copy_matches(path, digest):
    """Whether the file at `path` holds the image with `digest`, so its URL never gets a 409."""
    try:
        if path.stat().st_size > site.MAX_ASSET:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


OPTIONS_STALE = "This layout's options changed after the page opened. Here are its latest options."


def missing_secrets(chosen, root):
    """The top-level private settings of a layout that are still empty: the layout needs setup until they are set."""
    if chosen.builtin:
        return []  # the built-in Artwork has no settings
    store = chosen.store(root)
    manifest = store.manifest() or chosen.manifest
    configured = store.public_values(manifest)[1]
    return [item for item in manifest.settings if item.type == "secret" and item.key not in configured]


def setting_words(label):
    """A setting's label inside a sentence: "Shared album link" is "shared album link", "API key" stays."""
    first = label.split(" ", 1)[0]
    return label if len(first) > 1 and first.isupper() else label[:1].lower() + label[1:]


def setup_text(name, missing):
    """Why a layout needs setup, in one sentence: "Photos needs a shared album link before it can show anything." """
    words = setting_words(missing[0].label)
    article = "an" if words[:1].lower() in "aeiou" else "a"
    return f"{name} needs {article} {words} before it can show anything."


def test_text(record, now):
    """The result of Health's last "Test <app>" after the layout's last error, in words, or "".

    A passed test does not clear the problem: only a refresh on the TV does.
    """
    if not record.test_at or (record.last_error_at and record.test_at < record.last_error_at):
        return ""
    moment = record.test_at.astimezone(now.tzinfo).strftime("%-I:%M %p")
    if record.test_error:
        return f"Test failed at {moment}: {record.test_error.rstrip('.')}."
    return f"Test passed at {moment}. The next refresh on the TV clears this problem."


def timed_out(error):
    """Whether a layout's stored error says it took too long, so testing it again is the first fix."""
    text = (error or "").lower()
    return "in time" in text or "timed out" in text or "timeout" in text


def health_checks(screen, cfg, now, available, *, alive, demo, settings_root=None, sensor_rows=()):
    """This TV's health, top to bottom, from stored state only. Never contacts the TV.

    Each check is {"id", "tone" (PROBLEM, OK or INFO), "title", "detail", "when", "fixes", "app",
    "field", "test"}. "fixes" names the buttons the page draws: "pair", "check", "retry", "app" (the
    layout's page), "setup" (the layout's first empty setting, "field"), "test" (render the layout once, see
    preview_site; "test" holds the last result), "connection" (TV settings) and "sensor" (the Sensor
    page). Problems come first, in order; the first is the headline on Now, Home and the sidebar.
    `settings_root` holds the layouts' saved settings, to find layouts that need setup. `sensor_rows` are the rows
    of sensor_control.health_check() and hub_check(), without the Nones.
    """
    state = screen.library.state
    status, checked = state("tv_status", {}), state("tv_status_at")
    name = cfg.tv_name
    checks = []
    if alive:
        checks.append({"id": "scheduler", "tone": OK, "title": "Art Frames is running"})
    else:
        checks.append({"id": "scheduler", "tone": PROBLEM, "title": "The Art Frames scheduler stopped",
                       "detail": "Scheduled updates and wake times do not run. Restart Art Frames."})
    paired = demo or status.get("paired")
    if paired:
        checks.append({"id": "paired", "tone": OK, "title": "Paired"})
    else:
        checks.append({"id": "paired", "tone": PROBLEM, "title": f"{name} is not paired",
                       "detail": f"Pair {name} to show pictures on it.", "fixes": ["pair"]})
    if paired:
        if not checked:
            checks.append({"id": "answer", "tone": INFO, "title": f"Art Frames has not checked {name} yet",
                           "detail": "Check TV asks the TV what it shows. It never wakes the TV.", "fixes": ["check"]})
        elif status.get("known"):
            checks.append({"id": "answer", "tone": OK, "title": f"{name} answered · {tv_state(status, demo)['word']}",
                           "when": ago(checked, now)})
        else:
            checks.append({"id": "answer", "tone": INFO, "title": "TV did not answer at the last check",
                           "detail": "It may be off or in deep standby. Art Frames never wakes it to check.",
                           "when": ago(checked, now), "fixes": ["check"]})
    error, error_at = state("scheduler_error"), state("scheduler_error_at")
    shown = state("last_display") or {}
    shown = shown if shown.get("device") == cfg.tv.identity else {}
    if error:
        checks.append({"id": "update", "tone": PROBLEM, "title": "The last scheduled update failed",
                       "detail": str(error).rstrip(".") + ".", "when": ago(error_at, now) if error_at else "",
                       "fixes": ["retry"]})
    elif shown.get("at"):
        checks.append({"id": "update", "tone": OK, "title": "Last update reached the TV", "when": ago(shown["at"], now)})
    elif paired:
        checks.append({"id": "update", "tone": INFO, "title": "Nothing sent yet",
                       "detail": f"Art Frames sends a picture when {name} is on in Art Mode."})
    used = [item for item in available if apps.uses(cfg, item, available)]
    setup = [(item, missing) for item in used if settings_root is not None
             for missing in [missing_secrets(item, settings_root)] if missing]
    for item, missing in setup:
        checks.append({"id": f"setup-{item.id}", "tone": PROBLEM, "title": f"{item.name} needs setup",
                       "detail": f"Add the {setting_words(missing[0].label)} so {item.name} can show pictures.",
                       "fixes": ["setup"], "app": item.id, "field": missing[0].key})
    used = [item for item in used if item not in {app for app, _ in setup}]
    if used:
        failed, fine = failing_apps(screen.controller.rotation_status(now).apps, used)
        for item, record in failed:
            checks.append({"id": f"app-{item.id}", "tone": PROBLEM, "title": f"{item.name} could not refresh",
                           "detail": (record.last_error or "").rstrip(".") + "." if record.last_error else "",
                           "when": ago(record.last_error_at, now), "app": item.id, "test": test_text(record, now),
                           "fixes": ["test", "app"] if timed_out(record.last_error) else ["app", "test"]})
        if fine:
            checks.append({"id": "apps", "tone": OK, "title": names_text(item.name for item, _ in fine) + " refresh OK"})
        waiting = [item for item in used if item not in {app for app, _ in failed + fine}]
        if waiting:
            checks.append({"id": "apps-waiting", "tone": INFO,
                           "title": names_text(item.name for item in waiting) + (" has" if len(waiting) == 1 else " have")
                           + " not refreshed on this TV yet"})
    note = artwork_note(cfg, screen.library, now) if apps.sources(cfg, available) else ""
    if note:
        title, detail, fix = ARTWORK_CHECKS[note]
        checks.append({"id": "artwork", "tone": INFO, "title": title, "detail": detail, "fixes": [fix]})
    wake = wake_check(state("wake_result"), cfg)
    if wake:
        checks.append(wake)
    for row in sensor_rows:
        checks.append(dict(row))
    for check in checks:
        check.setdefault("test", "")
        check.setdefault("field", "")
        check.setdefault("detail", "")
        check.setdefault("when", "")
        check.setdefault("fixes", [])
        check.setdefault("app", "")
        check.setdefault("steps", [])
        check.setdefault("docs", "")
    checks.sort(key=lambda check: check["tone"] != PROBLEM)  # stable: problems first, each group in order
    return checks


def health_headline(checks):
    """The first problem, or None when every check passes; and the number of problems."""
    problems = [check for check in checks if check["tone"] == PROBLEM]
    return (problems[0] if problems else None), len(problems)


def renderer_ready(endpoint):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            probe.connect(endpoint)
    except OSError:
        return False
    return True


def no_network():
    raise RuntimeError("Tests and the demo never call GitHub")


def create_app(root: Path, *, config_path=None, password=None, demo=False, testing=False, reach=None, updates=None):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A password file must be long. Without one, auth.json holds the password, or first-run setup makes it.
    if not demo and password is not None and len(password) < auth.FILE_MINIMUM:
        raise ValueError(
            "Set ART_FRAMES_PASSWORD_FILE (or DAYFRAME_PASSWORD_FILE) to a file containing a password "
            f"of at least {auth.FILE_MINIMUM} characters, or leave it unset to create a password in the browser"
        )
    configs = ConfigStore(root, config_path)
    configs.load()  # fail early on invalid installation settings
    try:
        # A layout whose source was renamed gets each TV's options back from its earlier scope.
        apps.keep_tv_options(configs, apps.installed())
    except (ValueError, OSError) as error:
        apps.log.warning("Could not restore TV options from an earlier install: %s", type(error).__name__)
    library = Library(root)
    controller = Controller(configs, library, demo=demo)
    displays = Displays(configs, library, controller, demo=demo)
    # Tests run each queued command as it is added; a test can set mode "manual" and drain().
    jobs = displays.jobs = Jobs(mode="inline" if testing else "thread")
    key_file = root / ".session-secret"
    if not key_file.exists():
        private_write(key_file, secrets.token_bytes(32))
    secret_key = key_file.read_bytes()
    signin = auth.Auth(root, secret_key, password)
    if env("RESET_PASSWORD") == "1" and not demo:
        auth.log.warning(signin.reset())
    if signin.mode == "broken":
        auth.log.error(signin.problem())
    if reach is None:
        reach = Reach(env("BIND", "127.0.0.1"), env("PORT", "8765"), found=[], bridge=False)
    if updates is None:
        updates = Updates(root, default_on=signin.mode != "file", forced=forced_setting(env("UPDATE_CHECK")),
                          fetch=no_network if testing or demo else fetch_latest)
    # "*" in ART_FRAMES_HOSTS accepts any home network name (reach.host_allowed); Flask checks a plain list.
    hosts = env("HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    lan_hosts = "*" in hosts
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        MAX_CONTENT_LENGTH=25_000_000,
        SESSION_COOKIE_HTTPONLY=True,
        # Lax, not Strict: a link from another app or a QR code then opens signed in. Every POST still needs CSRF.
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=env("SECURE_COOKIES") == "1",
        # A sign-in lasts 30 days from the last visit: Flask sends the cookie again on each request.
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
        SESSION_REFRESH_EACH_REQUEST=True,
        TRUSTED_HOSTS=None if lan_hosts else hosts,
        TESTING=testing,
    )
    app.extensions.update(
        configs=configs, library=library, controller=controller, displays=displays, tv_discovery=TVDiscovery(), jobs=jobs,
        auth=signin, reach=reach, updates=updates,
    )
    # The Sensor page and its control loop (sensor_web.py, sensor_control.py). __main__.py starts the loop.
    # sensor_web imports save_card from this module, so its blueprint is imported here, after this module loads.
    from .sensor_web import now_status, room_view
    from .sensor_web import pages as sensor_pages

    app.extensions["sensors"] = SensorHub(displays, demo=demo)
    app.register_blueprint(sensor_pages)
    from .server_web import pages as server_pages

    app.register_blueprint(server_pages)
    app.jinja_env.globals.update(
        timezone_choices=timezones.choices,  # for timezone_field() in _ui.html
        duration=duration, choices=choices, day_names=day_names, rule_label=rule_label, clock=clock,
        help_parts=help_parts, choice_label=choice_label, setting_words=setting_words, changed_count=changed_count,
        SWITCH_CHOICES=SWITCH_CHOICES, REFRESH_CHOICES=REFRESH_CHOICES, REFRESH_OPTIONS=REFRESH_OPTIONS, refresh_preset=refresh_preset, DAYS=DAYS,
        DEFAULT_SWAP_SECONDS=DEFAULT_SWAP_SECONDS,
    )
    # Resolve a stable target from each URL, never from another tab's session state.
    configs = LocalProxy(lambda: g.screen.configs)
    library = LocalProxy(lambda: g.screen.library)

    def client():
        return request.remote_addr or "unknown"

    def wants_json():
        return request.accept_mimetypes.best == "application/json" or request.path.endswith(".json")

    def form_token():
        """The token for the sign-in and setup forms, kept in this browser's session cookie."""
        if "form_token" not in session:
            session["form_token"] = secrets.token_urlsafe(32)
        return session["form_token"]

    def bare(template, status=200, **words):
        """A page before sign-in. It has no TV or settings, so it skips the page context. A request that
        cannot build URLs (an untrusted host) gets plain text."""
        try:
            body = app.jinja_env.get_template(template).render(form_token=form_token(), **words)
        except Exception:
            body = "Sign in to Art Frames"
        return body, status, {"Content-Type": "text/html; charset=utf-8"}

    def signed_out():
        """The answer to a request without a sign-in. A page poll gets JSON, so app.js reloads to the form."""
        if wants_json():
            return {"status": "signed-out"}, 401, {"X-Sign-In": "required"}
        target = request.full_path.rstrip("?") if request.method == "GET" else "/"
        return bare("signin.html", 401, next=target, mode=signin.mode)

    def start_session():
        """Sign this browser in for SESSION_DAYS from its last visit."""
        session.clear()  # also drops the form token and the signed-out note
        session.permanent = True
        session["auth"] = signin.mark()
        session["csrf"] = secrets.token_urlsafe(32)

    def no_cookie():
        """A form post with no session cookie at all: the browser blocks cookies for this address."""
        return app.config["SESSION_COOKIE_NAME"] not in request.cookies

    def same_origin():
        """A sign-in or setup form must come from this site's own page.

        The form token is the main check: a page on another site cannot read it. Headers cannot be the
        main check, because on a plain-HTTP home address (http://192.168.1.20) browsers send no
        Sec-Fetch-Site, and some send "Origin: null". A header that names another site still refuses."""
        expected = session.get("form_token")
        supplied = request.form.get("form_token", "")
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return False
        if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return False
        origin = request.headers.get("Origin")
        return origin in (None, "null") or urlsplit(origin).netloc == request.host

    def basic_user():
        """The HTTP Basic sign-in of a script or curl: True, False (wrong) or None (none sent).
        Art Frames never asks for Basic auth (no WWW-Authenticate), so browsers show the sign-in form."""
        credentials = request.authorization
        if not credentials or credentials.type != "basic":
            return None
        return (credentials.username in (names.LOGIN_USER, "art-frames")
                and signin.check(credentials.password or ""))

    @app.before_request
    def access():
        # A request Flask could not route (an untrusted host, a bad address) gets Flask's plain 400.
        # Nothing below may build a URL for it.
        if isinstance(request.routing_exception, SecurityError):
            abort(400, HOST_REFUSED)
        if isinstance(request.routing_exception, BadRequest):
            return None
        if lan_hosts and not host_allowed(request.host, hosts):
            abort(400, HOST_REFUSED)
        # The sign-in page needs its stylesheet, fonts and icon before the browser signs in. Only those are public.
        if request.path == "/health" or (
                request.endpoint == "static" and (request.view_args or {}).get("filename") in SIGNIN_FILES):
            return None
        if not demo:
            if signin.mode == "broken":
                if wants_json():
                    return {"status": "broken", "message": BROKEN_PASSWORD}, 503
                return bare("signin.html", 503, mode="broken", next="/", done="", error=BROKEN_PASSWORD)
            if signin.mode == "setup":
                # Before the password exists, only first-run setup opens.
                if request.endpoint in ("first_run", "first_run_save"):
                    return None
                if request.method == "GET" and not wants_json():
                    return redirect(url_for("first_run"))
                return {"status": "setup"}, 401, {"X-Sign-In": "required"}
            signed = signin.valid_mark(session.get("auth"))
            # After a form sign-out, this browser's cached Basic sign-in no longer counts: it signs in
            # with the form again. A script sends no cookie, so Basic still works for it.
            credentials = request.authorization
            if not signed and credentials and credentials.type == "basic" and not session.get("signed_out"):
                wait = signin.throttle.wait(client())
                if wait:
                    return "Too many sign-in tries. Wait a minute.", 429, {"Retry-After": str(wait)}
                signed = basic_user()
                if signed:
                    signin.throttle.passed(client())
                else:
                    signin.throttle.failed(client())
                    app.logger.warning("A sign-in with a wrong password came from %s", client())
            if not signed:
                if request.endpoint == "sign_in":
                    return None
                return signed_out()
        g.signed_in = True  # error_page shows the app shell only after this point
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        # A TV page names its TV in the path (/tv/<tv>/...); every other page and POST uses ?tv=.
        try:
            g.screen = displays.get((request.view_args or {}).get("tv") or request.args.get("tv", "main"))
        except ValueError:
            abort(404, UNKNOWN_TV)
        if request.method == "POST":
            supplied = request.form.get("csrf", "")
            if not hmac.compare_digest(supplied, session["csrf"]):
                abort(400, STALE_SESSION)

    def safe_next(value):
        """A page on this site to open after sign-in; never another site."""
        value = value or ""
        home = url_for("dashboard", tv=None)
        # Browsers drop tabs and new lines from an address, so "/\t/evil.example" would open another site.
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value) or "\\" in value:
            return home
        if not value.startswith("/") or value.startswith("//"):
            return home
        parts = urlsplit(value)
        if parts.scheme or parts.netloc:
            return home
        return value

    @app.route("/sign-in", methods=["GET", "POST"])
    def sign_in():
        target = safe_next(request.values.get("next"))
        if g.get("signed_in"):
            return redirect(target)
        words = {"next": target, "mode": signin.mode, "done": request.args.get("done", "")}
        if request.method == "GET":
            return bare("signin.html", 200, **words)
        if no_cookie():
            return bare("signin.html", 400, **words, error=COOKIES_OFF)
        if not same_origin():
            return bare("signin.html", 400, **words, error="Open Art Frames again, then sign in.")
        wait = signin.throttle.wait(client())
        if wait:
            return bare("signin.html", 429, **words, error=f"Too many tries. Wait {wait} seconds, then try again.")
        if not signin.check(request.form.get("password", "")):
            signin.throttle.failed(client())
            app.logger.warning("A sign-in with a wrong password came from %s", client())
            return bare("signin.html", 401, **words, error="That password is not right. Try again.")
        signin.throttle.passed(client())
        start_session()
        app.logger.info("Signed in from %s", client())
        return redirect(target)

    @app.post("/sign-out")
    def sign_out():
        session.clear()
        session["signed_out"] = True
        return redirect(url_for("sign_in", tv=None, done="out"))

    @app.post("/sign-out-everywhere")
    def sign_out_everywhere():
        signin.sign_out_everywhere()
        session.clear()
        session["signed_out"] = True
        return redirect(url_for("sign_in", tv=None, done="everywhere"))

    def first_run_page(status=200, **words):
        typed = words.pop("code", request.args.get("code", ""))
        return bare("first_run.html", status, code=typed, code_ok=signin.check_code(typed),
                    minimum=auth.MINIMUM, **words)

    @app.get("/setup")
    def first_run():
        if signin.mode != "setup":
            return redirect(url_for("dashboard", tv=None))
        return first_run_page()

    @app.post("/setup")
    def first_run_save():
        if signin.mode != "setup":
            return redirect(url_for("dashboard", tv=None))
        code = request.form.get("code", "")
        if no_cookie():
            return first_run_page(400, code=code, error=COOKIES_OFF)
        if not same_origin():
            return first_run_page(400, code=code, error="Open the setup page again, then try again.")
        wait = signin.throttle.wait(client())
        if wait:
            return first_run_page(429, code=code, error=f"Too many tries. Wait {wait} seconds, then try again.")
        if signin.code_expired():
            fresh = auth.show_code(signin.new_code())
            app.logger.warning("The setup code expired. The new setup code is %s", fresh)
            print(f"New Art Frames setup code: {fresh}", flush=True)
            return first_run_page(400, code="", error="That setup code expired. Art Frames made a new one. "
                                                      "Find it in the log, then try again.")
        if not signin.check_code(code):
            signin.throttle.failed(client())
            app.logger.warning("A wrong setup code came from %s", client())
            return first_run_page(400, code="", error="That setup code is not right. Copy it from the log again.")
        try:
            signin.set_password(request.form.get("password", ""), request.form.get("repeat", ""), setup=True)
        except auth.PasswordError as error:
            return first_run_page(400, code=code, error=str(error))
        signin.throttle.passed(client())
        start_session()
        flash(f"Your password is saved. This device stays signed in for {SESSION_DAYS} days after each visit.",
              "success")
        return redirect(url_for("dashboard", tv=None))

    @app.url_defaults
    def target_tv(endpoint, values):
        if endpoint not in {"static", "health"} and "screen" in g:
            values.setdefault("tv", g.screen.id)

    @app.after_request
    def headers(response):
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                # The tile image URL carries its SHA-256, so its content never changes.
                "Cache-Control": "private, max-age=86400, immutable" if request.endpoint == "sent_image" and response.status_code == 200 else "no-store",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' https://images.metmuseum.org https://www.artic.edu https://openaccess-cdn.clevelandart.org; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            }
        )
        return response

    def sidebar_count(name, count):
        """A sidebar count runs on every page, so an unreadable record logs and shows no count, never a 500."""
        try:
            return count()
        except Exception as error:
            app.logger.warning("The %s count could not be read: %s", name, failure(error))
            return 0

    @app.context_processor
    def context():
        cfg = configs.load()
        job = jobs.get(request.args.get("job", "")) if "job" in request.args else None
        # Home shows every TV, so it watches a job of any TV.
        job = job if job and (job.tv == g.screen.id or request.endpoint == "dashboard") and job.source == "you" else None
        finished = bool(job) and job.finished  # read once, so the page shows the note or the watcher
        tvs = [{**item, "state": tv_state(displays.get(item["id"]).library.state("tv_status", {}), demo)}
               for item in displays.items()]
        installed = apps.usable(cfg)
        # The sidebar shows Artwork when layouts are installed (Artwork shows outside their blocks), or when the
        # website asks for it.
        uses_artwork = bool(installed) or "art" in cfg.site.providers
        # The pages describe the first layout's entry page and artwork need, as its renders use them.
        cfg = apps.configure(cfg, installed[0]) if installed else cfg
        return {
            # The page this request draws; a form POST draws the page it belongs to.
            "nav_page": PAGE_OF.get(request.endpoint, request.endpoint),
            "installed_apps": installed,
            "uses_artwork": uses_artwork,
            # The layout whose settings page is open.
            "current_app": (request.view_args or {}).get("app_id", ""),
            "tvs": tvs,
            "current_tv": next(item for item in tvs if item["id"] == g.screen.id),
            "switch_url": switch_url,
            "tv_state": tv_state,
            "version": __version__,
            "update": updates.view(),
            "password_mode": signin.mode,
            "scheduler_ok": scheduler_alive(displays),
            # The number of problems on this TV's Health page, for the dot next to Health.
            "health_count": sidebar_count("Health", lambda: health_headline(checks_of(g.screen, None, None, installed))[1]),
            "job_note": job if finished else None,
            "watch_job": job if job and not finished else None,
            "activity_count": jobs.active(),
            "cfg": cfg,
            "demo": demo,
            "csrf": session.get("csrf", ""),
            "panel_notes": panel_notes,
            "providers": sources.PROVIDERS,
            "now": datetime.now(ZoneInfo(cfg.timezone)),
            # The first installed layout's manifest: the Now page links to its settings page.
            "layout_manifest": installed[0].manifest if installed else None,
            "screens": displays.items(),
            "screen_id": g.screen.id,
        }

    def switch_url(tv_id):
        """The same page for another TV, so the TV switcher keeps the section. Other pages open its Overview."""
        endpoint = PAGE_OF.get(request.endpoint, request.endpoint)
        if endpoint == "tv_health" and "days" in request.args:
            return url_for(endpoint, tv=tv_id, days=request.args["days"])
        if endpoint == "app_page" and not displays.get(tv_id).configs.load().site.path:
            return url_for(endpoint, tv=tv_id, app_id=request.view_args["app_id"])
        if endpoint in TV_PAGES or endpoint in TV_AWARE_PAGES and endpoint != "app_page":
            return url_for(endpoint, tv=tv_id)
        return url_for("tv_overview", tv=tv_id)

    @app.errorhandler(413)
    def too_large(error):
        return error_page(error, message="Upload an image smaller than 25 MB.")

    def error_page(error, **words):
        """A styled error page. An unknown TV in the address leaves no TV for the sidebar: it shows the first TV.
        A request refused before it reached Art Frames (such as one for an untrusted host) gets the plain page."""
        if not g.get("signed_in"):
            return error  # the plain page: no sidebar, TV names or layout names before sign-in
        try:
            if "screen" not in g:
                g.screen = displays.get("main")
            return render_template("error.html", **words), error.code
        except (AttributeError, RuntimeError, ValueError, BuildError):
            return error

    @app.errorhandler(404)
    def not_found(error):
        if error.description == UNKNOWN_TV:
            return error_page(error, title="We can't find that TV", message="It may have been removed.", action="home")
        return error_page(error, title="We can't find that page", message="It may have moved, or it was removed.",
                          action="home")

    @app.errorhandler(400)
    def bad_request(error):
        if error.description == STALE_SESSION:
            # A form posts to its own route; reload the page the form belongs to.
            page = PAGE_OF.get(request.endpoint)
            try:
                url = request.url if request.method == "GET" else url_for(page) if page else url_for("dashboard", tv=None)
            except BuildError:
                url = url_for("dashboard", tv=None)
            return error_page(error, title="This page was open too long", message="Reload it and try again.",
                              action="reload", reload_url=url)
        message = error.description if error.description and not error.description.startswith("The browser") else ""
        return error_page(error, message=(message.rstrip(".") + ".") if message else "Check the address and try again.")

    def saved(fn, name):
        """Run a change and return its result text and category."""
        try:
            fn()
            return "Saved.", "success"
        except ValidationError as error:
            messages = [
                ".".join(map(str, item["loc"])) + ": " + item["msg"]
                for item in error.errors(include_input=False, include_url=False)
            ]
            return " · ".join(messages), "error"
        except Busy:
            return "Another update is running. Try again in a moment.", "info"
        except CardError as error:
            return str(error), "error"
        except (ValueError, OSError) as error:
            app.logger.warning("%s failed: %s", name, failure(error))
            return "That action could not be completed. Check the image, source, or TV settings and try again.", "error"
        except Exception as error:
            app.logger.warning("%s failed: %s", name, failure(error))
            return "That action could not be completed. Your existing library is unchanged.", "error"

    def operation(fn, destination="gallery"):
        flash(*saved(fn, request.endpoint))
        return redirect(url_for(destination))

    def queue(label, key, run, endpoint="tv_overview", *, place="notice", anchor=None, values=None, **options):
        """Queue a TV command and return to the page at once. The result shows when the job is done.

        `values` are the page's URL values; the TV's Now page opens on its "On the TV" tab by default.
        """
        screen = g.screen
        values = ({"view": "tv"} if endpoint == "tv_overview" else {}) if values is None else values
        job = Job(screen.id, screen.configs.load().tv_name, label, (screen.id, *key), run=run,
                  controller=screen.controller, place=place, **options)
        try:
            job, added = jobs.submit(job)
        except QueueFull as error:
            if place in PANELS:
                return display_note(str(error), "error", place)
            flash(str(error), "error")
            return redirect(url_for(endpoint, **values))
        if not job.finished:
            # "queued" marks the note, so the page script removes it when the result arrives.
            flash(("Queued: " if added else "Already queued: ") + label,
                  f"{place}:info queued" if place in PANELS else "info queued")
        return redirect(url_for(endpoint, job=job.id, _anchor=anchor, **values))

    health_state = {"failed": ""}

    @app.get("/health")
    def health():
        # No configuration, file names, network details or tokens in the body.
        endpoint = None if demo else env("RENDERER_SOCKET")
        failed = (
            "scheduler" if not scheduler_alive(displays)
            else "renderer" if endpoint and not renderer_ready(endpoint)
            else ""
        )
        if failed != health_state["failed"]:
            health_state["failed"] = failed
            if failed:
                app.logger.warning("Health check failed: %s unavailable", failed)
            else:
                app.logger.info("Health check recovered")
        if failed:
            return {"status": "unavailable"}, 503
        return {"status": "ok"}

    def hold_view(status, now, available):
        """The hold banner's words, or None without a hold: {"app", "until"}."""
        hold = status.hold
        if hold is None:
            return None
        shown = apps.find(hold.app, available)
        return {"app": shown.name if shown else hold.app,
                "until": "until you resume" if hold.until is None else "until " + when(hold.until, now)}

    def setup_keys(available):
        """{app id: the key of its first empty private setting}, for each layout that needs setup."""
        return {chosen.id: missing[0].key for chosen in available if (missing := missing_secrets(chosen, root))}

    def show_menu(screen, cfg, now, available):
        """The "Show now" menu of one TV: its layouts and how long a hold lasts.

        None without installed layouts, or for a TV that is not paired (outside demo mode).
        """
        if cfg.site.path or not available or not demo and not screen.library.state("tv_status", {}).get("paired"):
            return None
        plan = screen.controller.plan(cfg, now, available)
        until = rotation.hold_until_next_change(plan, now)
        nearest = plan.next_change is not None and until == plan.next_change
        hours = int(rotation.MAX_HOLD.total_seconds() // 3600)
        return {"tv": screen.id, "apps": available,
                # Layouts with an empty private setting cannot show anything yet: the menu links to their settings.
                "setup": setup_keys(available),
                "next": f"Until {when(until, now)} (next change)" if nearest
                        else f"For {hours} hours (until {when(until, now)})"}

    def tile(screen_id, available=None):
        """One home-screen tile from stored state only. Never contacts the TV."""
        screen = displays.get(screen_id)
        cfg, state = screen.configs.load(), screen.library.state
        now = datetime.now(ZoneInfo(cfg.timezone))
        available = apps.usable(cfg) if available is None else available
        checked, upload = state("tv_status_at"), state("last_upload") or {}
        sent = upload.get("device") == cfg.tv.identity and (screen.configs.root / "tv-upload.jpg").exists()
        badges = ["No mats"] if cfg.mats == "never" else []
        # The badges of the layout this TV shows now; layouts that take turns show each badge once.
        for shown in screen.controller.plan(cfg, now, available).apps:
            values = site.display_settings(cfg, screen.library, shown) or {}
            badges += [item.badge for item in shown.manifest.display_settings
                       if item.badge and values.get(item.key) != item.default and item.badge not in badges]
        summary = now_summary(screen, available)
        return {
            "id": screen_id, "name": cfg.tv_name, "status": state("tv_status", {}),
            "checked": ago(checked, now) if checked else "",
            "stale": not rotation.stored_time(checked) or (now - rotation.stored_time(checked)).total_seconds() > 3600,
            "sent": ago(upload["at"], now) if sent else "", "sent_revision": upload.get("image_sha256") if sent else "",
            "wake": clock(wake_time(cfg, now.date())) if wake_time(cfg, now.date()) else "", "badges": badges,
            "hold": summary["hold"], "show": summary["show"], "now": summary,
        }

    def moved(endpoint, code=302):
        """Redirect an older page URL to its /tv/<tv>/... page, keeping the other query values."""
        values = request.args.to_dict(flat=False)
        values.pop("tv", None)
        return redirect(url_for(endpoint, **values), code)

    def setup_checklist(available):
        """Home's first-run checklist for the first TV, or None once dismissed or a day after it was done."""
        if library.state(CHECKLIST_DISMISSED):
            return None
        cfg, shared = configs.load(), displays.configs.load()
        week = cfg.app_schedule.week
        # Artwork shows without a choice. The step is done once the TV has a block, or a starter plan was picked
        # (the Artwork-only plan has no blocks).
        chose = bool(library.state(PLAN_CHOSEN)) or any(week.day(day).blocks for day in range(7))
        if library.state(CHECKLIST_SEEDED) is None:
            # The first Home visit after this release: a TV that was paired and scheduled before never sees it.
            library.set_state(CHECKLIST_SEEDED, True)
            # An older save that chose a default layout or a turn-on time counts as set up too.
            if setup_paired(cfg) and (chose or cfg.app_schedule.default is not None or week.has_wake()):
                library.set_state(CHECKLIST_DISMISSED, True)
                return None
        usable = apps.usable(cfg)
        # Only a layout that some TV's schedule uses needs its setup finished.
        waiting = next((item for item in usable if (usage := app_usage(item, usable))
                        and any(row["used"] for row in usage) and app_state(item, usage)["tone"] == "setup"), None)
        tv = cfg.tv_name
        steps = [
            {"label": f"Find {tv} on your network", "done": bool(cfg.tv.ip), "href": url_for("tv_setup")},
            {"label": f"Pair {tv}", "done": setup_paired(cfg), "href": url_for("tv_setup")},
            {"label": "Set your timezone", "done": shared.timezone != "UTC" or bool(library.state(TIMEZONE_OK)),
             "href": url_for("household", _anchor="timezone"), "timezone": True},
        ]
        if usable:
            steps.append({"label": f"Choose when layouts show on {tv}", "done": chose, "href": url_for("tv_plans")})
            steps.append({"label": f"Finish setting up {waiting.name}" if waiting else "Finish layout setup",
                          "done": waiting is None,
                          "href": url_for("app_page", app_id=waiting.id) if waiting else url_for("apps_index")})
        optional = [{"label": f"Turn {tv} on at a set time", "done": week.has_wake(),
                     "href": url_for("tv_schedule", _anchor="wake-form")}]
        done = all(step["done"] for step in steps)
        finished = library.state(CHECKLIST_DONE_AT)
        if finished:
            # Done once: it shows "Setup is done" for a day, and never comes back as "Set up your first TV".
            if not done or datetime.now(UTC) - datetime.fromisoformat(finished) > timedelta(days=1):
                return None
        elif done and not usable:
            return None  # nothing to choose yet: it comes back with the Choose step once a layout is installed
        elif done:
            library.set_state(CHECKLIST_DONE_AT, datetime.now(UTC).isoformat())
        return {"steps": steps, "optional": optional, "done": done,
                "count": sum(step["done"] for step in steps), "next": next((s for s in steps if not s["done"]), None)}

    @app.get("/")
    def dashboard():
        """Home: every TV at a glance. The older TV page URL (/?tv=...) moved to /tv/<tv>."""
        if "tv" in request.args or "view" in request.args:
            return moved("tv_overview")
        available = apps.installed()  # each tile leaves them out for a TV with a custom website folder
        return render_template("home.html", home=True, tiles=[tile(item["id"], available) for item in displays.items()],
                               checklist=None if demo else setup_checklist(available))

    @app.post("/home/checklist/dismiss")
    def dismiss_checklist():
        library.set_state(CHECKLIST_DISMISSED, True)
        flash("The setup list is hidden. TV settings and Household settings have everything it linked to.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/home/checklist/timezone")
    def checklist_timezone():
        """The checklist's "Use <browser zone>" and "Keep UTC" buttons. They return to Home."""
        zone = request.form.get("timezone", "").strip()
        if zone == "UTC":
            library.set_state(TIMEZONE_OK, True)
            return redirect(url_for("dashboard"))
        try:
            displays.configs.mutate(lambda raw: raw.update(timezone=timezones.normalize(zone)))
        except (ValidationError, ValueError):
            flash("Art Frames does not know that timezone. Choose it in Household settings.", "error")
            return redirect(url_for("household", _anchor="timezone"))
        flash(f"Timezone set to {timezones.normalize(zone)}.", "success")
        return redirect(url_for("dashboard"))

    def stamp(value):
        """A stored ISO time as "Nov 1, 7:05 AM" in this TV's timezone."""
        moment = rotation.stored_time(value)
        return moment.astimezone(ZoneInfo(configs.load().timezone)).strftime("%b %-d, %-I:%M %p") if moment else ""

    def now_line(cfg, now, available, screen=None):
        """What the TV shows now and what comes next, for the Now, Schedule and Home pages."""
        screen = g.screen if screen is None else screen
        available = apps.sources(cfg, available)  # Artwork too: it can be on screen or held
        plan = screen.controller.plan(cfg, now, available)
        status = screen.controller.rotation_status(now)
        current = apps.find(status.current_app, available) if status.current_app else None
        hold = status.hold
        # During a hold the next change is the hold's end, when the schedule's plan for that time goes on.
        # A hold until resume has no end, so the plan for now stands in for it.
        after = screen.controller.plan(cfg, hold.until.astimezone(now.tzinfo), available) if hold is not None and hold.until else plan
        if hold is not None and hold.until is None:
            following = "When you resume the schedule"
        elif hold is not None:
            names = names_text(item.name for item in after.apps) or "The schedule"
            following = f"{names} {at(hold.until, now)} (after the hold)"
        elif plan.next_change:
            following = names_text(item.name for item in plan.next_apps) + " " + at(plan.next_change, now)
        else:
            following = ""
        return plan, status, {
            "text": schedule_text(plan), "rotate": plan.rotate, "apps": plan.apps,
            # The hold banner names the held layout; the Now line says what the schedule shows after it.
            "held": f"{schedule_text(after)}, after the hold." if hold else "",
            "showing": current.name if current and (plan.rotate or status.hold) else "",
            "missing": plan.missing, "fallback": plan.fallback,
            "next": following,
            "swaps": status.swaps,
            "status": status,  # a rotation.RotationStatus, for pages that show holds, pauses and layout health
        }

    def on_tv(screen, cfg, now, available, status):
        """The last picture Art Frames sent to this TV, and whether the TV shows it. Stored state only.

        "state" is "confirmed" (the TV confirmed it), "switched" (a rotation selected another layout's
        stored picture since), "changed" (Check TV found a picture Art Frames did not send) or
        "unconfirmed". "app" names what the TV shows; "sent_app" the layout of the sent picture.
        None when Art Frames has not sent a picture to this TV, or its record is unreadable.
        """
        state, device = screen.library.state, cfg.tv.identity
        upload = state("last_upload") or {}
        sent_at = rotation.stored_time(upload.get("at"))
        if upload.get("device") != device or sent_at is None or not (screen.configs.root / "tv-upload.jpg").exists():
            return None
        shown = state("last_display") or {}
        shown = shown if shown.get("device") == device else {}
        artwork = state("tv_artwork") or {}
        artwork = artwork if artwork.get("device") == device else {}
        made_by = apps.find(upload.get("app"), available)
        current = apps.find(status.current_app, available) if status and status.current_app else None
        name = made_by.name if made_by else ""
        result = {"revision": upload.get("image_sha256", ""), "app": name, "sent_app": name,
                  "sent": ago(sent_at, now), "state": "unconfirmed", "note": "Not confirmed by the TV yet.",
                  "tv_revision": "", "switched": "", "app_id": "", "app_revision": ""}

        def switched():
            result.update(state="switched", app=current.name if current else "",
                          note=f"The TV now shows {current.name if current else 'another layout'}'s stored picture.")
            if current is None:
                return
            # The swap time, from the confirmed selection that _swap records.
            if shown.get("app") == current.id and rotation.stored_time(shown.get("at")):
                result["switched"] = ago(shown["at"], now)
            # Art Frames' copy of that layout's stored image, when it has one that matches.
            copy = app_copy(screen.configs.root, current.id)
            stored = (rotation.Rotation(screen.library, device).images().get(current.id) or {}).get("image_sha256")
            if copy is not None and stored and copy_matches(copy, stored):
                result.update(app_id=current.id, app_revision=stored)
        checked = rotation.stored_time(artwork.get("checked_at"))
        shown_at = rotation.stored_time(shown.get("at"))
        if checked and checked >= sent_at and artwork.get("content_id") != upload.get("content_id"):
            if artwork.get("content_id") in state("owned_images:" + device, []):
                switched()
            else:
                result.update(state="changed", note="The TV shows something else. Someone may have used the remote.",
                              tv_revision=artwork.get("image_sha256", ""))
        elif made_by and current and current.id != made_by.id and status.rotate:
            # A rotation swap selects another layout's stored picture; the rotation record says which.
            switched()
        elif shown_at and shown_at >= sent_at:
            if shown.get("image_sha256") == upload.get("image_sha256"):
                result.update(state="confirmed", note="Confirmed by the TV.")
            else:
                switched()
        return result

    # Why the schedule does not switch layouts now (Rotation.status "paused"), in plain words.
    PAUSED = {
        "standby": "Paused while the TV is in standby",
        "screen-off": "Paused while the screen is off",
        "blanked": "Paused while the screen is blank",
        "watching": "Paused while someone watches TV",
        "unknown": "Paused until the TV answers",
        "wake": "Paused during the morning wake",
        "error": "Paused after a failed update. See Health.",
    }

    def room_dark(library):
        """Whether sensor control's room dark rule holds the blank (CONTROL "dark")."""
        return bool(library is not None and (library.state(sensor_control.CONTROL) or {}).get("dark"))

    def sensor_blanked(library):
        """Whether the sensor blanked the screen because no one is in the room (sensor_control.AWAY_ACTIONS)."""
        return bool(library is not None and (library.state(sensor_control.CONTROL) or {}).get("blanked"))

    def now_summary(screen, available=None):
        """What one TV shows, why, until when, what comes next, and whether it is healthy.

        The Now page and the Home tiles both use it. Stored state only: it never contacts the TV.
        """
        cfg = screen.configs.load()
        now = datetime.now(ZoneInfo(cfg.timezone))
        available = apps.usable(cfg) if available is None else available
        shown = apps.sources(cfg, available)  # the installed layouts and Artwork
        plan, status, line = now_line(cfg, now, shown, screen)
        if not plan.apps:
            why = "Custom website" if plan.custom_site else ""
        elif plan.key != "default":
            start, end = apps.stretch(cfg.app_schedule.week, now)
            why = "Schedule: " + (span_label(hhmm(start), hhmm(end)) if start is not None else "all day")
        else:
            why = NO_BLOCK
        switch = None
        if plan.rotate and not status.hold:
            if status.paused and status.paused != "hold":
                text = PAUSED.get(status.paused, "")
                if status.paused == "blanked" and sensor_blanked(screen.library):
                    text = "Paused: screen blank, room empty"
                switch = {"paused": text}
            elif status.next_switch and (plan.next_change is None or status.next_switch < plan.next_change):
                ids = [item.id for item in plan.apps]
                following = plan.apps[(ids.index(status.current_app) + 1) % len(ids)] if status.current_app in ids else plan.apps[0]
                switch = {"app": following.name, "at": status.next_switch.isoformat(), "time": when(status.next_switch, now)}
                if status.next_switch <= now:
                    # The swap is due or overdue: no timer, since each load would count down to a new "now".
                    switch = {"app": following.name, "soon": True}
        checks = checks_of(screen, cfg, now, available)
        headline, problems = health_headline(checks)
        # Artwork on screen as a plain frame: say why, so "Artwork" never reads as the day's piece.
        note = artwork_note(cfg, screen.library, now) if apps.ARTWORK in plan.apps else ""
        checked = screen.library.state("tv_status_at")
        return {
            "checked": ago(checked, now) if checked else "",
            "plan": plan, "status": status, "line": line, "why": why, "switch": switch,
            "on_tv": on_tv(screen, cfg, now, shown, status),
            "hold": hold_view(status, now, shown) if shown else None,
            "show": show_menu(screen, cfg, now, shown),
            "headline": headline, "problems": problems,
            "wake": wake_words(cfg.app_schedule.week, " · "),
            "artwork": note, "artwork_hint": ARTWORK_HINTS.get(note, ""),
        }

    @app.get("/tv/<tv>")
    def tv_overview(tv):
        """What the TV shows now, why, and what comes next. Never contacts the TV.

        ?preview=1 opens the Preview first dialog (the menu item's link without JavaScript).
        """
        cfg = configs.load()
        available = apps.usable(cfg)
        summary = now_summary(g.screen, available)
        preview = configs.root / "screen-preview.jpg"
        revision = hashlib.sha256(preview.read_bytes()).hexdigest() if preview.exists() else ""
        preview_app = apps.find(g.screen.controller.preview_app(revision), apps.sources(cfg, available)) if revision else None
        setup = setup_keys(available)
        # The dialog never picks a layout that needs setup for you.
        choice = next((item for item in (preview_app, summary["plan"].app) if item and item.id not in setup), None)
        artwork = library.state("tv_artwork", {})
        if artwork.get("device") != cfg.tv.identity:
            artwork = {}
        return render_template(
            "screen.html", now=summary, schedule=summary["line"], apps_supported=apps.supported(),
            status=library.state("tv_status", {}),
            preview_revision=revision, preview_name=preview_app.name if preview_app else "",
            preview_choice=choice.id if choice else "", setup_apps=setup,
            preview_open=request.args.get("preview") == "1", preview_apps=apps.sources(cfg, available),
            artwork=artwork, checked=stamp(artwork["checked_at"]) if artwork else "",
            room=room_view(g.screen, cfg),
            screen_status=now_status(g.screen, cfg, library.state("tv_status", {}), demo),
        )

    def checks_of(screen, cfg=None, now=None, available=None):
        """health_checks() of one TV, with this app's scheduler and demo state."""
        cfg = screen.configs.load() if cfg is None else cfg
        now = datetime.now(ZoneInfo(cfg.timezone)) if now is None else now
        available = apps.usable(cfg) if available is None else available
        hub = app.extensions["sensors"]
        control = screen.library.state(sensor_control.CONTROL) or {}
        wake = sensor_control.wake_check(screen.library, cfg, now.timestamp())
        rows = [sensor_health(hub.link(screen.id), cfg.sensor, now.timestamp()),
                sensor_wake_row(wake, cfg, control) if wake else None]
        if hub.alive is False:
            rows.append(sensor_control.hub_check(hub, any_sensor_enabled()))
        return health_checks(screen, cfg, now, available, alive=scheduler_alive(displays), demo=demo, settings_root=root,
                             sensor_rows=[row for row in rows if row])

    def any_sensor_enabled():
        """Whether any TV lets its sensor control it."""
        for item in displays.items():
            try:
                if displays.get(item["id"]).configs.load().sensor.enabled:
                    return True
            except ValueError:
                continue
        return False

    @app.get("/tv/<tv>/health")
    def tv_health(tv):
        """Is this TV OK, and what to fix: checks, suggested fixes, recent problems, then charts. Never contacts the TV."""
        cfg = configs.load()
        checks = checks_of(g.screen, cfg)
        headline, problems = health_headline(checks)
        return render_template(
            "health.html", checks=checks, headline=headline, problem_count=problems,
            fixes=analytics.findings(configs.root, cfg.timezone, display=analytics_web.display_record(library, cfg)),
            fixes_link=url_for("tv_picture"),
            **analytics_web.chart_context(g.screen, cfg, request.args.get("days", 7)),
        )

    @app.get("/tv/<tv>/display")
    def tv_display(tv):
        """The older Display page URL; the page is now Picture. The browser keeps the #anchor."""
        return moved("tv_picture", 301)

    def picture_page(edit=None, code=200):
        """Mats, Art Mode brightness, light sensor and options, and the screen tests, from the last stored reading."""
        display = analytics_web.display_record(library, configs.load())
        standby_test = library.state("standby_test", {})
        if standby_test.get("device") != configs.load().tv.identity:
            standby_test = {}
        controller = g.screen.controller  # stored state only: it never contacts the TV
        blank = controller.blank_record()
        blank_at = rotation.stored_time(blank.get("at")) if blank else None
        return render_template(
            "display.html",
            status=library.state("tv_status", {}),
            display=display, display_checked=stamp(display["checked_at"]) if display else "",
            standby_test=standby_test,
            standby_checked=stamp(standby_test["checked_at"]) if standby_test.get("checked_at") else "",
            blanked=blank is not None,
            blanked_since=blank_at.astimezone(ZoneInfo(configs.load().timezone)).strftime("%H:%M") if blank_at else "",
            brightness_range=brightness_range(display),
            sleep_choices=MOTION_TIMER_CHOICES, color_range=color_temperature_range(display),
            sensitivity_range=range(MOTION_SENSITIVITY_RANGE[0], MOTION_SENSITIVITY_RANGE[1] + 1),
            errors={edit["card"]: edit["error"]} if edit else {},
            mats=edit["values"]["mats"] if edit else configs.load().mats,
            **look_context(display, configs.load()),
        ), code

    def look_context(display, cfg):
        """The Look card, and what the sensor sets while sensor control is on. Stored state only."""
        sensor_on = cfg.sensor.enabled
        match = picture_looks.current(display, cfg.mats, sensor_on)
        looks = [{"key": look.key, "name": look.name, "line": picture_looks.line(look, sensor_on),
                  "checked": match is look,
                  "usable": picture_looks.usable(look, sensor_on)} for look in picture_looks.LOOKS]
        lux = None
        if sensor_on:
            link = app.extensions["sensors"].link(g.screen.id)
            lux, _ = sensor_control.light_now(link, cfg.sensor, datetime.now().timestamp())
        return {"looks": looks, "look_match": match, "sensor_on": sensor_on, "sensor_lux": lux,
                "sensor_hours_end": cfg.sensor.hours_end}

    @app.get("/tv/<tv>/picture")
    def tv_picture(tv):
        return picture_page()

    @app.post("/tv/<tv>/picture/mats")
    def save_mats(tv):
        """Mats on this TV: each artwork's own mat, or none. Saves only this setting."""
        values = {"mats": request.form.get("mats", "")}

        def change(raw):
            if values["mats"] not in {"show", "never"}:
                raise CardError("Choose one of the listed options.")
            raw["mats"] = values["mats"]
        return save_card(configs.mutate, picture_page, "mats", values, change, endpoint="tv_picture")

    @app.post("/tv/<tv>/picture/look")
    def apply_look(tv):
        """Apply a look in one job: the TV light sensor, brightness and color temperature through the
        Controller (each step checks power and Art Mode, and never wakes the TV), then the mats.

        With sensor control on, only color temperature and mats change. The job reads the settings again
        when it runs, so a look queued before sensor control turned on cannot set the brightness or turn the
        TV light sensor back on. If the first step finds the TV off or not in Art Mode, nothing changes, the
        mats included. If a later step fails, the note says what changed.
        """
        look = picture_looks.BY_KEY.get(request.form.get("look", ""))
        cfg = configs.load()
        if look is None or not picture_looks.usable(look, cfg.sensor.enabled):
            return display_note("Not done: choose one of the looks.", "info", "look")
        screen_configs, screen_library = g.screen.configs, g.screen.library

        def stopped(error):
            """Why a later step stopped, in art_mode_note's words."""
            if isinstance(error, Busy):
                return "another TV update is running."
            if isinstance(error, DisplayError):
                if error.code == "out-of-range":
                    return f"it takes a whole number from {error.low} to {error.high}."
                return REFUSALS.get(error.code, "it did not answer.")
            app.logger.warning("Apply look failed: %s", failure(error))
            return "it did not answer."

        def run(ctl):
            fresh = screen_configs.load()
            if not picture_looks.usable(look, fresh.sensor.enabled):
                return f"Not done: sensor control is on now, so {look.name} does not apply. Nothing changed.", \
                    "info", SKIPPED
            steps = picture_looks.steps(look, analytics_web.display_record(screen_library, fresh),
                                        fresh.sensor.enabled)
            done = []
            try:
                for field, value in steps:
                    if field == "light_sensor":
                        result = ctl.set_light_sensor(value)
                    elif field == "brightness":
                        result = ctl.set_brightness(value)
                        reported = sensor_control.as_level(result.reported)
                        sensor_control.accept_level(screen_library, value if reported is None else reported)
                    else:
                        result = ctl.set_option(field, value)
                    done.append((field, result))
            except Exception as error:
                if not done:
                    if isinstance(error, DisplayError) and error.code in {"not-art-mode", "asleep", "watching",
                                                                         "unknown"}:
                        return "Turn the TV on in Art Mode, then apply again. Nothing changed.", "info", SKIPPED
                    raise  # art_mode_note says why: nothing changed yet
                said = ", ".join(picture_looks.shown(field, result.reported) for field, result in done)
                return (f"Partly applied {look.name}. Read back from the TV: {said}. Then the TV stopped: "
                        f"{stopped(error)} The mats did not change."), "info"
            if look.mats is not None:
                screen_configs.mutate(lambda raw: raw.update(mats=look.mats))
            if not done:
                return f"Applied {look.name}. Mats saved.", "success"
            said = ", ".join(picture_looks.shown(field, result.reported) for field, result in done)
            if all(result.reported == result.requested for _, result in done):
                return f"Applied {look.name}. Read back from the TV: {said}.", "success"
            return f"Applied {look.name}, but the TV reports other values: {said}.", "info"
        return art_job(f"Apply {look.name}", ("look", look.key), run, lambda result: result, "look")

    @app.post("/screen/check-tv")
    def check_tv():
        check = g.screen.controller.check_artwork

        def run():
            try:
                result = check()
            except Exception as error:
                app.logger.warning("TV artwork check failed: %s", failure(error))
                result = "check-failed"
            return check_note(result)
        endpoint, values = back_page()
        return queue("Check TV artwork", ("check-tv",), run, endpoint, values=values, step_text="Reading the TV artwork")

    def check_note(result):
        messages = {
            "checked": "TV artwork checked.",
            "check-failed": "The TV could not provide its artwork. Art Frames keeps the last successful check.",
            "unpaired": "Pair this TV in TV settings first.",
            "unknown": "The TV could not be reached. Art Frames keeps the last successful check.",
            "asleep": "The TV is asleep. Art Frames keeps the last successful check.",
            "watching": "A TV programme is playing. Switch to Art Mode to check its artwork.",
            "busy": "Another TV update is running. Try checking again when it finishes.",
        }
        return (messages.get(result, "The TV artwork could not be checked."), "success" if result == "checked" else "info",
                SKIPPED if result in NOTHING_DONE else {})

    def display_note(text, category, place="display"):
        """Results show inside their panel, next to the controls (see display.html and _art_mode_options.html)."""
        flash(text, f"{place}:{category}")
        return redirect(url_for("tv_picture", _anchor=PANELS[place]))

    def art_job(label, key, action, describe, place="display"):
        """Queue an Art Mode action; its result shows in the panel of that place. Never wakes the TV."""
        ctl, name = g.screen.controller, request.endpoint
        return queue(label, key, lambda: art_mode_note(lambda: action(ctl), describe, name), "tv_picture",
                     place=place, anchor=PANELS[place])

    def art_mode_note(fn, describe, name):
        """Run an Art Mode action and return what the TV says.

        A refusal (the TV is not in Art Mode, not paired, a value out of range) is "Not done"
        and neutral. Only a real failure, such as no answer, is an error.
        """
        try:
            return describe(fn())
        except Busy:
            return "Not done: another TV update is running. Try again when it finishes.", "info", SKIPPED
        except DisplayError as error:
            if error.code in FAILURES:
                return FAILURES[error.code], "error"
            if error.code == "out-of-range":
                return f"Not done: choose a whole number from {error.low} to {error.high}.", "info", SKIPPED
            if error.code in REFUSALS:
                return "Not done: " + REFUSALS[error.code], "info", SKIPPED
            return NO_ANSWER, "error"
        except Exception as error:
            app.logger.warning("%s failed: %s", name, failure(error))
            return NO_ANSWER, "error"

    def readback(name, shown=str):
        """Describe a Readback: fixed text plus the requested and reported value.

        "Done" only when the TV reports the requested value, even if its reply to the request was lost.
        """
        def describe(result):
            requested, reported = result.requested, result.reported
            if reported is None or reported == "unknown":
                return f"Not done: the TV did not report its {name} setting afterward.", "info", SKIPPED
            if reported == requested:
                if not result.acknowledged:
                    return f"Done. The TV did not reply, but it now reports {shown(reported)}.", "success"
                return f"Done. {name[0].upper() + name[1:]} is now {shown(reported)}.", "success"
            if not result.acknowledged:
                return f"Not done: the TV did not confirm the request. It reports {shown(reported)}.", "info", SKIPPED
            return f"Not done: the TV accepted the request but still reports {shown(reported)}.", "info", SKIPPED
        return describe

    def sleep_word(value):
        return "Off" if value == "off" else f"{value} minutes"

    @app.post("/screen/display/refresh")
    def refresh_display():
        """Read the Art Mode settings. Both panels use this; "place" picks where the result shows."""
        place = request.form.get("place", "display")
        place = place if place in PANELS else "display"
        return art_job("Read Art Mode settings", ("display-settings",), lambda ctl: ctl.display_settings(),
                       lambda _: ("Done. The TV reported its Art Mode settings.", "success"), place)

    @app.post("/screen/display/brightness")
    def set_brightness():
        raw = request.form.get("brightness", "").strip()
        if not re.fullmatch(r"-?[0-9]{1,4}", raw):
            return display_note("Not done: enter brightness as a whole number.", "info")
        value = int(raw)
        screen_library = g.screen.library

        def run(ctl):
            result = ctl.set_brightness(value)
            # Sensor control takes this level as its own, so a brightness set here starts no pause.
            reported = sensor_control.as_level(result.reported)
            sensor_control.accept_level(screen_library, value if reported is None else reported)
            return result
        return art_job(f"Set brightness to {value}", ("brightness", value), run, readback("brightness"))

    @app.post("/screen/display/light-sensor")
    def set_light_sensor():
        choice = request.form.get("sensor", "")
        if choice not in {"on", "off"}:
            return display_note("Not done: choose On or Off.", "info", "options")
        return art_job(f"Turn the light sensor {choice}", ("light-sensor", choice), lambda ctl: ctl.set_light_sensor(choice == "on"),
                       readback("light sensor", str.capitalize), "options")

    # Art Mode options (_art_mode_options.html). Each change is a queued job that runs only in
    # Art Mode, reads the setting back and reports "Done" or "Not done: <reason>".

    @app.post("/screen/display/sleep-after")
    def set_sleep_after():
        choice = request.form.get("motion_timer", "")
        if choice not in MOTION_TIMER_CHOICES:
            return display_note("Not done: choose one of the listed times.", "info", "options")
        label = "Turn Sleep After off" if choice == "off" else f"Set Sleep After to {choice} minutes"
        return art_job(label, ("motion-timer", choice), lambda ctl: ctl.set_option("motion_timer", choice),
                       readback("Sleep After", sleep_word), "options")

    def whole(name):
        raw = request.form.get(name, "").strip()
        return int(raw) if re.fullmatch(r"-?[0-9]{1,2}", raw) else None

    @app.post("/screen/display/motion-sensitivity")
    def set_motion_sensitivity():
        value = whole("motion_sensitivity")
        if value is None:
            return display_note("Not done: enter motion sensitivity as a whole number.", "info", "options")
        return art_job(f"Set motion sensitivity to {value}", ("motion-sensitivity", value),
                       lambda ctl: ctl.set_option("motion_sensitivity", value), readback("motion sensitivity"), "options")

    @app.post("/screen/display/color-temperature")
    def set_color_temperature():
        value = whole("color_temperature")
        if value is None:
            return display_note("Not done: enter color temperature as a whole number.", "info", "options")
        return art_job(f"Set color temperature to {value}", ("color-temperature", value),
                       lambda ctl: ctl.set_option("color_temperature", value), readback("color temperature"), "options")

    def word(value):
        """Fixed display words for the probe's known values."""
        return {"on": "On", "off": "Off", "standby": "Standby"}.get(value, "Not reported")

    def screen_off_message(probe):
        if not probe["answers"]:
            return "Power-off key sent. The TV no longer answers on the network.", "success"
        return (f"Power-off key sent. The TV still answers. Power: {word(probe['power'])}. "
                f"Art Mode: {word(probe['art_mode'])}.", "success")

    def screen_check_message(probe):
        if not probe["answers"]:
            text = "The TV does not answer on the network."
        else:
            text = f"The TV answers on the network. Power: {word(probe['power'])}. Art Mode: {word(probe['art_mode'])}."
        if probe.get("since_off") is not None:
            text += f" {probe['since_off']} seconds since the power-off key."
        return text, "info"

    def screen_on_message(result):
        method, seconds = result.method, result.seconds
        if result.result == "confirmed":
            return f"The TV came back in {seconds} seconds using the {method}.", "success"
        if result.result == "tv-mode":
            return f"The TV came on in TV mode, not Art Mode, after {seconds} seconds using the {method}.", "info"
        if result.result == "watching":
            return "A TV programme is playing, so the wake signal was not sent.", "info", SKIPPED
        return f"Art Mode was not confirmed after {seconds} seconds using the {method}.", "info"

    def standby_load_message(result):
        """Plain sentences for the standby test: the art channel, the upload, the selection and the power state."""
        text = [f"Test done in {result['seconds']:.0f} seconds."]
        if not result["answered"]:
            text.append("The art channel did not answer, so nothing was uploaded.")
        elif not result["uploaded"]:
            text.append("The art channel answered, but the upload failed.")
        else:
            text.append("The art channel answered and the upload worked.")
            text.append({
                "sent": "The TV accepted the image as the next one to show.",
                "no-reply": "The TV did not confirm that it selected the image.",
                "skipped": "The TV left standby during the upload, so the image was not selected.",
            }.get(result["selected"], ""))
        if result["stayed"] is True:
            text.append("The TV stayed in standby.")
        elif result["stayed"] is False:
            text.append(f"The TV did not stay in standby. Power: {word(result['power_after'])}.")
        else:
            text.append("The TV did not report its power afterward.")
        worked = result["answered"] and result["uploaded"] and result["selected"] == "sent" and result["stayed"] is True
        return " ".join(part for part in text if part), "success" if worked else "info"

    @app.post("/screen/display/screen-off")
    def screen_off():
        return art_job("Try the power-off key", ("screen-off",), lambda ctl: ctl.screen_off(), screen_off_message, "power")

    @app.post("/screen/display/check-screen")
    def check_screen():
        return art_job("Read TV power", ("check-screen",), lambda ctl: ctl.check_screen(), screen_check_message, "power")

    @app.post("/screen/display/screen-on")
    def screen_on():
        return art_job("Turn screen on", ("screen-on",), lambda ctl: ctl.screen_on(), screen_on_message, "power")

    def blank_message(result):
        saved = (result.get("saved") or {}).get("brightness")
        readback = result["readback"]
        if not result["shown"]:
            return (f"The TV did not confirm the black image, so the brightness was not changed. "
                    f"Saved image and brightness {saved}. Use Undo blank to show them again."), "info"
        if readback.reported != readback.requested:
            return (f"Black image shown, but the TV reports brightness {word_or_number(readback.reported)}, "
                    f"not {readback.requested}. Saved image and brightness {saved}."), "info"
        return f"Screen blanked: black image at brightness {readback.reported}. Saved image and brightness {saved}.", "success"

    def undo_blank_message(result):
        readback, brightness = result["readback"], result["brightness"]
        if readback.reported != readback.requested:
            text = f"Blank undone, but the TV reports brightness {word_or_number(readback.reported)}, not {brightness}."
            category = "info"
        elif result["restored"]:
            text, category = f"Blank undone: image and brightness {brightness} restored.", "success"
        else:
            text, category = f"Blank undone: brightness {brightness} restored.", "success"
        if result["restored"] is False:
            text += " The TV did not show the saved image. The next update sends the current image."
        elif result["restored"] is None:
            text += " No image was saved. The next update sends the current image."
        if result.get("dark"):
            text += " The room is dark, so sensor control blanks the screen again."
        return text, category

    def word_or_number(value):
        return value if isinstance(value, int) else "nothing"

    @app.post("/screen/display/blank")
    def soft_blank():
        return art_job("Blank the screen", ("blank",), accepted(lambda ctl: ctl.soft_blank()), blank_message, "blank")

    @app.post("/screen/display/undo-blank")
    def undo_blank():
        def undo(ctl):
            result = ctl.undo_blank()
            if result.get("owner") == "sensor":
                # The sensor's blank: sensor control no longer holds it, and the empty-room count starts
                # again, so the dim and the blank follow their usual times.
                state = sensor_control.merge(ctl.library, sensor_control.CONTROL, blanked=False, empty_again=True)
                result = {**result, "dark": bool(state.get("dark"))}
            return result
        return art_job("Undo blank", ("undo-blank",), accepted(undo), undo_blank_message, "blank")

    def accepted(action):
        """Sensor control takes the blank's brightness as its own, like Set brightness, so it starts no pause."""
        screen_library = g.screen.library

        def run(ctl):
            result = action(ctl)
            readback = result["readback"]
            if readback is not None:  # None: the TV did not show the black image, so the brightness is unchanged
                reported = sensor_control.as_level(readback.reported)
                sensor_control.accept_level(screen_library, readback.requested if reported is None else reported)
            return result
        return run

    @app.post("/screen/display/standby-load")
    def standby_load():
        return art_job("Load the next image in standby (test)", ("standby-load",), lambda ctl: ctl.standby_load(),
                       standby_load_message, "standby")

    @app.get("/screen/tv-artwork.jpg")
    def tv_artwork_image():
        artwork = library.state("tv_artwork", {})
        path = configs.root / "tv-artwork.jpg"
        if artwork.get("device") != configs.load().tv.identity or not path.exists():
            abort(404)
        with path.open("rb") as handle:
            image = handle.read(site.MAX_ASSET + 1)
        if len(image) > site.MAX_ASSET:
            abort(413)
        digest = hashlib.sha256(image).hexdigest()
        if digest != artwork.get("image_sha256") or request.args.get("v") != digest:
            abort(409)
        return send_file(io.BytesIO(image), mimetype="image/jpeg")

    @app.get("/screen/sent.jpg")
    def sent_image():
        """The image Art Frames last sent to this TV: a small copy for Home, or ?size=large for the Now page.

        With ?app=<id>, the copy of the image this TV stores for that layout (shown after a rotation swap).
        """
        identity = configs.load().tv.identity
        if "app" in request.args:
            app_id = request.args["app"]
            path = app_copy(configs.root, app_id) if app_id == apps.ARTWORK_ID or apps.find(app_id) else None
            expected = (rotation.Rotation(library, identity).images().get(app_id) or {}).get("image_sha256")
            if path is None or not expected or not path.exists():
                abort(404)
        else:
            upload = library.state("last_upload") or {}
            path, expected = configs.root / "tv-upload.jpg", upload.get("image_sha256")
            if upload.get("device") != identity or not path.exists():
                abort(404)
        with path.open("rb") as handle:
            image = handle.read(site.MAX_ASSET + 1)
        if len(image) > site.MAX_ASSET:
            abort(413)
        digest = hashlib.sha256(image).hexdigest()
        if digest != expected or request.args.get("v") != digest:
            abort(409)
        if request.args.get("size") == "large":
            try:  # a broken file is 404 at both sizes
                with Image.open(io.BytesIO(image)) as picture:
                    picture.verify()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                abort(404)
            return send_file(io.BytesIO(image), mimetype="image/jpeg")
        try:
            with Image.open(io.BytesIO(image)) as picture:
                picture.draft("RGB", (640, 360))  # decode a 4K JPEG at reduced size
                small = picture.convert("RGB")
            small.thumbnail((640, 360), Image.Resampling.LANCZOS)
        except (OSError, ValueError, Image.DecompressionBombError):
            abort(404)
        output = io.BytesIO()
        small.save(output, "JPEG", quality=80)
        output.seek(0)
        return send_file(output, mimetype="image/jpeg")

    @app.post("/screen/preview")
    def preview_site():
        """Queue a render. A JSON caller gets the job ID and watches /activity.json for the result."""
        render = g.screen.controller.preview_site
        # A preview of one installed layout; without a choice, the layout this TV shows now.
        app_id = request.form.get("app", "")
        app_id = app_id if app_id in {item.id for item in apps.sources(configs.load())} else None
        # Only Health's "Test <app>" (back=health, or test=1) records a result; a routine preview records nothing.
        test = bool(app_id) and (request.form.get("back") == "health" or request.form.get("test") == "1")
        # A layout with an empty private setting can't make a picture: say what to add, and queue nothing.
        chosen = apps.find(app_id, apps.sources(configs.load())) if app_id else None
        missing = missing_secrets(chosen, root) if chosen else []
        if missing:
            text = f"Add the {setting_words(missing[0].label)} to make a preview."
            if request.accept_mimetypes.best == "application/json":
                return {"status": "failed", "message": text}, 409
            flash(text, "error")
            return redirect(url_for("app_page", app_id=app_id, _anchor=f"field-{missing[0].key}"))

        def run():
            try:
                image = render(app_id=app_id, test=test)
            except WebsitePending:
                return "Your website is preparing data. Try again in a moment.", "info", {"pending": True, **SKIPPED}
            except Busy:
                return "Another update is running. Try again in a moment.", "info", SKIPPED
            except Exception as error:
                app.logger.warning("Website preview failed: %s", failure(error))
                return "The preview could not be made. Check the layout and try again.", "error"
            return "Preview updated.", "success", {"revision": hashlib.sha256(image).hexdigest()}
        label, key = "Make a preview", ("preview", app_id, test)
        if chosen:
            label = f"Make a {chosen.name} preview"
        if request.accept_mimetypes.best == "application/json":
            screen = g.screen
            try:
                job, _ = jobs.submit(Job(screen.id, screen.configs.load().tv_name, label, (screen.id, *key), run=run,
                                         controller=screen.controller, step_text="Making the preview"))
            except QueueFull as error:
                return {"status": "failed", "message": str(error)}, 429
            if not job.finished:
                return {"status": "queued", "job": job.id}, 202
            if job.extra.get("pending"):
                return {"status": "pending"}, 202
            if "revision" in job.extra:
                revision = job.extra["revision"]
                return {"status": "ready", "image": url_for("screen_image", v=revision), "revision": revision}
            return {"status": "failed", "message": job.message}, 409 if job.category == "info" else 500
        if request.form.get("back"):
            # Health's "Test <app>" returns to Health; the result shows there when the job ends.
            endpoint, values = back_page()
            return queue(f"Test {chosen.name}" if chosen else label, key, run, endpoint,
                         values=values, step_text="Testing the layout")
        # Without JavaScript, the page opens again with the Preview first dialog open.
        return queue(label, key, run, values={"preview": "1"}, step_text="Making the preview")

    @app.get("/screen/preview.jpg")
    def screen_image():
        path = configs.root / "screen-preview.jpg"
        if not path.exists():
            abort(404)
        with path.open("rb") as handle:
            image = handle.read(site.MAX_ASSET + 1)
        if len(image) > site.MAX_ASSET:
            abort(413)
        revision = request.args.get("v", "")
        if len(revision) == 64 and all(c in "0123456789abcdef" for c in revision) and revision != hashlib.sha256(image).hexdigest():
            abort(409, "The preview changed. Reload this page.")
        return send_file(io.BytesIO(image), mimetype="image/jpeg")

    @app.get("/screen/preview-data")
    def preview_data():
        if request.args.get("mode", "current") != "current" or demo:
            abort(400)
        cfg = configs.load()
        now = datetime.now(ZoneInfo(cfg.timezone))
        shown = g.screen.controller.plan(cfg, now).app
        extras = site.snapshot(apps.configure(cfg, shown), library, now, app=shown, display=g.screen.id,
                               purpose="preview")
        data = json.loads(extras[names.SNAPSHOT_PATH][1])
        # Only the already selected artwork is exported, never config/feed URLs or TV tokens.
        if art := data.get("art"):
            for key in ("image",):
                if key in art:
                    content_type, body = extras[art[key]]
                    art[key] = "data:" + content_type + ";base64," + base64.b64encode(body).decode("ascii")
        return data

    @app.get("/gallery")
    def gallery():
        return render_template("gallery.html", artworks=library.all())

    @app.post("/upload")
    def upload():
        def run():
            image = request.files.get("image")
            if not image:
                raise ValueError("Choose an image")
            library.add(
                image.read(), title=request.form.get("title", ""), artist=request.form.get("artist", "")
            )

        return operation(run)

    @app.get("/artwork/<art_id>/<variant>.jpg")
    def artwork_image(art_id, variant):
        try:
            library.get(art_id)
            path = library.path_for(art_id, variant)
        except (ValueError, FileNotFoundError):
            abort(404)
        return send_file(path, mimetype="image/jpeg")

    @app.post("/artwork/<art_id>")
    def artwork_settings(art_id):
        def save():
            library.update(
                art_id,
                selected="selected" in request.form,
                fit=request.form.get("fit", "contain"),
                x=float(request.form.get("x", ".5")),
                y=float(request.form.get("y", ".5")),
                zoom=float(request.form["zoom"]) if "zoom" in request.form else None,
                mat_width=float(request.form["mat_width"]) if "mat_width" in request.form else None,
                mat_color=request.form.get("mat_color"),
            )
        if request.accept_mimetypes.best == "application/json":
            try:
                save()
            except (ValueError, OSError) as error:
                app.logger.info("Artwork framing was rejected: %s", failure(error))
                return {"status": "failed", "message": "Your changes could not be saved. Try again."}, 400
            except Exception as error:
                app.logger.warning("Artwork framing could not be saved: %s", failure(error))
                return {"status": "failed", "message": "Your changes could not be saved. Try again."}, 500
            return {"status": "saved"}
        return operation(save)

    @app.get("/discover")
    def discover():
        cfg = configs.load()
        source = request.args.get("source", next(iter(cfg.art.sources), "met"))
        query = request.args.get("q", "").strip()[:120]
        artist = request.args.get("artist") == "1"
        try:
            page = max(1, min(100, int(request.args.get("page", "1"))))
        except ValueError:
            abort(400)
        results = []
        has_more = False
        unavailable = ""
        if source not in cfg.art.sources:
            abort(400, "That museum is not enabled")
        if query:
            try:
                found = sources.search(source, query, page, artist=artist)
                results, has_more = found.items, found.has_more
            except FetchError as error:
                unavailable = (
                    "This museum is blocking automated access right now."
                    if error.status == 403
                    else "This museum could not be reached right now."
                )
            except Exception as error:
                app.logger.warning("Museum search failed: %s", failure(error))
                unavailable = "This museum could not be reached right now."
        return render_template(
            "discover.html",
            results=results,
            source=source,
            query=query,
            page=page,
            artist=artist,
            unavailable=unavailable,
            collection_url=sources.COLLECTIONS[source],
            more_collections=any(key not in cfg.art.sources for key in sources.PROVIDERS),
            has_more=has_more,
        )

    @app.post("/import")
    def import_image():
        def run():
            source = request.form.get("source")
            if source not in configs.load().art.sources:
                raise ValueError("Museum is not enabled")
            sources.import_artwork(library, source, request.form.get("source_id", ""))

        return operation(run)

    def settings_app(app_id, available=None):
        """The installed layout whose page this request is for. 404 for another ID, or for a TV with a custom website."""
        chosen = apps.find(app_id, available)
        if configs.load().site.path or chosen is None:
            abort(404)
        return chosen

    @app.get("/site/settings")
    def site_settings():
        """The older layout settings URL (/site/settings?app=<id>, or the first layout's without ?app=)."""
        available = apps.installed()
        chosen = apps.find(request.args.get("app"), available) if request.args.get("app") else (available[0] if available else None)
        if chosen is None:
            abort(404)
        # Without ?app= the target is the first layout, which changes when layouts change: not a permanent move.
        return redirect(url_for("app_page", app_id=chosen.id), 301 if request.args.get("app") else 302)

    # Shown under the settings card when its form was drawn for an older version of the layout.
    APP_STALE = ("This layout changed after the page opened. Here are its latest settings. "
                 "Make your change again if you still need it.")

    def app_usage(chosen, available=None):
        """Every TV, whether its schedule shows the layout and where (its blocks' words),
        its values for the layout's per-TV options, and whether the layout's last refresh failed there."""
        available = apps.installed() if available is None else available
        rows = []
        for item in displays.items():
            screen = displays.get(item["id"])
            cfg = screen.configs.load()
            used = apps.uses(cfg, chosen, available)
            places = block_places(cfg.app_schedule.week, chosen.id) if used else []
            health = rotation.Rotation(screen.library, cfg.tv.identity).health()
            failed, fine = failing_apps(health, [chosen]) if used else ([], [])
            rows.append({"id": item["id"], "name": item["name"], "used": used, "places": places,
                         "custom_site": bool(cfg.site.path), "failed": failed[0][1] if failed else None,
                         "refreshed": bool(fine),
                         "values": site.display_settings(cfg, screen.library, chosen) or {}})
        return rows

    def app_state(chosen, usage, missing=None):
        """A layout's state for the Layouts list and its page, with the words that explain it.

        "tone" is "setup" (a private setting is empty), "failing" (its last refresh failed on a TV that
        shows it), "unused" (no TV shows it), "waiting" (no TV has refreshed it yet) or "ok".
        "missing_keys" names the empty settings.
        """
        missing = missing_secrets(chosen, root) if missing is None else missing
        failing = [row for row in usage if row["failed"]]
        used = [row for row in usage if row["used"]]
        state = {"missing_keys": [item.key for item in missing], "text": "", "failing": failing}
        if missing:
            return {**state, "tone": "setup", "word": "Needs setup", "text": setup_text(chosen.name, missing)}
        if failing:
            names = names_text(row["name"] for row in failing)
            error = (failing[0]["failed"].last_error or "").rstrip(".")
            return {**state, "tone": "failing", "word": "Not refreshing",
                    "text": f"{chosen.name} could not refresh on {names}." + (f" {error}." if error else "")}
        if not used:
            return {**state, "tone": "unused", "word": "Not on a TV", "text": "Use it on a TV from its page."}
        if not any(row["refreshed"] for row in used):
            return {**state, "tone": "waiting", "word": "Not refreshed yet"}
        return {**state, "tone": "ok", "word": "Working"}

    @app.route("/apps", methods=["GET", "POST"], defaults={"rest": ""})
    @app.route("/apps/<path:rest>", methods=["GET", "POST"])
    def old_layouts_url(rest):
        """The older /apps URLs. Layouts now live under /layouts. A 308 keeps the method and the form,
        so an open page's POST still saves. Sign-in and CSRF checks run before this, as on every page."""
        target = "/layouts" + (f"/{quote(rest)}" if rest else "")
        if request.query_string:
            target += "?" + request.query_string.decode("ascii", "replace")
        return redirect(target, 308)

    @app.get("/layouts")
    def apps_index():
        """Every installed layout: whether it needs setup or fails, the TVs that show it, and where it came from."""
        cfg = configs.load()
        if cfg.site.path:
            abort(404)
        available = apps.installed()
        rows = []
        for chosen in available:
            usage = app_usage(chosen, available)
            rows.append({"app": chosen, "state": app_state(chosen, usage), "source": app_source(chosen),
                         "on": [row for row in usage if row["used"]]})
        return render_template("apps.html", rows=rows)

    def app_page_view(chosen, edit=None, code=200):
        """One layout's page. `edit` is {"card", "values", "error"}: the posted values and one plain sentence."""
        available = apps.installed()
        store = chosen.store(root)
        manifest = store.manifest()
        public, configured = store.public_values(manifest)
        usage = app_usage(chosen, available)
        errors = {}
        if edit:
            errors[edit["card"]] = edit["error"]
            if edit["card"] == "settings":
                public = {**public, **edit["values"]}
        preview = configs.root / "screen-preview.jpg"
        revision = hashlib.sha256(preview.read_bytes()).hexdigest() if preview.exists() else ""
        if revision and g.screen.controller.preview_app(revision) != chosen.id:
            revision = ""  # the saved preview shows another layout
        cfg = configs.load()
        here = next(row for row in usage if row["id"] == g.screen.id)
        refresh = displays.configs.load().site.refresh_minutes
        presets = []
        for preset in manifest.presets:
            choice = preset_choice(preset, public)
            needs = preset.options[choice].refresh_minutes if choice is not None else None
            presets.append({"preset": preset, "choice": choice, "refresh": refresh,
                            "needs": needs if needs and refresh > needs else None})
        return render_template(
            "site-settings.html", presets=presets,
            manifest=manifest, manifest_token=store.fingerprint(manifest),
            values=public, configured=configured, app_id=chosen.id, errors=errors,
            usage=usage, state=app_state(chosen, usage), source=app_source(chosen),
            here=here, preview_revision=revision, options_token=options_fingerprint(chosen.manifest),
            can_show=bool(demo or library.state("tv_status", {}).get("paired")) and not cfg.site.path,
            many_apps=len(available) > 1,
        ), code

    @app.get("/layouts/<app_id>")
    def app_page(app_id):
        """One installed layout's page: its settings, shared by every TV, the TVs that show it, its options on
        each TV and a preview on this TV."""
        return app_page_view(settings_app(app_id))

    def posted_settings(manifest, form):
        """The posted values of the settings card, to draw it again after an error. Private values never return."""
        values = {}
        for item in manifest.settings:
            if item.type == "checkbox":
                values[item.key] = item.key in form
            elif item.type not in ("secret", "list") and item.key in form:
                values[item.key] = form[item.key][:4096]
        return values

    @app.post("/layouts/<app_id>")
    def save_app(app_id):
        if not configs.load().site.path and apps.find(app_id) is None:
            # An open page of a layout that was removed since: say so instead of a bare 404.
            flash("This layout is no longer installed.", "error")
            return redirect(url_for("apps_index"))
        chosen = settings_app(app_id)
        store = chosen.store(root)
        manifest = store.manifest()
        if manifest is None or request.form.get("manifest") != store.fingerprint(manifest):
            flash(APP_STALE, "card-settings:error")
            return redirect(url_for("app_page", app_id=chosen.id, _anchor="settings"))
        form = apply_presets(manifest, request.form)

        def change(form):
            if any(key.startswith("clear_") and form.get(key[6:]) for key in form):
                raise CardError("Choose Replace or Remove, not both.")
            try:
                store.update(form)
            except ValueError as error:
                if str(error).startswith("Site changed"):
                    raise CardError(APP_STALE) from error
                if "size limit" in str(error):
                    raise CardError("These settings are too long to save. Shorten a list or a text field.") from error
                if "settings item" in str(error):
                    raise CardError("A list on this page changed. Reload the page, then make your change again.") from error
                raise
        def message():
            if not any(row["used"] for row in app_usage(chosen, apps.installed())):
                return "Saved."
            minutes = displays.configs.load().site.refresh_minutes
            return f"Saved. TVs that show {chosen.name} change at their next update, within {duration(minutes * 60)}."
        return save_card(change, lambda edit, code: app_page_view(chosen, edit, code), "settings",
                         posted_settings(manifest, form), form, endpoint="app_page", url_values={"app_id": chosen.id},
                         message=message)

    @app.post("/layouts/<app_id>/refresh")
    def app_refresh(app_id):
        """Make the household refresh as short as the layout's preset needs, such as a photo every 5 minutes."""
        chosen = settings_app(app_id)
        raw_value = request.form.get("minutes", "")
        minutes = int(raw_value) if re.fullmatch(r"[0-9]{1,4}", raw_value) else 0  # ASCII digits only
        if minutes not in REFRESH_CHOICES:
            abort(400)
        displays.configs.mutate(lambda raw: raw["site"].update(refresh_minutes=minutes))
        flash(refresh_saved(minutes), "card-settings:success")
        return redirect(url_for("app_page", app_id=chosen.id, _anchor="settings"))

    @app.post("/site/settings")
    def save_site_settings():
        """The older form of a layout's settings: it posts to /site/settings?app=<id>."""
        available = apps.installed()
        app_id = request.args.get("app") or (available[0].id if available else "")
        chosen = settings_app(app_id, available)
        flash(*saved(lambda: chosen.store(root).update(request.form), request.endpoint))
        return redirect(url_for("app_page", app_id=chosen.id))

    @app.post("/site/tv-settings")
    def save_app_tv_settings():
        """The layout's options on each TV. Any TV may save them, also one whose schedule does not show the layout yet."""
        available = apps.usable(configs.load())
        if not available:
            abort(404)
        # The form names its layout by manifest ID; the first layout's form always did.
        chosen = apps.find(request.form.get("manifest", ""), available)
        if chosen is None:
            flash("The installed layout changed. Reload the page.", "error")
            return redirect(url_for("tv_overview"))
        known = {item["id"] for item in displays.items()}
        posted = request.form.getlist("tvs")

        def save(form):
            if form.get("options") != options_fingerprint(chosen.manifest):
                raise StaleForm("on-each-tv", OPTIONS_STALE)
            if any(tv not in known for tv in posted):
                raise CardError("A TV on this page was removed. Reload the page, then make your change again.")
            scopes = {item.scope for item in available}
            for tv in dict.fromkeys(posted):
                values = {item.key: f"tv.{tv}.{item.key}" in form for item in chosen.manifest.display_settings}

                def update(raw, values=values):
                    # Keep only installed layouts' values, so old layout scopes don't pile up.
                    kept = {scope: stored for scope, stored in raw["app_settings"].items() if scope in scopes}
                    raw["app_settings"] = {**kept, chosen.scope: values}
                displays.get(tv).configs.mutate(update)
        return save_card(save, lambda edit, code: app_page_view(chosen, edit, code), "on-each-tv", {},
                         request.form, endpoint="app_page", anchor="on-each-tv", url_values={"app_id": chosen.id})

    def delivery_note(fn, name, library=None):
        """Run a send, refresh or wake and return its result text and category."""
        try:
            result = fn()
        except WakeError as error:
            messages = {
                "unpaired": "Pair this TV in TV settings before waking it.",
                "missing-mac": "Add this TV’s MAC address in TV settings to use the wake signal.",
                "network": "The server could not send the network wake packet. Check its network connection.",
                "state-unknown": "Wake packets were sent, but the TV still has not confirmed its viewing state. Check IP Remote and Power On with Mobile on the TV, then try again.",
                "not-confirmed": "Wake packets were sent, but the TV did not enter Art Mode. Check Power On with Mobile and the TV’s MAC address in TV settings.",
            }
            return messages.get(str(error), "The wake request could not be confirmed. Check the TV’s network settings."), "error"
        except WebsitePending:
            return "Your website is preparing data. The scheduled update will retry automatically.", "info"
        except Exception as error:
            app.logger.warning("%s failed: %s", name, failure(error))
            return "The TV update failed. Check that the TV is reachable and in Art Mode.", "error"
        return delivery_text(result, library)

    def delivery_text(result, library=None):
        if result == "room-empty" or result == "blanked" and sensor_blanked(library):
            # Sensor control's blank, not a test: no Undo blank hint.
            if room_dark(library):
                return ("The room is dark, so the screen stays blank. It comes back when the light is on and someone "
                        "is there."), "info", SKIPPED
            return "The room is empty, so the screen is blank. Art Frames did not send the image.", "info", SKIPPED
        messages = {
                "published": "The TV confirmed this artwork is selected.",
                # A forced update or a preview send skips bytes the TV confirms it already shows.
                "same-image": "The TV already shows this exact image, so Art Frames did not upload it again.",
                "selected": "The TV now shows the layout's stored image. Nothing new was made or uploaded.",
                "unconfirmed": "The image was uploaded, but the TV did not confirm switching to it. Check TV to see its reported artwork.",
                "unpaired": "Pair your TV in TV settings before sending an image.",
                "unknown": "Could not confirm the TV’s viewing state. No image was sent. If the screen is asleep, use Wake TV, then show the preview again.",
                "watching": "A TV programme is playing. Switch to Art Mode before sending an image.",
                "asleep": "The TV is asleep. Use Wake TV, then show the preview again.",
                "blanked": "The screen is blanked for a test. Use Undo blank in Picture, Tests. No image was sent.",
                "busy": "Another update is running. Try again when it finishes.",
                "changed": "The TV left Art Mode during the update. Its current viewing was preserved.",
                "no-preview": "Make a preview first. Nothing was sent.",
                "preview-changed": "The preview changed. Reload this page, then show it again.",
            }
        return (messages.get(result, "The TV did not confirm an update."), "success" if result in {"published", "same-image", "selected"} else "info",
                SKIPPED if result in NOTHING_DONE or result in UNCONFIRMED else {})

    def display_operation(label, key, action, endpoint="tv_overview", **options):
        ctl, name = g.screen.controller, request.endpoint
        return queue(label, key, lambda: delivery_note(lambda: action(ctl), name, ctl.library), endpoint, **options)

    def pass_note(kind, result, ctl, failed):
        """A scheduler pass in the activity record: only a wake, a delivery or a new failure."""
        if failed:
            return ERRORS.get((ctl._reported or ("status",))[0], ERRORS["status"]) + ".", "error"
        if kind == "wake" and result == "woke":
            text = ctl.library.state("wake_result") or "Wake was attempted"
            if text.startswith("Wake confirmed"):
                return text + ".", "success"
            # The TV reached Art Mode late: done. Attempted but not confirmed, or skipped: not done.
            return text + ".", "info", {} if text.startswith("Wake not confirmed") else SKIPPED
        if kind == "update" and result in {"published", "unconfirmed", "selected"}:
            text, *rest = delivery_text(result)
            # With several layouts installed, say which layout rendered (swaps are only counted; see swap_if_due).
            if ctl.last_app and len(apps.installed()) > 1:
                text = f"{ctl.last_app}: {text}"
            return text, *rest
        return None
    jobs.describe = pass_note

    @app.post("/refresh")
    def refresh():
        endpoint, values = back_page()
        return display_operation("Update the TV now", ("refresh",), lambda ctl: ctl.tick(force=True), staged=True,
                                 endpoint=endpoint, values=values)

    @app.post("/screen/send-preview")
    def send_preview():
        revision = request.form.get("preview_revision", "")
        previewed = apps.find(g.screen.controller.preview_app(revision) or "")
        if previewed and (refused := needs_setup(previewed)):
            return refused
        return display_operation("Show preview now", ("send-preview", revision), lambda ctl: ctl.send_preview(revision), staged=True)

    @app.post("/screen/show-app")
    def show_app():
        """Render one layout for this TV now, send it, then hold it on screen.

        The form names the layout ("app") and how long to hold it ("hold"): "next-change" (until
        the schedule changes next, at most 4 hours without a change), "hour" or "resumed".
        """
        cfg = configs.load()
        now = datetime.now(ZoneInfo(cfg.timezone))
        chosen = apps.find(request.form.get("app", ""), apps.sources(cfg))
        endpoint, values = back_page()
        if chosen is None:
            flash("Choose an installed layout.", "error")
            return redirect(url_for(endpoint, **values))
        if refused := needs_setup(chosen):
            return refused
        hold = request.form.get("hold", "next-change")
        until = {"hour": now + timedelta(hours=1), "resumed": None}.get(
            hold, rotation.hold_until_next_change(g.screen.controller.plan(cfg, now), now))
        return display_operation(f"Show {chosen.name} now", ("show-app", chosen.id, hold),
                                 lambda ctl: ctl.show_app(chosen.id, until), staged=True,
                                 endpoint=endpoint, values=values)

    def needs_setup(chosen):
        """For a layout with an empty private setting: a redirect to that setting, and nothing is queued.
        None when the layout is set up."""
        missing = missing_secrets(chosen, root)
        if not missing:
            return None
        flash(f"Set up {chosen.name} first.", "error")
        return redirect(url_for("app_page", app_id=chosen.id, _anchor=f"field-{missing[0].key}"))

    def back_page():
        """Where a form of a TV command returns, as (endpoint, URL values): Home, Schedule, Health, a layout's
        page ("app:<id>", an installed layout) or, by default, the TV's Now page."""
        back = request.form.get("back", "")
        if back == "home":
            return "dashboard", {"tv": None}
        if back == "schedule":
            return "tv_schedule", {}
        if back == "health":
            return "tv_health", {}
        kind, _, app_id = back.partition(":")
        # A TV with a custom website has no layout pages (they are 404 there): go to Now instead.
        if kind == "app" and apps.find(app_id) is not None and not configs.load().site.path:
            return "app_page", {"app_id": app_id}
        return "tv_overview", {"view": "tv"}

    def back_url():
        endpoint, values = back_page()
        return url_for(endpoint, **values)

    @app.post("/screen/resume")
    def resume_schedule():
        """End a hold. The next scheduler pass shows what the schedule says. Never contacts the TV."""
        g.screen.controller.resume()
        flash(f"The schedule decides what {configs.load().tv_name} shows again.", "success")
        return redirect(back_url())

    @app.get("/settings")
    def settings():
        """The older TV settings URL; the page moved to /tv/<tv>/settings."""
        return moved("tv_settings")

    # Per-card saves on the Schedule, Picture, TV settings and Household settings pages. Each card is a form
    # that posts to its own route and to its own #anchor, so the browser returns to the card after an error.

    def own_site(screen_id):
        """A TV's own website settings, or None when it uses the household's. The main TV has none."""
        if screen_id == "main":
            return None
        return next(item for item in displays.configs.load().displays if item.id == screen_id).site


    def tv_settings_page(edit=None, code=200):
        """TV settings: name, connection, pairing and address recovery. `edit` shows posted values again."""
        cfg = configs.load()
        search = tv_recovery.summary(library, cfg)
        if search and search["at"] is not None:
            moment = datetime.fromtimestamp(search["at"], ZoneInfo(cfg.timezone))
            day = "" if moment.date() == datetime.now(moment.tzinfo).date() else moment.strftime("%b %-d, ")
            search["time"] = day + clock(moment.strftime("%H:%M"))
        recovery = g.screen.controller.recovery
        values = edit["values"] if edit else {
            "tv_name": cfg.tv_name, "tv_ip": cfg.tv.ip, "tv_mac": cfg.tv.mac, "broadcast": cfg.tv.broadcast}
        # Old links to the sections that moved: app.js sends #app-schedule and #wake to Schedule,
        # #mats to Picture, and #website, #artwork and #museum-sources to Household settings.
        moves = {"app-schedule": url_for("tv_schedule"), "wake": url_for("tv_schedule", _anchor="wake-form"),
                 "mats": url_for("tv_picture", _anchor="mats"),
                 **{anchor: url_for("household", _anchor=anchor) for anchor in ("website", "artwork", "museum-sources")}}
        return render_template(
            "configure.html",
            values=values, hash_moves=moves,
            name_error=edit["error"] if edit and edit["card"] == "name" else "",
            connection_error=edit["error"] if edit and edit["card"] == "connection" else "",
            tv_network=suggested_network(cfg.tv.ip),
            address_search=search,
            address_searching=bool(recovery and recovery.searching()),
            status=library.state("tv_status", {}),
        ), code

    @app.get("/tv/<tv>/settings")
    def tv_settings(tv):
        return tv_settings_page()

    @app.post("/tv/<tv>/settings")
    def save_tv_settings(tv):
        """Save the TV's name (the Name card) or its address (the Address fold). A form saves only the fields
        it posts; the others keep their saved values. An error shows under its card, with the typed values."""
        form = request.form
        saved = configs.load()
        card = "connection" if "tv_ip" in form else "name"
        kept = {"tv_name": saved.tv_name, "tv_ip": saved.tv.ip, "tv_mac": saved.tv.mac, "broadcast": saved.tv.broadcast}
        values = {key: form[key].strip() if key in form else kept[key] for key in kept}
        values["broadcast"] = values["broadcast"] or "255.255.255.255"

        def change(raw):
            if card == "name":
                # The Name card changes only the name. The address is read under the lock and never
                # rewritten from this request's earlier load, so a recovered address stays.
                raw["tv_name"] = values["tv_name"]
                return
            ip = values["tv_ip"]
            # A page opened before address recovery posts the old address back unchanged: keep the new one.
            shown = form.get("tv_ip_shown")
            if shown is not None and ip == shown.strip() and raw["tv"]["ip"] != ip:
                ip = raw["tv"]["ip"]
            raw["tv_name"] = values["tv_name"]
            raw["tv"].update(ip=ip, mac=values["tv_mac"], broadcast=values["broadcast"])

        # A name save keeps the address-recovery proposal: it changes nothing about the address.
        return save_card(configs.mutate, tv_settings_page, card, values, change, endpoint="tv_settings",
                         done=clear_recovery if card == "connection" else None)

    def clear_recovery():
        """A saved address ends an address-recovery proposal (TV settings and the setup's Find step)."""
        recovery = g.screen.controller.recovery
        if recovery is not None:
            recovery.clear_proposal(configs.load())  # under the recovery's state lock
        else:
            tv_recovery.clear_proposal(library, configs.load())

    # The Schedule page: a day timeline. Tabs: "every" (every day's plan) and mon..sun (a day's own plan).
    # Each change saves on its own, through a normal POST, and the page opens again at the timeline.
    # Editors (the add, block, wake and take-turns forms) are hidden parts of the page that
    # open by their #id (CSS :target), so they work without JS. app.js drags blocks and fills the same forms.

    def schedule_apps():
        """The installed layouts a schedule can use. 404 without one: there is nothing to place."""
        available = apps.usable(configs.load())
        if not available:
            abort(404)
        return available

    def day_tab(day):
        """The tab of a URL's day: "every" or mon..sun. 404 for anything else."""
        day = day or "every"
        if day != "every" and day not in DAY_KEYS:
            abort(404)
        return day

    def schedule_page(edit=None, code=200):
        """The Schedule page. `edit` is {"card", "values", "error"} after a failed save."""
        cfg = configs.load()
        available = apps.usable(cfg)
        now = datetime.now(ZoneInfo(cfg.timezone))
        schedule = cfg.app_schedule
        week = schedule.week
        today = now.weekday()
        asked = (request.view_args or {}).get("day") or request.args.get("day") or request.form.get("day")
        # Without a day in the address, the page opens on today's tab when today has its own plan.
        tab = day_tab(asked or (DAY_KEYS[today] if week.custom(today) else "every"))
        weekday = None if tab == "every" else DAY_KEYS.index(tab)
        custom = weekday is not None and week.custom(weekday)
        plan_day = week.default if weekday is None else week.day(weekday)
        # A turn-on time needs no layout. Blocks need an installed layout and no custom website.
        wake_editable = weekday is None or custom
        editable = bool(available) and not cfg.site.path and wake_editable
        # The now line shows on today's tab, and on Every day when today follows it.
        shows_now = weekday == today or (weekday is None and not week.custom(today))
        weekend = weekday in (5, 6) and not custom
        cards = {"turns": {"swap": week.swap_seconds},
                 "wake-form": {"at": plan_day.wake or "07:00"},
                 "customize": {"days": [5, 6] if weekend else [] if weekday is None else [weekday]},
                 "copy-plan": {"days": []}}
        for item in available:
            cards[f"add-{item.id}"] = {"app": item.id, "start": "07:00", "end": "09:00"}
        for index, block in enumerate(plan_day.blocks):
            cards[f"block-{index}"] = {"app": block.app, "start": block.start, "end": block.end}
        errors = {}
        if edit:
            cards[edit["card"]] = edit["values"]
            errors[edit["card"]] = edit["error"]
        tabs = [{"key": "every", "label": "Every day", "custom": False}] + [
            {"key": key, "label": DAY_NAMES[index], "custom": week.custom(index)} for index, key in enumerate(DAY_KEYS)]
        plan, status, line = now_line(cfg, now, available) if available else (None, None, None)
        shown = apps.sources(cfg, available)
        return render_template(
            "schedule.html", cards=cards, errors=errors, now_line=line, tab=tab, tabs=tabs, custom=custom,
            day_name="every day" if weekday is None else DAY_FULL_NAMES[weekday], editable=editable,
            wake_editable=wake_editable,
            timeline=day_timeline(plan_day, available) if available and not cfg.site.path
            else {"lanes": [], "strip": []}, wake=plan_day.wake,
            now_minute=now.hour * 60 + now.minute if shows_now else None, swap_seconds=week.swap_seconds,
            hold=hold_view(status, now, shown) if status else None,
            schedule_version=schedule_version(schedule.model_dump()),
            plan_use=plan_use(week, weekday), weekday=weekday, turns_note=schedule.turns_note,
            # The turns line shows only where layouts take turns: blocks that overlap.
            show_turns=shares_time(week),
            own_days=[day for day in range(7) if week.custom(day)],
            artwork=artwork_note(cfg, library, now),
            apps_supported=apps.supported(), own_site=own_site(g.screen.id) is not None,
            custom_site=bool(cfg.site.path),
        ), code

    @app.get("/tv/<tv>/schedule")
    def tv_schedule(tv):
        return schedule_page()

    def save_schedule(card, values, change, *, day="every", message="Saved.", needs_apps=True):
        """Save one change of the schedule. change(saved schedule dict, apps) returns the new schedule dict.
        A change of blocks or layouts needs an installed layout (else 404). A turn-on time or a day's plan does not."""
        available = schedule_apps() if needs_apps else apps.usable(configs.load())

        def update(raw):
            raw["app_schedule"] = parse_schedule(change(raw["app_schedule"], available)).model_dump()
        url = {"day": day}  # also "every": without a day, the page may open on today's tab
        return save_card(configs.mutate, schedule_page, card, values, update, endpoint="tv_schedule",
                         anchor="timeline", message=message, url_values=url)

    def same_version(saved):
        """The form was drawn for this saved schedule. Else StaleForm: the note shows above the timeline."""
        # The saved file may leave out defaults, so compare the parsed schedule, as the page draws it.
        if request.form.get("schedule_version", "") != schedule_version(parse_schedule(saved).model_dump()):
            raise StaleForm("timeline")

    def day_of(saved, day):
        """The saved dict of a tab's plan, for a change. A day without its own plan cannot change (StaleForm)."""
        week = saved.setdefault("week", {})
        if day == "every":
            return week.setdefault("default", {"blocks": [], "wake": None})
        plan = (week.get("days") or {}).get(day)
        if plan is None:
            raise StaleForm("timeline", f"{DAY_FULL_NAMES[DAY_KEYS.index(day)]} uses every day's plan. "
                                        "Customize it first.")
        return plan

    @app.post("/tv/<tv>/schedule/turns")
    def save_turns(tv):
        raw = request.form.get("swap", "")
        values = {"swap": int(raw) if re.fullmatch(r"[0-9]{1,5}", raw) else 0}

        def change(saved, available):
            same_version(saved)
            saved["week"]["swap_seconds"] = values["swap"]
            return saved
        return save_schedule("turns", values, change, day=day_tab(request.form.get("day")))

    @app.post("/tv/<tv>/schedule/<day>/blocks")
    def add_block(tv, day):
        day = day_tab(day)
        values = block_values(request.form)
        # The error shows in the layout's Add form. A layout that is not installed has none: it shows on the timeline.
        chosen = apps.find(values["app"], apps.usable(configs.load()))
        card = f"add-{chosen.id}" if chosen else "timeline"

        def change(saved, available):
            same_version(saved)
            if not apps.find(values["app"], available):
                raise ScheduleError("Choose an installed layout.")
            check_block(values)
            blocks = day_of(saved, day)["blocks"]
            if len(blocks) >= MAX_BLOCKS:
                raise CardError(f"This day has {len(blocks)} blocks. Use at most {MAX_BLOCKS}. Remove a block first.")
            blocks.append(dict(values))
            return saved
        return save_schedule(card, values, change, day=day,
                             message=lambda: f"Added {chosen.name}, {span_text(values['start'], values['end'])}.")

    @app.post("/tv/<tv>/schedule/<day>/blocks/<int:index>")
    def save_block(tv, day, index):
        day = day_tab(day)
        values = block_values(request.form)

        def change(saved, available):
            same_version(saved)
            check_block(values)
            blocks = day_of(saved, day)["blocks"]
            if index >= len(blocks):
                raise StaleForm("timeline")
            # The editor's layout select can move the block to another installed layout. A form without a layout keeps it.
            app_id = blocks[index]["app"]
            if values["app"]:
                chosen = apps.find(values["app"], available)
                if not chosen:
                    raise ScheduleError("Choose an installed layout.")
                app_id = chosen.id
            blocks[index] = {"app": app_id, "start": values["start"], "end": values["end"]}
            values["app"] = app_id
            return saved
        return save_schedule(f"block-{index}", values, change, day=day,
                             message=lambda: f"Saved: {first_upper(span_text(values['start'], values['end']))}.")

    @app.post("/tv/<tv>/schedule/<day>/blocks/<int:index>/delete")
    def delete_block(tv, day, index):
        day = day_tab(day)

        def change(saved, available):
            same_version(saved)
            blocks = day_of(saved, day)["blocks"]
            if index >= len(blocks):
                raise StaleForm("timeline")
            blocks.pop(index)
            return saved
        return save_schedule("timeline", None, change, day=day, message="Removed the block.")

    @app.post("/tv/<tv>/schedule/<day>/wake")
    def save_wake(tv, day):
        """The day's wake time: turn the TV on in Art Mode at a set time. It never contacts the TV."""
        day = day_tab(day)
        values = {"at": request.form.get("at", "")}

        def change(saved, available):
            same_version(saved)
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", values["at"]):
                raise CardError("Pick a turn-on time.")
            day_of(saved, day)["wake"] = values["at"]
            return saved
        return save_schedule("wake-form", values, change, day=day, needs_apps=False,
                             message=lambda: f"{configs.load().tv_name} turns on at {clock(values['at'])}.")

    @app.post("/tv/<tv>/schedule/<day>/wake/delete")
    def delete_wake(tv, day):
        day = day_tab(day)

        def change(saved, available):
            same_version(saved)
            day_of(saved, day)["wake"] = None
            return saved
        return save_schedule("timeline", None, change, day=day, needs_apps=False, message="Removed the turn-on time.")

    @app.post("/tv/<tv>/schedule/<day>/customize")
    def customize_day(tv, day):
        """Give the picked days their own plan: a copy of every day's plan each. Days that already have one keep it.
        A form without day toggles picks only `day`."""
        if day_tab(day) == "every":
            abort(404)
        picked = form_days(request.form, "days") if "days_shown" in request.form else [DAY_KEYS.index(day)]
        values = {"days": picked}

        def change(saved, available):
            same_version(saved)
            if not picked:
                raise CardError("Pick at least one day.")
            week = parse_schedule(saved).week
            for weekday in picked:
                if not week.custom(weekday):
                    saved["week"]["days"][DAY_KEYS[weekday]] = week.default.model_dump()
            return saved
        # The page opens on the started day, or on the first picked day when the started day is not picked.
        shown = day if not picked or DAY_KEYS.index(day) in picked else DAY_KEYS[picked[0]]
        owners = DAY_FULL_NAMES[picked[0]] if len(picked) == 1 else day_names(picked)
        return save_schedule("customize", values, change, day=shown, needs_apps=False,
                             message=f"{owners} {'has its' if len(picked) == 1 else 'have their'} own plan now. "
                                     "It starts as a copy of every day's plan.")

    @app.post("/tv/<tv>/schedule/<day>/copy")
    def copy_day(tv, day):
        """Copy a day's own plan to other days. Their own plans, if any, are replaced."""
        if day_tab(day) == "every":
            abort(404)
        values = {"days": [item for item in form_days(request.form, "days") if DAY_KEYS[item] != day]}

        def change(saved, available):
            same_version(saved)
            if not values["days"]:
                raise CardError("Pick at least one other day.")
            plan = day_of(saved, day)
            for weekday in values["days"]:
                saved["week"]["days"][DAY_KEYS[weekday]] = json.loads(json.dumps(plan))
            return saved
        return save_schedule("copy-plan", values, change, day=day, needs_apps=False,
                             message=lambda: f"Copied {DAY_FULL_NAMES[DAY_KEYS.index(day)]}'s plan to "
                                             f"{full_day_names(values['days'])}. Change each day on its own.")

    @app.post("/tv/<tv>/schedule/turns-note")
    def dismiss_turns_note(tv):
        """Hide the note about the one turn speed that older time rules now share."""
        configs.mutate(lambda raw: raw["app_schedule"].update(turns_note=False))
        return redirect(url_for("tv_schedule", day=day_tab(request.form.get("day")), _anchor="timeline"))

    @app.post("/tv/<tv>/schedule/<day>/reset")
    def reset_day(tv, day):
        """A day uses every day's plan again. Its own plan is removed."""
        if day_tab(day) == "every":
            abort(404)

        def change(saved, available):
            same_version(saved)
            saved["week"]["days"][day] = None
            return saved
        return save_schedule("timeline", None, change, day=day, needs_apps=False,
                             message=f"{DAY_FULL_NAMES[DAY_KEYS.index(day)]} uses every day's plan again.")

    # Starter plans (starters.py): a ready-made schedule for this TV. The page shows what changes first,
    # and Undo puts the old values back while the schedule has not changed since.

    def plan_day_words(day_plan, available):
        """One day's plan in words: "News 6:00 to 9:00 AM · turns on at 6:30 AM"."""
        if day_plan is None:
            return "Same as every day"
        parts = [f"{app_name(block['app'], available)} {span_label(block['start'], block['end'])}"
                 for block in day_plan["blocks"]] or ["Artwork all day"]
        if day_plan.get("wake"):
            parts.append(f"turns on at {clock(day_plan['wake'])}")
        return " · ".join(parts)

    def app_name(app_id, available):
        found = apps.find(app_id, available)
        return found.name if found else app_id

    def starter_preview(plan, day):
        """What the plan changes on this TV: {"lines": [{label, before, after}], "notes": [...], "after": dict}."""
        cfg = configs.load()
        shared = displays.configs.load()
        available = apps.usable(cfg)
        saved = cfg.app_schedule.model_dump()
        can_wake = bool(cfg.tv.ip and cfg.tv.mac)
        after = starters.schedule_after(plan, saved, day, can_wake)
        lines, notes = [], []

        def line(label, before, now):
            if before != now:
                lines.append({"label": label, "before": before, "after": now})
        days = [day] + ([key for key in DAY_KEYS] if plan.weekdays_only and day == "every" else [])
        for key in days:
            label = "Every day" if key == "every" else DAY_FULL_NAMES[DAY_KEYS.index(key)]
            source = (saved["week"]["default"] if key == "every" else saved["week"]["days"].get(key))
            target = (after["week"]["default"] if key == "every" else after["week"]["days"].get(key))
            line(label, plan_day_words(source, available), plan_day_words(target, available))
        wake_note = clock(plan.wake) if plan.wake and not can_wake else ""
        options, settings = starters.declared(plan, available)
        for app_id, values in options.items():
            chosen = apps.find(app_id, available)
            current = display_values(chosen.manifest, cfg.app_settings.get(chosen.scope, {}))
            labels = {item.key: item.label for item in chosen.manifest.display_settings}
            for key, value in values.items():
                line(f"{chosen.name} on this TV: {labels[key]}", "On" if current[key] else "Off", "On" if value else "Off")
        for app_id, values in settings.items():
            chosen = apps.find(app_id, available)
            store = chosen.store(root)
            public, _ = store.public_values(store.manifest())
            labels = {item.key: item.label for item in chosen.manifest.settings}
            for key, value in values.items():
                line(f"{chosen.name} on every TV: {labels[key]}", str(public.get(key, "")), str(value))
        if plan.art:
            line("Daily artwork on every TV", "On" if shared.art.enabled else "Off", "On")
        if plan.refresh and shared.site.refresh_minutes > plan.refresh:
            line("Refresh on every TV", duration(shared.site.refresh_minutes * 60), duration(plan.refresh * 60))
        album = None
        if plan.album and (photos := apps.find(starters.PHOTOS, available)):
            store = photos.store(root)
            manifest = store.manifest()
            if any(item.key == "album_url" for item in manifest.settings) and "album_url" not in store.public_values(manifest)[1]:
                album = next(item for item in manifest.settings if item.key == "album_url")
        return {"lines": lines, "notes": notes, "after": after, "options": options, "settings": settings,
                "album": album, "wake_note": wake_note}

    def starter_bar(plan):
        """The plan's day as bar parts for a small 24-hour picture: [{left, width, kind}] in percent."""
        parts = [{"left": 0, "width": 100, "kind": "rest"}]  # Artwork, where no block is
        for _, start, end in plan.blocks:
            begin, finish = minutes(start), minutes(end)
            parts.append({"left": round(begin / 14.4, 2), "width": round((finish - begin) / 14.4, 2), "kind": "block"})
        if plan.wake:
            parts.append({"left": round(minutes(plan.wake) / 14.4, 2), "width": 0, "kind": "wake"})
        return parts

    @app.get("/tv/<tv>/plans")
    def tv_plans(tv):
        cfg = configs.load()
        available = apps.usable(cfg)
        if not available:
            abort(404)
        day = day_tab(request.args.get("day"))
        offered = starters.offered(available, day)
        chosen = starters.find(request.args.get("plan", ""))
        if chosen is not None and chosen not in offered:
            abort(404)
        preview = starter_preview(chosen, day) if chosen else None
        return render_template(
            "plans.html", plans=[{"plan": plan, "bar": starter_bar(plan)} for plan in offered], chosen=chosen,
            preview=preview, day=day, day_name="every day" if day == "every" else DAY_FULL_NAMES[DAY_KEYS.index(day)],
            schedule_version=schedule_version(cfg.app_schedule.model_dump()), first_run=request.args.get("from") == "setup")

    class PlanStale(Exception):
        """The schedule changed after the plan page loaded."""

    def undo_fresh(undo, cfg):
        """Undo is offered for a day, and only while the schedule is still the one the plan wrote."""
        return bool(undo) and undo.get("version") == schedule_version(cfg.app_schedule.model_dump()) and (
            datetime.now(UTC).timestamp() - undo.get("at", 0) < UNDO_SECONDS)

    @app.post("/tv/<tv>/plans")
    def apply_plan(tv):
        cfg = configs.load()
        available = apps.usable(cfg)
        day = day_tab(request.form.get("day"))
        plan = starters.find(request.form.get("plan", ""))
        if not available or plan is None or plan not in starters.offered(available, day):
            abort(404)
        version = request.form.get("schedule_version")
        preview = starter_preview(plan, day)
        # The undo record keeps, for each value, what was there before and what the plan wrote. Undo puts a
        # value back only while it still holds what the plan wrote.
        undo = {"plan": plan.name, "day": day, "at": datetime.now(UTC).timestamp(), "options": {}, "settings": {},
                "shared": {}}
        album = request.form.get("album_url", "").strip()
        if album and preview["album"] is None:
            album = ""  # the link is saved already, or the plan does not ask for it

        def update(raw):
            # Check the version in the same write, so two tabs cannot both apply over each other.
            if version != schedule_version(parse_schedule(raw.get("app_schedule") or {}).model_dump()):
                raise PlanStale
            undo["schedule"] = parse_schedule(raw.get("app_schedule") or {}).model_dump()
            raw["app_schedule"] = parse_schedule(preview["after"]).model_dump()
            for app_id, values in preview["options"].items():
                chosen = apps.find(app_id, available)
                stored = display_values(chosen.manifest, raw["app_settings"].get(chosen.scope, {}))
                undo["options"][chosen.scope] = {key: {"before": stored[key], "after": value}
                                                 for key, value in values.items()}
                raw["app_settings"][chosen.scope] = {**raw["app_settings"].get(chosen.scope, {}), **values}
        try:
            configs.mutate(update)
        except PlanStale:
            flash(STALE, "card-timeline:error")
            return redirect(url_for("tv_plans", day=day, plan=plan.key))
        except (ValidationError, ValueError) as error:
            flash(plain(error) if isinstance(error, ValidationError) else "The plan could not be applied. Try again.",
                  "card-timeline:error")
            return redirect(url_for("tv_plans", day=day, plan=plan.key))
        undo["version"] = schedule_version(configs.load().app_schedule.model_dump())
        shared = displays.configs.load()
        changes = {}
        if plan.art and not shared.art.enabled:
            changes["art"] = True
            undo["shared"]["art"] = {"before": False, "after": True}
        if plan.refresh and shared.site.refresh_minutes > plan.refresh:
            changes["refresh"] = plan.refresh
            undo["shared"]["refresh"] = {"before": shared.site.refresh_minutes, "after": plan.refresh}
        stores = {app_id: apps.find(app_id, available).store(root) for app_id in preview["settings"]}
        for app_id, values in preview["settings"].items():
            before = stores[app_id].get_values(values)
            undo["settings"][app_id] = {key: {"before": before[key], "after": None} for key in before}
        # The undo record is saved before the layout and household writes, so a failed write can still be undone.
        library.set_state(STARTER_UNDO, undo)
        for app_id, values in preview["settings"].items():
            stores[app_id].set_values(values)
            for key, value in stores[app_id].get_values(values).items():
                undo["settings"][app_id][key]["after"] = value
            library.set_state(STARTER_UNDO, undo)
        if album:
            apps.find(starters.PHOTOS, available).store(root).set_values({"album_url": album})
        if changes:
            displays.configs.mutate(lambda raw: (
                raw["art"].update(enabled=True) if "art" in changes else None,
                raw["site"].update(refresh_minutes=changes["refresh"]) if "refresh" in changes else None))
        library.set_state(PLAN_CHOSEN, True)
        flash(f"Applied “{plan.name}”.", "card-timeline:success")
        return redirect(url_for("tv_schedule", day=day, _anchor="timeline"))

    @app.post("/tv/<tv>/plans/undo")
    def undo_plan(tv):
        undo = library.state(STARTER_UNDO)
        cfg = configs.load()

        def too_late():
            library.delete_state(STARTER_UNDO)
            flash("The schedule changed since, or the plan is more than a day old, so Art Frames can't undo it.",
                  "card-timeline:error")
            return redirect(url_for("tv_schedule", _anchor="timeline"))
        if not undo_fresh(undo, cfg):
            return too_late()
        available = apps.usable(cfg)
        kept = []  # the values changed since the plan: Undo leaves them

        def update(raw):
            # Check the version in the same write, so a schedule saved in another tab is never overwritten.
            if undo["version"] != schedule_version(parse_schedule(raw.get("app_schedule") or {}).model_dump()):
                raise PlanStale
            raw["app_schedule"] = undo["schedule"]
            for scope, values in undo["options"].items():
                current = raw["app_settings"].setdefault(scope, {})
                for key, change in values.items():
                    if current.get(key, change["after"]) == change["after"]:
                        current[key] = change["before"]
                    elif current.get(key) != change["before"]:
                        kept.append(key)
        try:
            configs.mutate(update)
        except PlanStale:
            return too_late()
        for app_id, values in undo["settings"].items():
            chosen = apps.find(app_id, available)
            if chosen is None:
                continue
            store = chosen.store(root)
            current = store.get_values(values)
            labels = {item.key: item.label for item in chosen.manifest.settings}
            restore = {key: change["before"] for key, change in values.items()
                       if change["after"] is not None and current.get(key) == change["after"]}
            kept += [f"{chosen.name} {labels.get(key, key)}" for key, change in values.items()
                     if key not in restore and current.get(key) != change["before"]]
            if restore:
                store.set_values(restore)
        if shared_undo := undo["shared"]:
            shared = displays.configs.load()
            now = {"art": shared.art.enabled, "refresh": shared.site.refresh_minutes}
            back = {key: change["before"] for key, change in shared_undo.items() if now[key] == change["after"]}
            kept += [{"art": "Daily artwork", "refresh": "Refresh"}[key] for key, change in shared_undo.items()
                     if key not in back and now[key] != change["before"]]
            if back:
                displays.configs.mutate(lambda raw: (
                    raw["art"].update(enabled=back["art"]) if "art" in back else None,
                    raw["site"].update(refresh_minutes=back["refresh"]) if "refresh" in back else None))
        library.delete_state(STARTER_UNDO)
        message = f"Undid “{undo['plan']}”."
        if kept:
            message += f" These changed since, so Art Frames kept them: {', '.join(kept)}."
        flash(message, "card-timeline:success")
        return redirect(url_for("tv_schedule", day=undo.get("day", "every"), _anchor="timeline"))

    def starter_offer(day):
        """For the Schedule page: the Undo of a plan just applied, and plans to offer on an empty day."""
        cfg = configs.load()
        undo = library.state(STARTER_UNDO)
        if not undo_fresh(undo, cfg):
            undo = None
        available = apps.usable(cfg)
        # After a plan, the Schedule keeps a quiet link to change it.
        return {"undo": undo, "plans": starters.offered(available, day)[:3] if available else [],
                "chosen": bool(available) and bool(library.state(PLAN_CHOSEN))}
    app.jinja_env.globals["starter_offer"] = starter_offer

    # Household settings: shared by every TV. They write the shared settings, not one TV's.

    def household_page(edit=None, code=200):
        shared = displays.configs.load()
        own = [{"id": item.id, "name": item.name, "refresh": item.site.refresh_minutes}
               for item in shared.displays if item.site is not None]
        installed = apps.usable(shared)
        cards = {
            "timezone": {"timezone": shared.timezone},
            "refresh": {"refresh": shared.site.refresh_minutes},
            "artwork": {"enabled": shared.art.enabled, "order": shared.art.order, "change_at": shared.art.change_at,
                        "sources": list(shared.art.sources), "supply": "art" in shared.site.providers},
            "website": {"path": shared.site.path, "entry": shared.site.entry,
                        "ready_selector": shared.site.ready_selector, "data": json.dumps(shared.site.data, indent=2)},
        }
        errors = {}
        if edit:
            if edit["values"] is not None:  # a card without fields, such as follow_shared, keeps the saved values
                cards[edit["card"]] = edit["values"]
            errors[edit["card"]] = edit["error"]
        return render_template("household.html", cards=cards, errors=errors, own_site=own, shared=shared,
                               household_apps=installed,
                               timezone_ok=bool(displays.get("main").library.state(TIMEZONE_OK))), code

    @app.get("/household")
    def household():
        return household_page()

    def save_household(card, values, change, message="Saved."):
        """Save one Household settings card into the shared settings."""
        return save_card(displays.configs.mutate, household_page, card, values, change, endpoint="household",
                         message=message)

    @app.post("/household/timezone")
    def save_timezone():
        values = {"timezone": request.form.get("timezone", "").strip()}
        # "pst" -> America/Los_Angeles; "EST" stays
        return save_household("timezone", values, lambda raw: raw.update(timezone=timezones.normalize(values["timezone"])),
                              message=lambda: f"Saved. Schedules use {displays.configs.load().timezone} now.")

    @app.post("/household/refresh")
    def save_refresh():
        raw_value = request.form.get("refresh", "").strip()
        values = {"refresh": int(raw_value) if re.fullmatch(r"[0-9]{1,4}", raw_value) else 0}
        preset = {key: minutes for key, _, minutes, _ in REFRESH_PRESETS}.get(request.form.get("preset", ""))
        if preset:  # Often, Normal or Calm; Custom uses the select
            values["refresh"] = preset
        return save_household("refresh", values, lambda raw: raw["site"].update(refresh_minutes=values["refresh"]),
                              message=refresh_saved(values["refresh"]))

    @app.post("/household/artwork")
    def save_artwork():
        form = request.form
        values = {"enabled": "enabled" in form, "order": form.get("order", "shuffle"),
                  "change_at": form.get("change_at", ""), "sources": form.getlist("sources"),
                  "supply": "supply" in form}

        def change(raw):
            raw["art"].update(enabled=values["enabled"], order=values["order"], change_at=values["change_at"],
                              sources=values["sources"])
            if "supply_shown" in form:  # only a website without installed layouts asks for artwork itself
                raw["site"]["providers"] = ["art"] if values["supply"] else []
        def message():
            art = displays.configs.load().art
            return f"Saved. The next artwork comes at {clock(art.change_at)}." if art.enabled else "Saved."
        return save_household("artwork", values, change, message=message)

    @app.post("/household/website")
    def save_website():
        form = request.form
        values = {key: form.get(key, "").strip() for key in ("path", "entry", "ready_selector")}
        values["data"] = form.get("data", "{}")
        locked = bool(apps.usable(displays.configs.load()))

        def change(raw):
            try:
                data = json.loads(values["data"] or "{}")
            except (ValueError, RecursionError):  # RecursionError: data nested too deep
                data = None
            if not isinstance(data, dict):
                raise CardError('Page data must be a JSON object, such as {"city": "Lisbon"}.')
            if not locked:  # an installed layout sets its own folder and entry page
                raw["site"].update(path=values["path"], entry=values["entry"] or "index.html")
            raw["site"].update(ready_selector=values["ready_selector"], data=data)
        return save_household("website", values, change)

    @app.post("/household/follow-shared")
    def follow_shared():
        """A TV with its own website settings uses the household's again. Making new per-TV settings is gone."""
        target = request.form.get("display", "")
        if target not in {item.id for item in displays.configs.load().displays}:
            flash("This TV no longer exists.", "card-website:error")
            return redirect(url_for("household", _anchor="website"))

        def change(raw):
            for item in raw["displays"]:
                if item["id"] == target:
                    item["site"] = None
        return save_household("website", None, change)

    def recovery_note(text, category):
        """Address recovery results show inside its card on the Settings page, next to Find this TV."""
        flash(text, f"recovery:{category}")
        return redirect(url_for("tv_settings", _anchor=NOTE_PLACES["recovery"]))

    @app.post("/settings/find-tv")
    def find_tv():
        """Start an address search for this TV. A later scheduler pass applies the result."""
        recovery = g.screen.controller.recovery
        if recovery is None:
            return recovery_note("Finding a TV is unavailable in demo mode.", "info")
        started = recovery.request(configs.load())
        return recovery_note(*{
            "started": ("Searching for this TV. Reload this page in a minute to see the result.", "info"),
            "searching": ("A search for this TV is already running.", "info"),
            "busy": ("Art Frames is searching for another TV. Try again in a minute.", "info"),
            "incomplete": ("Save the TV's IP and MAC address first.", "error"),
        }[started])

    @app.post("/settings/tv-address")
    def tv_address():
        """Accept or dismiss a proposed TV address. Only the stored proposal can be applied."""
        recovery, cfg = g.screen.controller.recovery, configs.load()
        if recovery is None:
            return redirect(url_for("tv_settings"))
        if request.form.get("action") == "keep":
            recovery.dismiss(cfg)
            return recovery_note("Kept the saved address.", "success")
        result = recovery.accept(cfg, request.form.get("ip", ""))
        if result == "updated":
            return recovery_note("Updated the TV address.", "success")
        if result == "stale":
            return recovery_note("That address is no longer proposed. Check the TV settings and try again.", "error")
        return recovery_note(tv_recovery.MESSAGES[result], "error")

    def add_tv_error(values):
        """Why a new TV can't be added, as one plain sentence, or "" when it can."""
        shared = displays.configs.load()
        tvs = [(shared.tv_name, shared.tv.ip), *((item.name, item.tv.ip) for item in shared.displays)]
        name, ip = values["name"], values["tv_ip"]
        if len(tvs) >= MAX_TVS:
            return "Art Frames supports up to eight TVs. Remove one to add another."
        if not name:
            return "Give this TV a name."
        if len(name) > 60:
            return FIELD_ERRORS["tv_name"]
        if any(other.casefold() == name.casefold() for other, _ in tvs):
            return f"Another TV is named {name}. Pick another name."
        owner = next((other for other, address in tvs if ip and address == ip), None)
        if owner:
            return f"{owner} already uses {ip}. Each TV needs its own address."
        if not ip:
            return "Add the TV's address. Find TVs fills it in."
        return ""

    # Guided setup of one TV: find it, name it, pair it, then choose what it shows (the Plans page).

    def setup_paired(cfg):
        """The TV is paired: its status says so, or the setup's Pair step worked for its current address."""
        return bool(library.state("tv_status", {}).get("paired")) or (
            bool(cfg.tv.ip) and library.state(SETUP_PAIRED) == cfg.tv.ip)

    def tv_setup_page(values=None, error="", code=200, change=False):
        cfg = configs.load()
        paired = setup_paired(cfg)
        step = "find" if not cfg.tv.ip or error or change else "pair" if not paired else "done"
        values = values or {"tv_name": cfg.tv_name, "tv_ip": cfg.tv.ip, "tv_mac": cfg.tv.mac}
        return render_template("setup.html", step=step, values=values, error=error,
                               tv_network=suggested_network(cfg.tv.ip), discovery_autostart=not cfg.tv.ip and not demo,
                               has_apps=bool(apps.usable(cfg))), code

    @app.get("/tv/<tv>/setup")
    def tv_setup(tv):
        # "Change the address" after a failed pair opens the Find step again.
        return tv_setup_page(change=request.args.get("step") == "find")

    @app.post("/tv/<tv>/setup")
    def save_tv_setup(tv):
        """Step 1: the TV's name and address, then the Pair step."""
        values = {key: request.form.get(key, "").strip() for key in ("tv_name", "tv_ip", "tv_mac")}
        if not values["tv_name"]:
            return tv_setup_page(values, "Give this TV a name.", 400)
        if not values["tv_ip"]:
            return tv_setup_page(values, "Choose your TV, or enter its address yourself.", 400)
        shared = displays.configs.load()
        others = [(shared.tv_name, shared.tv.ip, "main"), *((item.name, item.tv.ip, item.id) for item in shared.displays)]
        owner = next((name for name, ip, owner_id in others if ip == values["tv_ip"] and owner_id != g.screen.id), None)
        if owner:
            return tv_setup_page(values, f"{owner} already uses {values['tv_ip']}. Each TV needs its own address.", 400)

        def change(raw):
            raw["tv_name"] = values["tv_name"]
            raw["tv"].update(ip=values["tv_ip"], mac=values["tv_mac"])
        old_ip = configs.load().tv.ip
        try:
            configs.mutate(change)
        except ValidationError as error:
            return tv_setup_page(values, plain(error), 400)
        clear_recovery()
        if old_ip != values["tv_ip"]:  # a new address pairs again; the TV check sets it back if the old pairing works
            library.set_state("tv_status", {**library.state("tv_status", {}), "paired": False})
        return redirect(url_for("tv_setup"))

    @app.post("/tv/<tv>/setup/pair")
    def setup_pair(tv):
        """Step 2: pair, with the same command as TV settings. The page reloads when the job ends."""
        ctl, screen_library, cfg = g.screen.controller, g.screen.library, configs.load()
        if not cfg.tv.ip:
            return redirect(url_for("tv_setup"))
        name, address = cfg.tv_name, cfg.tv.ip

        def run():
            try:
                ctl.pair()
            except Busy:
                return "Another update is running. Try again in a moment.", "info"
            except Exception as error:
                app.logger.warning("pair failed: %s", failure(error))
                # The TV answered, but nobody selected Allow: Deny or a timed-out prompt raises a samsungtvws
                # ConnectionFailure (UnauthorizedError is one). television.pair says "Pairing was not completed"
                # when the TV closed without a token. A socket error or timeout means it did not answer.
                if isinstance(error, ConnectionFailure) or "Pairing was not completed" in str(error):
                    return (f"{name} answered, but Allow was not selected on the TV. Select Try again, "
                            "then select Allow on the TV within 30 seconds."), "error"
                return f"{name} did not answer. Check that it is on and that the address is right, then try again.", "error"
            screen_library.set_state(SETUP_PAIRED, address)
            screen_library.set_state("tv_status", {**screen_library.state("tv_status", {}), "paired": True})
            return f"{name} is paired.", "success"
        return queue("Pair TV", ("pair",), run, "tv_setup", place="setup", values={},
                     step_text="Waiting for you to select Allow on the TV")

    @app.route("/tvs/new", methods=["GET", "POST"])
    def add_tv():
        values = {key: request.form.get(key, "").strip() for key in ("name", "tv_ip", "tv_mac")}
        error = ""
        if request.method == "POST":
            error = add_tv_error(values)
            if not error:
                try:
                    display_id = displays.add(values["name"], values["tv_ip"], values["tv_mac"])
                except ValidationError as failed:
                    error = plain(failed)
                except ValueError as failed:
                    app.logger.warning("A TV could not be added: %s", failure(failed))
                    error = "The TV could not be added. Try again."
                else:
                    flash(f"{values['name']} added. Next, pair it.", "success")
                    return redirect(url_for("tv_setup", tv=display_id))
        return render_template("add_tv.html", tv_network=suggested_network(configs.load().tv.ip), values=values,
                               discovery_autostart=not demo and request.method == "GET",
                               error=error), 400 if error else 200

    @app.post("/tvs/discover")
    def discover_tvs():
        if demo:
            return {"error": "TV discovery is unavailable in demo mode."}, 400
        try:
            devices = app.extensions["tv_discovery"].scan(request.form.get("network", ""))
            return {"devices": devices}
        except DiscoveryBusy as error:
            return {"error": str(error)}, 429
        except ValueError as error:
            return {"error": str(error)}, 400

    @app.post("/settings")
    def save_settings():
        def change(raw):
            form = request.form
            raw["tv_name"] = form.get("tv_name", raw["tv_name"]).strip()
            raw["site"].update(
                path=form.get("site_path", "").strip(),
                entry=form.get("site_entry", "index.html").strip(),
                refresh_minutes=int(form.get("site_refresh", 15)),
                ready_selector=form.get("ready_selector", "").strip(),
                providers=json.loads(form["inherited_providers"]) if "inherited_providers" in form else form.getlist("providers"),
                data=json.loads(form.get("site_data", "{}")),
            )
            raw["timezone"] = timezones.normalize(form.get("timezone", "UTC"))  # "pst" -> America/Los_Angeles; "EST" stays
            raw["art"].update(
                enabled="art_enabled" in form,
                order=form.get("order", "shuffle"),
                change_at=form.get("change_at", "00:00"),
                sources=form.getlist("sources"),
            )
            ip = form.get("tv_ip", "").strip()
            # A page opened before address recovery posts the old address back unchanged: keep the new one.
            shown = form.get("tv_ip_shown")
            if shown is not None and ip == shown.strip() and raw["tv"]["ip"] != ip:
                ip = raw["tv"]["ip"]
            raw["tv"].update(
                ip=ip,
                mac=form.get("tv_mac", "").strip(),
                broadcast=form.get("broadcast", "255.255.255.255").strip(),
            )
            raw["mats"] = form.get("mats", raw["mats"])

        recovery = g.screen.controller.recovery

        def save():
            if g.screen.id == "main":
                configs.mutate(change)
            else:
                configs.mutate(change, inherit_site="shared_site" in request.form)
            if recovery is not None:
                recovery.clear_proposal(configs.load())  # under the recovery's state lock
            else:
                tv_recovery.clear_proposal(library, configs.load())
        return operation(save, "tv_settings")

    @app.post("/pair")
    def pair():
        ctl, name = g.screen.controller, request.endpoint
        if not configs.load().tv.ip:
            flash("Save the TV's address first.", "error")
            return redirect(url_for("tv_settings", _anchor="connection"))
        def run():
            text, category = saved(ctl.pair, name)
            return ("TV paired." if category == "success" else text), category
        return queue("Pair TV", ("pair",), run, "tv_settings",
                     step_text="Waiting for you to select Allow on the TV")

    @app.post("/wake")
    def wake_now():
        endpoint, values = back_page()
        return display_operation("Turn on now", ("wake",), lambda ctl: ctl.wake_screen(), endpoint, values=values,
                                 step_text="Turning the TV on")

    def activity_record():
        """The record with times in the TV's own timezone, already formatted for people."""
        record, zones = jobs.snapshot(), {}
        for entry in (entry for entries in record.values() for entry in entries):
            if entry["tv"] not in zones:
                try:
                    zones[entry["tv"]] = ZoneInfo(displays.get(entry["tv"]).configs.load().timezone)
                except ValueError:
                    zones[entry["tv"]] = None  # the TV was removed; keep the server's time
            stamp = entry["finished_at"] or entry["started_at"] or entry["queued_at"]
            moment = datetime.fromisoformat(stamp).astimezone(zones[entry["tv"]])
            seconds = entry["duration"]
            now = datetime.now(moment.tzinfo)
            entry.update(time=activity_time(moment, now), ago=ago(stamp, now),
                         took="" if seconds is None else f"{seconds:g} s" if seconds < 60 else f"{round(seconds / 60)} min")
        return record

    def activity_view(record):
        """The record ordered, filtered and grouped for people, from the page's query string."""
        args = request.args  # "only" and "show" are the names older links used
        return activity_lines(record, schedule=args.get("schedule") == "1",
                              filter_tv=args.get("filter_tv", args.get("only", "")),
                              outcome=args.get("outcome", args.get("show", "all")))

    @app.get("/activity.json")
    def activity_json():
        record = activity_record()
        if request.args.get("view") == "1":  # the drawer: the same view the page shows
            return {**record, "view": activity_view(record)}
        return record

    @app.get("/activity")
    def activity():
        return render_template("activity.html", activity=activity_view(activity_record()))

    @app.post("/activity/<job_id>/cancel")
    def cancel_job(job_id):
        job = jobs.get(job_id)
        if job is None or job.tv != g.screen.id:
            abort(404)
        cancelled = jobs.cancel(job_id)
        if request.accept_mimetypes.best == "application/json":
            if cancelled:
                return {"status": "cancelled"}
            return {"status": "started", "message": "That command already started, so it cannot be cancelled."}, 409
        flash("Cancelled." if cancelled else "That command already started, so it cannot be cancelled.", "success" if cancelled else "info")
        return redirect(url_for("activity"))

    analytics_web.register(app)
    return app


def seed_demo(library):
    if library.all():
        return
    from PIL import Image, ImageDraw

    for index, (title, colors) in enumerate(
        [
            ("Still water", ("#d7d2bf", "#7d9294", "#384f54")),
            ("Last light", ("#e4c8a5", "#bf8d6f", "#725b55")),
            ("A quiet landscape", ("#d0d5c8", "#909e7f", "#596953")),
        ]
    ):
        image = Image.new("RGB", (1600, 900), colors[0])
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 420, 1600, 900), fill=colors[1])
        draw.polygon(((0, 700), (500, 540), (1000, 700), (1600, 580), (1600, 900), (0, 900)), fill=colors[2])
        output = io.BytesIO()
        image.save(output, "JPEG")
        library.add(
            output.getvalue(),
            title=title,
            artist="Art Frames demo",
            source="demo",
            source_id=str(index),
            rights="Generated demo placeholder · not a museum artwork",
        )
