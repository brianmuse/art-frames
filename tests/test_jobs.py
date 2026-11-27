import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from art_frames import jobs as jobs_module
from art_frames.config import Config
from art_frames.controller import Controller
from art_frames.displays import Displays
from art_frames.jobs import QUEUE_LIMIT, WAITING, Job, Jobs, QueueFull
from art_frames.television import DisplayError
from art_frames.web import create_app

PASSWORD = "a-long-test-password-not-a-real-secret"


def auth():
    return {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}


def csrf(client):
    client.get("/", headers=auth())
    with client.session_transaction() as session:
        return session["csrf"]


def job(key, run=None, tv="main"):
    return Job(tv, "Living Room", f"Command {key}", (tv, key), run=run or (lambda: ("Done.", "success")))


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    app.extensions["jobs"].mode = "manual"  # routes only queue; each test drains
    return app


def test_each_tv_runs_its_jobs_in_order():
    queue, order = Jobs(mode="manual"), []
    for tv in ("main", "den"):
        for key in ("a", "b", "c"):
            queue.submit(job(key, lambda tv=tv, key=key: order.append((tv, key)) or ("Done.", "success"), tv))
    queue.drain("den")
    assert order == [("den", "a"), ("den", "b"), ("den", "c")]
    queue.drain()
    assert order[3:] == [("main", "a"), ("main", "b"), ("main", "c")]
    assert [entry["state"] for entry in queue.snapshot()["recent"]] == ["done"] * 6


def test_the_same_command_is_not_queued_twice_in_a_row():
    queue = Jobs(mode="manual")
    first, added = queue.submit(job("check"))
    again, added_again = queue.submit(job("check"))
    assert added and not added_again and again is first
    assert queue.submit(job("check", tv="den"))[1]  # another TV has its own queue
    queue.drain()
    assert queue.submit(job("check"))[0] is not first  # a finished command can run again


@pytest.mark.parametrize("sequence", [["on", "off", "on"], [10, 50, 10]])
def test_a_repeated_command_after_a_different_one_still_runs_last(sequence):
    queue, ran = Jobs(mode="manual"), []
    for value in sequence:
        queue.submit(job(value, lambda value=value: ran.append(value) or ("Done.", "success")))
    queue.drain()
    assert ran == sequence


def test_a_click_during_a_running_job_queues_a_new_one():
    queue, ran = Jobs(mode="manual"), []

    def first():
        ran.append("first")
        later, added = queue.submit(job("preview", lambda: ran.append("second") or ("Done.", "success")))
        assert added and later.state == "queued"
        return "Done.", "success"
    queue.submit(job("preview", first))
    queue.drain()
    assert ran == ["first", "second"]


def test_a_result_that_did_nothing_shows_as_not_done(app, monkeypatch):
    client, jobs = app.test_client(), app.extensions["jobs"]
    monkeypatch.setattr(app.extensions["controller"], "check_artwork", lambda: "asleep")
    client.post("/screen/check-tv", headers=auth(), data={"csrf": csrf(client)})
    jobs.drain()
    [entry] = client.get("/activity.json", headers=auth()).json["recent"]
    assert entry["state"] == "skipped" and "skipped" not in entry
    assert "Not done" in client.get("/activity", headers=auth()).text


def test_a_tv_queue_holds_at_most_ten_commands():
    queue = Jobs(mode="manual")
    for index in range(QUEUE_LIMIT):
        queue.submit(job(index))
    with pytest.raises(QueueFull, match="10 commands waiting"):
        queue.submit(job("one more"))
    assert queue.submit(job(QUEUE_LIMIT - 1))[1] is False  # a repeat of the last one is answered, not refused
    assert queue.submit(job("other", tv="den"))[1]


def test_only_a_queued_command_can_be_cancelled():
    queue, ran, refused = Jobs(mode="manual"), [], []
    first = queue.submit(job("a", lambda: refused.append(queue.cancel(first.id)) or ("Done.", "success")))[0]
    second = queue.submit(job("b", lambda: ran.append("b") or ("Done.", "success")))[0]
    assert queue.cancel(second.id) and not queue.cancel(second.id)
    queue.drain()
    assert not ran and second.state == "cancelled"
    assert refused == [False] and first.state == "done"  # a running job cannot be cancelled
    assert not queue.cancel("unknown")


def test_a_job_waits_for_a_held_tv_then_runs(configs, library, monkeypatch):
    monkeypatch.setattr(jobs_module, "JOB_WAIT", 30)
    controller, queue = Controller(configs, library, demo=True), Jobs()
    controller.lock.acquire()
    try:
        entry = queue.submit(Job("main", "Living Room", "Test", ("main", "x"), controller=controller,
                                 run=lambda: (controller._exclusive(lambda: "Ran."), "success")))[0]
        with queue.cond:
            assert queue.cond.wait_for(lambda: entry.state == "running", 5)
        assert entry.step() == WAITING and not entry.locked
        # The scheduler never waits: its pass is skipped while the TV is held.
        assert controller.run_once() == "busy"
    finally:
        controller.lock.release()
    assert queue.wait(entry.id, 10)
    assert entry.state == "done" and entry.message == "Ran." and entry.locked


