"""The activity view: order, filters, grouping and status words."""

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from art_frames.activity import group, status, view, when
from art_frames.web import create_app

PASSWORD = "a-long-test-password-not-a-real-secret"


def auth():
    return {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}


def entry(id, *, tv="main", tv_name="Living Room", label="Check TV artwork", source="you", state="done",
          message="Done.", time="9:40 PM", category="success"):
    return {"id": id, "tv": tv, "tv_name": tv_name, "label": label, "source": source, "state": state, "step": "",
            "message": message, "category": category, "time": time, "ago": "just now", "took": "1 s",
            "finished_at": "x" if state not in {"queued", "running"} else None}


def scheduled(id, **options):
    return entry(id, **{"label": "Scheduled update", "source": "schedule",
                        "message": "The TV confirmed this artwork is selected.", **options})


def record(running=(), queued=(), recent=()):
    return {"running": list(running), "queued": list(queued), "recent": list(recent)}


@pytest.mark.parametrize("state,word,tone", [
    ("done", "Done", "done"), ("skipped", "Not done", "not-done"), ("failed", "Failed", "failed"),
    ("running", "Running", "running"), ("queued", "Waiting", "waiting"), ("cancelled", "Cancelled", "not-done"),
])
def test_each_state_has_a_status_word_and_a_tone(state, word, tone):
    assert status(state) == (word, tone)


def test_times_have_no_seconds_and_name_the_day_when_it_is_not_today():
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 0, 30, tzinfo=zone)
    assert when(datetime(2026, 11, 1, 0, 5, 12, tzinfo=zone), now) == "12:05 AM"
    assert when(datetime(2026, 10, 31, 21, 40, 59, tzinfo=zone), now) == "yesterday 9:40 PM"
    assert when(datetime(2026, 10, 29, 9, 3, tzinfo=zone), now) == "Oct 29, 9:03 AM"
    # now in another timezone: the day still comes from the TV's own zone
    assert when(datetime(2026, 10, 31, 21, 40, tzinfo=zone), now.astimezone(ZoneInfo("UTC"))) == "yesterday 9:40 PM"


def test_a_run_of_repeats_across_midnight_names_the_day():
    lines = group([scheduled(str(index), time="12:20 AM") for index in range(11)] + [scheduled("old", time="yesterday 9:40 PM")])
    assert lines[0]["repeat"] == "12× since yesterday 9:40 PM" and lines[0]["time"] == "12:20 AM"


def test_running_and_waiting_jobs_are_pinned_first_and_your_commands_come_before_the_schedule():
    result = view(record(running=[entry("r", state="running")], queued=[entry("q", state="queued")],
                         recent=[scheduled("s1"), entry("y1"), scheduled("s2"), entry("y2")]), schedule=True)
    assert [line["id"] for line in result["now"]] == ["r", "q"]
    assert [line["status"] for line in result["now"]] == ["Running", "Waiting"]
    assert [line["id"] for line in result["yours"]] == ["y1", "y2"]  # newest first, as recorded
    assert [line["count"] for line in result["schedule"]] == [2]


def test_the_schedule_is_hidden_unless_the_switch_is_on_but_its_problems_always_show():
    data = record(recent=[scheduled("s1"), entry("y1"), scheduled("s2", state="failed", message="No."),
                          scheduled("s3", state="skipped", message="Asleep.")])
    hidden = view(data)
    assert hidden["schedule"] == [] and hidden["hidden"] == 1 and hidden["filters"]["schedule"] is False
    assert [(line["label"], line["status"], line["badge"]) for line in hidden["yours"]] == [
        ("Check TV artwork", "Done", ""), ("Scheduled update", "Failed", "Scheduled"),
        ("Scheduled update", "Not done", "Scheduled")]
    shown = view(data, schedule=True)
    assert shown["hidden"] == 0 and [line["entries"][0]["id"] for line in shown["schedule"]] == ["s1", "s2", "s3"]
    assert [line["id"] for line in shown["yours"]] == ["y1"] and not any(line["badge"] for line in shown["schedule"])


def test_problems_include_the_schedule_even_with_the_switch_off():
    data = record(recent=[scheduled("s1"), scheduled("s2", state="failed", message="No."), entry("y1", state="failed")])
    problems = view(data, outcome="problems")
    assert [line["entries"][0]["id"] for line in problems["yours"]] == ["s2", "y1"] and problems["hidden"] == 0


@pytest.mark.parametrize("filter_tv,outcome,text", [
    ("den", "problems", "No problems for this TV."), ("", "problems", "No problems."),
    ("den", "all", "No commands for this TV."), ("", "all", "No commands yet."),
])
def test_the_empty_text_follows_the_filters(filter_tv, outcome, text):
    result = view(record(queued=[entry("q", tv="den", tv_name="Den", state="queued")]), filter_tv=filter_tv, outcome=outcome)
    assert result["yours"] == [] and result["empty"] == text


def test_a_running_scheduler_pass_still_shows_under_now():
    result = view(record(running=[scheduled("s", state="running")]))
    assert [line["id"] for line in result["now"]] == ["s"]


