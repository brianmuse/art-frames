"""Timezone choices for the timezone picker, and the common aliases people type, such as pst or est.

The picker lists one current IANA name per region, from zone.tab in the tzdata package. So it leaves out
legacy names (EST5EDT, Etc/GMT+5, US/Eastern) and renamed links (Asia/Calcutta). Common zones come first:
the US and Canada, then Europe, then the rest.
"""

from datetime import UTC, datetime
from functools import cache, lru_cache
from importlib.resources import files
from typing import NamedTuple
from zoneinfo import ZoneInfo, available_timezones


class Common(NamedTuple):
    zone: str  # IANA name
    name: str
    abbreviations: tuple  # shown in the label and searched
    search_words: tuple  # searched only
    city: str


COMMON = (
    Common("America/New_York", "Eastern Time", ("EST", "EDT"), ("ET", "Eastern"), "New York"),
    Common("America/Chicago", "Central Time", ("CST", "CDT"), ("CT", "Central"), "Chicago"),
    Common("America/Denver", "Mountain Time", ("MST", "MDT"), ("MT", "Mountain"), "Denver"),
    Common("America/Phoenix", "Arizona Time", ("MST",), ("Arizona",), "Phoenix"),  # MST all year, no DST
    Common("America/Los_Angeles", "Pacific Time", ("PST", "PDT"), ("PT", "Pacific"), "Los Angeles"),
    Common("America/Anchorage", "Alaska Time", ("AKST", "AKDT"), ("Alaska",), "Anchorage"),
    Common("Pacific/Honolulu", "Hawaii Time", ("HST",), ("Hawaii",), "Honolulu"),
    Common("America/Halifax", "Atlantic Time", ("AST", "ADT"), ("Atlantic",), "Halifax"),
    Common("America/St_Johns", "Newfoundland Time", ("NST", "NDT"), ("Newfoundland",), "St. John's"),
    Common("Europe/London", "UK Time", ("GMT", "BST"), (), "London"),
    Common("Europe/Lisbon", "Western European Time", ("WET", "WEST"), (), "Lisbon"),
    Common("Europe/Berlin", "Central European Time", ("CET", "CEST"), (), "Berlin"),
    Common("Europe/Paris", "Central European Time", ("CET", "CEST"), (), "Paris"),
    Common("Europe/Athens", "Eastern European Time", ("EET", "EEST"), (), "Athens"),
    # IST is ambiguous: it also means Irish and Israel Standard Time. India is the common meaning.
    Common("Asia/Kolkata", "India Time", ("IST",), (), "Kolkata"),
    Common("Asia/Tokyo", "Japan Time", ("JST",), (), "Tokyo"),
    Common("Asia/Seoul", "Korea Time", ("KST",), (), "Seoul"),
    Common("Asia/Singapore", "Singapore Time", ("SGT",), (), "Singapore"),
    Common("Asia/Hong_Kong", "Hong Kong Time", ("HKT",), (), "Hong Kong"),
    Common("Australia/Sydney", "Australian Eastern Time", ("AEST", "AEDT"), (), "Sydney"),
    Common("Australia/Adelaide", "Australian Central Time", ("ACST", "ACDT"), (), "Adelaide"),
    Common("Australia/Perth", "Australian Western Time", ("AWST",), (), "Perth"),
    Common("Pacific/Auckland", "New Zealand Time", ("NZST", "NZDT"), (), "Auckland"),
    Common("UTC", "Coordinated Universal Time", (), ("UTC", "Z", "Zulu"), ""),
)

# A typed alias maps to the first row that lists it, except these. Python's MST is a fixed UTC−7 with no DST,
# the same as Phoenix, so a typed "mst" means Phoenix even though Denver shows MST in its label.
ALIAS_ZONES = {"mst": "America/Phoenix"}


@cache
def _known():
    """Every zone name ZoneInfo can load, in its exact case. The same on case-sensitive and case-insensitive disks."""
    return frozenset(available_timezones())


@cache
def zone_names():
    """Every current IANA zone name, plus UTC."""
    table = files("tzdata.zoneinfo").joinpath("zone.tab").read_text(encoding="utf-8")
    names = {line.split("\t")[2] for line in table.splitlines() if line and not line.startswith("#")}
    return sorted(names & _known()) + ["UTC"]


@cache
def _lookup():
    """Lowercase alias, zone name or IANA name -> IANA name."""
    found = dict(ALIAS_ZONES)
    for row in COMMON:
        for word in (*row.abbreviations, *row.search_words, row.name):
            found.setdefault(word.lower(), row.zone)
    for zone in zone_names():
        found.setdefault(zone.lower(), zone)
    return found


def normalize(value):
    """The value to store for a typed timezone.

    A name ZoneInfo knows in its exact case stays as entered, so a re-save never moves a stored zone such as
    GMT or EST. Anything else that matches an alias or zone name in any case ("pst", "america/new_york")
    becomes its IANA name. Other values come back unchanged, for the config validator to reject.
    """
    value = (value or "").strip()
    if value in _known():
        return value
    return _lookup().get(value.lower(), value)


def offset(zone, now=None):
    """The zone's UTC offset now, such as "UTC−5" or "UTC+5:30"."""
    delta = (now or datetime.now(UTC)).astimezone(ZoneInfo(zone)).utcoffset()
    minutes = int(delta.total_seconds()) // 60
    sign = "−" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours}" + (f":{rest:02d}" if rest else "")


def choices(now=None):
    """Picker options, common zones first: dicts with value, label and keywords (extra search words).

    Offsets change on the UTC hour or half hour (Lord Howe Island in October), so the list is
    built once per half hour.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return _choices(now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0))


@lru_cache(maxsize=2)
def _choices(moment):
    options, seen = [], set()
    for row in COMMON:
        name = f"{row.name} ({'/'.join(row.abbreviations)})" if row.abbreviations else row.name
        label = ", ".join(part for part in (name, row.city, offset(row.zone, moment)) if part)
        options.append({"value": row.zone, "label": label, "keywords": " ".join((*row.abbreviations, *row.search_words))})
        seen.add(row.zone)
    for zone in zone_names():
        if zone not in seen:
            city = zone.rsplit("/", 1)[-1].replace("_", " ")
            options.append({"value": zone, "label": f"{city}, {offset(zone, moment)}", "keywords": ""})
    return tuple(options)
