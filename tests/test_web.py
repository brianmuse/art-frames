import base64
from datetime import UTC, datetime

import pytest

from art_frames.config import Config
from art_frames.web import create_app

PASSWORD = "a-long-test-password-not-a-real-secret"


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


def auth():
    return {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}


def csrf(client):
    client.get("/", headers=auth())
    with client.session_transaction() as session:
        return session["csrf"]


def test_production_without_a_password_starts_first_run_setup(tmp_path):
    # No password file and no auth.json: the browser asks for the setup code (tests/test_sign_in.py).
    assert create_app(tmp_path / "new", testing=True).extensions["auth"].mode == "setup"
    # A password file must still be long.
    with pytest.raises(ValueError):
        create_app(tmp_path / "short", password="too-short")


def test_auth_and_csrf_gate_all_changes(app):
    client = app.test_client()
    assert client.get("/").status_code == 401
    assert client.post("/wake").status_code == 401
    assert client.post("/wake", headers=auth()).status_code == 400
    assert client.get("/health").json == {"status": "ok"}


@pytest.mark.parametrize("reason,text", [("missing-mac","MAC address"),("state-unknown","Wake packets were sent"),("not-confirmed","Power On with Mobile"),("unpaired","Pair this TV")])
def test_wake_reports_specific_safe_reason_instead_of_generic_saved(app, monkeypatch, reason, text):
    from art_frames.television import WakeError

    def fail():
        raise WakeError(reason)
    monkeypatch.setattr(app.extensions["controller"], "wake_screen", fail)
    client = app.test_client()
    response = client.post("/wake", headers=auth(), data={"csrf":csrf(client)}, follow_redirects=True)
    assert text in response.text
    assert "That action could not be completed" not in response.text and "Saved." not in response.text


def test_framing_autosave_reports_saved_or_failed_without_redirects(app, image_bytes, monkeypatch):
    client = app.test_client()
    library = app.extensions["library"]
    piece = library.add(image_bytes, title="Autosave")
    headers = {**auth(), "Accept": "application/json"}
    data = {"selected": "on", "fit": "crop", "mat_width": ".03", "mat_color": "#aabbcc"}
    assert client.post(f"/artwork/{piece}", headers=headers, data=data).status_code == 400
    data["csrf"] = csrf(client)
    response = client.post(f"/artwork/{piece}", headers=headers, data=data)
    assert response.status_code == 200 and response.json == {"status": "saved"}
    assert library.get(piece)["mat_width"] == .03
    data["mat_width"] = "1"
    response = client.post(f"/artwork/{piece}", headers=headers, data=data)
    assert response.status_code == 400 and response.json["status"] == "failed"
    assert library.get(piece)["mat_width"] == .03
    def fail(*args, **kwargs):
        raise RuntimeError("Private path must not be returned")
    monkeypatch.setattr(library, "update", fail)
    response = client.post(f"/artwork/{piece}", headers=headers, data=data)
    assert response.status_code == 500 and "Private path" not in response.text


