"""Several installed apps, each TV's app schedule, and rotation swaps."""

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from time import monotonic
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from test_controller import Screen, at
from test_television import Art, tv
from test_web import PASSWORD, auth, csrf

from art_frames import apps
from art_frames.config import (
    TURN_SECONDS,
    AppSchedule,
    AppWindow,
    Config,
    ConfigStore,
    factor_week,
    hhmm,
    legacy_days,
    minutes,
    private_write,
)
from art_frames.controller import AppsFailed, Controller
from art_frames.rotation import Hold, Rotation
from art_frames.television import DisplayError
from art_frames.web import create_app

ART_MODE = {"paired": True, "known": True, "awake": True, "playing": False}
LOCATION = {"key": "location", "label": "Location", "type": "text"}


def write(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest))
    return path


@pytest.fixture
def three_apps(tmp_path, monkeypatch):
    """Morning in sites/installed as today, plus News and Photos in sites/apps/<id>."""
    sites = tmp_path / "sites"
    first = write(sites / "installed" / "layout.json", {"id": "morning", "name": "Morning", "artwork": True})
    write(sites / "apps" / "news" / "layout.json", {"id": "news", "name": "News", "settings": [LOCATION]})
    write(sites / "apps" / "photos" / "layout.json", {"id": "photos", "name": "Photos"})
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(first))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    monkeypatch.setenv("DAYFRAME_SITES_DIR", str(sites))
    monkeypatch.setenv("DAYFRAME_APPS", "news=news-scope photos=photos-scope")
    return sites


class RotationScreen(Screen):
    """A fake TV that keeps the images it is told to keep and can select a stored one."""

    def __init__(self):
        super().__init__()
        self.awake = True
        self.selected = []
        self.select_calls = 0  # every select() call, also one the TV refuses
        self.busy = False  # True while an upload or a selection runs
        self.overlaps = 0
        self.during_publish = None

    def publish(self, image, owned_ids, record_upload, keep=()):
        self.overlaps += self.busy
        self.busy = True
        try:
            if self.playing or not self.awake:
                return None
            if self.during_publish:
                self.during_publish()
            self.publications.append(image)
            content_id = f"img-{len(self.publications)}"
            record_upload(content_id)
            return content_id, [*[item for item in owned_ids if item in keep], content_id], True
        finally:
            self.busy = False

    def select(self, content_id):
        self.select_calls += 1
        self.overlaps += self.busy
        if not self.awake or self.playing:
            raise DisplayError("not-art-mode")
        self.selected.append(content_id)
        return True


def rest(*ids, windows=(), swap=TURN_SECONDS):
    """An app schedule where the apps `ids` show wherever no older time rule (`windows`) does.

    Artwork is the base layer now, and the saved "rest of the time" is ignored. A test that needs apps
    outside its rules gets them as blocks that fill each day's gaps, so the TV shows the same as before.
    """
    days = []
    for blocks in legacy_days([AppWindow(**window) for window in windows]):
        spans = sorted((minutes(block["start"]), minutes(block["end"])) for block in blocks)
        gaps, reach = [], 0
        for begin, end in spans:
            if begin > reach:
                gaps.append((reach, begin))
            reach = max(reach, end)
        if reach < 1440:
            gaps.append((reach, 1440))
        filled = [{"app": app, "start": hhmm(begin), "end": "24:00" if end == 1440 else hhmm(end)}
                  for begin, end in gaps for app in ids]
        days.append({"blocks": blocks + filled, "wake": None})
    return {"week": factor_week(days, swap)}


def rotation(configs, library, *, refresh=20, swap=60, windows=(), default=None):
    ids = (default or {"apps": ["news", "photos"]})["apps"]
    cfg = Config(
        site={"providers": [], "refresh_minutes": refresh},
        tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"},
        app_schedule=rest(*ids, windows=windows, swap=swap),
    )
    configs.save(cfg)
    screen = RotationScreen()
    renders = []

    def capture(cfg, library, now, *, app=None, **kwargs):
        renders.append(app.id if app else None)
        return f"{app.id if app else 'site'}:{now:%H%M}".encode()

    controller = Controller(configs, library, tv_factory=lambda _: screen, capture=capture)
    return controller, screen, renders


def stored(library, configs):
    return library.state("app_images:" + configs.load().tv.identity) or {}


# Config validation

def test_schedule_rejects_overlaps_bad_times_and_bad_rotations():
    news = {"apps": ["news"]}
    with pytest.raises(ValidationError, match="These time rules overlap"):
        AppSchedule(windows=[{**news, "start": "07:00", "end": "10:00"}, {**news, "start": "09:00", "end": "11:00"}])
    with pytest.raises(ValidationError, match="These time rules overlap"):  # one crosses midnight into the other
        AppSchedule(windows=[{**news, "start": "22:00", "end": "02:00"}, {**news, "start": "01:00", "end": "03:00"}])
    with pytest.raises(ValidationError, match="These time rules overlap"):  # Sunday night runs into Monday morning
        AppSchedule(windows=[{**news, "start": "23:00", "end": "06:00", "days": [6]},
                             {**news, "start": "05:00", "end": "07:00", "days": [0]}])
    for start, end in (("25:00", "10:00"), ("7am", "10:00"), ("07:00", "07:00")):
        with pytest.raises(ValidationError):
            AppSchedule(windows=[{**news, "start": start, "end": end}])
    # Two or more apps rotate. The stored "rotate" flag of older saves is read and ignored.
    assert AppSchedule(default={"apps": ["news", "photos"]}).default.rotate
    assert not AppSchedule(default={"apps": ["news"], "rotate": True}).default.rotate
    assert "rotate" not in AppSchedule(default={"apps": ["news", "photos"], "rotate": True}).model_dump()["default"]
    with pytest.raises(ValidationError):
        AppSchedule(default={"apps": ["news", "photos"], "rotate": True, "swap_seconds": 29})
    with pytest.raises(ValidationError):
        AppSchedule(windows=[{**news, "start": "07:00", "end": "10:00", "days": []}])
    # Touching windows and windows on other days do not overlap.
    AppSchedule(windows=[{**news, "start": "07:00", "end": "10:00"}, {**news, "start": "10:00", "end": "07:00"}])
    AppSchedule(windows=[{**news, "start": "07:00", "end": "10:00", "days": [0]},
                         {**news, "start": "08:00", "end": "09:00", "days": [1]}])
    assert AppSchedule(default={"apps": ["news", "photos"], "rotate": True}).default.swap_seconds == 60


