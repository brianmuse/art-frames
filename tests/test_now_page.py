"""The Now page and the Home tiles: the sent picture, whether the TV shows it, what comes next, and health."""

import hashlib
import html
import io
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from PIL import Image
from test_app_schedule import (
    rest,
    three_apps,  # noqa: F401  (a fixture)
)
from test_health_page import identity, paired
from test_web import PASSWORD, auth

from art_frames import apps
from art_frames.rotation import Hold, Rotation
from art_frames.web import create_app


@pytest.fixture
def site(three_apps, tmp_path):  # noqa: F811
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("morning")))
    return app, app.extensions["library"], app.test_client()


def jpeg(color="#668877"):
    stream = io.BytesIO()
    Image.new("RGB", (1280, 720), color).save(stream, "JPEG")
    return stream.getvalue()


def sent(app, *, device=None, minutes=10, content="MY_F0001"):
    """Store a picture as the last upload to this TV; returns its digest and time."""
    image = jpeg()
    (app.extensions["configs"].root / "tv-upload.jpg").write_bytes(image)
    digest = hashlib.sha256(image).hexdigest()
    at = datetime.now().astimezone() - timedelta(minutes=minutes)
    app.extensions["library"].set_state("last_upload", {
        "device": device or identity(app), "content_id": content, "at": at.isoformat(),
        "image_sha256": digest, "app": "morning"})
    return digest, at


def test_the_sent_picture_shows_large_with_its_app_and_confirmation(site):
    app, library, client = site
    paired(library)
    digest, at = sent(app)
    library.set_state("last_display", {"device": identity(app), "at": (at + timedelta(seconds=5)).isoformat(),
                                       "image_sha256": digest})
    page = client.get("/tv/main", headers=auth()).text
    assert re.search(rf'<img src="/screen/sent.jpg\?v={digest}&amp;size=large&amp;tv=main" alt="The last picture Art Frames sent to Frame 1">', page)
    assert '<p class="now-value">Morning</p>' in page and "Sent 10 minutes ago · Confirmed by the TV." in page
    assert "Nothing shown yet" not in page


def test_the_large_picture_route_checks_its_revision(site):
    app, library, client = site
    digest, _ = sent(app)
    image = (app.extensions["configs"].root / "tv-upload.jpg").read_bytes()
    url = next(rule.rule for rule in app.url_map.iter_rules() if rule.endpoint == "sent_image")
    assert client.get(f"{url}?v={digest}&size=large", headers=auth()).data == image
    small = client.get(f"{url}?v={digest}", headers=auth()).data
    assert Image.open(io.BytesIO(small)).size[0] <= 640
    assert client.get(f"{url}?v={'a' * 64}&size=large", headers=auth()).status_code == 409
    assert client.get(f"{url}?v={digest}&size=large").status_code == 401


def test_a_picture_sent_to_another_tv_is_not_shown(site):
    app, library, client = site
    paired(library)
    sent(app, device="another-tv")
    page = client.get("/tv/main", headers=auth()).text
    assert "Nothing shown yet" in page and "Nothing sent yet" in page
    # A paired TV with an app gets one button to show it now, through the Show now route.
    form = re.search(r'<form method="post" action="/screen/show-app\?tv=main">(.*?)</form>', page, re.S).group(1)
    assert 'name="hold" value="next-change"' in form and "Show Morning now" in form


def test_the_tv_showing_something_else_names_it_and_shows_its_thumbnail(site, image_bytes):
    app, library, client = site
    paired(library)
    _, at = sent(app)
    controller = app.extensions["displays"].get("main").controller
    controller._remember_artwork(identity(app), "SAM-S0001", image_bytes, "thumbnail", at + timedelta(minutes=2))
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert "The TV shows something else. Someone may have used the remote." in page
    assert 'class="card now-changed"' in page and "/screen/tv-artwork.jpg" in page


def test_a_stored_picture_of_dayframe_counts_as_a_switch_not_a_change(site, image_bytes):
    app, library, client = site
    paired(library)
    _, at = sent(app)
    library.set_state("owned_images:" + identity(app), ["MY_F0001", "MY_F0002"])
    controller = app.extensions["displays"].get("main").controller
    controller._remember_artwork(identity(app), "MY_F0002", image_bytes, "thumbnail", at + timedelta(minutes=2))
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert "is on the TV" in page and "Art Frames has no copy of this picture yet." in page and "now-changed" not in page


