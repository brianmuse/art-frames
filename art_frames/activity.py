"""Order, filter and group the activity record for people to scan.

The page and the drawer both show this view: commands that run or wait now, then your commands,
then (behind the "Show schedule" switch) the scheduler's own passes. Scheduled passes that failed
or were not done always show in the main list, marked "Scheduled", so a problem is never hidden.
Repeats of the same scheduled result on the same TV collapse into one line.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

# Status in words, with a tone the page shows as a colored dot. The words always show.
STATUS = {
    "queued": ("Waiting", "waiting"),
    "running": ("Running", "running"),
    "done": ("Done", "done"),
    "skipped": ("Not done", "not-done"),
    "failed": ("Failed", "failed"),
    "cancelled": ("Cancelled", "not-done"),
}
PROBLEMS = {"failed", "skipped"}
OUTCOMES = ("all", "problems")


def status(state):
    """The status word and tone for a job state."""
    return STATUS.get(state, (state.capitalize(), "not-done"))


def when(moment, now):
    """A time for people: "9:40 PM" today, "yesterday 9:40 PM", or "Oct 30, 9:40 PM".

    moment and now are aware datetimes; the day comes from moment's own timezone.
    """
    now = now.astimezone(moment.tzinfo)
    clock = f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
    if moment.date() == now.date():
        return clock
    if moment.date() == now.date() - timedelta(days=1):
        return f"yesterday {clock}"
    return f"{moment:%b} {moment.day}, {clock}"


def _line(entries, *, key=None):
    """One line in the list: a single entry, or a run of repeats (newest first).

    key: a stable ID for a scheduled run, so an expanded line stays open while repeats arrive
    and old entries leave the history.
    """
    newest, oldest = entries[0], entries[-1]
    word, tone = status(newest["state"])
    count = len(entries)
    repeat = f"{count}× since {oldest['time']}" if count > 1 else ""
    return {
        "id": key or newest["id"],
        "tv": newest["tv"], "tv_name": newest["tv_name"], "label": newest["label"], "source": newest["source"],
        "state": newest["state"], "status": word, "tone": tone, "time": newest["time"],
        "action": f"{newest['label']} · {repeat}" if repeat else newest["label"], "repeat": repeat, "count": count,
        "badge": "",
        "entries": [{**entry, "status": status(entry["state"])[0], "tone": status(entry["state"])[1]} for entry in entries],
    }


def group(entries):
    """Collapse consecutive identical scheduled results for the same TV and action.

    "Consecutive" counts within one TV and action, so another TV's pass in between does not split
    a run. A different result for that TV and action starts a new line. Only scheduled entries
    collapse; your own commands stay one line each.

    A run's ID comes from its TV, action and result and the ID of its oldest entry. New repeats
    and newer runs do not change it, and neither do entries that leave the history, unless they
    are the run's own oldest entry.
    """
    lines, runs = [], {}
    for entry in entries:
        if entry["source"] != "schedule":
            lines.append(([entry], False))
            continue
        key = (entry["tv"], entry["label"])
        run = runs.get(key)
        if run is not None and (run[0]["state"], run[0]["message"]) == (entry["state"], entry["message"]):
            run.append(entry)
            continue
        run = [entry]
        runs[key] = run
        lines.append((run, True))
    return [_line(run, key=_run_key(run) if grouped else None) for run, grouped in lines]


def _run_key(run):
    """A run's stable ID. The run is newest first, so run[-1] is its oldest entry."""
    entry = run[-1]
    result = "\0".join((entry["tv"], entry["label"], entry["state"], entry["message"] or "", entry["id"]))
    return f"run-{hashlib.sha1(result.encode(), usedforsecurity=False).hexdigest()[:12]}"


def _empty(filter_tv, outcome):
    if outcome == "problems":
        return "No problems for this TV." if filter_tv else "No problems."
    return "No commands for this TV." if filter_tv else "No commands yet."


def view(record, *, schedule=False, filter_tv="", outcome="all"):
    """The activity record, ordered for scanning.

    record: jobs.snapshot() with web.py's formatted times ("time", "ago", "took").
    schedule: show the scheduler's passes in their own section. Running passes always show under
    "now", because they explain why your command waits. Failed and not-done passes always show:
    in the main list when the switch is off, in the schedule section when it is on.
    filter_tv: only this TV. outcome: "all" or "problems" (Failed or Not done).
    """
    outcome = outcome if outcome in OUTCOMES else "all"
    everything = [*record["running"], *record["queued"], *record["recent"]]
    tvs = {}
    for entry in everything:
        tvs.setdefault(entry["tv"], entry["tv_name"])
    filter_tv = filter_tv if filter_tv in tvs else ""

    def keep(entry):
        return (not filter_tv or entry["tv"] == filter_tv) and (outcome == "all" or entry["state"] in PROBLEMS)

    now = [_line([entry]) for entry in [*record["running"], *record["queued"]] if not filter_tv or entry["tv"] == filter_tv]
    recent = [entry for entry in record["recent"] if keep(entry)]
    if schedule:
        yours = group([entry for entry in recent if entry["source"] != "schedule"])
        scheduled = group([entry for entry in recent if entry["source"] == "schedule"])
        hidden = 0
    else:
        shown = [entry for entry in recent if entry["source"] != "schedule" or entry["state"] in PROBLEMS]
        yours = group(shown)
        for line in yours:
            if line["source"] == "schedule":
                line["badge"] = "Scheduled"
        scheduled = []
        hidden = len(recent) - len(shown)
    return {
        "now": now,
        "yours": yours,
        "schedule": scheduled,
        "hidden": hidden,
        "empty": _empty(filter_tv, outcome),
        "tvs": [{"id": key, "name": name} for key, name in sorted(tvs.items(), key=lambda item: item[1].lower())],
        "filters": {"schedule": schedule, "filter_tv": filter_tv, "outcome": outcome},
    }
