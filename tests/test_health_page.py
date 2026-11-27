"""The Health page: ordered checks from stored state, one fix per problem, and the sidebar dot."""

import html
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_app_schedule import (
    rest,
    three_apps,  # noqa: F401  (a fixture)
)
from test_web import PASSWORD, auth, csrf

from art_frames.config import Config
from art_frames.rotation import Rotation
from art_frames.web import OK, PROBLEM, create_app, health_headline, wake_check


@pytest.fixture
def site(three_apps, tmp_path):  # noqa: F811
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("morning", "news")))
    return app, app.extensions["library"], app.test_client()


def paired(library, **status):
    library.set_state("tv_status", {"paired": True, "known": True, "awake": True, "playing": False, **status})
    library.set_state("tv_status_at", (datetime.now().astimezone() - timedelta(minutes=4)).isoformat())


def rows(page):
    """Each check row as (tone, title)."""
    return re.findall(r'<li class="health-row tone-(\w+)" id="check-[\w-]+">.*?<p class="health-title">([^<]+)</p>', page, re.S)


def identity(app):
    return app.extensions["configs"].load().tv.identity


def test_an_unpaired_tv_leads_with_pairing_and_one_fix(site):
    _, _, client = site
    page = client.get("/tv/main/health", headers=auth()).text
    # Problems come first; the headline is the first problem. OK rows fold below the problems and notes.
    assert rows(page)[0] == ("problem", "Frame 1 is not paired")
    assert ("ok", "Art Frames is running") in rows(page[page.index('id="checks-ok"'):])
    assert ">1 problem</h2>" in page
    assert 'href="/tv/main/settings#pairing">Pair TV</a>' in page
    # Without pairing, Art Frames does not say the TV did not answer or that nothing was sent.
    assert "did not answer" not in page and "Nothing sent yet" not in page


def test_every_check_passes_on_a_healthy_tv(site, image_bytes):
    app, library, client = site
    paired(library)
    library.add(image_bytes, title="Piece")  # without artwork, Health says Artwork shows a plain frame
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    library.set_state("last_display", {"device": identity(app), "at": (now - timedelta(minutes=9)).isoformat()})
    rotation = Rotation(library, identity(app))
    for app_id in ("morning", "news"):
        rotation.record_success(app_id, now)
    page = client.get("/tv/main/health", headers=auth()).text
    assert "All good</h2>" in page and 'tone-problem' not in page
    assert re.search(r"<summary><span>All \d+ checks OK</span>", page)
    assert ("ok", "Frame 1 answered · Art Mode") in rows(page) and "4 minutes ago" in page
    assert ("ok", "Last update reached the TV") in rows(page) and "9 minutes ago" in page
    assert ("ok", "Morning and News refresh OK") in rows(page)
    assert "sidebar-count" not in page


def test_a_failed_app_names_its_error_and_offers_a_test_of_that_app(site):
    app, library, client = site
    paired(library)
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    rotation = Rotation(library, identity(app))
    rotation.record_success("news", now - timedelta(hours=1))
    rotation.record_failure("news", "The app did not answer in time", now - timedelta(minutes=20), 15)
    rotation.record_success("morning", now)
    page = client.get("/tv/main/health", headers=auth()).text
    assert ("problem", "News could not refresh") in rows(page)
    assert "The app did not answer in time." in page and "20 minutes ago" in page
    # A timeout: the test comes first, the settings link second.
    fixes = re.search(r'id="check-app-news">.*?<div class="health-fixes">(.*?)</div>\s*</li>', page, re.S).group(1)
    test = re.search(r'<form method="post" action="/screen/preview\?tv=main">(.*?)</form>', fixes, re.S).group(1)
    assert 'name="app" value="news"' in test and 'name="app_preview"' not in test
    assert 'name="back" value="health"' in test and '<button class="button sm" type="submit">Test News</button>' in test
    assert fixes.index("Test News") < fixes.index('class="button outline sm" href="/layouts/news?tv=main">Open News settings</a>')
    assert "/refresh" not in fixes
    # Morning works; News does not count as fine.
    assert ("ok", "Morning refresh OK") in rows(page)
    # The sidebar shows the number of problems next to Health.
    assert re.search(r'<span>Health</span><span class="sidebar-count"[^>]*>1<', page)


