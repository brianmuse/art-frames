"""Per-TV mats, per-TV app settings and the "Your TVs" home screen."""

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from test_web import PASSWORD, auth, csrf

from art_frames import apps, site
from art_frames.config import Config, ConfigStore
from art_frames.layouts import Manifest
from art_frames.web import create_app, options_fingerprint

NOW = datetime(2030, 1, 2, 8, tzinfo=UTC)
CALENDAR = {"key": "show_calendar", "type": "boolean", "default": True, "label": "Show the morning calendar", "badge": "Calendar off"}


def first_app():
    return next(iter(apps.installed()), None)


def context(cfg, library):
    return json.loads(site.snapshot(cfg, library, NOW, app=first_app())["/dayframe.json"][1])


def revision(cfg, library):
    return site.revision(cfg, library, NOW, app=first_app())


@pytest.fixture
def app_manifest(tmp_path, monkeypatch):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"id": "morning", "name": "Morning", "artwork": True, "display_settings": [CALENDAR]}))
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(path))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    return path


@pytest.fixture
def two_tvs(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    displays = app.extensions["displays"]
    second = displays.add("Den", "192.168.1.51")
    def no_tv(cfg):
        raise AssertionError("This page must not contact a TV")
    for screen_id in ("main", second):
        displays.get(screen_id).controller.tv_factory = no_tv
    return app, displays, second


def save(client, screen_id, **fields):
    data = {"csrf": csrf(client), "tv_name": "Den" if screen_id != "main" else "Frame 1", "providers": "art",
            "art_enabled": "on", "sources": ["met"], **fields}
    if screen_id != "main":
        data.update(tv_ip="192.168.1.51", shared_site="on")
    return client.post(f"/settings?tv={screen_id}", headers=auth(), data=data)


def test_saved_settings_without_the_new_fields_load_unchanged(tmp_path):
    old = Config(displays=[{"id": "a" * 32, "name": "Den"}]).model_dump()
    for item in (old, old["displays"][0]):
        del item["mats"], item["app_settings"]
    (tmp_path / "settings.json").write_text(json.dumps(old))
    cfg = ConfigStore(tmp_path).load()
    assert cfg.mats == cfg.displays[0].mats == "show"
    assert cfg.app_settings == cfg.displays[0].app_settings == {}
    with pytest.raises(ValueError):
        Config(mats="sometimes")


def test_never_show_mats_is_per_tv_unmatted_and_changes_the_revision(two_tvs, image_bytes):
    app, displays, second = two_tvs
    library = app.extensions["library"]
    piece = library.add(image_bytes, title="Matted")
    library.update(piece, selected=True, fit="contain", x=.5, y=.5, mat_width=.08, mat_color="#112233")
    client = app.test_client()
    main, den = displays.get("main"), displays.get(second)
    before = revision(main.configs.load(), main.library)
    assert save(client, "main", mats="never").status_code == 302
    assert save(client, second, mats="show").status_code == 302
    assert main.configs.load().mats == "never" and den.configs.load().mats == "show"
    assert revision(main.configs.load(), main.library) != before
    plain, framed = context(main.configs.load(), main.library), context(den.configs.load(), den.library)
    assert "viewport" not in plain["art"] and framed["art"]["viewport"]["x"] > 0
    image = site.snapshot(main.configs.load(), main.library, NOW)["/dayframe-art.jpg"][1]
    with Image.open(io.BytesIO(image)) as shown:
        # The painting's own mat color is gone; the corner shows the plain background.
        assert shown.size == (3840, 2160) and shown.getpixel((10, 10))[0] > 200
    assert library.get(piece)["mat_width"] == .08  # the shared artwork row is unchanged
    # The cached render follows a framing change.
    assert library.path_for(piece, "plain").exists()
    library.update(piece, selected=True, fit="crop", x=.5, y=.5)
    assert not library.path_for(piece, "plain").exists()


def test_manifest_display_settings_are_validated():
    base = {"id": "morning", "name": "Morning"}
    assert Manifest.model_validate({**base, "display_settings": [CALENDAR]}).display_settings[0].default is True
    for bad in (
        [{**CALENDAR, "type": "text"}],
        [{**CALENDAR, "default": "yes"}],
        [{**CALENDAR, "colour": "red"}],
        [CALENDAR, CALENDAR],
        [{**CALENDAR, "badge": "x" * 31}],
    ):
        with pytest.raises(ValueError):
            Manifest.model_validate({**base, "display_settings": bad})
    with pytest.raises(ValueError):
        Manifest.model_validate({**base, "settings": [{"key": "show_calendar", "label": "Clash", "type": "text"}],
                                 "display_settings": [CALENDAR]})


def save_app_tvs(client, checked, *, app="morning", tvs=None):
    found = apps.find(app)
    data = {"csrf": csrf(client), "manifest": app, "options": options_fingerprint(found.manifest) if found else "",
            "tvs": tvs or ["main", *checked.keys()]}
    data.update({f"tv.{tv}.show_calendar": "on" for tv, on in checked.items() if on})
    return client.post("/site/tv-settings", headers=auth(), data=data)


def test_app_display_settings_are_per_tv_in_the_snapshot_and_revision(app_manifest, two_tvs):
    app, displays, second = two_tvs
    client = app.test_client()
    main, den = displays.get("main"), displays.get(second)
    # The app's own settings page holds the per-TV switches; TV settings know nothing about the app.
    assert b"Show the morning calendar" not in client.get(f"/tv/{second}/settings", headers=auth()).data
    page = client.get("/site/settings", headers=auth(), follow_redirects=True).text
    assert "On each TV" in page and page.count('Show the morning calendar<span class="sr-only"> on ') == 2 and "Den" in page
    assert context(den.configs.load(), den.library)["display_settings"] == {"show_calendar": True}
    before = revision(den.configs.load(), den.library)
    assert save_app_tvs(client, {"main": True, second: False}, tvs=["main", second]).status_code == 302
    assert context(den.configs.load(), den.library)["display_settings"] == {"show_calendar": False}
    assert context(main.configs.load(), main.library)["display_settings"] == {"show_calendar": True}
    assert revision(den.configs.load(), den.library) != before
    # Unknown stored keys are dropped; a form for another app saves nothing.
    den.configs.mutate(lambda raw: raw["app_settings"]["morning"].update(old_key=True))
    assert context(den.configs.load(), den.library)["display_settings"] == {"show_calendar": False}
    save_app_tvs(client, {second: True}, app="other-app", tvs=[second])
    assert den.configs.load().app_settings["morning"]["show_calendar"] is False
    # Saving keeps only the installed app's values, so old layout scopes don't pile up.
    den.configs.mutate(lambda raw: raw["app_settings"].update({"old-app": {"x": True}}))
    save_app_tvs(client, {second: False}, tvs=[second])
    assert den.configs.load().app_settings == {"morning": {"show_calendar": False}}
    assert main.configs.load().app_settings == {"morning": {"show_calendar": True}}  # left out of the form


def test_a_change_on_one_tv_does_not_re_render_another(app_manifest, two_tvs):
    _, displays, second = two_tvs
    main, den = displays.get("main"), displays.get(second)
    before = revision(main.configs.load(), main.library)
    den.configs.mutate(lambda raw: raw.update(mats="never", app_settings={"morning": {"show_calendar": False}}))
    assert revision(main.configs.load(), main.library) == before


def test_a_broken_manifest_does_not_stop_rendering(app_manifest, two_tvs, caplog, monkeypatch):
    _, displays, _ = two_tvs
    main = displays.get("main")
    monkeypatch.setattr(apps, "_warned", set())
    app_manifest.write_text("{not json")
    assert apps.installed() == []  # the TV renders the starter website instead
    assert site.display_settings(main.configs.load(), main.library, None) is None
    assert site.revision(main.configs.load(), main.library, NOW)
    assert "The first installed layout is unavailable" in caplog.text


def test_home_screen_shows_a_tile_per_tv_from_stored_state_only(app_manifest, two_tvs):
    app, displays, second = two_tvs
    client = app.test_client()
    main, den = displays.get("main"), displays.get(second)
    main.library.set_state("tv_status", {"awake": True, "playing": False, "paired": True, "known": True})
    main.library.set_state("tv_status_at", (datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    main.library.set_state("wake_result", "")
    main.configs.mutate(lambda raw: raw.update(mats="never"))
    den.configs.mutate(lambda raw: (raw["app_settings"].update(morning={"show_calendar": False}), raw.update(
        app_schedule={"week": {"default": {"blocks": [{"app": "morning", "start": "00:00", "end": "24:00"}]}}})))
    page = client.get("/", headers=auth()).text
    assert "Home" in page and page.index("Frame 1") < page.index("Den")
    assert f'href="/tv/{second}"' in page and 'href="/tv/main"' in page
    assert "Art Mode" in page and "5 minutes ago" in page
    assert "Not paired" in page and "Not checked yet" in page and "Nothing shown yet" in page
    assert page.count("No mats") == 1 and page.count("Calendar off") == 1
    assert "Add TV" in page and ">Pair Den</a>" in page and 'href="/tvs/new"' in page
    # Each tile has Show now and a Schedule link; its other pages are on its Now page.
    assert f'href="/tv/{second}/schedule"' in page and page.count("Show now") >= 1
    assert "Brightness" in client.get(f"/tv/{second}/picture", headers=auth()).text


def test_sent_image_route_is_authenticated_scoped_and_downscaled(two_tvs):
    app, displays, second = two_tvs
    den = displays.get(second)
    stream = io.BytesIO()
    Image.new("RGB", (3840, 2160), "#335577").save(stream, "JPEG")
    image = stream.getvalue()
    digest = hashlib.sha256(image).hexdigest()
    (den.configs.root / "tv-upload.jpg").write_bytes(image)
    den.library.set_state("last_upload", {"device": den.configs.load().tv.identity, "content_id": "x",
                                          "at": datetime.now(UTC).isoformat(), "image_sha256": digest})
    client = app.test_client()
    url = f"/screen/sent.jpg?tv={second}&v={digest}"
    assert client.get(url).status_code == 401
    response = client.get(url, headers=auth())
    assert "immutable" in response.headers["Cache-Control"]
    with Image.open(io.BytesIO(response.data)) as small:
        assert small.size == (640, 360)
    assert client.get(f"/screen/sent.jpg?tv=main&v={digest}", headers=auth()).status_code == 404
    stale = client.get(f"/screen/sent.jpg?tv={second}&v={'a' * 64}", headers=auth())
    assert stale.status_code == 409 and stale.headers["Cache-Control"] == "no-store"
    page = client.get("/", headers=auth()).text
    assert f"/screen/sent.jpg?tv={second}&amp;v={digest}" in page and "just now" in page
