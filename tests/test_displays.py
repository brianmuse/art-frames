from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from art_frames.config import Config, ConfigStore, private_write
from art_frames.controller import Controller
from art_frames.displays import Displays
from art_frames.web import create_app


def test_legacy_screen_keeps_pairing_preview_and_history(configs, library, image_bytes):
    configs.save(Config(tv={"ip": "192.168.1.50"}, tv_name="Living Room"))
    token = configs.root / "tokens" / f"{configs.load().tv.identity}.txt"
    private_write(token, b"synthetic-token")
    private_write(configs.root / "screen-preview.jpg", b"old-preview")
    piece = library.add(image_bytes, title="Existing")
    library.choose("2026-10-26")
    controller = Controller(configs, library, demo=True)
    displays = Displays(configs, library, controller, demo=True)
    second = displays.add("Den", "192.168.1.51")
    assert displays.get("main").controller is controller
    assert displays.get("main").configs.root == configs.root
    assert token.read_bytes() == b"synthetic-token"
    assert (configs.root / "screen-preview.jpg").read_bytes() == b"old-preview"
    assert library.choose("2026-10-26")["id"] == piece
    assert displays.get(second).configs.root != configs.root
    assert displays.get(second).library.path_for(piece) == library.path_for(piece)


def test_three_frames_get_distinct_stable_daily_art_even_when_selected_concurrently(library, image_bytes):
    for index in range(3):
        library.add(image_bytes, title=f"Painting {index}")
    ids = ["main", "a" * 32, "b" * 32]
    for day in ["2026-10-26", "2026-10-27"]:
        with ThreadPoolExecutor(max_workers=3) as pool:
            chosen = list(pool.map(lambda target, day=day: library.choose(day, display_id=target)["id"], ids))
        assert len(set(chosen)) == 3
        assert [library.choose(day, display_id=target)["id"] for target in ids] == chosen
    # A small collection remains usable, even when each TV cannot have a unique piece.
    for piece in library.all()[1:]:
        library.update(piece["id"], selected=False, fit="contain", x=.5, y=.5)
    assert len({library.choose("2026-10-28", display_id=target)["id"] for target in ids}) == 1


def test_display_overrides_and_state_do_not_change_another_tv(configs, library):
    configs.save(Config(tv={"ip": "192.168.1.50"}, site={"refresh_minutes": 15}))
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True)
    second = displays.add("Den", "192.168.1.51", "AA:BB:CC:DD:EE:01")
    third = displays.add("Office", "192.168.1.52")
    target = displays.get(second)
    configs.mutate(lambda raw: raw["site"].update(refresh_minutes=30))
    assert target.configs.load().site.refresh_minutes == 30
    target.configs.mutate(lambda raw: raw.update(site={"path": "other/dist"}, wake={"enabled": True, "at": "08:00"}, tv_name="Den"),
                           inherit_site=False)
    assert target.configs.load().site.path == "other/dist"
    assert target.configs.load().app_schedule.week.day(0).wake == "08:00"
    assert displays.items()[1]["name"] == "Den"
    assert not configs.load().app_schedule.week.has_wake()
    assert displays.get(third).configs.load().site.path == ""
    assert displays.get(third).configs.load().site.refresh_minutes == 30
    target.library.set_state("last_display", {"device": "second"})
    assert library.state("last_display") is None
    assert displays.get(third).library.state("last_display") is None
    library.set_state("art_revision:test", 7)
    assert target.library.state("art_revision:test") == 7
    # Equal website settings stay the TV's own until it links to the household's again.
    target.configs.mutate(lambda raw: raw.update(site=configs.load().site.model_dump()))
    assert configs.load().displays[0].site is not None
    target.configs.mutate(lambda raw: None, inherit_site=True)
    assert configs.load().displays[0].site is None


def test_duplicate_tv_or_untrusted_id_never_changes_configuration(configs, library):
    configs.save(Config(tv={"ip": "192.168.1.50"}))
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True)
    second = displays.add("Den", "192.168.1.51")
    before = configs.overrides.read_bytes()
    with pytest.raises(ValidationError):
        displays.add("Duplicate", "192.168.1.50")
    with pytest.raises(ValidationError):
        displays.get(second).configs.mutate(lambda raw: raw["tv"].update(ip="192.168.1.50"))
    with pytest.raises(ValueError):
        displays.get("../../outside")
    assert configs.overrides.read_bytes() == before


def test_scheduler_failure_on_one_tv_does_not_block_the_next(configs, library):
    displays = Displays(configs, library, Controller(configs, library, demo=True), demo=True)
    second = displays.add("Den", "192.168.1.51")
    def fail():
        raise OSError("offline")
    displays.get("main").controller.tick = fail
    calls = []
    displays.get(second).controller.tick = lambda: calls.append(True)
    displays.tick()
    assert calls == [True]
    assert library.state("scheduler_error")
    assert displays.get(second).library.state("scheduler_error") == ""