def test_preview_waits_for_app_without_replacing_image_and_returns_ready(app, monkeypatch):
    from art_frames.site_service import WebsitePending

    client = app.test_client()
    controller = app.extensions["controller"]
    preview = app.extensions["configs"].root / "screen-preview.jpg"
    preview.write_bytes(b"existing preview")
    calls = []

    def capture(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise WebsitePending("private provider details")
        return b"completed preview"

    monkeypatch.setattr(controller, "capture", capture)
    headers = {**auth(), "Accept": "application/json"}
    assert client.post("/screen/preview", headers=headers).status_code == 400
    token = csrf(client)
    first = client.post("/screen/preview", headers=headers, data={"csrf": token})
    assert first.status_code == 202 and first.json == {"status": "pending"}
    assert preview.read_bytes() == b"existing preview"
    second = client.post("/screen/preview", headers=headers, data={"csrf": token})
    assert second.status_code == 200 and second.json["status"] == "ready"
    assert second.json["image"].startswith("/screen/preview.jpg?v=")
    assert preview.read_bytes() == b"completed preview"


def test_every_preview_renders_with_the_preview_purpose(app, monkeypatch):
    client = app.test_client()
    modes = []
    def capture(*args, **kwargs):
        modes.append((kwargs["purpose"], "ignore_hours" in kwargs))
        return b"rendered-image"
    monkeypatch.setattr(app.extensions["controller"], "capture", capture)
    data = {"csrf": csrf(client)}
    headers = {**auth(), "Accept": "application/json"}
    # An old page can still send app_preview; Art Frames ignores it.
    for extra in [{}, {"app_preview": "1"}]:
        assert client.post("/screen/preview", headers=headers, data={**data, **extra}).status_code == 200
    assert modes == [("preview", False), ("preview", False)]


@pytest.mark.parametrize("result,text", [
    ("published", "The TV confirmed this artwork is selected"),
    ("unknown", "Could not confirm the TV"),
    ("asleep", "The TV is asleep"),
    ("watching", "A TV programme is playing"),
    ("unpaired", "Pair your TV in TV settings"),
    ("busy", "Another update is running"),
    ("changed", "The TV left Art Mode"),
])
def test_refresh_reports_real_result_instead_of_saved(app, monkeypatch, result, text):
    client = app.test_client()
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "tick", lambda **kw: calls.append(kw) or result)
    assert client.post("/refresh", headers=auth()).status_code == 400
    assert not calls
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert text in response.text
    assert calls == [{"force": True}]


def test_refresh_failure_never_exposes_tv_exception_contents(app, monkeypatch, caplog):
    client = app.test_client()

    def failed(**kwargs):
        raise RuntimeError("SYNTHETIC-PRIVATE-TV-TOKEN")

    monkeypatch.setattr(app.extensions["controller"], "tick", failed)
    response = client.post("/refresh", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "The TV update failed" in response.text
    assert "SYNTHETIC-PRIVATE-TV-TOKEN" not in response.text + caplog.text


def test_send_preview_requires_csrf_and_passes_the_displayed_revision(app, monkeypatch):
    import hashlib
    client = app.test_client()
    image = b"full dashboard preview"
    (app.extensions["configs"].root / "screen-preview.jpg").write_bytes(image)
    revision = hashlib.sha256(image).hexdigest()
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "send_preview", lambda value: calls.append(value) or "published")
    assert client.post("/screen/send-preview", headers=auth()).status_code == 400
    assert not calls
    page = client.get("/tv/main", headers=auth())
    assert revision in page.text and "Show on Frame 1</button>" in page.text
    response = client.post("/screen/send-preview", headers=auth(), data={"csrf": csrf(client), "preview_revision": revision}, follow_redirects=True)
    assert calls == [revision]
    assert "The TV confirmed this artwork is selected" in response.text


