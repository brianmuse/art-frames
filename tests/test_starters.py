"""Starter plans: each plan sets exactly its values, shows what changes first, and can be undone."""

import pytest
from test_app_schedule import write
from test_web import PASSWORD, auth, csrf

from art_frames import apps
from art_frames.web import create_app

MORNING = {"id": "morning", "name": "Morning", "artwork": True,
           "display_settings": [{"key": "show_calendar", "label": "Calendar", "type": "boolean", "default": True},
                                {"key": "show_weather", "label": "Weather", "type": "boolean", "default": True}]}
NEWS = {"id": "news", "name": "News", "display_settings": [
    {"key": "show_art", "label": "Art", "type": "boolean", "default": False}]}
PHOTOS = {"id": "photos", "name": "Photos", "settings": [
    {"key": "album_url", "label": "Shared album link", "type": "secret"},
    {"key": "photo_interval", "label": "Change photo every", "type": "choice", "default": "30 minutes",
     "choices": ["5 minutes", "15 minutes", "30 minutes", "60 minutes", "Once a day"]},
    {"key": "order", "label": "Order", "type": "choice", "choices": ["Shuffle", "Newest first"], "default": "Newest first"},
    {"key": "fit", "label": "Fit", "type": "choice", "choices": ["contain", "cover"], "default": "cover"}],
    "display_settings": [{"key": "show_caption", "label": "Caption", "type": "boolean", "default": False},
                         {"key": "show_clock", "label": "Clock", "type": "boolean", "default": True}]}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    sites = tmp_path / "sites"
    first = write(sites / "installed" / "layout.json", MORNING)
    write(sites / "apps" / "news" / "layout.json", NEWS)
    write(sites / "apps" / "photos" / "layout.json", PHOTOS)
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(first))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    monkeypatch.setenv("DAYFRAME_SITES_DIR", str(sites))
    monkeypatch.setenv("DAYFRAME_APPS", "news=news photos=photos")
    return sites


def make(tmp_path, mac=True):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    if mac:
        app.extensions["configs"].mutate(lambda raw: raw["tv"].update(ip="192.0.2.10", mac="AA:BB:CC:DD:EE:01"))
    return app, app.test_client()


def version(client, day="every"):
    page = client.get(f"/tv/main/plans?day={day}&plan=art", headers=auth()).text
    return page.split('name="schedule_version" value="')[1].split('"')[0]


def apply(client, plan, day="every", **extra):
    return client.post("/tv/main/plans", headers=auth(), data={
        "csrf": csrf(client), "plan": plan, "day": day, "schedule_version": version(client, day), **extra})


def state(app, tmp_path):
    cfg = app.extensions["configs"].load()
    shared = app.extensions["displays"].configs.load()
    stores = {name: apps.find(name).store(tmp_path / "data") for name in ("morning", "photos")}
    values = {name: store.values(store.manifest()) for name, store in stores.items()}
    return cfg.app_schedule.model_dump(), cfg.app_settings, values, shared


def day(blocks=(), wake=None):
    return {"blocks": [{"app": app, "start": start, "end": end} for app, start, end in blocks], "wake": wake}


# Artwork shows where no block is, so each plan sets only blocks; an app all day is a full-day block.
MORNING_BLOCK = ("morning", "06:00", "09:00")
EXPECTED = {
    "art": dict(every=day(), options={}, values={}, art=True),
    "morning": dict(every=day([MORNING_BLOCK], wake="06:30"), options={}, values={}, art=True),
    "news": dict(every=day([("news", "06:00", "09:00")]), options={"news": {"show_art": True}}, values={}, art=True),
    "evening-photos": dict(every=day([("photos", "18:00", "22:00")]), options={}, values={}, art=True),
    "photo-frame": dict(every=day([("photos", "00:00", "24:00")]),
                        options={"photos": {"show_caption": True, "show_clock": False}},
                        values={"photos": {"photo_interval": "15 minutes", "order": "Shuffle", "fit": "contain"}},
                        refresh=15),
    "weekdays": dict(every=day([MORNING_BLOCK], wake="06:30"), options={}, values={},
                     days={"sat": day(), "sun": day()}, art=True),
}