def test_another_failure_puts_the_settings_first(site):
    app, library, client = site
    paired(library)
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    Rotation(library, identity(app)).record_failure("news", "The layout could not be rendered.", now, 15)
    page = client.get("/tv/main/health", headers=auth()).text
    fixes = re.search(r'id="check-app-news">.*?<div class="health-fixes">(.*?)</div>\s*</li>', page, re.S).group(1)
    assert fixes.index('class="button sm" href="/layouts/news?tv=main">Open News settings') < fixes.index("Test News")
    assert "The layout could not be rendered." in page and "rendered.." not in page


def test_an_app_that_needs_setup_is_a_problem_with_one_fix(site, three_apps):  # noqa: F811
    from test_app_schedule import write
    write(three_apps / "apps" / "news" / "layout.json", {"id": "news", "name": "News", "settings": [
        {"key": "api_key", "label": "API key", "type": "secret"}]})
    app, library, client = site
    paired(library)
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    Rotation(library, identity(app)).record_failure("news", "No key", now, 15)
    page = client.get("/tv/main/health", headers=auth()).text
    assert rows(page)[0] == ("problem", "News needs setup")
    assert "Add the API key so News can show pictures." in page
    assert "News could not refresh" not in page and "News have not" not in page and "News has not" not in page
    assert 'class="button sm" href="/layouts/news?tv=main#field-api_key">Set up News</a>' in page


def test_the_headline_names_the_first_problem_and_how_many_more(site, monkeypatch):
    _, library, client = site
    monkeypatch.setattr("art_frames.web.scheduler_alive", lambda displays: False)
    page = html.unescape(client.get("/tv/main/health", headers=auth()).text)
    assert ">2 problems</h2>" in page
    assert "Is Frame 1 OK, and what to fix." in page and "Start with the first problem" not in page
    assert "never contacts the TV here" not in page


def test_an_app_that_worked_after_its_error_is_fine(site):
    app, library, client = site
    paired(library)
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    rotation = Rotation(library, identity(app))
    rotation.record_failure("news", "The app did not answer in time", now - timedelta(hours=1), 15)
    rotation.record_success("news", now)
    page = client.get("/tv/main/health", headers=auth()).text
    assert "could not refresh" not in page
    assert ("info", "Morning has not refreshed on this TV yet") in rows(page)


def test_a_failed_scheduled_update_and_a_silent_tv(site):
    app, library, client = site
    paired(library, known=False, awake=False)
    library.set_state("scheduler_error", "The TV did not accept the update; Art Frames will retry")
    library.set_state("scheduler_error_at", (datetime.now().astimezone() - timedelta(minutes=3)).isoformat())
    page = client.get("/tv/main/health", headers=auth()).text
    assert ("info", "TV did not answer at the last check") in rows(page)
    assert "The TV did not accept the update; Art Frames will retry." in page
    check = re.search(r'<form method="post" action="/screen/check-tv\?tv=main">(.*?)</form>', page, re.S).group(1)
    assert 'name="back" value="health"' in check


def test_a_stopped_scheduler_is_the_first_problem(site, monkeypatch):
    app, library, client = site
    paired(library)
    monkeypatch.setattr("art_frames.web.scheduler_alive", lambda displays: False)
    page = html.unescape(client.get("/tv/main/health", headers=auth()).text)
    assert rows(page)[0] == ("problem", "The Art Frames scheduler stopped")
    assert "Restart Art Frames." in page


