"""A new refresh period renders again; identical bytes the TV still shows are not uploaded again."""

import hashlib
import logging

from test_controller import at, setup

from art_frames import analytics
from art_frames.config import private_write

SKIP_LINE = "TV main: image unchanged; skipped the upload"


def shown_by_tv(screen):
    """The fake TV reports the content ID of its last upload while it is in Art Mode."""
    screen.checks = 0

    def current():
        screen.checks += 1
        return f"app-{len(screen.publications)}" if screen.awake and screen.publications else None
    screen.current_artwork = current


def test_identical_render_in_a_new_period_skips_upload_and_select(configs, library, monkeypatch, caplog):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    shown_by_tv(screen)
    events = []
    monkeypatch.setattr(analytics, "_event", lambda ctl, kind, outcome, *a, **k: events.append((kind, outcome)))
    assert controller.tick(at("18:00")) == "published"
    device = configs.load().tv.identity
    first_key = library.state("display_key:" + device)
    with caplog.at_level(logging.INFO):
        assert controller.tick(at("18:20")) == "same-image"
    assert screen.publications == [b"website"]  # no second upload, so no second selection either
    assert library.state("display_key:" + device) not in {None, first_key}  # the new revision counts as delivered
    assert library.state("last_upload")["content_id"] == "app-1"
    assert caplog.text.count(SKIP_LINE) == 1
    # Within the same period the scheduler does not render or contact the TV again.
    checks = screen.checks
    assert controller.tick(at("18:25")) == "unchanged" and screen.checks == checks
    # Only the real upload counts as a delivery in analytics.
    assert [event for event in events if event[0] == "delivery"] == [("delivery", "published")]


def test_changed_bytes_or_other_tv_artwork_still_upload(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    shown_by_tv(screen)
    assert controller.tick(at("18:00")) == "published"
    controller.capture = lambda *a, **kw: b"a new day"
    assert controller.tick(at("18:20")) == "published"
    screen.current_artwork = lambda: "someone-else"  # the owner picked other art with the remote
    assert controller.tick(at("18:40")) == "published"
    assert screen.publications == [b"website", b"a new day", b"a new day"]


def test_forced_update_and_preview_send_skip_identical_bytes_the_tv_shows(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    shown_by_tv(screen)
    assert controller.tick(at("18:00"), force=True) == "published"
    assert controller.tick(at("18:00"), force=True) == "same-image"
    preview = b"website"
    private_write(configs.root / "screen-preview.jpg", preview)
    assert controller.send_preview(hashlib.sha256(preview).hexdigest()) == "same-image"
    assert screen.publications == [b"website"]
    # A TV that no longer confirms the image gets it again.
    screen.current_artwork = lambda: None
    assert controller.send_preview(hashlib.sha256(preview).hexdigest()) == "published"
    assert screen.publications == [b"website", b"website"]


def test_log_does_not_switch_between_results_every_period(configs, library, monkeypatch, caplog):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    shown_by_tv(screen)
    controller._note(controller.tick(at("18:00")))
    with caplog.at_level(logging.INFO):
        for clock in ("18:20", "18:21", "18:40", "18:41"):
            controller._note(controller.tick(at(clock)))
    assert caplog.text.count(SKIP_LINE) == 2
    assert caplog.text.count("[unchanged]") == 1 and "[published]" not in caplog.text


def test_scheduled_skip_is_not_shown_as_a_delivery_in_activity(tmp_path):
    from test_web import PASSWORD

    from art_frames.web import create_app
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    describe, controller = app.extensions["jobs"].describe, app.extensions["controller"]
    assert describe("update", "same-image", controller, False) is None
    assert describe("update", "published", controller, False)[1] == "success"


def test_manual_update_says_the_tv_already_shows_the_image(tmp_path, monkeypatch):
    from test_web import PASSWORD, auth, csrf

    from art_frames.web import create_app
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    monkeypatch.setattr(app.extensions["controller"], "tick", lambda **kwargs: "same-image")
    client = app.test_client()
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "The TV already shows this exact image, so Art Frames did not upload it again." in response.text
    assert app.extensions["jobs"].snapshot()["recent"][0]["state"] == "done"


def test_same_image_also_records_the_last_display(configs, library, monkeypatch):
    controller, screen = setup(configs, library, monkeypatch, wake=False)
    screen.awake = True
    shown_by_tv(screen)
    assert controller.tick(at("18:00")) == "published"
    library.set_state("last_display", None)
    private_write(configs.root / "screen-preview.jpg", b"website")
    assert controller.send_preview(hashlib.sha256(b"website").hexdigest()) == "same-image"
    last = library.state("last_display")
    assert last["kind"] == "preview" and last["image_sha256"] == hashlib.sha256(b"website").hexdigest()
    assert last["device"] == configs.load().tv.identity


def test_unconfirmed_delivery_and_wake_show_not_done(tmp_path, monkeypatch):
    from test_web import PASSWORD, auth, csrf

    from art_frames.controller import CALLER
    from art_frames.web import create_app
    app = create_app(tmp_path, password=PASSWORD, testing=True)
    controller, jobs = app.extensions["controller"], app.extensions["jobs"]
    monkeypatch.setattr(controller, "tick", lambda **kwargs: "unconfirmed")
    client = app.test_client()
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "the TV did not confirm switching to it" in response.text
    assert jobs.snapshot()["recent"][0]["state"] == "skipped"
    # Scheduler passes: an unconfirmed image and an unconfirmed wake are "Not done" with their caveat.
    note = jobs.describe("update", "unconfirmed", controller, False)
    assert note[1] == "info" and note[2] == {"skipped": True}
    controller.library.set_state("wake_result", "Wake was attempted; the TV did not confirm Art Mode")
    assert jobs.describe("wake", "woke", controller, False)[2] == {"skipped": True}
    controller.library.set_state("wake_result", "Wake not confirmed at 07:00; the TV reached Art Mode at 07:05")
    assert jobs.describe("wake", "woke", controller, False)[2] == {}
    controller.library.set_state("wake_result", "Wake confirmed at 07:00")
    assert jobs.describe("wake", "woke", controller, False)[1] == "success"

    def unconfirmed_pass():
        CALLER.locked()  # the pass holds the TV
        return "unconfirmed"
    jobs.scheduled("main", "Living Room", controller, "update", "Scheduled update", unconfirmed_pass)
    assert jobs.snapshot()["recent"][0]["state"] == "skipped"