def test_an_unpaired_tv_says_how_to_pair_and_has_no_show_now(site):
    _, _, client = site
    page = client.get("/tv/main", headers=auth()).text
    assert "Pair Frame 1 to show pictures on it." in page
    assert 'class="dropdown show-now"' not in page and "/screen/show-app" not in page


def test_a_rotation_counts_down_to_the_next_app(site):
    app, library, client = site
    paired(library)
    configs = app.extensions["configs"]
    configs.mutate(lambda raw: raw.update(app_schedule=rest("news", "photos", swap=600)))
    cfg = configs.load()
    now = datetime.now(ZoneInfo(cfg.timezone))
    plan = app.extensions["displays"].get("main").controller.plan(cfg, now, apps.installed())
    Rotation(library, identity(app)).record_shown(plan, "news", now)
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    countdown = re.search(r'data-countdown="([^"]+)" data-countdown-text="([^"]+)" data-countdown-done="([^"]+)">([^<]+)<', page)
    assert 9 <= (datetime.fromisoformat(countdown.group(1)) - now).total_seconds() / 60 <= 10
    assert countdown.group(2) == "Switches to Photos in {left}" and countdown.group(3) == "Switching to Photos"
    assert countdown.group(4).startswith("Switches to Photos at ")
    # The Home tile counts down with the same words, shorter.
    assert 'data-countdown-text="Photos in {left}"' in html.unescape(client.get("/", headers=auth()).text)


def test_a_rotation_in_standby_says_it_is_paused(site):
    app, library, client = site
    paired(library, awake=False)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("news", "photos", swap=600)))
    page = client.get("/tv/main", headers=auth()).text
    assert "Paused while the TV is in standby" in page and "data-countdown=" not in page


def test_the_preview_dialog_opens_from_the_link_without_javascript(site):
    _, library, client = site
    paired(library)
    page = client.get("/tv/main", headers=auth()).text
    assert re.search(r'<dialog class="dialog preview-dialog" id="preview-dialog"[^>]*>', page)
    assert not re.search(r'<dialog[^>]* open ', page)
    assert page.count('href="/tv/main?preview=1" data-open-dialog="preview-dialog"') == 2  # Show now and More
    page = client.get("/tv/main?preview=1", headers=auth()).text
    assert re.search(r'<dialog class="dialog preview-dialog" id="preview-dialog"[^>]* open ', page)


def test_now_page_has_no_suggested_fixes_and_links_health(site):
    app, library, client = site
    page = client.get("/tv/main", headers=auth()).text
    assert "Suggested fixes" not in page
    assert re.search(r'<p class="health-line tone-problem">.*?<a href="/tv/main/health#check-paired">Frame 1 is not paired</a>', page, re.S)


def test_home_tiles_show_now_next_and_health(site):
    app, library, client = site
    paired(library)
    now = datetime.now(ZoneInfo(app.extensions["configs"].load().timezone))
    Rotation(library, identity(app)).record_success("morning", now)
    library.set_state("last_display", {"device": identity(app), "at": now.isoformat()})
    sent(app)
    page = client.get("/", headers=auth()).text
    assert "<dt>Now</dt><dd>Morning · sent 10 minutes ago</dd>" in page
    assert re.search(r'<div class="tv-card-health"><p class="health-line tone-ok">.*?<a href="/tv/main/health">All good</a>', page, re.S)
    assert 'href="/tv/main/schedule"' in page


def test_now_and_home_never_contact_the_tv(site):
    app, library, client = site
    paired(library)
    sent(app)

    def no_tv(cfg):
        raise AssertionError("A page contacted the TV")
    app.extensions["displays"].get("main").controller.tv_factory = no_tv
    for path in ("/tv/main", "/tv/main?preview=1", "/"):
        assert client.get(path, headers=auth()).status_code == 200, path


