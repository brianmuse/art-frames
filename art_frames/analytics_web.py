"""The Analytics page: server-rendered SVG charts from one TV's analytics file. No script runs."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

from flask import redirect, request, url_for

from . import analytics

RANGES = (7, 30, 90)
LABELS = {"awake": "Art Mode", "playing": "Watching TV", "standby": "Standby",
          "deep": "Off (deep standby)", "unknown": "Unknown"}
BAR_HEIGHT, LINE_HEIGHT, CELL = 160, 140, 20
GAP = 2  # px of surface between touching marks
BAR_WIDTH = 20
# Heatmap steps: average whole minutes of Art Mode per hour. One hue, light to dark; 0 has its own neutral.
HEAT = ((1, 12, "heat-1"), (13, 24, "heat-2"), (25, 36, "heat-3"), (37, 48, "heat-4"), (49, 60, "heat-5"))
HEAT_LEGEND = [("No data", "heat-none"), ("0 min", "heat-zero"),
               *[(f"{low}–{high} min", css) for low, high, css in HEAT]]
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_POINTS = 400


def day(value):
    return value.strftime("%b %-d")


def moment(value):
    return value.strftime("%b %-d, %-I:%M %p")


def hour_label(hour):
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def hours(value):
    return "—" if value is None else f"{value:.1f} h"


def amount(tracked_hours):
    """How much data an average is based on, in plain words."""
    if tracked_hours >= 24:
        days = round(tracked_hours / 24, 1)
        return f"{days:g} day{'s' if days != 1 else ''}"
    whole = max(1, round(tracked_hours))
    return f"{whole} hour{'s' if whole != 1 else ''}"


def bar_chart(daily):
    """Stacked daily hours by state, bottom to top in a fixed state order."""
    count = len(daily)
    slot = 100 / count
    # A DST day can have 25 hours; scale to the longest day so no bar is clipped.
    scale = max(24.0, *(sum(row[state] for state in analytics.STATES) for row in daily))
    # Each day is a nested SVG one slot wide. A week gets fixed 20 px bars (marks stay at most 24 px);
    # longer ranges use a share of the slot so the bars fit at phone width.
    if count <= 7:
        bar = {"x": "50%", "width": str(BAR_WIDTH), "shift": -BAR_WIDTH / 2}
    else:
        share = .5 if count <= 30 else .7
        bar = {"x": f"{(1 - share) * 50:g}%", "width": f"{share * 100:g}%", "shift": 0}
    columns = []
    for index, row in enumerate(daily):
        segments, bottom = [], 0.0
        present = [state for state in analytics.STATES if row[state] * BAR_HEIGHT / scale >= 1]
        for position, state in enumerate(present):
            height = row[state] * BAR_HEIGHT / scale
            drawn = height - (GAP if position else 0)
            top = BAR_HEIGHT - bottom - height
            segments.append({"state": state, "y": round(top, 1), "height": round(max(drawn, .5), 1),
                             "last": position == len(present) - 1})
            bottom += height
        tip = "; ".join(f"{LABELS[state]} {row[state]:.1f} h" for state in analytics.STATES if row[state] >= .05)
        if not row["collected"]:
            tip = "before Art Frames started collecting"
        columns.append({"segments": segments, "slot_x": round(index * slot, 3), "slot": round(slot, 3),
                        "title": f"{day(row['date'])}: {tip or 'no data'}"})
    ticks = sorted({0, count // 2, count - 1})
    return {"columns": columns, "height": BAR_HEIGHT, "bar": bar,
            "grid": [{"y": round(BAR_HEIGHT - value * BAR_HEIGHT / scale, 1), "label": f"{value} h"} for value in (0, 6, 12, 18, 24)],
            "ticks": [{"x": round((index + .5) * slot, 3), "label": day(daily[index]["date"])} for index in ticks]}


def heat_class(minutes):
    if minutes == 0:
        return "heat-zero"
    return next(css for low, high, css in HEAT if minutes <= high)


def heatmap(cells):
    rows = []
    for weekday, name in enumerate(DAYS):
        row = []
        for hour in range(24):
            cell = cells.get((weekday, hour))
            if not cell or cell["tracked"] < 60:
                row.append({"x": round(hour * 100 / 24, 3), "class": "heat-none", "minutes": None,
                            "title": f"{name} {hour_label(hour)}: no data"})
                continue
            minutes = min(60, round(cell["art"] / cell["tracked"] * 60))
            row.append({"x": round(hour * 100 / 24, 3), "class": heat_class(minutes), "minutes": minutes,
                        "title": f"{name} {hour_label(hour)}: {minutes} min of Art Mode per hour, on average"})
        rows.append({"name": name, "y": weekday * CELL, "cells": row})
    table = [{"hour": hour_label(hour), "cells": [rows[weekday]["cells"][hour]["minutes"] for weekday in range(7)]}
             for hour in range(24)]
    return {"rows": rows, "width": round(100 / 24, 3), "cell": CELL, "height": 7 * CELL, "table": table,
            "ticks": [{"x": round(hour * 100 / 24, 3), "label": hour_label(hour)} for hour in (0, 6, 12, 18)]}


def _thin(points):
    """At most MAX_POINTS points: the median value of each time bucket."""
    if len(points) <= MAX_POINTS:
        return points
    size = -(-len(points) // MAX_POINTS)
    return [(group[len(group) // 2][0], sorted(value for _, value in group)[len(group) // 2])
            for group in (points[index:index + size] for index in range(0, len(points), size))]


def line_chart(report, tz):
    raw = report["brightness"]
    start, end = report["start"], report["now"]
    # Less than a day of readings: zoom to the hours with data instead of a nearly empty range.
    zoomed = bool(raw) and raw[-1][0] - raw[0][0] < 86400
    if zoomed:
        start, end = max(start, raw[0][0] - 1800), min(end, raw[-1][0] + 1800)
    span = max(end - start, 1)

    def x(at):
        return round(min(100, max(0, (at - start) / span * 100)), 3)
    points = _thin(raw)
    top = max((value for _, value in points), default=10)
    scale = next((value for value in (10, 20, 50, 100) if top <= value), top)

    def y(value):
        return round(LINE_HEIGHT - 4 - value / scale * (LINE_HEIGHT - 8), 1)
    joined = analytics.SAMPLE_SECONDS * 2.5  # a longer gap means the TV left Art Mode: break the line
    limit = joined if len(points) == len(raw) else max(joined, 2.5 * span / MAX_POINTS)
    segments = [{"x1": x(a), "y1": y(va), "x2": x(b), "y2": y(vb)} for (a, va), (b, vb) in pairwise(points) if b - a <= limit]
    marks = [{"x": x(at), "y": y(value), "title": f"{moment(datetime.fromtimestamp(at, tz))}: brightness {value}"}
             for at, value in points]
    bands, changes, previous = [], [], None
    sensor = [(at, value) for at, value in report["sensor"] if start <= at <= end]
    for index, (at, value) in enumerate(sensor):
        if previous is not None and value != previous[1]:
            changes.append(f"Turned {'on' if value else 'off'} between {moment(datetime.fromtimestamp(previous[0], tz))} "
                           f"and {moment(datetime.fromtimestamp(at, tz))}")
        previous = (at, value)
        if value:
            following = sensor[index + 1][0] if index + 1 < len(sensor) else None
            until = min(following if following is not None and following - at <= joined else at + analytics.SAMPLE_SECONDS, end)
            if bands and abs(bands[-1]["until"] - at) < 1:
                bands[-1]["until"] = until
            else:
                bands.append({"from": at, "until": until})
    if zoomed:
        ticks = [{"x": x(at), "label": datetime.fromtimestamp(at, tz).strftime("%-I:%M %p")} for at in (start, (start + end) / 2, end)]
    else:
        # Day centers, like the bar chart; today's center can lie past now, so it is clamped to the edge.
        days = report["dates"]
        noon = [datetime.combine(days[index], datetime.min.time(), tzinfo=tz) + timedelta(hours=12) for index in sorted({0, len(days) // 2, len(days) - 1})]
        ticks = [{"x": x(value.timestamp()), "label": day(value)} for value in noon]
    for tick in ticks:
        tick["anchor"] = "start" if tick["x"] < 8 else "end" if tick["x"] > 92 else "middle"
    return {"height": LINE_HEIGHT, "segments": segments, "marks": marks, "dots": len(points) <= 60, "zoomed": zoomed,
            "bands": [{"x": x(band["from"]), "width": max(round(x(band["until"]) - x(band["from"]), 3), .2)} for band in bands],
            "changes": changes, "ticks": ticks,
            "grid": [{"y": y(value), "label": str(value)} for value in (0, scale // 2, scale)]}


def display_record(library, cfg):
    """The last stored Art Mode reading ("art_display") of this TV, or {} when it is for another TV."""
    record = library.state("art_display") or {}
    return record if record.get("device") == cfg.tv.identity else {}


def chart_context(screen, cfg, days):
    """The charts of one TV's Health page, from Art Frames' own records only.

    `days` is one of RANGES; any other value shows 7 days.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = days if days in RANGES else 7
    report = analytics.report(screen.configs.root, cfg.timezone, days)
    context = {"report": report, "days": days, "ranges": RANGES}
    if not report["unreadable"]:
        tz = ZoneInfo(cfg.timezone)
        context.update(
            labels=LABELS, heat_legend=HEAT_LEGEND, days_of_week=DAYS, amount=amount(report["tracked_hours"]),
            bars=bar_chart(report["daily"]), heat=heatmap(report["cells"]), line=line_chart(report, tz),
            day=day, hours=hours, problems=[(moment(when), text) for when, text in report["problems"]],
        )
    return context


def register(app):
    @app.get("/analytics")
    def analytics_page():
        """The older Analytics URL; the page is now the TV's Health page."""
        days = request.args.get("days")
        return redirect(url_for("tv_health", **({"days": days} if days else {})), 301)

    @app.get("/tv/<tv>/insights")
    def tv_insights(tv):
        """The older Insights URL; its charts are now on the TV's Health page."""
        days = request.args.get("days")
        return redirect(url_for("tv_health", **({"days": days} if days else {})), 301)