def test_consecutive_identical_scheduled_entries_collapse_into_one_line():
    lines = group([scheduled("a", time="9:52 PM"), scheduled("b", time="9:47 PM"), scheduled("c", time="9:40 PM"),
                   scheduled("d", state="failed", message="The website could not be rendered.", time="9:35 PM"),
                   scheduled("e", time="9:30 PM")])
    assert [line["count"] for line in lines] == [3, 1, 1]
    first = lines[0]
    assert first["action"] == "Scheduled update · 3× since 9:40 PM"
    assert first["time"] == "9:52 PM" and first["status"] == "Done" and first["id"].startswith("run-")
    assert [item["id"] for item in first["entries"]] == ["a", "b", "c"]
    assert lines[1]["status"] == "Failed" and lines[1]["action"] == "Scheduled update"


def test_a_run_keeps_its_id_while_repeats_arrive_and_old_entries_leave_the_history():
    failed = {"state": "failed", "message": "No."}
    before = group([scheduled("c"), scheduled("b"), scheduled("x", **failed), scheduled("a")])
    after = group([scheduled("e"), scheduled("d"), scheduled("c"), scheduled("b"), scheduled("x", **failed)])  # "a" left
    assert [line["id"] for line in before][:2] == [line["id"] for line in after]
    assert len({line["id"] for line in before}) == 3  # two runs of the same result are still two lines


def test_a_newer_run_with_the_same_result_does_not_change_older_ids():
    failed = {"state": "failed", "message": "No."}
    before = group([scheduled("c"), scheduled("b"), scheduled("x", **failed), scheduled("a")])
    after = group([scheduled("f"), scheduled("y", **failed), *[scheduled(i) for i in "cb"], scheduled("x", **failed),
                   scheduled("a")])
    assert [line["id"] for line in after][2:] == [line["id"] for line in before]
    assert len({line["id"] for line in after}) == 5


def test_another_tvs_pass_in_between_does_not_split_a_run():
    lines = group([scheduled("a"), scheduled("x", tv="den", tv_name="Den"), scheduled("b"),
                   scheduled("y", tv="den", tv_name="Den")])
    assert [(line["tv_name"], line["count"]) for line in lines] == [("Living Room", 2), ("Den", 2)]


def test_different_actions_on_the_same_tv_are_separate_lines():
    lines = group([scheduled("a"), {**scheduled("w"), "label": "Scheduled wake"}, scheduled("b")])
    assert [(line["label"], line["count"]) for line in lines] == [("Scheduled update", 2), ("Scheduled wake", 1)]


def test_filter_by_tv_and_by_problems():
    data = record(queued=[entry("q", tv="den", tv_name="Den", state="queued")], recent=[
        entry("ok"), entry("failed", state="failed", category="error"), entry("skipped", state="skipped"),
        entry("cancelled", state="cancelled"), entry("den", tv="den", tv_name="Den", state="failed"),
    ])
    problems = view(data, outcome="problems")
    assert [line["id"] for line in problems["yours"]] == ["failed", "skipped", "den"]
    assert [line["id"] for line in problems["now"]] == ["q"]  # Now stays pinned
    den = view(data, filter_tv="den")
    assert [line["id"] for line in den["yours"]] == ["den"] and [line["id"] for line in den["now"]] == ["q"]
    assert den["tvs"] == [{"id": "den", "name": "Den"}, {"id": "main", "name": "Living Room"}]
    unknown = view(data, filter_tv="gone", outcome="nonsense")
    assert unknown["filters"] == {"schedule": False, "filter_tv": "", "outcome": "all"} and len(unknown["yours"]) == 5


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    app.extensions["jobs"].mode = "manual"
    return app


def test_the_page_hides_the_schedule_and_shows_status_words(app, monkeypatch):
    displays, controller, client = app.extensions["displays"], app.extensions["controller"], app.test_client()
    monkeypatch.setattr(controller, "_tick", lambda now, **kwargs: "published")
    for _ in range(3):
        displays.tick()
    page = client.get("/activity", headers=auth()).text
    assert "Scheduled update" not in page and "Include automatic updates" in page and "(3 hidden)" in page
    page = client.get("/activity?schedule=1", headers=auth()).text
    assert "Scheduled update</span><span class=\"activity-repeat\"> · 3× since" in page and ">Done<" in page and "(3 hidden)" not in page
    assert 'name="schedule" value="1" checked' in page


def test_the_drawer_asks_for_the_view_and_the_plain_record_stays_the_same(app, monkeypatch):
    client, controller = app.test_client(), app.extensions["controller"]
    monkeypatch.setattr(controller, "_tick", lambda now, **kwargs: "published")
    app.extensions["displays"].tick()
    assert set(client.get("/activity.json", headers=auth()).json) == {"running", "queued", "recent"}
    body = client.get("/activity.json?view=1&schedule=1&outcome=problems", headers=auth()).json
    assert body["view"]["filters"] == {"schedule": True, "filter_tv": "", "outcome": "problems"}
    assert body["view"]["schedule"] == [] and len(body["recent"]) == 1  # Done is not a problem
    assert body["view"]["empty"] == "No problems."
    assert body["recent"][0]["time"].count(":") == 1  # "9:40 PM", no seconds
    # the names older links used still work
    old = client.get("/activity.json?view=1&show=problems&only=main", headers=auth()).json["view"]["filters"]
    assert old == {"schedule": False, "filter_tv": "main", "outcome": "problems"}


def test_the_page_names_its_filters_filter_tv_and_outcome(app):
    page = app.test_client().get("/activity?outcome=problems", headers=auth()).text
    assert 'name="filter_tv"' in page and 'name="outcome" value="problems"' in page and "No problems." in page
    assert 'role="switch"' in page and "data-auto-submit-go>Apply</button>" in page
    assert "only=" not in page and "show=" not in page
