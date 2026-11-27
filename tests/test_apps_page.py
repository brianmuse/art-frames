"""The Layouts list and each layout's page: state, TVs, source, settings card guards and per-TV options."""

import html
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_app_schedule import rest, three_apps, write  # noqa: F401  (a fixture)
from test_health_page import identity, paired
from test_web import PASSWORD, auth, csrf

from art_frames import apps
from art_frames.rotation import Rotation
from art_frames.web import app_source, choice_label, create_app, help_parts, options_fingerprint

SECRET = {"key": "album_url", "label": "Album link", "type": "secret",
          "description": "The shared album's link. Open the album in Photos, then copy its public link."}
FIT = {"key": "fit", "label": "Fit", "type": "choice", "choices": ["contain", "cover"], "default": "cover"}
CALENDAR = {"key": "show_calendar", "label": "Show the calendar", "type": "boolean", "default": True}


@pytest.fixture
def site(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", {"id": "photos", "name": "Photos", "settings": [SECRET, FIT]})
    write(three_apps / "installed" / "layout.json", {"id": "morning", "name": "Morning", "artwork": True,
                                                    "display_settings": [CALENDAR]})
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule={"week": {"default": {"blocks": [
        {"app": "morning", "start": "06:00", "end": "09:00"}, {"app": "photos", "start": "18:00", "end": "22:00"}]}}}))
    return app, app.test_client(), three_apps



def fingerprint(app, app_id):
    chosen = apps.find(app_id)
    held = chosen.store(app.extensions["configs"].root)
    return held.fingerprint(held.manifest())


def rows(page):
    """Each app row as (name, state word)."""
    return re.findall(r'<h2><a href="/layouts/[\w-]+\?tv=main">([^<]+)</a></h2>\s*<span class="badge [\w ]+ app-state">(?:<svg.*?</svg>)?([^<]+)</span>', page, re.S)


def test_old_apps_urls_redirect_to_layouts(site):
    """Bookmarks and open pages from before the rename keep working: GETs and POSTs move with a 308."""
    app, client, _ = site
    response = client.get("/apps", headers=auth())
    assert response.status_code == 308 and response.location == "/layouts"
    response = client.get("/apps/photos?tv=main", headers=auth())
    assert response.status_code == 308 and response.location == "/layouts/photos?tv=main"
    response = client.get("/apps/photos", headers=auth(), follow_redirects=True)
    assert response.status_code == 200 and "Photos" in response.text
    # A POST moves with a 308, so the browser sends the same form to the new URL. The CSRF check still runs first.
    response = client.post("/apps/photos/refresh", headers=auth(), data={"csrf": csrf(client), "minutes": "5"})
    assert response.status_code == 308 and response.location == "/layouts/photos/refresh"
    response = client.post("/apps/photos", headers=auth(), data={"csrf": csrf(client), "manifest": fingerprint(app, "photos")})
    assert response.status_code == 308 and response.location == "/layouts/photos"
    assert client.post("/apps/photos", headers=auth(), data={"csrf": "wrong"}).status_code == 400
    # Signed out, the old URL asks for sign-in like any other page.
    assert client.get("/apps", follow_redirects=False).status_code != 308

def test_the_apps_list_shows_each_app_s_state_and_tvs(site):
    app, client, _ = site
    paired(app.extensions["library"])
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    rotation = Rotation(app.extensions["library"], identity(app))
    rotation.record_success("morning", now - timedelta(hours=1))
    rotation.record_failure("morning", "The app did not answer in time", now, 15)
    page = html.unescape(client.get("/layouts", headers=auth()).text)
    assert rows(page) == [("Morning", "Not refreshing"), ("News", "Not on a TV"), ("Photos", "Needs setup")]
    assert "Morning could not refresh on Frame 1. The app did not answer in time." in page
    assert "Photos needs an album link before it can show anything." in page
    assert "Used on Frame 1 (6:00 to 9:00 AM every day)</p>" in page and "Used on Frame 1 (6:00 to 10:00 PM every day)</p>" in page and "Not on any TV yet" in page and "Use it on a TV from its page." in page
    # A button only where the user has work: setup and a failure. Other rows are one link.
    assert 'href="/layouts/photos?tv=main#field-album_url">Set up' in page
    assert 'href="/tv/main/health#check-app-morning">See problem' in page
    assert page.count("app-row-action") == 2 and ">Open<" not in page