def test_after_a_rotation_swap_now_and_home_name_the_app_the_tv_shows(site):
    app, library, client = site
    paired(library)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("morning", "news", swap=600)))
    digest, at = sent(app)  # Morning's picture
    library.set_state("last_display", {"device": identity(app), "at": (at + timedelta(seconds=5)).isoformat(),
                                       "image_sha256": digest})
    cfg = app.extensions["configs"].load()
    now = datetime.now(ZoneInfo(cfg.timezone))
    plan = app.extensions["displays"].get("main").controller.plan(cfg, now, apps.installed())
    # A swap selects News's stored picture and records it (see Controller._swap).
    Rotation(library, identity(app)).record_shown(plan, "news", now - timedelta(minutes=1))
    library.set_state("last_display", {"device": identity(app), "at": now.isoformat(), "image_sha256": "other", "app": "news"})
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert "Confirmed by the TV" not in page and "News is on the TV" in page
    assert 'size=large' not in page  # Morning's picture is not shown as the TV's picture
    assert "Art Frames has no copy of this picture yet." in page and "Taking turns · switched just now" in page
    home = html.unescape(client.get("/", headers=auth()).text)
    assert "<dt>Now</dt><dd>News · taking turns</dd>" in home and "News is on the TV" in home
    # With Art Frames' copy of News's stored image, both pages show that picture.
    image = jpeg("#334455")
    digest = hashlib.sha256(image).hexdigest()
    rotation = Rotation(library, identity(app))
    rotation.store(apps.find("news"), "MY_F0002", digest, "key", now)
    (app.extensions["configs"].root / "tv-upload-news.jpg").write_bytes(image)
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert f'src="/screen/sent.jpg?app=news&v={digest}&size=large&tv=main"' in page
    assert f'app=news&v={digest}"' in html.unescape(client.get("/", headers=auth()).text)
    large = client.get(f"/screen/sent.jpg?tv=main&app=news&v={digest}&size=large", headers=auth())
    assert large.status_code == 200 and large.data == image
    assert client.get(f"/screen/sent.jpg?tv=main&app=news&v={digest}", headers=auth()).status_code == 200
    assert client.get("/screen/sent.jpg?tv=main&app=news&v=old", headers=auth()).status_code == 409
    # A copy that no longer matches the stored image is not offered (its URL would get a 409).
    (app.extensions["configs"].root / "tv-upload-news.jpg").write_bytes(jpeg("#000000"))
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert "app=news" not in page and "Art Frames has no copy of this picture yet." in page
    assert client.get(f"/screen/sent.jpg?tv=main&app=photos&v={digest}", headers=auth()).status_code == 404
    assert client.get(f"/screen/sent.jpg?tv=main&app=../x&v={digest}", headers=auth()).status_code == 404


def test_during_a_hold_why_names_the_held_app_and_when_the_schedule_resumes(site):
    app, library, client = site
    paired(library)
    cfg = app.extensions["configs"].load()
    now = datetime.now(ZoneInfo(cfg.timezone))
    Rotation(library, identity(app)).set_hold(Hold("news", now + timedelta(hours=1)))
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert '<p class="now-value">Show now: holding News</p>' in page
    assert re.search(r'<p class="small">The schedule waits until [^<]+\.</p>', page)


def test_the_preview_dialog_has_no_options(site):
    _, library, client = site
    paired(library)
    page = client.get("/tv/main", headers=auth()).text
    dialog = re.search(r'<dialog class="dialog preview-dialog".*?</dialog>', page, re.S).group(0)
    assert 'name="app_preview"' not in dialog and "Options</summary>" not in dialog


def test_a_home_tile_has_no_tv_state_row_and_its_badge_says_when_it_was_checked(site):
    _, library, client = site
    paired(library)
    page = client.get("/", headers=auth()).text
    assert "<dt>TV state</dt>" not in page
    assert re.search(r'class="badge outline status-badge[^"]*" title="Checked 4 minutes ago"', page)


def test_an_overdue_swap_says_switching_soon_with_no_timer(site):
    app, library, client = site
    paired(library)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("morning", "news", swap=60)))
    cfg = app.extensions["configs"].load()
    now = datetime.now(ZoneInfo(cfg.timezone))
    plan = app.extensions["displays"].get("main").controller.plan(cfg, now, apps.installed())
    Rotation(library, identity(app)).record_shown(plan, "morning", now - timedelta(minutes=10))
    for path in ("/tv/main", "/"):
        page = client.get(path, headers=auth()).text
        assert "data-countdown" not in page and "Switching soon" in page


def test_a_home_tile_names_the_schedule_s_app_for_an_upload_without_one(site):
    app, library, client = site
    paired(library)
    digest, at = sent(app)
    upload = library.state("last_upload")
    library.set_state("last_upload", {**upload, "app": None})
    page = client.get("/", headers=auth()).text
    assert "<dt>Now</dt><dd>Morning · sent 10 minutes ago</dd>" in page