def test_tv_target_is_bound_to_urls_pairing_and_preview_not_a_shared_session(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    client = app.test_client()
    manager = app.extensions["displays"]
    second = manager.add("Living room", "192.168.1.51")
    client.get("/")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    calls = []
    class TV:
        def pair(self):
            calls.append(second)
    manager.get(second).controller.tv_factory = lambda cfg: TV()
    assert client.post(f"/pair?tv={second}", data={"csrf": csrf}).status_code == 302
    assert calls == [second]
    for target, image in [("main", b"first-jpeg"), (second, b"second-jpeg")]:
        manager.get(target).controller.capture = lambda *args, data=image, **kwargs: data
        response = client.post(f"/screen/preview?tv={target}", data={"csrf": csrf}, headers={"Accept": "application/json"})
        assert f"tv={target}" in response.json["image"]
        assert client.get(response.json["image"]).data == image
    assert client.get("/screen/preview.jpg?tv=main").data == b"first-jpeg"
    assert client.get(f"/tv/{second}/settings").status_code == 200
    assert client.get("/tv/unknown/settings").status_code == 404
    assert client.post("/pair?tv=unknown", data={"csrf": csrf}).status_code == 404
    # Restart preserves target IDs and the existing primary data location.
    reopened = ConfigStore(tmp_path)
    assert reopened.load().displays[0].id == second


def test_add_tv_form_is_authenticated_and_csrf_protected(tmp_path):
    app = create_app(tmp_path, password="synthetic-password-for-tests", testing=True)
    client = app.test_client()
    assert client.get("/tvs/new").status_code == 401
    import base64
    headers = {"Authorization": "Basic " + base64.b64encode(b"dayframe:synthetic-password-for-tests").decode()}
    client.get("/", headers=headers)
    assert client.post("/tvs/new", headers=headers, data={"name": "Den", "tv_ip": "192.168.1.51"}).status_code == 400
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post("/tvs/new", headers=headers, data={"csrf": token, "name": "Den", "tv_ip": "192.168.1.51"})
    assert response.status_code == 302 and response.location.startswith("/tv/") and response.location.endswith("/setup")


def test_tv_artwork_checks_and_image_cache_are_isolated_per_display(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    client = app.test_client()
    manager = app.extensions["displays"]
    second = manager.add("Living room", "192.168.1.51")
    client.get("/")
    with client.session_transaction() as session:
        csrf = session["csrf"]
    for target, image in [("main", b"first-tv-art"), (second, b"second-tv-art")]:
        screen = manager.get(target)
        class TV:
            def status(self): return {"paired": True, "known": True, "awake": True, "playing": False}
            def read_artwork(self, *args, image=image): return "art", image, "thumbnail"
        screen.controller.tv_factory = lambda cfg: TV()
        response = client.post(f"/screen/check-tv?tv={target}", data={"csrf": csrf})
        assert response.location.startswith(f"/tv/{target}?")
        revision = screen.library.state("tv_artwork")["image_sha256"]
        assert client.get(f"/screen/tv-artwork.jpg?tv={target}&v={revision}").data == image
    first_revision = manager.get("main").library.state("tv_artwork")["image_sha256"]
    assert client.get(f"/screen/tv-artwork.jpg?tv={second}&v={first_revision}").status_code == 409
    assert client.get(f"/screen/tv-artwork.jpg?tv=main&v={first_revision}").data == b"first-tv-art"


def test_shared_site_can_be_unlinked_without_js_or_losing_art_provider(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    manager = app.extensions["displays"]
    manager.configs.mutate(lambda raw: raw["site"].update(providers=["art"]))
    second = manager.add("Den", "192.168.1.51")
    client = app.test_client()
    client.get(f"/tv/{second}/settings")
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post(f"/settings?tv={second}", data={
        "csrf": token, "tv_name": "Den", "tv_ip": "192.168.1.51",
        "site_entry": "index.html", "site_refresh": "15", "timezone": "UTC",
        "site_data": "{}", "wake_at": "07:00", "change_at": "00:00", "art_enabled": "on",
        "inherited_providers": '["art"]',
        "sources": ["met"],
    })
    assert response.status_code == 302
    assert manager.configs.load().displays[0].site is not None
    assert manager.get(second).configs.load().site.providers == ["art"]
    manager.configs.mutate(lambda raw: raw["site"].update(refresh_minutes=45))
    assert manager.get(second).configs.load().site.refresh_minutes == 15
    search = client.get(f"/discover?tv={second}").text
    assert f'<input type="hidden" name="tv" value="{second}">' in search
    # Household settings lists the TV with its own website settings and can link it again.
    page = client.get("/household").text
    assert "TVs with their own website settings" in page and "Den" in page
    response = client.post("/household/follow-shared", data={"csrf": token, "display": second})
    assert response.status_code == 302 and response.location.endswith("#website")
    assert manager.configs.load().displays[0].site is None
    assert manager.get(second).configs.load().site.refresh_minutes == 45
    assert "TVs with their own website settings" not in client.get("/household").text