def test_an_app_no_tv_has_refreshed_waits_and_does_not_count(site):
    app, client, _ = site
    page = html.unescape(client.get("/layouts", headers=auth()).text)
    assert ("Morning", "Not refreshed yet") in rows(page)
    # Apps has no sidebar count: Health flags setup and failures for apps a TV uses.
    assert '<span>Apps</span><span class="sidebar-count"' not in page


def test_the_sidebar_indents_each_app_without_a_count(site):
    _, client, _ = site
    page = client.get("/layouts", headers=auth()).text
    assert '<span>Apps</span><span class="sidebar-count"' not in page
    assert page.count('<li class="sidebar-menu-child">') == 3


def test_an_app_source_reads_only_its_whitelisted_fields(site, tmp_path):
    app, client, sites = site
    folder = sites / "apps" / "photos"
    (folder / ".dayframe-source.json").write_text(json.dumps({
        "schema_version": 1, "id": "photos", "label": "dayframe-layouts", "kind": "repo", "ref": "photos-app",
        "commit": "acbdd4a0123456789", "installed_at": "2026-11-01T09:30:00+00:00",
        "repository": "brianmuse/dayframe-layouts", "url": "https://github.com/brianmuse/dayframe-layouts/tree/acbdd4a/photos",
        "token": "PRIVATE-NEVER-RENDER"}))
    assert app_source(apps.find("photos")) == {
        "text": "brianmuse/dayframe-layouts · photos-app at acbdd4a", "installed": "Nov 1, 2026",
        "url": "https://github.com/brianmuse/dayframe-layouts/tree/acbdd4a/photos"}
    for path in ("/layouts", "/layouts/photos"):
        page = client.get(path, headers=auth()).text
        assert "brianmuse/dayframe-layouts · photos-app at acbdd4a" in page and "PRIVATE-NEVER-RENDER" not in page
        assert "installed Nov 1, 2026" in page
    (folder / ".dayframe-source.json").write_text(json.dumps({"kind": "path", "label": "photos", "url": "javascript:alert(1)"}))
    assert app_source(apps.find("photos")) == {"text": "Local folder photos", "installed": "", "url": ""}
    for broken in ("{not json", "[1, 2]", json.dumps({"label": "x" * 10_000})):
        (folder / ".dayframe-source.json").write_text(broken)
        assert app_source(apps.find("photos")) in (None, {"text": "x" * 200, "installed": "", "url": ""})
    (folder / ".dayframe-source.json").unlink()
    assert app_source(apps.find("photos")) is None