def test_a_job_that_cannot_get_the_tv_fails_after_the_wait(configs, library):
    controller, queue = Controller(configs, library, demo=True), Jobs(mode="manual")
    with controller.lock:
        entry = queue.submit(Job("main", "Living Room", "Check TV artwork", ("main", "x"), controller=controller,
                                 run=lambda: ("Busy text", "info") if controller.check_artwork() == "busy" else ("?", "info")))[0]
        queue.drain()
    assert entry.state == "failed" and entry.message == jobs_module.BUSY_TEXT


def test_a_busy_scheduler_pass_is_skipped_and_not_recorded(configs, library):
    controller = Controller(configs, library, demo=True)
    displays = Displays(configs, library, controller, demo=True)
    displays.jobs = Jobs(mode="manual")
    with controller.lock:
        displays.tick()
    assert displays.jobs.snapshot() == {"running": [], "queued": [], "recent": []}


def test_scheduler_records_a_delivery_but_not_a_quiet_check(app, monkeypatch):
    displays, jobs, controller = (app.extensions[key] for key in ("displays", "jobs", "controller"))
    seen, results = [], iter(["unchanged", "published"])

    def tick(now, **kwargs):
        seen.append(jobs.snapshot()["running"])
        return next(results)
    monkeypatch.setattr(controller, "_tick", tick)
    displays.tick()
    assert seen[0][0]["label"] == "Scheduled update" and seen[0][0]["state"] == "running"
    assert seen[0][0]["source"] == "schedule"
    assert jobs.snapshot()["recent"] == []
    displays.tick()
    recent = jobs.snapshot()["recent"]
    assert [(entry["label"], entry["state"], entry["category"]) for entry in recent] == [("Scheduled update", "done", "success")]
    assert recent[0]["message"] == "The TV confirmed this artwork is selected."
    assert jobs.snapshot()["running"] == []


def test_scheduler_records_a_failure_once(app, monkeypatch):
    displays, jobs, controller = (app.extensions[key] for key in ("displays", "jobs", "controller"))

    def tick(now, **kwargs):
        controller._stage = "render"
        raise RuntimeError("private detail")
    monkeypatch.setattr(controller, "_tick", tick)
    displays.tick()
    displays.tick()
    recent = jobs.snapshot()["recent"]
    assert [(entry["state"], entry["message"]) for entry in recent] == [("failed", "The website could not be rendered; Art Frames will retry.")]


def test_activity_json_needs_auth_and_is_never_cached(app):
    client = app.test_client()
    assert client.get("/activity.json").status_code == 401
    assert client.get("/activity").status_code == 401
    response = client.get("/activity.json", headers=auth())
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json == {"running": [], "queued": [], "recent": []}


def test_cancel_needs_csrf(app):
    client, jobs = app.test_client(), app.extensions["jobs"]
    token = csrf(client)
    client.post("/screen/check-tv", headers=auth(), data={"csrf": token})
    queued = jobs.snapshot()["queued"][0]["id"]
    assert client.post(f"/activity/{queued}/cancel", headers=auth()).status_code == 400
    assert jobs.get(queued).state == "queued"
    response = client.post(f"/activity/{queued}/cancel", headers={**auth(), "Accept": "application/json"}, data={"csrf": token})
    assert response.json == {"status": "cancelled"} and jobs.get(queued).state == "cancelled"
    den = app.extensions["displays"].add("Den", "192.168.1.51")
    client.post(f"/screen/check-tv?tv={den}", headers=auth(), data={"csrf": token})
    other = jobs.snapshot()["queued"][0]["id"]
    assert client.post(f"/activity/{other}/cancel", headers=auth(), data={"csrf": token}).status_code == 404
    assert client.post(f"/activity/{other}/cancel?tv={den}", headers=auth(), data={"csrf": token}).status_code == 302
    response = client.post(f"/activity/{queued}/cancel", headers=auth(), data={"csrf": token}, follow_redirects=True)
    assert "already started" in response.text and "Cancelled before it started." in response.text