def test_tv_readback_requires_auth_csrf_and_retains_last_check_on_failure(app, monkeypatch, image_bytes):
    controller = app.extensions["controller"]
    controller._remember_artwork(controller.configs.load().tv.identity, "external-art", image_bytes, "thumbnail", datetime.now(UTC))
    revision = app.extensions["library"].state("tv_artwork")["image_sha256"]
    client = app.test_client()
    assert client.get(f"/screen/tv-artwork.jpg?v={revision}").status_code == 401
    assert client.get(f"/screen/tv-artwork.jpg?v={revision}", headers=auth()).data == image_bytes
    assert client.get("/screen/tv-artwork.jpg?v=" + "a" * 64, headers=auth()).status_code == 409
    monkeypatch.setattr(controller, "check_artwork", lambda: "asleep")
    assert client.post("/screen/check-tv", headers=auth()).status_code == 400
    response = client.post("/screen/check-tv", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "The TV is asleep" in response.text and "last successful check" in response.text
    assert client.get(f"/screen/tv-artwork.jpg?v={revision}", headers=auth()).data == image_bytes
    app.extensions["configs"].mutate(lambda raw: raw["tv"].update(ip="192.168.1.99"))
    assert client.get(f"/screen/tv-artwork.jpg?v={revision}", headers=auth()).status_code == 404


def test_preview_image_revision_binds_the_returned_bytes(app):
    import hashlib
    client = app.test_client()
    path = app.extensions["configs"].root / "screen-preview.jpg"
    path.write_bytes(b"first image")
    first = hashlib.sha256(b"first image").hexdigest()
    path.write_bytes(b"second image")
    second = hashlib.sha256(b"second image").hexdigest()
    assert client.get(f"/screen/preview.jpg?v={first}", headers=auth()).status_code == 409
    result = client.get(f"/screen/preview.jpg?v={second}", headers=auth())
    assert result.status_code == 200 and result.data == b"second image"


def test_mat_controls_save_per_artwork_and_preserve_on_legacy_posts(app, image_bytes):
    client = app.test_client()
    library = app.extensions["library"]
    piece = library.add(image_bytes, title="Matted painting")
    token = csrf(client)
    result = client.post(f"/artwork/{piece}", headers=auth(), data={"csrf": token, "selected": "on", "fit": "crop", "x": ".5", "y": ".5", "mat_width": ".03", "mat_color": "#aabbcc"})
    assert result.status_code == 302
    assert library.get(piece)["mat_width"] == .03
    assert library.get(piece)["mat_color"] == "#aabbcc"
    client.post(f"/artwork/{piece}", headers=auth(), data={"csrf": token, "selected": "on", "fit": "crop", "x": ".5", "y": ".5"})
    assert library.get(piece)["mat_width"] == .03
    assert library.get(piece)["mat_color"] == "#aabbcc"


def test_tv_discovery_requires_auth_csrf_and_preserves_settings(app, monkeypatch):
    client = app.test_client()
    calls = []
    finder = app.extensions["tv_discovery"]
    result = {"ip": "192.168.1.2", "name": "Living Room", "model": "Frame", "mac": "", "frame": True}
    monkeypatch.setattr(finder, "scan", lambda network: calls.append(network) or [result])
    assert client.post("/tvs/discover").status_code == 401
    assert client.post("/tvs/discover", headers=auth()).status_code == 400
    assert not calls
    before = app.extensions["configs"].load().model_dump()
    response = client.post(
        "/tvs/discover", headers=auth(), data={"csrf": csrf(client), "network": "192.168.1.0/24"}
    )
    assert response.json == {"devices": [result]}
    assert calls == ["192.168.1.0/24"]
    assert app.extensions["configs"].load().model_dump() == before


def test_discovery_validation_busy_and_demo_responses(app, tmp_path, monkeypatch):
    from art_frames.tv_discovery import DiscoveryBusy

    client = app.test_client()
    token = csrf(client)
    assert (
        client.post("/tvs/discover", headers=auth(), data={"csrf": token, "network": "0.0.0.0/0"}).status_code
        == 400
    )

    def busy(network):
        raise DiscoveryBusy("Already searching")

    monkeypatch.setattr(app.extensions["tv_discovery"], "scan", busy)
    assert client.post("/tvs/discover", headers=auth(), data={"csrf": token}).status_code == 429
    demo = create_app(tmp_path / "demo", demo=True, testing=True).test_client()
    demo.get("/settings")
    with demo.session_transaction() as session:
        demo_token = session["csrf"]
    assert demo.post("/tvs/discover", data={"csrf": demo_token}).status_code == 400


@pytest.mark.parametrize("path", ["/", "/gallery", "/discover", "/tv/main", "/tv/main/picture", "/tv/main/settings", "/tv/main/health", "/activity"])
def test_pages_render_with_security_headers(app, path):
    response = app.test_client().get(path, headers=auth())
    assert response.status_code == 200
    assert b"Art Frames" in response.data
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_upload_xss_escape_and_persistent_selection(app, image_bytes):
    import io

    client = app.test_client()
    token = csrf(client)
    response = client.post(
        "/upload",
        headers=auth(),
        data={
            "csrf": token,
            "title": "<script>alert(1)</script>",
            "image": (io.BytesIO(image_bytes), "test.png"),
        },
    )
    assert response.status_code == 302
    page = client.get("/gallery", headers=auth())
    assert b"&lt;script&gt;" in page.data
    assert b"<script>alert(1)</script>" not in page.data
    library = app.extensions["library"]
    art = library.all()[0]
    client.post(
        f"/artwork/{art['id']}", headers=auth(), data={"csrf": token, "fit": "crop", "x": ".2", "y": ".3"}
    )
    assert not library.get(art["id"])["selected"]


def test_museum_import_cannot_accept_arbitrary_url(app, monkeypatch):
    client = app.test_client()
    token = csrf(client)
    calls = []
    monkeypatch.setattr("art_frames.web.sources.import_artwork", lambda *args: calls.append(args))
    response = client.post(
        "/import", headers=auth(), data={"csrf": token, "source": "https://192.168.1.70", "source_id": "123"}
    )
    assert response.status_code == 302
    assert not calls


def test_blocked_collection_is_not_presented_as_empty_search(app, monkeypatch):
    from art_frames.network import FetchError

    def blocked(*args, **kwargs):
        raise FetchError("Source returned HTTP 403", status=403)

    monkeypatch.setattr("art_frames.web.sources.search", blocked)
    response = app.test_client().get("/discover?source=artic&q=Monet&artist=1", headers=auth())
    assert b"blocking automated access" in response.data
    assert b"No downloadable images on this page" not in response.data
    assert b"https://www.artic.edu/collection" in response.data


def test_museum_artist_search_and_empty_page_keep_pagination(app, monkeypatch):
    from art_frames.sources import SearchPage

    calls = []
    monkeypatch.setattr(
        "art_frames.web.sources.search",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SearchPage([], True),
    )
    page = app.test_client().get("/discover?source=met&q=Monet&artist=1", headers=auth())
    assert calls == [(("met", "Monet", 1), {"artist": True})]
    assert b"Next page" in page.data and b"artist=1" in page.data


def test_existing_museum_choices_remain_opt_in(app, monkeypatch):
    app.extensions["configs"].save(Config(art={"sources": ["met", "artic"]}))
    monkeypatch.setattr(
        "art_frames.web.sources.search", lambda *args, **kwargs: pytest.fail("Disabled provider queried")
    )
    client = app.test_client()
    assert client.get("/discover?source=cleveland&q=Monet", headers=auth()).status_code == 400
    assert b"Turn on more museums in Household settings" in client.get("/discover", headers=auth()).data


def test_live_preview_requires_auth_and_exports_data_without_tv_actions(app, monkeypatch, image_bytes):
    import base64

    app.extensions["configs"].save(Config(site={"providers": ["art"]}))
    app.extensions["library"].add(image_bytes, title="Our selected piece")

    def no_tv(*args, **kwargs):
        pytest.fail("Preview data must never access the TV")

    monkeypatch.setattr(app.extensions["controller"], "tv_factory", no_tv)
    client = app.test_client()
    assert client.get("/screen/preview-data").status_code == 401
    response = client.get("/screen/preview-data", headers=auth())
    assert response.status_code == 200 and "planner" not in response.json
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["art"]["title"] == "Our selected piece"
    uri = response.json["art"]["image"]
    assert uri.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]).startswith(b"\xff\xd8")
    assert "glass_image" not in response.json["art"]
    assert client.get("/screen/preview-data?mode=invalid", headers=auth()).status_code == 400


def test_untrusted_host_is_rejected(app):
    assert app.test_client().get("/", base_url="http://evil.example", headers=auth()).status_code == 400


def test_artwork_zoom_is_saved_and_displayed_in_collection(app, image_bytes):
    client = app.test_client()
    token = csrf(client)
    library = app.extensions["library"]
    art_id = library.add(image_bytes, title="Zoom example")
    response = client.post(
        f"/artwork/{art_id}",
        headers=auth(),
        data={"csrf": token, "selected": "on", "fit": "crop", "x": ".5", "y": ".5", "zoom": "1.12"},
    )
    assert response.status_code == 302
    assert library.get(art_id)["zoom"] == 1.12
    page = client.get("/gallery", headers=auth())
    assert b"112%" in page.data and b'name="zoom"' in page.data