def test_an_app_that_needs_setup_says_so_and_shows_secret_states(site):
    app, client, _ = site
    page = html.unescape(client.get("/layouts/photos", headers=auth()).text)
    assert "Needs setup." in page and 'href="#field-album_url">Set up</a>' in page and 'id="field-album_url"' in page
    assert 'placeholder="Paste your album link"' in page and "Replace</summary>" not in page
    # Choices read as words; the saved value stays the same.
    assert '<option value="cover" selected>Cover</option>' in page and '<option value="contain" >Contain</option>' in page
    # Help shows its first sentence; the rest is under Learn more.
    assert "<span>The shared album's link.</span>" in page
    assert "<summary>Learn more</summary><p>Open the album in Photos, then copy its public link.</p>" in page
    response = client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": fingerprint(app, "photos"), "album_url": "https://example.test/PRIVATE", "fit": "contain"})
    assert response.status_code == 302 and response.location.endswith("/layouts/photos?tv=main#settings")
    page = html.unescape(client.get("/layouts/photos", headers=auth()).text)
    assert "PRIVATE" not in page and "Needs setup" not in page
    assert "change at their next update, within" in page and "Art Frames keeps it private" in page and "Replace</summary>" in page
    # Remove asks first: the submit sits behind the question.
    question = re.search(r'<details class="confirm-delete secret-remove" data-confirm>(.*?)</details>', page, re.S).group(1)
    assert "Remove the saved album link? Photos stops until you add it again." in question
    assert 'data-confirm-cancel>Cancel</button>' in question
    assert '<button class="button destructive sm" type="submit" name="clear_album_url" value="on">Remove</button>' in question
    # Enter in a field presses the first submit button: that is Save, not Remove.
    assert page.index('aria-hidden="true">Save</button>') < page.index('name="clear_album_url"')
    # Replace and Remove together is refused, and the saved link stays.
    response = client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": fingerprint(app, "photos"), "album_url": "https://example.test/NEW",
        "clear_album_url": "on", "fit": "contain"})
    assert response.status_code == 400 and "Choose Replace or Remove, not both." in html.unescape(response.text)
    assert apps.find("photos").store(app.extensions["configs"].root).values(apps.find("photos").manifest)["album_url"].endswith("PRIVATE")
    # Removing it brings back Needs setup.
    client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": fingerprint(app, "photos"), "clear_album_url": "on", "fit": "contain"})
    assert "Needs setup." in client.get("/layouts/photos", headers=auth()).text


def test_a_form_drawn_for_an_older_app_says_so_at_the_card(site):
    app, client, _ = site
    response = client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": "old", "album_url": "https://example.test/x"})
    assert response.status_code == 302 and response.location.endswith("#settings")
    page = html.unescape(client.get(response.location, headers=auth()).text)
    assert re.search(r'id="settings".*?This layout changed after the page opened\. Here are its latest settings\.', page, re.S)
    assert apps.find("photos").store(app.extensions["configs"].root).values(apps.find("photos").manifest).get("album_url", "") == ""


def test_a_settings_error_draws_the_card_again_with_one_sentence(site, monkeypatch):
    app, client, _ = site

    def too_big(self, form):
        raise ValueError("Layout settings exceed their size limit")
    monkeypatch.setattr("art_frames.layouts.LayoutStore.update", too_big)
    response = client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": fingerprint(app, "photos"), "album_url": "SECRET-NEVER-RENDER", "fit": "contain"})
    page = html.unescape(response.text)
    assert response.status_code == 400
    assert "These settings are too long to save. Shorten a list or a text field." in page
    assert '<option value="contain" selected>' in page and "SECRET-NEVER-RENDER" not in page


def test_used_on_links_the_app_s_add_form(site):
    app, client, _ = site
    displays = app.extensions["displays"]
    second = displays.add("Den", "192.168.1.51")
    page = html.unescape(client.get("/layouts/photos", headers=auth()).text)
    assert "6:00 to 10:00 PM every day" in page and "Not in its schedule" in page
    link = f'href="/tv/{second}/schedule#add-photos">Use on Den…</a>'
    assert link in page
    # The link opens the Add form of Photos on Den's timeline (CSS :target).
    schedule = client.get(f"/tv/{second}/schedule", headers=auth()).text
    assert 'id="add-photos"' in schedule and 'name="app" value="photos"' in schedule