NOT_ART_MODE = "Not done: the TV is not on in Art Mode. Art Frames did not wake it or change anything."
ROUTES = [
    ("/screen/check-tv", {}, "check_artwork", "asleep", "Check TV artwork",
     "The TV is asleep. Art Frames keeps the last successful check.", "notice"),
    ("/refresh", {}, "tick", "published", "Update the TV now", "The TV confirmed this artwork is selected.", "notice"),
    ("/screen/send-preview", {"preview_revision": "a" * 64}, "send_preview", "published", "Show preview now",
     "The TV confirmed this artwork is selected.", "notice"),
    ("/screen/preview", {}, "preview_site", b"jpeg", "Make a preview", "Preview updated.", "notice"),
    ("/wake", {}, "wake_screen", "published", "Turn on now", "The TV confirmed this artwork is selected.", "notice"),
    ("/pair", {}, "pair", None, "Pair TV", "TV paired.", "notice"),
    ("/screen/display/refresh", {}, "display_settings", DisplayError("not-art-mode"), "Read Art Mode settings", NOT_ART_MODE, "display"),
    ("/screen/display/brightness", {"brightness": "30"}, "set_brightness", DisplayError("not-art-mode"), "Set brightness to 30", NOT_ART_MODE, "display"),
    ("/screen/display/light-sensor", {"sensor": "on"}, "set_light_sensor", DisplayError("not-art-mode"), "Turn the light sensor on", NOT_ART_MODE, "options"),
    ("/screen/display/screen-off", {}, "screen_off", DisplayError("not-art-mode"), "Try the power-off key", NOT_ART_MODE, "power"),
    ("/screen/display/check-screen", {}, "check_screen", DisplayError("not-art-mode"), "Read TV power", NOT_ART_MODE, "power"),
    ("/screen/display/screen-on", {}, "screen_on", DisplayError("not-art-mode"), "Turn screen on", NOT_ART_MODE, "power"),
]


@pytest.mark.parametrize("path,form,method,result,label,message,place", ROUTES)
def test_each_tv_button_queues_and_returns_at_once(app, monkeypatch, path, form, method, result, label, message, place):
    client, jobs = app.test_client(), app.extensions["jobs"]
    calls = []

    def action(*args, **kwargs):
        calls.append(args)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(app.extensions["controller"], method, action)
    if path == "/pair":  # Pair TV needs a saved address (see test_pair_needs_a_saved_address)
        app.extensions["configs"].mutate(lambda raw: raw["tv"].update(ip="192.0.2.10"))
    token = csrf(client)
    response = client.post(path, headers=auth(), data={"csrf": token, **form})
    assert response.status_code == 302 and not calls
    job_id = parse_qs(urlparse(response.location).query)["job"][0]
    [queued] = jobs.snapshot()["queued"]
    assert (queued["id"], queued["label"], queued["state"], queued["place"]) == (job_id, label, "queued", place)
    page = client.get(response.location, headers=auth()).text
    assert f"Queued: {label}" in page and f'data-job="{job_id}"' in page and message not in page
    # A second press while it waits returns the same job.
    again = client.post(path, headers=auth(), data={"csrf": token, **form})
    assert parse_qs(urlparse(again.location).query)["job"][0] == job_id and len(jobs.snapshot()["queued"]) == 1
    jobs.drain()
    assert len(calls) == 1 and jobs.get(job_id).message == message
    page = client.get(response.location, headers=auth()).text
    assert message in page and "data-job=" not in page
    if place in ("display", "power", "standby"):
        assert f'data-display-result tabindex="-1">{message}' in page


def test_activity_never_shows_addresses_tokens_or_device_details(tmp_path, monkeypatch):
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    configs, controller = app.extensions["configs"], app.extensions["controller"]
    configs.save(Config(tv={"ip": "192.168.1.77", "mac": "AA:BB:CC:DD:EE:77"}, tv_name="Living Room"))
    monkeypatch.setattr(controller, "_tick", lambda now, **kwargs: "published")
    monkeypatch.setattr(controller, "check_artwork", lambda: "checked")
    client = app.test_client()
    client.post("/screen/check-tv", headers=auth(), data={"csrf": csrf(client)})
    app.extensions["displays"].tick()
    body = client.get("/activity.json", headers=auth()).json
    text = json.dumps(body)
    assert len(body["recent"]) == 2
    for private in ("192.168.1.77", "AA:BB:CC:DD:EE:77", "token", "identity", "device", "ical"):
        assert private.lower() not in text.lower()
    assert set(body["recent"][0]) == {"id", "tv", "tv_name", "label", "source", "state", "step", "queued_at", "started_at",
                                      "finished_at", "duration", "message", "category", "place", "time", "ago", "took"}
    page = client.get("/activity?schedule=1", headers=auth()).text
    assert "Check TV artwork" in page and "Scheduled update" in page and "192.168.1.77" not in page
    view = json.dumps(client.get("/activity.json?view=1&schedule=1", headers=auth()).json["view"])
    for private in ("192.168.1.77", "AA:BB:CC:DD:EE:77"):
        assert private not in view