@pytest.mark.parametrize("text,tone,title", [
    ("Wake confirmed at 07:00", OK, "Last turn-on reached Art Mode at 7:00 AM"),
    ("Wake not confirmed at 06:45; the TV reached Art Mode at 07:20", "info", "Last turn-on was late"),
    ("Wake was attempted; the TV did not confirm Art Mode", PROBLEM, "Last turn-on did not reach Art Mode"),
    ("Wake skipped because a TV programme was in progress", "info", "Last turn-on was skipped because someone was watching TV"),
    ("Wake skipped because the TV has not been paired", "info", "Last turn-on was skipped because the TV was not paired"),
    (None, "info", "Turns on at 6:45 AM, every day. It has not run yet."),
    ({"text": "not a string"}, "info", "Turns on at 6:45 AM, every day. It has not run yet."),
])
def test_wake_results_read_as_plain_words(text, tone, title):
    cfg = Config.model_validate({"tv": {"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"},
                                 "wake": {"enabled": True, "at": "06:45"}})
    check = wake_check(text, cfg)
    assert (check["tone"], check["title"]) == (tone, title)
    if "late" in title:
        assert check["detail"] == "The TV reached Art Mode at 7:20 AM, after the turn-on time."
    if tone == PROBLEM:
        assert check["fixes"] == ["connection"]


def test_no_wake_row_without_a_wake_time():
    assert wake_check("Wake confirmed at 07:00", Config()) is None


def test_the_headline_is_the_first_problem():
    checks = [{"tone": OK}, {"tone": PROBLEM, "id": "a"}, {"tone": PROBLEM, "id": "b"}]
    assert health_headline(checks) == ({"tone": PROBLEM, "id": "a"}, 2)
    assert health_headline([{"tone": OK}]) == (None, 0)


def test_fix_forms_return_to_health_and_unknown_places_to_now(site):
    app, library, client = site
    paired(library)
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client), "back": "health"})
    assert response.location.startswith("/tv/main/health?job=")
    response = client.post("/screen/check-tv", headers=auth(), data={"csrf": csrf(client), "back": "health"})
    assert response.location.startswith("/tv/main/health?job=")
    for back in ("app:weather", "elsewhere", ""):
        response = client.post("/screen/check-tv", headers=auth(), data={"csrf": csrf(client), "back": back})
        assert response.location.startswith("/tv/main?") and "view=tv" in response.location, back


def test_the_health_page_never_contacts_the_tv(site):
    app, library, client = site
    paired(library)
    controller = app.extensions["displays"].get("main").controller

    def no_tv(cfg):
        raise AssertionError("The Health page contacted the TV")
    controller.tv_factory = no_tv
    assert client.get("/tv/main/health", headers=auth()).status_code == 200
    assert client.get("/tv/main/health?days=90", headers=auth()).status_code == 200


def test_a_test_result_shows_on_the_row_and_never_clears_the_problem(site):
    app, library, client = site
    paired(library)
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    rotation = Rotation(library, identity(app))
    rotation.record_failure("news", "The app did not answer in time", now - timedelta(minutes=5), 15)
    rotation.record_test("news", now)
    page = html.unescape(client.get("/tv/main/health", headers=auth()).text)
    assert ("problem", "News could not refresh") in rows(page)
    assert re.search(r'<p class="small health-test">Test passed at \d+:\d\d [AP]M\. The next refresh on the TV clears this problem\.</p>', page)
    assert "Test News</button>" in page
    rotation.record_test("news", now, "The layout could not be rendered.")
    page = html.unescape(client.get("/tv/main/health", headers=auth()).text)
    assert re.search(r"Test failed at \d+:\d\d [AP]M: The layout could not be rendered\.</p>", page)


def test_only_health_s_test_records_a_result(site, monkeypatch):
    app, library, client = site
    paired(library)
    seen = []
    controller = app.extensions["displays"].get("main").controller
    monkeypatch.setattr(controller, "preview_site", lambda **kwargs: seen.append(kwargs["test"]) or b"jpeg")
    for form in ({"app": "news", "back": "health"}, {"app": "news", "test": "1"}, {"app": "news"}, {}):
        client.post("/screen/preview", headers=auth(), data={"csrf": csrf(client), **form})
    assert seen == [True, True, False, False]