def test_options_on_each_tv_list_every_tv_and_refuse_an_unknown_one(site):
    app, client, _ = site
    displays = app.extensions["displays"]
    second = displays.add("Den", "192.168.1.51")
    displays.get(second).configs.mutate(lambda raw: raw.update(app_schedule=rest("photos")))
    page = client.get("/layouts/morning", headers=auth()).text
    assert page.count('name="tvs"') == 2 and '<li class="tv-use-row muted">' in page  # Den does not show Morning yet
    # One card lists each TV, its places, its options and one action, with one Save.
    assert 'id="used-on"' not in page and "settings-nav" not in page
    assert 'href="/tv/main/schedule">Schedule ›</a>' in page and f'href="/tv/{second}/schedule#add-morning">Use on Den…</a>' in page
    assert page.count('<button class="button" type="submit">Save</button>') == 1
    response = client.post("/site/tv-settings", headers=auth(), data={
        "csrf": csrf(client), "manifest": "morning", "options": options_fingerprint(apps.find("morning").manifest), "tvs": ["main", second], f"tv.{second}.show_calendar": "on"})
    assert response.status_code == 302 and response.location.endswith("/layouts/morning?tv=main#on-each-tv")
    assert displays.get(second).configs.load().app_settings["morning"] == {"show_calendar": True}
    assert displays.get("main").configs.load().app_settings["morning"] == {"show_calendar": False}
    response = client.post("/site/tv-settings", headers=auth(), data={
        "csrf": csrf(client), "manifest": "morning", "options": options_fingerprint(apps.find("morning").manifest), "tvs": ["main", "gone"]})
    assert response.status_code == 400
    assert "A TV on this page was removed. Reload the page, then make your change again." in html.unescape(response.text)
    assert displays.get("main").configs.load().app_settings["morning"] == {"show_calendar": False}


def test_show_now_from_an_app_page_returns_to_it(site, monkeypatch):
    app, client, _ = site
    paired(app.extensions["library"])
    page = client.get("/layouts/morning", headers=auth()).text
    form = re.search(r'<form method="post" action="/screen/show-app\?tv=main">(.*?)</form>', page, re.S).group(1)
    assert 'name="back" value="app:morning"' in form and "Show on Frame 1 now" in form
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "show_app", lambda app_id, until: calls.append(app_id))
    response = client.post("/screen/show-app?tv=main", headers=auth(), data={
        "csrf": csrf(client), "app": "morning", "hold": "next-change", "back": "app:morning"})
    assert response.location.startswith("/layouts/morning?") and calls == ["morning"]


def test_an_app_that_needs_setup_has_no_show_now(site):
    app, client, _ = site
    paired(app.extensions["library"])
    page = client.get("/layouts/photos", headers=auth()).text
    assert "Show on Frame 1 now" not in page and "Needs setup." in page


def test_old_and_removed_app_urls(site):
    _, client, sites = site
    response = client.get("/site/settings", headers=auth())
    assert response.status_code == 302 and response.location.startswith("/layouts/")
    assert client.get("/site/settings?app=photos", headers=auth()).status_code == 301
    response = client.post("/layouts/gone", headers=auth(), data={"csrf": csrf(client), "manifest": "x"})
    assert response.status_code == 302 and response.location.startswith("/layouts")
    assert "This layout is no longer installed." in client.get(response.location, headers=auth()).text


def test_an_app_page_has_no_show_now_for_an_unpaired_tv(site):
    _, client, _ = site
    assert "/screen/show-app" not in client.get("/layouts/photos", headers=auth()).text


def test_the_preview_card_shows_only_this_app_s_preview(site, monkeypatch):
    app, client, _ = site
    (app.extensions["configs"].root / "screen-preview.jpg").write_bytes(b"preview")
    controller = app.extensions["controller"]
    monkeypatch.setattr(controller, "preview_app", lambda revision: "photos")
    assert "Latest preview of Photos" in client.get("/layouts/photos", headers=auth()).text
    assert "No preview yet" in client.get("/layouts/news", headers=auth()).text


def test_no_apps_page_for_a_custom_website(site):
    app, client, _ = site
    app.extensions["configs"].mutate(lambda raw: raw["site"].update(path="custom"))
    assert client.get("/layouts", headers=auth()).status_code == 404