def test_a_saved_single_app_config_shows_artwork_all_day_on_every_tv(tmp_path, monkeypatch):
    sites = tmp_path / "sites"
    first = write(sites / "installed" / "layout.json", {"id": "morning", "name": "Morning"})
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(first))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    monkeypatch.delenv("DAYFRAME_APPS", raising=False)
    old = Config(displays=[{"id": "a" * 32, "name": "Den"}]).model_dump()
    for item in (old, old["displays"][0]):
        del item["app_schedule"]
    (tmp_path / "settings.json").write_text(json.dumps(old))
    cfg = ConfigStore(tmp_path).load()
    assert cfg.app_schedule == AppSchedule() == cfg.displays[0].app_schedule
    [morning] = apps.installed()
    assert (morning.id, morning.scope, morning.socket, morning.artwork) == ("morning", "morning", "website.sock", False)
    for moment in (at("00:00"), at("07:30"), at("23:59")):
        plan = apps.plan(cfg, moment)
        # No block: Artwork, the base layer, all day.
        assert plan.apps == [apps.ARTWORK] and not plan.rotate and plan.next_change is None
    # The first app's entry and artwork need come from its App, like any other app's.
    assert (apps.configure(cfg, morning).site.entry, apps.configure(cfg, morning).site.providers) == ("index.html", [])


def test_the_first_app_keeps_its_settings_scope(three_apps, monkeypatch):
    monkeypatch.setenv("DAYFRAME_LAYOUT_CONFIG_ID", "morning-source")
    first, news, photos = apps.installed()
    assert (first.id, first.scope, first.artwork) == ("morning", "morning-source", True)
    assert (news.scope, news.socket) == ("news-scope", "/run/dayframe-apps/news/site.sock")
    assert first.store(three_apps).path(first.manifest).parent.name == "morning-source"
    assert news.store(three_apps).path(news.manifest).parent.name == "news-scope"


def test_a_broken_extra_app_is_left_out(three_apps, monkeypatch):
    (three_apps / "apps" / "photos" / "layout.json").write_text('{"id": "other", "name": "Other"}')
    monkeypatch.setenv("DAYFRAME_APPS", "news=news-scope photos=photos-scope BAD=x")
    assert [app.id for app in apps.installed()] == ["morning", "news"]


# Slot resolution

def test_slots_change_at_their_boundaries_and_follow_their_days(three_apps):
    cfg = Config(app_schedule={
        "default": {"apps": ["morning"]},
        "windows": [{"apps": ["news"], "start": "07:00", "end": "10:00", "days": [0, 1, 2, 3, 4]},
                    {"apps": ["news", "photos"], "rotate": True, "start": "22:00", "end": "02:00", "days": [6]}],
    })
    names = lambda moment: [app.id for app in apps.plan(cfg, moment).apps]  # noqa: E731
    monday = at("06:59")  # 2026-10-26 is a Monday
    assert monday.weekday() == 0
    assert names(monday) == ["artwork"]  # the saved default is ignored: Artwork shows outside the blocks
    assert names(at("07:00")) == ["news"]
    assert names(at("09:59")) == ["news"]
    assert names(at("10:00")) == ["artwork"]
    saturday = at("08:00") + timedelta(days=5)
    assert names(saturday) == ["artwork"]  # the window runs on weekdays only
    sunday_night = at("22:00") + timedelta(days=6)
    plan = apps.plan(cfg, sunday_night)
    assert plan.rotate and [app.id for app in plan.apps] == ["news", "photos"]
    # It runs past midnight into Monday morning, which is a new week.
    assert apps.plan(cfg, at("01:59")).rotate
    assert not apps.plan(cfg, at("02:00")).rotate
    assert names(sunday_night - timedelta(minutes=1)) == ["artwork"]
    # The next change and what it shows.
    plan = apps.plan(cfg, monday)
    assert plan.next_change == at("07:00") and [app.id for app in plan.next_apps] == ["news"]
    assert apps.plan(cfg, at("08:00")).next_change == at("10:00")


def test_a_missing_block_app_falls_back_to_artwork(three_apps):
    cfg = Config(app_schedule=rest("weather"))
    plan = apps.plan(cfg, at("08:00"))
    assert [app.id for app in plan.apps] == ["artwork"] and plan.missing == ["weather"] and plan.fallback


# Rotation

