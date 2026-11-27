"""Bad stored records never break a page: each page still draws, and Health still names its checks."""

import hashlib
import json
from datetime import datetime

import pytest
from test_app_schedule import three_apps  # noqa: F401  (a fixture)
from test_health_page import identity, paired
from test_web import PASSWORD, auth, csrf

from art_frames.web import app_source, create_app

PAGES = ["/household", "/tv/main/settings", "/tv/main/schedule", "/tv/main", "/tv/main/health", "/layouts", "/"]


@pytest.fixture
def site(three_apps, tmp_path):  # noqa: F811
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule={"default": {"apps": ["morning", "news"]}}))
    return app, app.extensions["library"], app.test_client()


def naive_screen_off(app, library):
    paired(library)
    library.delete_state("tv_status_at")
    library.set_state("screen_off_at:" + identity(app), "2026-11-01T07:00:00")


def bad_app_health(app, library):
    paired(library)
    library.set_state("app_health:" + identity(app), {"news": {"error": "x", "error_at": "not a time"}})


def bad_status_at(app, library):
    library.set_state("tv_status", {"paired": True, "known": True, "awake": True})
    library.set_state("tv_status_at", "garbage")


def bad_scheduler_error_at(app, library):
    paired(library)
    library.set_state("scheduler_error", "Render failed")
    library.set_state("scheduler_error_at", "yesterday")


def wrong_types(app, library):
    paired(library)
    library.set_state("tv_status_at", 12345)
    library.set_state("last_display", {"device": identity(app), "at": ["not", "a", "time"]})
    library.set_state("wake_result", {"not": "text"})


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("record", [naive_screen_off, bad_app_health, bad_status_at, bad_scheduler_error_at, wrong_types])
def test_a_bad_record_never_breaks_a_page(site, page, record):
    app, library, client = site
    record(app, library)
    response = client.get(page, headers=auth())
    assert response.status_code == 200
    if page.endswith("/health"):
        assert 'class="health-row' in response.text


def test_a_broken_sidebar_count_logs_and_shows_no_count(site, monkeypatch, caplog):
    _, _, client = site

    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr("art_frames.web.health_checks", broken)
    response = client.get("/layouts", headers=auth())
    assert response.status_code == 200 and "sidebar-count" not in response.text.split("<span>Health</span>")[1][:80]
    assert any("count" in record.getMessage().lower() for record in caplog.records)


def test_a_corrupt_sent_image_is_404_at_both_sizes(site):
    app, library, client = site
    data = b"not a jpeg"
    (app.extensions["configs"].root / "tv-upload.jpg").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    library.set_state("last_upload", {"device": identity(app), "at": datetime.now().astimezone().isoformat(),
                                      "image_sha256": digest})
    for size in ("&size=large", ""):
        assert client.get(f"/screen/sent.jpg?v={digest}{size}", headers=auth()).status_code == 404


def test_an_app_source_link_is_never_followed(tmp_path):
    class Chosen:
        manifest_path = tmp_path / "app" / "layout.json"
    (tmp_path / "app").mkdir()
    (tmp_path / "private.json").write_text(json.dumps({"label": "TOKEN-123"}))
    (tmp_path / "app" / ".dayframe-source.json").symlink_to(tmp_path / "private.json")
    assert app_source(Chosen()) is None


def test_back_to_an_app_on_a_tv_with_a_custom_website_goes_to_now(site):
    app, _, client = site
    app.extensions["configs"].mutate(lambda raw: raw["site"].update(path="my-site"))
    response = client.post("/screen/check-tv?tv=main", headers=auth(), data={"csrf": csrf(client), "back": "app:news"})
    assert response.headers["Location"].split("?")[0] == "/tv/main"