@pytest.mark.parametrize("text,parts", [
    ("One. Two three.", ("One.", "Two three.")),
    ("Only one sentence", ("Only one sentence", "")),
    ("", ("", "")),
    ("Version 1.2 is fine. More.", ("Version 1.2 is fine.", "More.")),
])
def test_help_splits_at_the_first_sentence(text, parts):
    assert help_parts(text) == parts


def test_choice_labels():
    assert choice_label("cover") == "Cover" and choice_label("top_stories") == "Top stories"


def test_the_show_now_menu_sends_a_needs_setup_app_to_its_settings(site):
    app, client, _ = site
    paired(app.extensions["library"])
    page = client.get("/tv/main", headers=auth()).text
    menu = re.search(r'<form class="dropdown-content show-now-menu".*?</form>', page, re.S).group(0)
    assert '<a class="dropdown-item" href="/layouts/photos?tv=main#field-album_url">' in menu and "Set up first" in menu
    assert 'value="photos"' not in menu and 'name="app" value="morning"' in menu


def test_the_server_refuses_to_show_an_app_that_needs_setup(site, monkeypatch):
    app, client, _ = site
    paired(app.extensions["library"])
    queued = []
    from art_frames.jobs import Jobs
    monkeypatch.setattr(Jobs, "submit", lambda self, job: queued.append(job.label) or (job, True))
    response = client.post("/screen/show-app?tv=main", headers=auth(), data={
        "csrf": csrf(client), "app": "photos", "hold": "next-change"})
    assert response.status_code == 302 and response.location.endswith("/layouts/photos?tv=main#field-album_url")
    assert not queued and "Set up Photos first." in client.get(response.location, headers=auth()).text
    # A saved preview of Photos cannot be sent either.
    controller = app.extensions["controller"]
    monkeypatch.setattr(controller, "preview_app", lambda revision=None: "photos")
    response = client.post("/screen/send-preview?tv=main", headers=auth(), data={
        "csrf": csrf(client), "preview_revision": "a" * 64})
    assert response.location.endswith("#field-album_url") and not queued


def test_needs_setup_apps_are_never_offered_to_send(site):
    app, client, _ = site
    paired(app.extensions["library"])
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("photos")))
    page = client.get("/tv/main", headers=auth()).text
    # The empty frame links to setup instead of a Show button.
    assert 'href="/layouts/photos?tv=main#field-album_url">Set up Photos</a>' in page
    assert 'name="app" value="photos">' not in page
    # The preview dialog marks the app and does not pick it.
    assert '<option value="photos"  data-setup>Photos (needs setup)</option>' in page
    # The app page keeps Make a preview but has no send.
    page = client.get("/layouts/photos", headers=auth()).text
    assert "Make a preview" in page and "/screen/send-preview" not in page


def test_on_each_tv_refuses_a_form_drawn_for_older_options(site):
    app, client, _ = site
    page = client.get("/layouts/morning", headers=auth()).text
    assert f'name="options" value="{options_fingerprint(apps.find("morning").manifest)}"' in page
    response = client.post("/site/tv-settings", headers=auth(), data={
        "csrf": csrf(client), "manifest": "morning", "options": "old", "tvs": ["main"]})
    assert response.status_code == 302 and response.location.endswith("#on-each-tv")
    page = html.unescape(client.get(response.location, headers=auth()).text)
    assert "This layout's options changed after the page opened. Here are its latest options." in page
    assert app.extensions["configs"].load().app_settings.get("morning") != {"show_calendar": False}


def test_the_options_fingerprint_follows_only_keys_and_types(site):
    from art_frames.layouts import Manifest
    base = {"id": "x", "name": "X", "display_settings": [CALENDAR]}
    renamed = {**base, "display_settings": [{**CALENDAR, "label": "Calendar", "description": "New help."}]}
    other = {**base, "display_settings": [{**CALENDAR, "key": "show_weather"}]}
    first = options_fingerprint(Manifest.model_validate(base))
    assert options_fingerprint(Manifest.model_validate(renamed)) == first
    assert options_fingerprint(Manifest.model_validate(other)) != first
