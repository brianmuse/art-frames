"""The built-in Artwork: the base layer outside app blocks, composed in this process."""

import html
import importlib.util
import io
import re
from pathlib import Path

import pytest
from PIL import Image
from test_app_schedule import RotationScreen, three_apps, write  # noqa: F401  (a fixture)
from test_controller import at
from test_web import PASSWORD, auth, csrf

from art_frames import apps, artwork
from art_frames.config import Config
from art_frames.controller import Controller
from art_frames.library import SIZE
from art_frames.rotation import Hold
from art_frames.web import create_app

MAT = "#202020"
ART = (0x66, 0x88, 0x77)

spec = importlib.util.spec_from_file_location("installer", Path(__file__).parents[1] / "scripts/install-layout.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def picture(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


def near(pixel, color, slack=12):
    return all(abs(a - b) <= slack for a, b in zip(pixel, color, strict=True))


def framed(library, image_bytes, *, fit="contain", mat_width=0.0):
    piece = library.add(image_bytes, title="Piece")
    library.update(piece, selected=True, fit=fit, x=0.5, y=0.5, mat_width=mat_width, mat_color=MAT)
    return piece


# The picture

def test_artwork_is_the_day_s_piece_full_frame_with_its_mat(library, image_bytes):
    framed(library, image_bytes, fit="crop", mat_width=0.05)
    image = picture(artwork.compose(Config(), library, at("08:00")))
    assert image.size == SIZE
    assert near(image.getpixel((5, 5)), (0x20, 0x20, 0x20))  # the mat
    assert near(image.getpixel((SIZE[0] // 2, SIZE[1] // 2)), ART)


def test_a_tv_that_never_shows_mats_gets_the_framing_without_the_mat(library, image_bytes):
    framed(library, image_bytes, fit="crop", mat_width=0.05)
    image = picture(artwork.compose(Config(mats="never"), library, at("08:00")))
    assert image.size == SIZE and near(image.getpixel((5, 5)), ART)  # crop fills the frame


def test_contain_keeps_the_whole_piece_and_crop_fills_the_frame(library, image_bytes):
    piece = framed(library, image_bytes)  # 3:2 in a 16:9 frame
    image = picture(artwork.compose(Config(), library, at("08:00")))
    assert not near(image.getpixel((5, SIZE[1] // 2)), ART) and near(image.getpixel((SIZE[0] // 2, SIZE[1] // 2)), ART)
    library.update(piece, selected=True, fit="crop", x=0.5, y=0.5)
    image = picture(artwork.compose(Config(), library, at("08:00")))
    assert near(image.getpixel((5, SIZE[1] // 2)), ART)


def test_a_missing_or_wrong_size_framing_is_rebuilt_from_the_original_once(library, image_bytes):
    piece = framed(library, image_bytes, fit="crop")
    display = library.path_for(piece)
    display.unlink()
    image = picture(artwork.compose(Config(), library, at("08:00")))
    assert image.size == SIZE and near(image.getpixel((SIZE[0] // 2, SIZE[1] // 2)), ART)
    assert display.exists() and picture(display.read_bytes()).size == SIZE  # the library's file is fixed
    small = io.BytesIO()
    Image.new("RGB", (800, 450), "#ff0000").save(small, "JPEG")
    display.write_bytes(small.getvalue())
    assert picture(artwork.compose(Config(), library, at("08:00"))).size == SIZE
    assert picture(display.read_bytes()).size == SIZE


def test_without_the_original_the_tv_shows_the_calm_frame(library, image_bytes):
    piece = framed(library, image_bytes)
    library.path_for(piece).unlink()
    library.path_for(piece, "original").unlink()
    assert artwork.compose(Config(), library, at("08:00")) == artwork.placeholder(artwork.EMPTY)


@pytest.mark.parametrize("enabled,line", [(True, artwork.EMPTY), (False, artwork.OFF)])
def test_without_artwork_the_tv_shows_a_plain_frame_with_one_line(library, image_bytes, enabled, line):
    if not enabled:
        library.add(image_bytes, title="Piece")  # included, but daily artwork is off
    data = artwork.compose(Config(art={"enabled": enabled}), library, at("08:00"))
    assert data == artwork.placeholder(line)
    image = picture(data)
    assert image.size == SIZE and near(image.getpixel((5, 5)), (0xEC, 0xE8, 0xE0), slack=4)


def test_the_revision_changes_only_with_the_piece_its_framing_the_mats_or_the_switch(library, image_bytes):
    now = at("08:00")
    empty = artwork.revision(Config(), library, now)
    piece = framed(library, image_bytes)
    first = artwork.revision(Config(), library, now)
    assert first != empty and artwork.revision(Config(), library, at("09:00")) == first
    library.update(piece, selected=True, fit="crop", x=0.5, y=0.5)
    framing = artwork.revision(Config(), library, now)
    assert framing != first
    assert artwork.revision(Config(mats="never"), library, now) != framing
    assert artwork.revision(Config(art={"enabled": False}), library, now) not in {framing, empty}


# The base layer

def test_outside_its_blocks_the_tv_shows_artwork_composed_without_a_render(three_apps, configs, library, image_bytes):  # noqa: F811
    framed(library, image_bytes, fit="crop")
    configs.save(Config(tv={"ip": "192.168.1.50"}, app_schedule={"week": {"default": {"blocks": [
        {"app": "news", "start": "07:00", "end": "09:00"}, {"app": "photos", "start": "08:00", "end": "09:00"}]}}}))
    screen, renders = RotationScreen(), []
    controller = Controller(configs, library, tv_factory=lambda _: screen,
                            capture=lambda *a, app=None, **k: renders.append(app.id) or app.id.encode())
    cfg = configs.load()
    assert [app.id for app in controller.plan(cfg, at("08:30")).apps] == ["news", "photos"]  # apps take turns
    assert controller.plan(cfg, at("10:00")).apps == [apps.ARTWORK]
    assert controller.tick(at("10:00")) == "published"
    assert renders == [] and picture(screen.publications[-1]).size == SIZE
    # Artwork never joins the apps that take turns.
    assert apps.ARTWORK not in controller.plan(cfg, at("08:30")).apps


def test_a_preview_of_artwork_needs_no_renderer(three_apps, configs, library, image_bytes):  # noqa: F811
    framed(library, image_bytes)
    configs.save(Config(tv={"ip": "192.168.1.50"}))
    controller = Controller(configs, library, tv_factory=lambda _: RotationScreen(),
                            capture=lambda *a, **k: pytest.fail("Artwork needs no website render"))
    image = controller.preview_site(app_id=apps.ARTWORK_ID)
    assert picture(image).size == SIZE and controller.preview_app() == apps.ARTWORK_ID


def test_the_artwork_id_is_reserved(tmp_path, monkeypatch):
    sites = tmp_path / "sites"
    first = write(sites / "installed" / "layout.json", {"id": "morning", "name": "Morning"})
    write(sites / "apps" / "artwork" / "layout.json", {"id": "artwork", "name": "Fake"})
    monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(first))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")
    monkeypatch.setenv("DAYFRAME_SITES_DIR", str(sites))
    monkeypatch.setenv("DAYFRAME_APPS", "artwork=artwork-scope")
    assert [app.id for app in apps.installed()] == ["morning"]
    assert [app.id for app in apps.sources(Config())] == ["artwork", "morning"]
    write(first, {"id": "artwork", "name": "Fake"})
    assert apps.installed() == []
    origin = installer.Origin("repo", "repo", "main", None, "example/repo", app_path="app")
    with pytest.raises(installer.InstallError, match="reserved for the built-in Artwork"):
        installer.check_id("artwork", origin)


# The Schedule page

@pytest.fixture
def page(three_apps, tmp_path):  # noqa: F811
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    return app, lambda day="every": client.get(f"/tv/main/schedule?day={day}", headers=auth()).text


def test_the_schedule_says_artwork_is_the_base_layer_and_how_to_fix_a_placeholder(page, image_bytes):
    app, schedule = page
    text = schedule()
    assert "Artwork shows when no layout is scheduled. No artwork is included yet." in text
    assert '<a href="/gallery?tv=main">Add artwork</a>' in text
    app.extensions["library"].add(image_bytes, title="Piece")
    text = schedule()
    assert "Artwork shows when no layout is scheduled.</p>" in text and "Add artwork</a>" not in text
    app.extensions["configs"].mutate(lambda raw: raw["art"].update(enabled=False))
    text = schedule()
    assert "Daily artwork is off" in text and re.search(r'<a href="/household[^"]*#artwork">Turn it on</a>', text)


def test_the_strip_merges_artwork_and_every_day_needs_no_note(page):
    app, schedule = page
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule={"week": {"default": {"blocks": [
        {"app": "news", "start": "07:00", "end": "09:00"}]}}}))
    text = schedule()
    assert text.count('class="segment artwork"') == 2 and "Over the day" not in text
    assert "Used on every day." not in text and 'id="usually-shows"' not in text
    assert 'data-app="artwork"' not in text and '<option value="artwork"' not in text
    # A day's tab still says whose plan it uses.
    assert "Monday uses every day's plan." in schedule("mon")


def test_old_saves_with_a_default_still_load(configs):
    configs.save(Config(app_schedule={"default": {"apps": ["news", "photos"]}}))
    assert configs.load().app_schedule.default.apps == ["news", "photos"]


# Show now and the hints

def test_show_now_lists_artwork_and_holds_it(three_apps, configs, library, image_bytes):  # noqa: F811
    framed(library, image_bytes)
    configs.save(Config(tv={"ip": "192.168.1.50"}, app_schedule={"week": {"default": {"blocks": [
        {"app": "news", "start": "07:00", "end": "09:00"}]}}}))
    screen = RotationScreen()
    controller = Controller(configs, library, tv_factory=lambda _: screen, capture=lambda *a, app=None, **k: b"news")
    assert controller.show_app(apps.ARTWORK_ID, at("09:00"), now=at("08:00")) == "published"
    assert controller.rotation_status(at("08:30")).hold == Hold(apps.ARTWORK_ID, at("09:00"))
    assert picture(screen.publications[-1]).size == SIZE


def test_the_show_now_menu_and_route_offer_artwork(page, monkeypatch):
    app, schedule = page
    app.extensions["library"].set_state("tv_status", {"paired": True, "known": True})
    text = app.test_client().get("/tv/main", headers=auth()).text
    assert 'name="app" value="artwork">' in text
    # The preview list: Artwork first, on its own, then the installed layouts in their own group.
    select = re.search(r'<label class="preview-app">Show<select name="app">(.*?)</select>', text, re.S).group(1)
    assert select.startswith('<option value="artwork"')
    assert re.fullmatch(r'<option value="artwork"[^>]*>Artwork</option><optgroup label="Layouts">(<option [^>]*>[^<]+</option>)+</optgroup>', select)
    calls = []
    controller = app.extensions["controller"]
    monkeypatch.setattr(controller, "show_app", lambda app_id, until: calls.append(app_id) or "published")
    client = app.test_client()
    response = client.post("/screen/show-app", headers=auth(), data={"csrf": csrf(client), "app": "artwork"})
    assert response.status_code == 302 and calls == ["artwork"]


@pytest.mark.parametrize("enabled,title,link", [
    (True, "No artwork is included yet", '<a class="button outline sm" href="/gallery'),
    (False, "Daily artwork is off", '<a class="button outline sm" href="/household')])
def test_health_and_now_say_why_artwork_is_a_plain_frame(page, image_bytes, enabled, title, link):
    app, _ = page
    if not enabled:
        app.extensions["library"].add(image_bytes, title="Piece")
        app.extensions["configs"].mutate(lambda raw: raw["art"].update(enabled=False))
    client = app.test_client()
    health = client.get("/tv/main/health", headers=auth()).text
    assert title in health and link in health
    now = client.get("/tv/main", headers=auth()).text
    hint = "Plain frame: " + title[0].lower() + title[1:]
    assert hint in now and hint in client.get("/", headers=auth()).text
    # With artwork included and on, nothing to say.
    if enabled:
        app.extensions["library"].add(image_bytes, title="Piece")
        assert title not in client.get("/tv/main/health", headers=auth()).text
        assert "Plain frame" not in client.get("/tv/main", headers=auth()).text


def test_a_household_switch_names_what_it_turns_off(page):
    app, _ = page
    text = app.test_client().get("/household", headers=auth()).text
    assert "Show daily artwork on your TVs" in text and "Pick a new artwork each day" not in text
    assert "Artwork shows on every TV when no layout is scheduled. When it is off, the TVs show a plain frame instead." in text


def test_a_full_day_block_reads_all_day(page):
    app, schedule = page
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule={"week": {"default": {"blocks": [
        {"app": "photos", "start": "00:00", "end": "24:00"}]}}}))
    text = html.unescape(schedule())
    assert '<span class="block-text">All day</span>' in text and "Photos, all day" in text
    assert "12:00 to 12:00 AM" not in text and "12–12 AM" not in text
    assert "All day, every day." in text  # the block's editor
    used = html.unescape(app.test_client().get("/layouts", headers=auth()).text)
    assert "all day every day" in used and "12:00 to 12:00" not in used
