import base64
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from art_frames.config import Config
from art_frames.ipc import decode, encode
from art_frames.site import asset_path, capture, frame_capture, resource, site_root, snapshot


def test_custom_site_cannot_escape_sites_mount(tmp_path, monkeypatch):
    root = tmp_path / "sites"
    root.mkdir()
    monkeypatch.setenv("DAYFRAME_SITES_DIR", str(root))
    target = root / "custom"
    target.mkdir()
    (target / "index.html").write_text("<h1>My own screen</h1>")
    cfg = Config(site={"path": "custom"})
    assert site_root(cfg) == target
    assert resource(target, "http://dayframe.local/index.html", {})[1] == b"<h1>My own screen</h1>"
    private = tmp_path / "private.txt"
    private.write_text("private")
    (target / "outside.txt").symlink_to(private)
    for path in ("../private.txt", "%2e%2e/private.txt", "outside.txt", ".secret"):
        with pytest.raises(ValueError):
            asset_path(target, path)
    for url in ("http://10.0.0.1:5000", "https://example.com/", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            resource(target, url, {})


def test_default_website_gets_no_private_providers(library, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A generic website should not read private dashboard providers")

    monkeypatch.setattr(library, "choose", forbidden)
    cfg = Config(site={"data": {"headline": "My own content"}})
    extras = snapshot(cfg, library, datetime.now(ZoneInfo("UTC")))
    data = json.loads(extras["/dayframe.json"][1])
    assert set(data) == {"version", "now", "timezone", "display", "data", "render_purpose", "refresh_minutes"}
    assert data["render_purpose"] == "scheduled" and data["display"] == {"width": 3840, "height": 2160}
    assert data["data"]["headline"] == "My own content"


def test_preview_purpose_is_opt_in_and_contains_no_app_settings(library):
    extras = snapshot(Config(), library, datetime.now(ZoneInfo("UTC")), purpose="preview")
    data = json.loads(extras["/dayframe.json"][1])
    assert data["render_purpose"] == "preview"
    assert "planner" not in data and "calendars" not in data


@pytest.mark.parametrize("fit", ["crop", "contain"])
@pytest.mark.parametrize("mat_width", [.04, .1])
def test_framed_website_pixels_stay_inside_painting_opening(library, image_bytes, fit, mat_width):
    import io

    from PIL import Image

    piece = library.add(image_bytes, title="Framed artwork")
    library.update(piece, selected=True, fit=fit, x=.2, y=.8, zoom=1.1, mat_width=mat_width, mat_color="#e9e2d7")
    extras = snapshot(Config(site={"providers": ["art"]}), library, datetime.now(ZoneInfo("UTC")))
    opening = json.loads(extras["/dayframe.json"][1])["art"]["viewport"]
    assert opening["x"] > 0 and opening["y"] > 0
    # A website filling every pixel must still leave the mat's face untouched.
    rendered = io.BytesIO()
    Image.new("RGB", (3840, 2160), "#e02020").save(rendered, "JPEG")
    result = frame_capture(rendered.getvalue(), extras)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (3840, 2160)
        assert max(abs(a-b) for a,b in zip(image.getpixel((20,20)), (233,226,215), strict=True)) < 5
        assert image.getpixel((1920,1080))[0] > 200 and image.getpixel((1920,1080))[1] < 50
    unframed = snapshot(Config(), library, datetime.now(ZoneInfo("UTC")))
    raw = rendered.getvalue()
    assert frame_capture(raw, unframed) is raw


def test_morning_and_evening_use_identical_daily_art(library, image_bytes):

    library.add(image_bytes, title="Selected artwork")
    cfg = Config(site={"providers": ["art"]})
    morning = snapshot(cfg, library, datetime(2030, 1, 2, 8, tzinfo=ZoneInfo("UTC")))
    evening = snapshot(cfg, library, datetime(2030, 1, 2, 18, tzinfo=ZoneInfo("UTC")))
    assert morning["/dayframe-art.jpg"] == evening["/dayframe-art.jpg"]
    before = json.loads(morning["/dayframe.json"][1])
    after = json.loads(evening["/dayframe.json"][1])
    assert "planner" not in before and "weather" not in before
    assert before["art"]["title"] == after["art"]["title"] == "Selected artwork"
    assert "glass_image" not in before["art"]


def test_renderer_ipc_supplies_only_local_assets(tmp_path, library, image_bytes, monkeypatch):
    root = tmp_path / "sites" / "custom"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>Personal content</h1>")
    monkeypatch.setenv("DAYFRAME_SITES_DIR", str(root.parent))
    cfg = Config(site={"path": "custom"})
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "example.sock")
    monkeypatch.setattr(
        "art_frames.site.service_request",
        lambda *args, **kwargs: pytest.fail("A custom folder must not use the default website service"),
    )

    class Socket:
        def __init__(self):
            self.sent = []
            self.messages = bytearray(
                b"".join(
                    encode(message)
                    for message in [
                        {"type": "fetch", "url": "http://dayframe.local/index.html", "method": "GET"},
                        {"type": "fetch", "url": "http://10.0.0.1:5001", "method": "GET"},
                        {"type": "fetch", "url": "http://dayframe.local/dayframe.json", "method": "GET"},
                        {"type": "image", "body": base64.b64encode(image_bytes).decode()},
                    ]
                )
            )
            self.closed = False

        def connect(self, endpoint):
            self.endpoint = endpoint

        def settimeout(self, timeout):
            pass

        def sendall(self, value):
            self.sent.append(decode(value[4:]))

        def recv(self, size):
            result = self.messages[:size]
            del self.messages[:size]
            return bytes(result)

        def close(self):
            self.closed = True

    socket = Socket()
    monkeypatch.setattr("art_frames.site.socket.socket", lambda *args, **kwargs: socket)
    assert capture(cfg, library, datetime.now(ZoneInfo("UTC"))) == image_bytes
    assert socket.closed
    assert [message["status"] for message in socket.sent[1:]] == [200, 403, 200]
    assert b"Personal content" in base64.b64decode(socket.sent[1]["body"])


@pytest.mark.parametrize("value", ["../private", "/private", "a/../../etc", "a\\private"])
def test_website_path_validation(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config(site={"path": value})


def test_pending_website_asset_defers_the_entire_capture(library, monkeypatch):
    from art_frames.site_service import WebsitePending

    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "test-site.sock")

    def website(endpoint, route, context=None):
        if route == "/index.html":
            return "text/html", b'<img src="/pending.jpg">'
        raise WebsitePending("not ready")

    monkeypatch.setattr("art_frames.site.service_request", website)

    class Socket:
        closed = False
        messages = bytearray(encode({"type": "fetch", "url": "http://dayframe.local/pending.jpg", "method": "GET"}))

        def connect(self, endpoint): pass
        def settimeout(self, timeout): pass
        def sendall(self, body): pass
        def recv(self, size):
            result = self.messages[:size]
            del self.messages[:size]
            return bytes(result)
        def close(self): self.closed = True

    channel = Socket()
    monkeypatch.setattr("art_frames.site.socket.socket", lambda *args, **kwargs: channel)
    with pytest.raises(WebsitePending):
        capture(Config(), library, datetime.now(ZoneInfo("UTC")))
    assert channel.closed


def test_each_app_gets_a_stable_opaque_display_id_and_the_refresh_interval(library):
    from types import SimpleNamespace

    def data(display, scope):
        app = SimpleNamespace(scope=scope, manifest=SimpleNamespace(display_settings=[]))
        cfg = Config(site={"refresh_minutes": 20})
        return json.loads(snapshot(cfg, library, datetime.now(ZoneInfo("UTC")), app=app, display=display)["/dayframe.json"][1])

    first = data("main", "news-scope")["display"]["id"]
    assert first == data("main", "news-scope")["display"]["id"]  # stable
    assert len(first) == 16 and set(first) <= set("0123456789abcdef") and "main" not in first
    # Another app on the same TV, or the same app on another TV, sees another ID.
    assert first != data("main", "photos-scope")["display"]["id"]
    assert first != data("a" * 32, "news-scope")["display"]["id"]
    assert data("main", "news-scope")["refresh_minutes"] == 20
    # A custom website without an app gets no ID.
    plain = json.loads(snapshot(Config(), library, datetime.now(ZoneInfo("UTC")))["/dayframe.json"][1])
    assert "id" not in plain["display"]


def test_the_display_id_key_is_private_shared_and_needed(library, monkeypatch):
    import hashlib
    from types import SimpleNamespace

    from art_frames import site
    app = SimpleNamespace(scope="news-scope")
    first = site.display_id(library, "main", app)
    key = library.root / "display-id.key"
    assert key.stat().st_mode & 0o777 == 0o600 and len(key.read_bytes()) == 32
    assert first != hashlib.sha256(b"main:news-scope").hexdigest()[:16]  # keyed, not a plain hash
    assert [path.name for path in library.root.iterdir() if path.name.startswith(".display-id")] == []
    # Two writers that both find no key: the second keeps the first one's key.
    key.unlink()
    real_exists = type(key).exists
    monkeypatch.setattr(type(key), "exists", lambda self: False if self.name == "display-id.key" else real_exists(self))
    one = site.display_id(library, "main", app)
    two = site.display_id(library, "main", app)
    monkeypatch.undo()
    assert one == two != first  # a new key gives a new ID
    key.write_bytes(b"x" * 32)
    assert site.display_id(library, "main", app) != one


def test_the_display_id_works_without_hard_links(library, monkeypatch):
    from types import SimpleNamespace

    from art_frames import site
    monkeypatch.setattr(site.os, "link", lambda *a: (_ for _ in ()).throw(PermissionError("no hard links")))
    app = SimpleNamespace(scope="news-scope")
    first = site.display_id(library, "main", app)
    assert first == site.display_id(library, "main", app) and len(first) == 16
    key = library.root / "display-id.key"
    assert key.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in library.root.iterdir() if path.name.startswith(".display-id")] == []