@pytest.mark.parametrize("key", EXPECTED)
def test_each_plan_sets_exactly_its_values(catalog, tmp_path, key):
    app, client = make(tmp_path)
    app.extensions["displays"].configs.mutate(lambda raw: (raw["art"].update(enabled=False),
                                                           raw["site"].update(refresh_minutes=60)))
    response = apply(client, key)
    assert response.status_code == 302 and response.location.endswith("#timeline")
    schedule, options, values, shared = state(app, tmp_path)
    want = EXPECTED[key]
    assert schedule["default"] is None  # a plan never sets the older "rest of the time"
    assert schedule["week"]["default"] == want["every"]
    days = {name: None for name in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    assert schedule["week"]["days"] == {**days, **want.get("days", {})}
    for app_id, expected in want["options"].items():
        stored = options[app_id]
        assert {name: stored[name] for name in expected} == expected
    for app_id, expected in want["values"].items():
        assert values[app_id] == expected
    for app_id in {"morning", "photos"} - set(want["values"]):
        assert values[app_id] == {}
    assert shared.art.enabled is want.get("art", False)
    assert shared.site.refresh_minutes == want.get("refresh", 60)


def test_the_page_shows_what_changes_before_applying(catalog, tmp_path):
    app, client = make(tmp_path)
    page = client.get("/tv/main/plans?plan=news", headers=auth()).text
    assert "What changes" in page and "Every day" in page and "News 6:00 to 9:00 AM" in page
    assert "News on this TV: Art" in page and "The rest of the time" not in page
    assert app.extensions["configs"].load().app_schedule.week.default.blocks == []  # nothing saved yet
    picker = client.get("/tv/main/plans", headers=auth()).text
    for name in ("Art all day", "Morning info, art the rest of the day", "News in the morning, art after",
                 "Art by day, photos at night", "Photo frame", "Weekday mornings only", "Custom"):
        assert name in picker
    assert 'class="plan-block"' in picker
    # The 6-9 AM plans draw their block: 360 / 14.4 = 25%, 180 / 14.4 = 12.5% wide. Artwork fills the rest.
    assert picker.count('class="plan-block" x="25.0" y="1" width="12.5"') == 3 and "plan-calendar" not in picker
    # Weekday mornings only sets weekends too, so a single day's tab leaves it out.
    assert "Weekday mornings only" not in client.get("/tv/main/plans?day=sat", headers=auth()).text
    # A full-day block reads "all day", and a day without blocks shows Artwork all day.
    page = client.get("/tv/main/plans?plan=photo-frame", headers=auth()).text
    assert "Photos all day" in page and "12:00 to 12:00" not in page and "Nothing at set times" not in page
    apply(client, "photo-frame")
    page = client.get("/tv/main/plans?plan=art", headers=auth()).text
    assert "Photos all day" in page and "Artwork all day" in page
    assert client.get("/tv/main/plans?day=sat&plan=weekdays", headers=auth()).status_code == 404


def test_undo_puts_every_value_back(catalog, tmp_path):
    app, client = make(tmp_path)
    app.extensions["displays"].configs.mutate(lambda raw: raw["site"].update(refresh_minutes=60))
    before = state(app, tmp_path)
    apply(client, "photo-frame")
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "uses “Photo frame”." in page and ">Undo</button>" in page
    response = client.post("/tv/main/plans/undo", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Undid “Photo frame”." in response.text
    after = state(app, tmp_path)
    # Undo puts back each value the plan set. It keeps the scope, so Photos holds its defaults again.
    assert after[0] == before[0] and before[1] == {}
    assert after[1] == {"photos": {"show_caption": False, "show_clock": True}}
    assert after[3].site.refresh_minutes == 60
    assert after[2]["photos"] == {"photo_interval": "30 minutes", "order": "Newest first", "fit": "cover"}
    assert ">Undo</button>" not in response.text


def test_undo_stops_once_the_schedule_changes(catalog, tmp_path):
    app, client = make(tmp_path)
    apply(client, "news")
    app.extensions["configs"].mutate(lambda raw: raw["app_schedule"]["week"]["default"].update(wake="08:00"))
    assert ">Undo</button>" not in client.get("/tv/main/schedule", headers=auth()).text
    response = client.post("/tv/main/plans/undo", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "The schedule changed since, or the plan is more than a day old, so Art Frames can&#39;t undo it." in response.text
    assert app.extensions["configs"].load().app_schedule.week.default.blocks[0].app == "news"


def test_a_plan_from_an_old_page_is_refused(catalog, tmp_path):
    app, client = make(tmp_path)
    response = client.post("/tv/main/plans", headers=auth(), data={
        "csrf": csrf(client), "plan": "news", "day": "every", "schedule_version": "old"})
    assert response.status_code == 302 and "plan=news" in response.location
    assert app.extensions["configs"].load().app_schedule.week.default.blocks == []


def test_a_plan_on_one_day_changes_only_that_day(catalog, tmp_path):
    app, client = make(tmp_path)
    apply(client, "news", day="sat")
    week = app.extensions["configs"].load().app_schedule.week
    assert week.default.blocks == [] and [block.app for block in week.days["sat"].blocks] == ["news"]
    assert all(week.days[name] is None for name in ("mon", "tue", "wed", "thu", "fri", "sun"))


def test_a_turn_on_time_needs_a_mac(catalog, tmp_path):
    app, client = make(tmp_path, mac=False)
    page = client.get("/tv/main/plans?plan=morning", headers=auth()).text
    assert ("To turn on at 6:30 AM, add the TV's address and MAC address in "
            '<a href="/tv/main/settings#connection-advanced">TV settings</a>.') in page
    apply(client, "morning")
    assert app.extensions["configs"].load().app_schedule.week.default.wake is None


def test_a_photo_plan_asks_for_the_album_link(catalog, tmp_path):
    app, client = make(tmp_path)
    assert 'name="album_url"' in client.get("/tv/main/plans?plan=photo-frame", headers=auth()).text
    apply(client, "photo-frame", album_url="https://example.invalid/album")
    store = apps.find("photos").store(tmp_path / "data")
    assert store.values(store.manifest())["album_url"] == "https://example.invalid/album"
    assert 'name="album_url"' not in client.get("/tv/main/plans?plan=photo-frame", headers=auth()).text


def test_plans_hide_when_their_app_is_missing(catalog, tmp_path, monkeypatch):
    monkeypatch.setenv("DAYFRAME_APPS", "")
    _, client = make(tmp_path)
    page = client.get("/tv/main/plans", headers=auth()).text
    assert "Art all day" in page and "Photo frame" not in page and "News in the morning" not in page
    assert client.get("/tv/main/plans?plan=news", headers=auth()).status_code == 404


def test_an_empty_schedule_offers_plans(catalog, tmp_path):
    _, client = make(tmp_path)
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "Or start from a plan:" in page and 'href="/tv/main/plans?day=every&amp;plan=art"' in page
    assert 'href="/tv/main/plans?day=every"' in page


# Review fixes: Undo restores only what the plan wrote and still holds, for a day; apply is atomic.

def test_undo_keeps_values_changed_since_and_says_which(catalog, tmp_path):
    app, client = make(tmp_path)
    shared = app.extensions["displays"].configs
    shared.mutate(lambda raw: raw["site"].update(refresh_minutes=60))
    apply(client, "photo-frame")
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "Undo also puts back the layout and household values the plan changed." in page
    # After the plan: the refresh and the photo pace change by hand.
    shared.mutate(lambda raw: raw["site"].update(refresh_minutes=30))
    store = apps.find("photos").store(tmp_path / "data")
    store.set_values({"photo_interval": "5 minutes"})
    response = client.post("/tv/main/plans/undo", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "These changed since, so Art Frames kept them: Photos Change photo every, Refresh." in response.text
    assert shared.load().site.refresh_minutes == 30
    assert store.values(store.manifest()) == {"photo_interval": "5 minutes", "order": "Newest first", "fit": "cover"}


def test_undo_restores_only_the_keys_the_plan_set(catalog, tmp_path):
    app, client = make(tmp_path)
    configs = app.extensions["configs"]
    configs.mutate(lambda raw: raw["app_settings"].update(morning={"show_calendar": True, "show_weather": False}))
    apply(client, "art")  # sets show_calendar only
    configs.mutate(lambda raw: raw["app_settings"]["morning"].update(show_weather=True))
    client.post("/tv/main/plans/undo", headers=auth(), data={"csrf": csrf(client)})
    assert configs.load().app_settings["morning"] == {"show_calendar": True, "show_weather": True}


def test_undo_lasts_a_day(catalog, tmp_path):
    app, client = make(tmp_path)
    apply(client, "news")
    library = app.extensions["displays"].get("main").library
    undo = library.state("starter_undo")
    library.set_state("starter_undo", {**undo, "at": undo["at"] - 24 * 3600 - 1})
    assert ">Undo</button>" not in client.get("/tv/main/schedule", headers=auth()).text
    client.post("/tv/main/plans/undo", headers=auth(), data={"csrf": csrf(client)})
    assert app.extensions["configs"].load().app_schedule.week.default.blocks[0].app == "news"


def test_a_plan_checks_the_schedule_in_the_same_write(catalog, tmp_path, monkeypatch):
    app, client = make(tmp_path)
    configs = app.extensions["configs"]
    token, seen = version(client), csrf(client)
    real = configs.mutate

    def race(change):  # another tab saves the schedule just before this write
        monkeypatch.setattr(configs, "mutate", real)
        real(lambda raw: raw["app_schedule"]["week"]["default"].update(wake="08:00"))
        return real(change)
    monkeypatch.setattr(configs, "mutate", race)
    client.post("/tv/main/plans", headers=auth(), data={
        "csrf": seen, "plan": "news", "day": "every", "schedule_version": token})
    week = configs.load().app_schedule.week.default
    assert week.wake == "08:00" and week.blocks == []


def test_undo_checks_the_schedule_in_the_same_write(catalog, tmp_path, monkeypatch):
    app, client = make(tmp_path)
    configs = app.extensions["configs"]
    apply(client, "news")
    seen = csrf(client)
    real = configs.mutate

    def race(change):  # another tab saves the schedule just before Undo writes
        monkeypatch.setattr(configs, "mutate", real)
        real(lambda raw: raw["app_schedule"]["week"]["default"].update(wake="08:00"))
        return real(change)
    monkeypatch.setattr(configs, "mutate", race)
    response = client.post("/tv/main/plans/undo", headers=auth(), data={"csrf": seen}, follow_redirects=True)
    week = configs.load().app_schedule.week.default
    assert week.wake == "08:00" and week.blocks[0].app == "news"
    assert "The schedule changed since" in response.text


def test_the_undo_record_is_saved_before_app_settings(catalog, tmp_path, monkeypatch):
    app, client = make(tmp_path)
    token, seen = version(client), csrf(client)

    def broken(self, changes):
        raise OSError("disk full")
    monkeypatch.setattr(type(apps.find("photos").store(tmp_path / "data")), "set_values", broken)
    with pytest.raises(OSError):
        client.post("/tv/main/plans", headers=auth(), data={
            "csrf": seen, "plan": "photo-frame", "day": "every", "schedule_version": token})
    assert app.extensions["displays"].get("main").library.state("starter_undo")["plan"] == "Photo frame"


def test_a_plan_never_sets_a_secret_or_a_list():
    from art_frames import starters
    from art_frames.layouts import Manifest

    manifest = Manifest.model_validate({"id": "photos", "name": "Photos", "settings": [
        {"key": "photo_interval", "label": "Every", "type": "secret"},
        {"key": "order", "label": "Order", "type": "list", "fields": [{"key": "a", "label": "A", "type": "text"}]},
        {"key": "fit", "label": "Fit", "type": "choice", "choices": ["contain", "cover"], "default": "cover"}]})

    class Installed:
        id = "photos"
    Installed.manifest = manifest
    _, settings = starters.declared(starters.find("photo-frame"), [Installed])
    assert settings == {"photos": {"fit": "contain"}}


def test_the_schedule_keeps_a_change_plan_link(catalog, tmp_path):
    app, client = make(tmp_path)
    apply(client, "news")
    app.extensions["displays"].get("main").library.delete_state("starter_undo")
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert 'class="small plan-change"><a href="/tv/main/plans?day=every">Change plan</a>' in page
    # An empty day lists the plans, so it needs no separate Change plan link.
    app.extensions["configs"].mutate(lambda raw: raw["app_schedule"]["week"].update(default={"blocks": []}))
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "Or start from a plan:" in page and 'class="small plan-change"' not in page