def test_a_swap_selects_the_stored_image_without_a_render(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    assert controller.tick(at("18:00")) == "published"
    assert controller.tick(at("18:00").replace(second=30)) == "published"
    assert renders == ["news", "photos"]
    images = stored(library, configs)
    assert {app: entry["content_id"] for app, entry in images.items()} == {"news": "img-1", "photos": "img-2"}
    assert library.state("owned_images:" + configs.load().tv.identity) == ["img-1", "img-2"]  # one per app
    library.set_state("tv_status", ART_MODE)
    assert controller.swap_if_due(at("18:01")) is None  # 30 s after Photos went up: not due yet
    assert controller.swap_if_due(at("18:01").replace(second=30)) == "swapped"
    assert screen.selected == ["img-1"] and renders == ["news", "photos"] and len(screen.publications) == 2
    assert controller.swap_if_due(at("18:02").replace(second=30)) == "swapped"
    assert screen.selected == ["img-1", "img-2"]
    assert library.state("swaps:" + configs.load().tv.identity)["done"] == 2
    assert renders == ["news", "photos"]
    # A confirmed swap records what the TV shows, so the Now page never calls the old picture confirmed.
    shown = library.state("last_display")
    assert shown["app"] == "photos" and shown["image_sha256"] == images["photos"]["image_sha256"]
    assert shown["at"] == at("18:02").replace(second=30).isoformat() and shown["device"] == configs.load().tv.identity


def test_a_refused_swap_records_no_display(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    before = library.state("last_display")
    library.set_state("tv_status", ART_MODE)
    screen.select = lambda content_id: False  # the TV does not confirm the selection
    assert controller.swap_if_due(at("18:01").replace(second=30)) == "failed"
    assert library.state("last_display") == before


@pytest.mark.parametrize("status", [
    {**ART_MODE, "awake": False},  # standby
    {**ART_MODE, "playing": True},  # watching a programme
    {**ART_MODE, "known": False},  # unknown
    {},
])
def test_a_swap_skips_silently_unless_the_tv_is_in_art_mode(three_apps, configs, library, status, caplog):
    controller, screen, renders = rotation(configs, library)
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    library.set_state("tv_status", status)
    screen.wakes = 0
    assert controller.swap_if_due(at("18:02")) == "skipped"
    # The stored status alone stops the swap: nothing is sent to the TV.
    assert screen.select_calls == 0 and screen.wakes == 0 and len(screen.publications) == 2
    assert caplog.text == ""


def test_a_swap_the_tv_refuses_is_not_retried(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    library.set_state("tv_status", ART_MODE)
    screen.awake = False  # the TV went to standby after the last status reading
    assert controller.swap_if_due(at("18:02")) == "skipped"
    assert controller.swap_if_due(at("18:02").replace(second=5)) is None  # no retry until the next swap time
    assert screen.selected == [] and screen.wakes == 0


def test_swaps_and_uploads_never_overlap(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.tick(at("18:00"))
    library.set_state("tv_status", ART_MODE)
    results = []

    def swap_now():
        thread = threading.Thread(target=lambda: results.append(controller.swap_if_due(at("18:05"))))
        thread.start()
        thread.join()
    screen.during_publish = swap_now
    assert controller.tick(at("18:00").replace(second=30)) == "published"
    assert results == ["busy"] and screen.selected == [] and screen.overlaps == 0
    # A held TV lock (any other TV operation) also makes the swap skip, without waiting.
    screen.during_publish = None
    with controller.lock:
        swap_now()
    assert results == ["busy", "busy"] and screen.selected == []
    assert controller.swap_if_due(at("18:05")) == "swapped" and screen.overlaps == 0


def test_each_app_renders_on_the_render_interval_never_on_a_swap(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library, refresh=20, swap=30)
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    library.set_state("tv_status", ART_MODE)
    moment = at("18:01")
    for _ in range(30):  # 15 minutes of swaps and render passes
        controller.swap_if_due(moment)
        controller.tick(moment)
        moment += timedelta(seconds=30)
    assert renders == ["news", "photos"]
    assert len(screen.selected) >= 25
    controller.tick(at("18:20"))  # a new render period: one app renders per pass
    controller.tick(at("18:20").replace(second=30))
    assert renders == ["news", "photos", "news", "photos"]


def test_a_rotation_skips_identical_bytes_per_app(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.capture = lambda cfg, library, now, *, app=None, **kw: renders.append(app.id) or app.id.encode()
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    assert controller.tick(at("18:20")) == "same-image"
    assert controller.tick(at("18:20").replace(second=30)) == "same-image"
    assert len(screen.publications) == 2 and renders == ["news", "photos", "news", "photos"]


def test_a_new_slot_shows_a_fresh_stored_image_without_a_render(three_apps, configs, library):
    window = {"apps": ["news"], "start": "18:10", "end": "19:00"}
    controller, screen, renders = rotation(configs, library, refresh=60, windows=[window])
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    assert controller.tick(at("18:10")) == "selected"
    assert screen.selected == ["img-1"] and renders == ["news", "photos"]
    assert controller.tick(at("18:11")) == "unchanged"
    # Photos is not in use now: its image goes at the next cleanup.
    assert set(stored(library, configs)) == {"news"}


def test_the_wake_handoff_renders_the_app_active_at_wake_time(three_apps, configs, library):
    window = {"apps": ["news"], "start": "07:00", "end": "09:00"}
    controller, screen, renders = rotation(configs, library, windows=[window], default={"apps": ["photos"]})
    controller._prepare(configs.load(), at("07:00"), 0)
    assert renders == ["news"]


def test_apps_no_longer_shown_lose_their_stored_image(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.tick(at("18:00"))
    controller.tick(at("18:00").replace(second=30))
    configs.mutate(lambda raw: raw.update(app_schedule=rest("morning")))
    assert controller.tick(at("18:01")) == "published"
    assert renders[-1] is None or renders[-1] == "morning"
    assert stored(library, configs).keys() == {"morning"}
    assert library.state("owned_images:" + configs.load().tv.identity) == ["img-3"]


# Web: settings form, per-app settings pages, Overview and preview

def tv_form(client, **fields):
    return {"csrf": csrf(client), "tv_name": "Frame 1", "providers": "art", "art_enabled": "on",
            "sources": ["met"], "schedule": "1", "schedule.default.app": "morning", **fields}


def test_tv_settings_no_longer_change_the_schedule_or_the_turn_on_time(three_apps, tmp_path):
    """The Schedule page owns the schedule and the turn-on times. The older /settings fields change nothing."""
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    configs = app.extensions["configs"]
    configs.mutate(lambda raw: (raw["tv"].update(ip="192.168.1.50", mac="AA:BB:CC:DD:EE:01"), raw.update(app_schedule={
        "default": {"apps": ["morning"]}, "week": {"default": {"blocks": [], "wake": "06:45"}}})))
    before = configs.load().app_schedule
    client = app.test_client()
    response = client.post("/settings", headers=auth(), data={
        **tv_form(client), "tv_ip": "192.168.1.50", "tv_mac": "AA:BB:CC:DD:EE:01",
        "window.rows": ["w0"], "window.w0.start": "07:00", "window.w0.end": "10:00", "window.w0.days": ["0"],
        "window.w0.app": "news", "wake_at": "09:00", "wake_enabled": "on", "days": ["0"]})
    assert response.status_code == 302
    assert configs.load().app_schedule == before


def test_each_app_has_its_own_settings_page(three_apps, tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    page = client.get("/", headers=auth()).get_data(as_text=True)
    for name, url in (("Morning", "/layouts/morning"), ("News", "/layouts/news"), ("Photos", "/layouts/photos")):
        assert f'href="{url}?tv=main"' in page and name in page
    news = client.get("/layouts/news", headers=auth())
    assert news.status_code == 200 and 'name="location"' in news.get_data(as_text=True)
    assert client.get("/layouts/weather", headers=auth()).status_code == 404
    store = apps.find("news").store(tmp_path / "data")
    response = client.post("/layouts/news", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "location": "Lisbon"})
    assert response.headers["Location"].startswith("/layouts/news")
    saved = json.loads((tmp_path / "data" / "layout-settings" / "news-scope" / "config.json").read_text())
    assert saved == {"location": "Lisbon"}
    assert not (tmp_path / "data" / "layout-settings" / "morning").exists()


def test_overview_shows_the_rotation_and_the_next_change(three_apps, tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    configs = app.extensions["configs"]
    configs.mutate(lambda raw: raw.update(app_schedule=rest("news", "photos", swap=60, windows=[
        {"apps": ["morning"], "start": "00:00", "end": "00:01", "days": [0, 1, 2, 3, 4, 5, 6]}])))
    page = app.test_client().get("/tv/main", headers=auth()).get_data(as_text=True)
    assert "News and Photos take turns, switching every minute" in page
    assert '<p class="now-label">Next</p>' in page and re.search(r"Morning (at|on \w{3} at) 12:00 AM", page)
    assert 'id="preview-dialog"' in page and 'name="app"' in page


def test_preview_renders_the_chosen_app(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.preview_site(app_id="photos")
    controller.preview_site()
    assert renders == ["photos", "news"]
    with pytest.raises(ValueError):
        controller.preview_site(app_id="weather")


@pytest.mark.parametrize("power,mode", [("standby", "on"), ("off", "on"), ("on", "off"), (None, "on")])
def test_tv_select_runs_only_in_confirmed_art_mode(tmp_path, monkeypatch, power, mode):
    device = tv(tmp_path)
    private_write(device.token, b"synthetic-token-value")
    art = Art()
    art.get_artmode = lambda: mode

    def info():
        if power is None:
            raise OSError("REST endpoint failed")  # an unknown state never permits a change
        return {"device": {"PowerState": power}}
    monkeypatch.setattr(device, "remote", lambda **kw: SimpleNamespace(art=lambda **_: art, rest_device_info=info))
    with pytest.raises(DisplayError):
        device.select("stored-id")
    assert not [call for call in art.calls if call[0] in {"select", "upload", "delete"}]
    art.get_artmode = lambda: "on"
    monkeypatch.setattr(device, "remote", lambda **kw: SimpleNamespace(
        art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    art.get_current = lambda: {"content_id": "stored-id"}
    assert device.select("stored-id") is True
    assert ("select", "stored-id", True) in art.calls and not [c for c in art.calls if c[0] == "upload"]


def test_a_window_from_sunday_into_monday_has_no_edge_at_midnight(three_apps):
    cfg = Config(app_schedule={
        "default": {"apps": ["morning"]},
        "windows": [{"apps": ["news"], "start": "23:00", "end": "06:00", "days": [6]}],
    })
    sunday_night = at("23:30") + timedelta(days=6)
    # Monday 00:00 is inside the window: the next change is when it ends.
    assert apps.plan(cfg, sunday_night).next_change == at("06:00") + timedelta(days=7)
    assert apps.plan(cfg, at("03:00")).next_change == at("06:00")


def test_a_broken_app_is_logged_once_until_it_changes(three_apps, monkeypatch, caplog):
    photos = three_apps / "apps" / "photos" / "layout.json"
    photos.write_text('{"id": "other", "name": "Other"}')
    monkeypatch.setenv("DAYFRAME_APPS", "news=news-scope photos=photos-scope BAD=x")
    monkeypatch.setattr(apps, "_warned", set())  # forget what earlier tests logged
    for _ in range(3):  # the swap timer reads the apps every 5 s per TV
        assert [app.id for app in apps.installed()] == ["morning", "news"]
    assert caplog.text.count("Layout photos is unavailable") == 1
    assert caplog.text.count("Ignored an invalid entry") == 1
    photos.write_text('{"id": "another", "name": "Another"}')  # a new failure logs again
    apps.installed()
    assert caplog.text.count("Layout photos is unavailable") == 2


def badge(key, text):
    return {"key": key, "type": "boolean", "default": True, "label": text, "badge": text}


def test_home_tile_badges_follow_the_apps_the_tv_shows(three_apps, tmp_path):
    write(three_apps / "installed" / "layout.json",
          {"id": "morning", "name": "Morning", "display_settings": [badge("calendar", "Calendar off")]})
    write(three_apps / "apps" / "news" / "layout.json",
          {"id": "news", "name": "News", "display_settings": [badge("weather", "Weather off")]})
    write(three_apps / "apps" / "photos" / "layout.json",
          {"id": "photos", "name": "Photos", "display_settings": [badge("faces", "Faces off"), badge("weather", "Weather off")]})
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    configs, client = app.extensions["configs"], app.test_client()
    values = {"morning": {"calendar": False}, "news-scope": {"weather": False},
              "photos-scope": {"faces": False, "weather": False}}
    configs.mutate(lambda raw: raw.update(app_settings=values, app_schedule=rest("news")))
    page = client.get("/", headers=auth()).get_data(as_text=True)
    assert "Weather off" in page and "Calendar off" not in page and "Faces off" not in page
    # A rotation shows each of its apps' badges once.
    configs.mutate(lambda raw: raw.update(app_schedule=rest("news", "photos")))
    page = client.get("/", headers=auth()).get_data(as_text=True)
    assert page.count("Weather off") == 1 and "Faces off" in page and "Calendar off" not in page


def test_settings_links_name_every_app_and_the_bare_url_opens_the_first(three_apps, tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    response = client.get("/site/settings", headers=auth())
    # Without ?app= the first app can change, so the move is not permanent.
    assert response.status_code == 302 and response.headers["Location"] == "/layouts/morning?tv=main"
    assert client.get("/site/settings?app=news", headers=auth()).headers["Location"] == "/layouts/news?tv=main"
    assert client.get("/site/settings?app=weather", headers=auth()).status_code == 404
    page = client.get("/layouts/morning", headers=auth()).get_data(as_text=True)
    assert 'href="/layouts/morning?tv=main"' in page and "/site/settings" not in page


# A broken app, stale images, Screen off, holds

def device_of(configs):
    return configs.load().tv.identity


def two_images(controller):
    assert controller.tick(at("18:00")) == "published"
    assert controller.tick(at("18:00").replace(second=30)) == "published"


def test_a_broken_app_does_not_stop_the_rotation(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    capture = controller.capture

    def broken_news(cfg, library, now, *, app=None, **kwargs):
        if app.id == "news":
            raise ValueError("https://calendar.example/private-token")  # not Art Frames' own text
        return capture(cfg, library, now, app=app, **kwargs)
    controller.capture = broken_news
    assert controller.tick(at("18:00")) == "published"  # News fails; Photos renders in the same pass
    assert set(stored(library, configs)) == {"photos"}
    assert library.state("render_backoff") is None  # one broken app does not slow down the TV
    # News waits on its own backoff, so the next passes do not try it every 30 s.
    assert controller.tick(at("18:00").replace(second=30)) == "backoff"
    health = controller.rotation_status(at("18:01")).apps
    assert health["news"].last_error == "The layout could not be rendered."  # never the library's text
    assert health["news"].last_error_at == at("18:00") and health["news"].last_success is None
    assert health["photos"].last_success == at("18:00") and health["photos"].last_error is None
    assert controller.rotation_status(at("18:01")).to_json()["apps"]["news"]["last_error_at"] == at("18:00").isoformat()
    # After its backoff News is tried again, and it works.
    controller.capture = capture
    assert controller.tick(at("18:02")) == "published"
    assert set(stored(library, configs)) == {"news", "photos"}
    assert controller.rotation_status(at("18:03")).apps["news"].last_success == at("18:02")


def test_a_rotation_raises_only_when_no_app_renders(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.capture = lambda *a, **kw: (_ for _ in ()).throw(ValueError("Website could not be rendered"))
    with pytest.raises(AppsFailed) as failed:
        controller.tick(at("18:00"))
    assert isinstance(failed.value.__cause__, ValueError)
    assert controller.run_once() == "failed"  # now, after each app's own backoff
    assert library.state("render_backoff") is None and screen.publications == []


def test_a_swap_never_selects_an_image_of_an_old_revision(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library, refresh=20)
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    controller._refresh_all(device_of(configs))  # what a wake does: every stored image is out of date
    assert controller.swap_if_due(at("18:02")) is None
    assert screen.selected == []
    # A new render period also makes last period's images out of date.
    controller.tick(at("18:20"))  # renders News for this period
    assert controller.swap_if_due(at("18:22")) is None and screen.selected == []
    controller.tick(at("18:20").replace(second=30))  # and Photos
    assert controller.swap_if_due(at("18:22")) == "swapped" and screen.selected == ["img-3"]


def test_no_swap_while_a_wake_handoff_waits(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    controller._handoff = {"device": device_of(configs), "status": ART_MODE, "at": monotonic(), "phases": {}}
    assert controller.swap_if_due(at("18:02")) == "skipped" and screen.select_calls == 0
    controller._handoff["at"] -= 60  # an expired handoff no longer stops swaps
    assert controller.swap_if_due(at("18:02")) == "swapped"


def test_a_scheduler_pass_waits_for_a_running_swap(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    controller._swapping.set()  # a swap holds the TV lock for a moment
    controller.lock.acquire()
    threading.Timer(0.3, controller.lock.release).start()
    assert controller.tick(at("18:20")) != "busy"
    controller._swapping.clear()


def test_no_swap_right_after_screen_off(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    device = device_of(configs)
    library.set_state("tv_status", ART_MODE)
    off = at("18:01")
    library.set_state("screen_off_at:" + device, off.isoformat())
    library.set_state("tv_status_at", (off - timedelta(minutes=1)).isoformat())
    assert controller.swap_if_due(off + timedelta(seconds=30)) == "skipped"
    # 60 s later, the last status reading is still from before Screen off: wait for a new one.
    assert controller.swap_if_due(off + timedelta(seconds=90)) == "skipped"
    library.set_state("tv_status_at", (off + timedelta(seconds=20)).isoformat())
    assert controller.swap_if_due(off + timedelta(seconds=30)) == "skipped"  # a new status, but too soon
    assert screen.select_calls == 0
    assert controller.swap_if_due(off + timedelta(seconds=90)) == "swapped"
    status = controller.rotation_status(off + timedelta(seconds=30))
    assert status.paused == "screen-off"


def test_no_swap_or_upload_while_the_screen_is_blanked(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    device = device_of(configs)
    library.set_state("tv_status", ART_MODE)
    library.set_state("soft_blank:" + device, {"content_id": "img-1", "brightness": 6, "at": at("18:01").isoformat()})
    published = len(screen.publications)
    assert controller.swap_if_due(at("18:02")) == "skipped" and screen.select_calls == 0
    assert controller.rotation_status(at("18:02")).paused == "blanked"
    # A render pass sends nothing either, even a forced one: only Undo blank ends the pause.
    assert controller.tick(at("18:40")) == "blanked" and controller.tick(at("18:41"), force=True) == "blanked"
    assert len(screen.publications) == published and screen.select_calls == 0
    library.delete_state("soft_blank:" + device)
    assert controller.swap_if_due(at("18:02")) == "swapped"


def test_a_bad_blank_record_never_pauses_the_schedule(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    library.set_state("soft_blank:" + device_of(configs), {"brightness": "dim"})
    assert controller.swap_if_due(at("18:02")) == "swapped"


def test_a_preview_sent_during_a_rotation_holds_until_the_next_change(three_apps, configs, library):
    window = {"apps": ["morning"], "start": "20:00", "end": "21:00"}
    controller, screen, renders = rotation(configs, library, windows=[window])
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    image = b"a photos preview"
    private_write(configs.root / "screen-preview.jpg", image)
    private_write(configs.root / "screen-preview.json", json.dumps(
        {"app": "photos", "image_sha256": hashlib.sha256(image).hexdigest()}).encode())
    assert controller.tick(at("18:04"), force=True, preview_revision=hashlib.sha256(image).hexdigest()) == "published"
    status = controller.rotation_status(at("18:05"))
    assert status.paused == "hold" and status.current_app == "photos"
    assert status.hold == Hold("photos", at("20:00")) and status.next_switch == at("20:00")
    # Neither a swap nor a render pass undoes the preview.
    assert controller.swap_if_due(at("18:05")) is None and screen.selected == []
    assert controller.tick(at("18:05")) == "unchanged" and len(screen.publications) == 3
    assert library.state("last_upload")["app"] == "photos"
    # A preview hold lasts at most 4 hours without a schedule change.
    configs.mutate(lambda raw: raw.update(app_schedule=rest("news", "photos")))
    private_write(configs.root / "screen-preview.jpg", image + b"!")
    private_write(configs.root / "screen-preview.json", json.dumps(
        {"app": "news", "image_sha256": hashlib.sha256(image + b"!").hexdigest()}).encode())
    controller.send_preview(hashlib.sha256(image + b"!").hexdigest())
    assert controller.rotation_status().hold.app == "news"
    until = controller.rotation_status().hold.until
    assert timedelta(hours=3, minutes=59) < until - datetime.now(until.tzinfo) <= timedelta(hours=4)


def test_a_hold_ends_by_itself_and_by_resume(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    assert controller.show_app("morning", None, at("18:01")) == "published"
    assert renders[-1] == "morning" and library.state("last_upload")["app"] == "morning"
    held = controller.rotation_status()
    assert held.hold == Hold("morning") and held.next_switch is None
    assert controller.swap_if_due(at("18:02")) is None and screen.selected == []
    controller.resume()
    assert controller.rotation_status().hold is None
    assert controller.swap_if_due(at("18:02")) == "swapped"
    # A hold with an end stops at that time.
    Rotation(library, device_of(configs)).set_hold(Hold("morning", at("18:10")))
    assert controller.rotation_status(at("18:09")).hold is not None
    assert controller.rotation_status(at("18:10")).hold is None


def test_a_hold_of_a_removed_app_is_dropped(three_apps, configs, library, monkeypatch):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    rotation_state = Rotation(library, device_of(configs))
    rotation_state.set_hold(Hold("photos"))  # until resumed
    monkeypatch.setenv("DAYFRAME_APPS", "news=news-scope")
    controller.tick(at("18:20"))
    assert rotation_state._hold() is None  # removed, so it does not come back when Photos does
    monkeypatch.setenv("DAYFRAME_APPS", "news=news-scope photos=photos-scope")
    assert rotation_state.active_hold(at("18:21")) is None


def test_show_app_never_wakes_the_tv_or_holds_without_a_send(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    screen.awake = False
    assert controller.show_app("photos", None) == "asleep"
    assert screen.wakes == 0 and screen.publications == [] and controller.rotation_status().hold is None


def test_each_render_says_why_it_renders(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    purposes = []
    capture = controller.capture
    controller.capture = lambda cfg, library, now, *, app=None, purpose=None, **kw: (
        purposes.append(purpose) or capture(cfg, library, now, app=app))
    two_images(controller)
    controller.tick(at("18:20"))  # News renders again while Photos shows
    controller._prepare(configs.load(), at("07:00"), 0)
    controller.preview_site(app_id="news")
    configs.mutate(lambda raw: raw.update(app_schedule=rest("morning")))
    controller.tick(at("18:21"))
    assert purposes == ["rotation", "rotation", "rotation", "prepare", "preview", "scheduled"]
    assert controller.preview_app() == "news"


def test_the_activity_record_names_the_app_from_the_public_property(three_apps, tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    library = app.extensions["library"]
    ctl = SimpleNamespace(last_app="News", library=library, _reported=None)  # no private _last_app
    text, *_ = app.extensions["jobs"].describe("update", "published", ctl, False)
    assert text.startswith("News: ")


def test_show_now_and_resume_routes(three_apps, tmp_path, monkeypatch):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client, controller = app.test_client(), app.extensions["controller"]
    calls = []
    monkeypatch.setattr(controller, "show_app", lambda app_id, until: calls.append((app_id, until)) or "published")
    monkeypatch.setattr(controller, "resume", lambda: calls.append("resume"))
    for hold in ("resumed", "hour", "next-change"):
        response = client.post("/screen/show-app", headers=auth(), data={"csrf": csrf(client), "app": "photos", "hold": hold})
        assert response.status_code == 302
    assert calls[0] == ("photos", None)
    assert timedelta(minutes=59) < calls[1][1] - datetime.now(calls[1][1].tzinfo) <= timedelta(hours=1)
    assert calls[2][1] - datetime.now(calls[2][1].tzinfo) <= timedelta(hours=4)  # no schedule change: 4 hours
    assert client.post("/screen/show-app", headers=auth(), data={"csrf": csrf(client), "app": "weather"}).status_code == 302
    assert len(calls) == 3  # not an installed app: nothing runs
    client.post("/screen/resume", headers=auth(), data={"csrf": csrf(client)})
    assert calls[-1] == "resume"
    assert client.post("/screen/resume", headers=auth()).status_code == 400  # CSRF


# Security: app entries, app settings pages

@pytest.mark.parametrize("entry", ["../news=news-scope", "news/../x=scope", "News=scope", "news=../scope",
                                   "news=Scope", "news", "news=", "=scope", "morning=morning-copy", "1news=scope"])
def test_bad_app_entries_are_ignored(three_apps, monkeypatch, entry):
    monkeypatch.setenv("DAYFRAME_APPS", f"photos=photos-scope {entry}")
    assert [app.id for app in apps.installed()] == ["morning", "photos"]


SECRET = {"key": "token", "label": "Token", "type": "secret"}


@pytest.fixture
def secret_app(three_apps, tmp_path):
    write(three_apps / "apps" / "news" / "layout.json", {"id": "news", "name": "News", "settings": [LOCATION, SECRET]})
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    return app, app.test_client()


def test_a_secret_of_an_extra_app_is_masked(secret_app, tmp_path):
    app, client = secret_app
    store = apps.find("news").store(tmp_path / "data")
    client.post("/site/settings?app=news", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "location": "Lisbon", "token": "SYNTHETIC-TOKEN"})
    assert store.values(store.manifest())["token"] == "SYNTHETIC-TOKEN"
    page = client.get("/layouts/news", headers=auth()).get_data(as_text=True)
    assert "SYNTHETIC-TOKEN" not in page and "Art Frames keeps it private" in page and "Lisbon" in page


def test_a_form_of_one_app_cannot_save_another_app(secret_app, tmp_path):
    app, client = secret_app
    news = apps.find("news").store(tmp_path / "data")
    response = client.post("/site/settings?app=photos", headers=auth(), follow_redirects=True, data={
        "csrf": csrf(client), "manifest": news.fingerprint(news.manifest()), "token": "SYNTHETIC-TOKEN"})
    assert "SYNTHETIC-TOKEN" not in response.get_data(as_text=True)
    assert not (tmp_path / "data" / "layout-settings" / "photos-scope").exists()
    assert not (tmp_path / "data" / "layout-settings" / "news-scope").exists()


def test_app_settings_need_csrf(secret_app, tmp_path):
    app, client = secret_app
    news = apps.find("news").store(tmp_path / "data")
    client.get("/site/settings?app=news", headers=auth())
    response = client.post("/site/settings?app=news", headers=auth(), data={
        "manifest": news.fingerprint(news.manifest()), "location": "Elsewhere"})
    assert response.status_code == 400
    assert not (tmp_path / "data" / "layout-settings" / "news-scope").exists()


# Each TV's default app, and the browser profile

def test_a_tv_without_blocks_shows_artwork_and_ignores_a_saved_default(three_apps, configs, library):
    from art_frames.displays import DisplayConfig, DisplayLibrary
    configs.save(Config(tv={"ip": "192.168.1.50"}, displays=[{"id": "a" * 32, "name": "Den"}],
                        app_schedule={"default": {"apps": ["morning"]}}))
    den = DisplayConfig(configs, "a" * 32)
    screen = RotationScreen()
    renders = []
    controllers = [Controller(store, lib, tv_factory=lambda _: screen,
                              capture=lambda *a, app=None, **k: renders.append(app) or b"x")
                   for store, lib in ((configs, library), (den, DisplayLibrary(library, "a" * 32)))]
    for controller in controllers:
        plan = controller.plan(controller.configs.load(), at("08:00"))
        assert plan.apps == [apps.ARTWORK] and plan.key == "default" and not plan.missing
        assert controller.tick(at("08:00")) == "published"
    # Artwork needs no renderer: the website capture never ran, and nothing pinned an app.
    assert renders == [] and len(screen.publications) == 2
    assert library.state("default_app") is None
    assert configs.load().app_schedule.default.apps == ["morning"]  # a saved default stays as it is


def test_a_pass_never_writes_the_settings_file(three_apps, tmp_path, library):
    toml = tmp_path / "dayframe.toml"
    toml.write_text('tv_name = "Lounge"\n[tv]\nip = "192.168.1.50"\n')
    (tmp_path / "data").mkdir()
    store = ConfigStore(tmp_path / "data", toml)
    controller = Controller(store, library, tv_factory=lambda _: RotationScreen(), capture=lambda *a, **k: b"x")
    controller.tick(at("08:00"))
    assert not store.overrides.exists()
    toml.write_text('tv_name = "Den"\n[tv]\nip = "192.168.1.50"\n')
    assert store.load().tv_name == "Den"  # a later edit of the config file still applies


def test_a_partly_missing_rotation_says_which_app_is_missing(three_apps):
    cfg = Config(app_schedule=rest("news", "weather", "photos"))
    plan = apps.plan(cfg, at("08:00"))
    assert [app.id for app in plan.apps] == ["news", "photos"] and plan.missing == ["weather"] and not plan.fallback
    assert apps.plan(Config(), at("08:00")).missing == []


def test_a_browser_profile_runs_no_apps(three_apps, tmp_path, monkeypatch):
    # A stale app entry in .env: the browser profile has no website socket.
    monkeypatch.delenv("DAYFRAME_DEFAULT_SITE_SOCKET")
    monkeypatch.setenv("DAYFRAME_LAYOUT_ENTRY", "morning/index.html")
    assert apps.installed() == [] and not apps.supported()
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    configs, library = app.extensions["configs"], app.extensions["library"]
    cfg = configs.load()
    assert cfg.site.entry == "index.html"  # the stale entry is ignored
    plan = apps.plan(cfg, at("08:00"))
    assert plan.apps == [] and not plan.rotate
    assert Rotation(library, cfg.tv.identity).status(plan, at("08:00")).current_app is None
    client = app.test_client()
    assert client.get("/tv/main", headers=auth()).status_code == 200
    assert "App schedule" not in client.get("/tv/main/settings", headers=auth()).get_data(as_text=True)


def test_the_overview_and_the_swap_timer_use_one_gate(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    controller._handoff = {"device": device_of(configs), "status": ART_MODE, "at": monotonic(), "phases": {}}
    assert controller.swap_if_due(at("18:02")) == "skipped"
    assert controller.rotation_status(at("18:02")).paused == "wake"  # the Overview says why
    controller._handoff["at"] -= 60
    assert controller.rotation_status(at("18:02")).paused is None
    library.set_state("tv_status", {**ART_MODE, "playing": True})
    assert controller.swap_if_due(at("18:02")) == "skipped"
    assert controller.rotation_status(at("18:02")).paused == "watching"


def test_resume_works_between_an_identity_change_and_the_next_pass(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    old = device_of(configs)
    assert controller.show_app("morning", None, at("18:01")) == "published"
    configs.mutate(lambda raw: raw["tv"].update(mac="AA:BB:CC:DD:EE:01"))  # a new identity, no pass yet
    assert device_of(configs) != old and controller.rotation_status().hold == Hold("morning")
    controller.resume()
    assert controller.rotation_status().hold is None and Rotation(library, old)._hold() is None


def test_the_overview_shows_a_hold_on_a_single_app_schedule(three_apps, tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    configs, library = app.extensions["configs"], app.extensions["library"]
    configs.mutate(lambda raw: raw.update(app_schedule=rest("morning")))
    Rotation(library, configs.load().tv.identity).set_hold(Hold("news"))
    page = app.test_client().get("/tv/main", headers=auth()).get_data(as_text=True)
    assert "Holding News until you resume." in page and "Resume schedule" in page


def test_a_preview_of_another_app_stays_on_a_single_app_schedule(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library, default={"apps": ["morning"]})
    assert controller.tick(at("18:00")) == "published"
    image = b"a news preview"
    private_write(configs.root / "screen-preview.jpg", image)
    private_write(configs.root / "screen-preview.json", json.dumps(
        {"app": "news", "image_sha256": hashlib.sha256(image).hexdigest()}).encode())
    assert controller.tick(at("18:01"), force=True, preview_revision=hashlib.sha256(image).hexdigest()) == "published"
    assert controller.tick(at("18:01").replace(second=30)) == "unchanged"
    assert screen.publications[-1] == image and controller.rotation_status(at("18:02")).hold.app == "news"
    # A preview of the app the schedule shows needs no hold.
    controller.resume()
    controller.tick(at("18:02"))
    private_write(configs.root / "screen-preview.jpg", b"a morning preview")
    private_write(configs.root / "screen-preview.json", json.dumps(
        {"app": "morning", "image_sha256": hashlib.sha256(b"a morning preview").hexdigest()}).encode())
    controller.tick(at("18:03"), force=True, preview_revision=hashlib.sha256(b"a morning preview").hexdigest())
    assert controller.rotation_status(at("18:03")).hold is None


def test_a_hold_that_ends_swaps_back_while_another_app_backs_off(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    two_images(controller)
    library.set_state("tv_status", ART_MODE)
    assert controller.swap_if_due(at("18:01").replace(second=30)) == "swapped"  # News, img-1
    assert controller.show_app("photos", at("18:05"), at("18:02")) == "published"
    library.set_state("tv_status", ART_MODE)
    state = Rotation(library, device_of(configs))
    state.drop("photos")
    state.record_failure("photos", "The layout could not be rendered.", at("18:02"), 20)  # Photos waits
    assert controller.swap_if_due(at("18:05").replace(second=5)) == "swapped"
    assert screen.selected[-1] == "img-1"  # News again, not Photos until News's next render


def test_the_settings_never_read_the_first_app_from_the_environment(three_apps, configs, monkeypatch):
    monkeypatch.setenv("DAYFRAME_LAYOUT_ENTRY", "other/index.html")
    monkeypatch.setenv("DAYFRAME_LAYOUT_ARTWORK", "1")
    cfg = configs.load()
    assert (cfg.site.entry, cfg.site.providers) == ("index.html", [])
    first = apps.installed()[0]  # the one source: apps._first()
    assert (apps.configure(cfg, first).site.entry, apps.configure(cfg, first).site.providers) == ("index.html", ["art"])


def test_show_app_needs_auth_and_csrf_and_reads_only_known_holds(three_apps, tmp_path, monkeypatch):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client, controller = app.test_client(), app.extensions["controller"]
    calls = []
    monkeypatch.setattr(controller, "show_app", lambda app_id, until: calls.append((app_id, until)) or "published")
    token = csrf(client)
    assert client.post("/screen/show-app", data={"csrf": token, "app": "news"}).status_code == 401
    assert client.post("/screen/show-app", headers=auth(), data={"app": "news"}).status_code == 400
    assert client.post("/screen/resume", data={"csrf": token}).status_code == 401
    assert calls == []
    client.post("/screen/show-app", headers=auth(), data={"csrf": token, "app": "news", "hold": "forever"})
    [(app_id, until)] = calls
    now = datetime.now(until.tzinfo)
    assert app_id == "news" and timedelta(hours=3, minutes=59) < until - now <= timedelta(hours=4)  # next-change


def test_a_form_token_names_the_settings_scope(three_apps, tmp_path, monkeypatch):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    news = apps.find("news").store(tmp_path / "data")
    old_token = news.fingerprint(news.manifest())
    # The same manifest, now installed under another settings scope.
    monkeypatch.setenv("DAYFRAME_APPS", "news=news-other photos=photos-scope")
    again = apps.find("news").store(tmp_path / "data")
    assert again.fingerprint(again.manifest()) != old_token
    form = {"csrf": csrf(client), "location": "Lisbon"}
    client.post("/site/settings?app=news", headers=auth(), data={**form, "manifest": old_token})
    assert not (tmp_path / "data" / "layout-settings" / "news-other").exists()
    client.post("/site/settings?app=news", headers=auth(), data={**form, "manifest": again.fingerprint(again.manifest())})
    assert again.values(again.manifest())["location"] == "Lisbon"


def test_a_preview_of_the_schedule_app_ends_a_hold_of_another_app(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library, default={"apps": ["morning"]})
    assert controller.tick(at("18:00")) == "published"
    Rotation(library, device_of(configs)).set_hold(Hold("news"))
    image = b"a morning preview"
    private_write(configs.root / "screen-preview.jpg", image)
    private_write(configs.root / "screen-preview.json", json.dumps(
        {"app": "morning", "image_sha256": hashlib.sha256(image).hexdigest()}).encode())
    assert controller.tick(at("18:01"), force=True, preview_revision=hashlib.sha256(image).hexdigest()) == "published"
    assert controller.rotation_status(at("18:02")).hold is None


def test_a_preview_never_writes_the_tv_render_state_and_a_test_records_apart(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    controller.tv_factory = lambda _: pytest.fail("A preview contacted the TV")
    identity = configs.load().tv.identity
    now = datetime.now(ZoneInfo(configs.load().timezone))

    def broken(*args, **kwargs):
        raise RuntimeError("The layout could not be rendered")
    controller.capture = broken
    # A routine preview that fails records nothing: no backoff, no error.
    with pytest.raises(RuntimeError):
        controller.preview_site(app_id="news")
    assert "news" not in Rotation(library, identity).health() and not Rotation(library, identity).failing("news", now)
    # Health's test that fails records a test error only.
    with pytest.raises(RuntimeError):
        controller.preview_site(app_id="news", test=True)
    news = Rotation(library, identity).health()["news"]
    assert news.test_error == "The layout could not be rendered." and news.last_error is None
    assert not Rotation(library, identity).failing("news", now)
    # A TV-path failure stays a failure after a passed test.
    Rotation(library, identity).record_failure("news", "The TV path failed.", now, 20)
    controller.capture = lambda cfg, library, now, *, app=None, **kwargs: b"jpeg"
    controller.preview_site(app_id="news", test=True)
    news = Rotation(library, identity).health()["news"]
    assert news.test_at and news.test_error is None and news.last_success is None
    assert news.last_error == "The TV path failed." and Rotation(library, identity).failing("news", datetime.now(ZoneInfo(configs.load().timezone)))
    assert not screen.publications and not screen.selected


def test_an_app_upload_keeps_one_copy_per_installed_app(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    (configs.root / "tv-upload-weather.jpg").write_bytes(b"an uninstalled app's copy")
    assert controller.tick(at("18:00")) == "published"
    assert controller.tick(at("18:00").replace(second=30)) == "published"
    copies = sorted(path.name for path in configs.root.glob("tv-upload-*.jpg"))
    assert copies == ["tv-upload-news.jpg", "tv-upload-photos.jpg"]
    images = Rotation(library, configs.load().tv.identity).images()
    for app_id in ("news", "photos"):
        assert hashlib.sha256((configs.root / f"tv-upload-{app_id}.jpg").read_bytes()).hexdigest() == images[app_id]["image_sha256"]


def test_a_disk_error_on_the_app_copy_never_breaks_the_upload(three_apps, configs, library, monkeypatch, caplog):
    import art_frames.controller as controller_module
    controller, screen, renders = rotation(configs, library)
    real = controller_module.private_write

    def full(path, data):
        if path.name.startswith("tv-upload-"):
            raise OSError(28, "No space left on device")
        return real(path, data)
    monkeypatch.setattr(controller_module, "private_write", full)
    assert controller.tick(at("18:00")) == "published"
    assert library.state("last_upload")["content_id"] == "img-1" and len(screen.publications) == 1
    assert "No space left on device" in caplog.text and not list(configs.root.glob("tv-upload-*.jpg"))


def test_a_lost_upload_leaves_the_copy_matching_the_stored_image(three_apps, configs, library):
    controller, screen, renders = rotation(configs, library)
    assert controller.tick(at("18:00")) == "published"
    real = screen.publish

    def lost(image, owned_ids, record_upload, keep=()):
        screen.publications.append(image)
        record_upload(f"img-{len(screen.publications)}")
        return None  # the TV left Art Mode before the selection
    screen.publish = lost
    controller.tick(at("18:21"), force=True)
    screen.publish = real
    for app_id, entry in Rotation(library, configs.load().tv.identity).images().items():
        copy = configs.root / f"tv-upload-{app_id}.jpg"
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == entry["image_sha256"]
