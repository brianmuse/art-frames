"""The household UX pass: error pages, Add TV, Pair TV, previews before setup and the Apps empty state."""

import html

import pytest
from test_app_schedule import rest, three_apps, write  # noqa: F401  (a fixture)
from test_web import PASSWORD, auth, csrf

from art_frames import apps
from art_frames.web import create_app


def make(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    return app, app.test_client()


def test_an_unknown_tv_and_page_get_a_styled_404(tmp_path):
    _, client = make(tmp_path)
    response = client.get("/tv/nope", headers=auth())
    assert response.status_code == 404
    assert "We can&#39;t find that TV" in response.text and "It may have been removed." in response.text
    assert 'class="sidebar"' in response.text and ">Go to Home<" in response.text
    response = client.get("/no-such-page", headers=auth())
    assert response.status_code == 404 and "We can&#39;t find that page" in response.text


def test_a_form_open_too_long_gets_a_styled_400_that_reloads_its_page(tmp_path):
    _, client = make(tmp_path)
    csrf(client)
    response = client.post("/household/timezone", headers=auth(), data={"csrf": "old", "timezone": "UTC"})
    assert response.status_code == 400
    assert "This page was open too long" in response.text and "Reload it and try again." in response.text
    assert 'href="/household?tv=main">Reload<' in response.text
    # Another 400 keeps its own sentence.
    response = client.get("/discover?page=x", headers=auth())
    assert response.status_code == 400 and "That didn&#39;t work" in response.text


def test_an_untrusted_host_still_gets_a_plain_400(tmp_path):
    _, client = make(tmp_path)
    response = client.get("/", headers={**auth(), "Host": "evil.example"})
    assert response.status_code == 400 and 'class="sidebar"' not in response.text


def test_add_tv_keeps_typed_values_and_names_each_problem(tmp_path):
    app, client = make(tmp_path)
    app.extensions["configs"].mutate(lambda raw: (raw.update(tv_name="Living Room"), raw["tv"].update(ip="192.0.2.10")))
    token = csrf(client)

    def add(**form):
        return client.post("/tvs/new", headers=auth(), data={"csrf": token, **form})
    response = add(name="", tv_ip="192.0.2.11", tv_mac="AA:BB:CC:DD:EE:02")
    assert response.status_code == 400 and "Give this TV a name." in response.text
    assert 'value="192.0.2.11"' in response.text and 'value="AA:BB:CC:DD:EE:02"' in response.text
    response = add(name="Kitchen", tv_ip="192.0.2.10")
    assert "Living Room already uses 192.0.2.10. Each TV needs its own address." in response.text
    assert 'value="Kitchen"' in response.text
    assert "Another TV is named living room." in html.unescape(add(name="living room", tv_ip="192.0.2.12").text)
    assert "Use the TV&#39;s local IP address" in add(name="Kitchen", tv_ip="8.8.8.8").text
    assert "Use a MAC address" in add(name="Kitchen", tv_ip="192.0.2.12", tv_mac="nope").text
    response = add(name="Kitchen", tv_ip="192.0.2.12")
    assert response.status_code == 302
    assert [item.name for item in app.extensions["configs"].load().displays] == ["Kitchen"]
    for index in range(6):
        app.extensions["displays"].add(f"TV {index}", f"192.0.2.{20 + index}")
    response = add(name="Ninth", tv_ip="192.0.2.40")
    assert "Art Frames supports up to eight TVs. Remove one to add another." in response.text


def test_pair_needs_a_saved_address(tmp_path):
    app, client = make(tmp_path)
    page = client.get("/tv/main/settings", headers=auth()).text
    assert 'form="pair-tv" disabled>Pair TV' in page and "Save the TV's address first." in page
    response = client.post("/pair", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Save the TV&#39;s address first." in response.text
    assert app.extensions["jobs"].snapshot()["queued"] == []


def test_a_preview_waits_for_app_setup(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", {"id": "photos", "name": "Photos", "settings": [
        {"key": "album_url", "label": "Shared album link", "type": "secret"}]})
    app, client = make(tmp_path)
    page = client.get("/layouts/photos", headers=auth()).text
    assert 'type="submit" disabled>Make a preview' in page and "Add the shared album link to make a preview." in page
    token = csrf(client)
    response = client.post("/screen/preview", headers={**auth(), "Accept": "application/json"},
                           data={"csrf": token, "app": "photos"})
    assert response.status_code == 409 and response.json["message"] == "Add the shared album link to make a preview."
    response = client.post("/screen/preview", headers=auth(), data={"csrf": token, "app": "photos"})
    assert response.status_code == 302 and response.location.endswith("#field-album_url")
    assert app.extensions["jobs"].snapshot()["queued"] == []


def test_the_apps_page_says_how_to_install_an_app(tmp_path):
    _, client = make(tmp_path)
    home = client.get("/", headers=auth()).text
    assert ">Layouts</span>" in home  # the sidebar shows Layouts with no layout installed
    page = client.get("/layouts", headers=auth()).text
    assert "No layouts yet" in page and "<code data-copy-source>npm run deploy</code>" in page
    assert "cli-deployment.md#layouts" in page
    assert "Layouts show a calendar, news or your photos at the times you choose. Artwork shows the rest of the day." in page


def test_home_says_what_each_tv_shows(tmp_path):
    _, client = make(tmp_path)
    page = client.get("/", headers=auth()).text
    assert "What each TV shows now." in page and "Select a TV to see what it shows" not in page


def test_advanced_settings_sit_in_a_row_that_counts_changes(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", {"id": "photos", "name": "Photos", "settings": [
        {"key": "order", "label": "Order", "type": "choice", "choices": ["Shuffle", "Newest first"], "default": "Shuffle"},
        {"key": "fit", "label": "Fit", "type": "choice", "choices": ["contain", "cover"], "default": "contain",
         "advanced": True},
        {"key": "sync", "label": "Sync", "type": "choice", "choices": ["1", "3"], "default": "1", "advanced": True}]})
    app, client = make(tmp_path)
    page = client.get("/layouts/photos", headers=auth()).text
    row = page.split('id="settings-advanced"')[1].split("</details>")[0]
    assert 'name="fit"' in row and 'name="sync"' in row and 'name="order"' not in row
    assert "changed</span>" not in row and row.startswith(" data-advanced-row >")
    store = apps.find("photos").store(tmp_path / "data")
    client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "order": "Shuffle", "fit": "cover", "sync": "1"})
    assert ">1 changed</span>" in client.get("/layouts/photos", headers=auth()).text
    # A failed save opens the row, so an error inside it shows.
    response = client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "fit": "nope"})
    assert 'id="settings-advanced" data-advanced-row open' in response.text


def test_tv_connection_keeps_mac_and_broadcast_under_advanced(tmp_path):
    app, client = make(tmp_path)
    page = client.get("/tv/main/settings", headers=auth()).text
    row = page.split('id="connection-advanced"')[1].split("</details>")[0]
    assert 'name="tv_mac"' in row and 'name="broadcast"' in row and "changed</span>" not in row
    assert 'id="tv-search-network"' in page and "Search a different network" in page
    app.extensions["configs"].mutate(lambda raw: raw["tv"].update(mac="AA:BB:CC:DD:EE:01"))
    assert ">1 changed</span>" in client.get("/tv/main/settings", headers=auth()).text
    response = client.post("/tv/main/settings", headers=auth(), data={
        "csrf": csrf(client), "tv_name": "Frame", "tv_ip": "192.0.2.10", "tv_mac": "nope", "broadcast": "255.255.255.255"})
    assert 'id="connection-advanced" data-advanced-row open' in response.text


PACE = {"key": "pace", "label": "Pace", "style": "cards", "fields": ["photo_interval", "sync_interval"], "options": [
    {"label": "Slow", "description": "A new photo each hour.", "refresh_minutes": 60,
     "values": {"photo_interval": "60 minutes", "sync_interval": "Every 3 hours"}},
    {"label": "Normal", "description": "A new photo every 15 minutes.", "refresh_minutes": 15,
     "values": {"photo_interval": "15 minutes", "sync_interval": "Every 3 hours"}},
    {"label": "Lively", "description": "A new photo every 5 minutes.", "refresh_minutes": 5,
     "values": {"photo_interval": "5 minutes", "sync_interval": "Every hour"}},
    {"label": "One photo a day", "values": {"photo_interval": "Once a day", "sync_interval": "Every 6 hours"}}]}
PHOTOS = {"id": "photos", "name": "Photos", "presets": [PACE], "settings": [
    {"key": "photo_interval", "label": "Change photo every", "type": "choice", "default": "15 minutes",
     "choices": ["5 minutes", "15 minutes", "30 minutes", "60 minutes", "Once a day"]},
    {"key": "sync_interval", "label": "Check the album", "type": "choice", "default": "Every 3 hours",
     "choices": ["Every hour", "Every 3 hours", "Every 6 hours"], "advanced": True},
    {"key": "show_extra", "label": "Extra", "type": "checkbox", "default": False}]}


def test_an_app_preset_sets_exactly_its_values(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", PHOTOS)
    app, client = make(tmp_path)
    store = apps.find("photos").store(tmp_path / "data")
    page = client.get("/layouts/photos", headers=auth()).text
    assert 'name="preset.pace" value="1" checked' in page  # the defaults are Normal
    assert "Refresh every" not in page  # the household refresh is 15 minutes
    expected = [("60 minutes", "Every 3 hours"), ("15 minutes", "Every 3 hours"), ("5 minutes", "Every hour"),
                ("Once a day", "Every 6 hours")]
    for index, (interval, sync) in enumerate(expected):
        # app.js writes the option's values into the fields, so the form posts them.
        client.post("/layouts/photos", headers=auth(), data={
            "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "preset.pace": str(index),
            "photo_interval": interval, "sync_interval": sync, "show_extra": "on"})
        assert store.values(store.manifest()) == {"photo_interval": interval, "sync_interval": sync, "show_extra": True}
    # Without JavaScript the hidden fields keep older values: the chosen preset still wins.
    client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "preset.pace": "2",
        "photo_interval": "30 minutes", "sync_interval": "Every 6 hours"})
    assert store.values(store.manifest())["photo_interval"] == "5 minutes"
    # Custom keeps the typed values.
    client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "preset.pace": "custom",
        "photo_interval": "30 minutes", "sync_interval": "Every hour"})
    assert store.values(store.manifest()) == {"photo_interval": "30 minutes", "sync_interval": "Every hour",
                                              "show_extra": False}
    assert 'name="preset.pace" value="custom" checked' in client.get("/layouts/photos", headers=auth()).text


def test_a_fast_preset_offers_a_faster_household_refresh(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", PHOTOS)
    app, client = make(tmp_path)
    store = apps.find("photos").store(tmp_path / "data")
    client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "preset.pace": "2"})
    page = client.get("/layouts/photos", headers=auth()).text
    assert "Art Frames refreshes layouts every 15 min, so Photos changes at most that often." in page
    assert 'form="app-refresh" name="minutes" value="5">Refresh every 5 min<' in page
    response = client.post("/layouts/photos/refresh", headers=auth(), data={"csrf": csrf(client), "minutes": "5"},
                           follow_redirects=True)
    assert "Saved. Layouts refresh every 5 min, starting with the next update." in response.text
    assert app.extensions["displays"].configs.load().site.refresh_minutes == 5
    assert "Art Frames refreshes layouts every" not in response.text
    assert client.post("/layouts/photos/refresh", headers=auth(), data={"csrf": csrf(client), "minutes": "7"}).status_code == 400


def test_a_manifest_preset_must_match_its_settings():
    from pydantic import ValidationError

    from art_frames.layouts import Manifest
    Manifest.model_validate(PHOTOS)
    unknown = {**PACE, "fields": ["nope"]}
    wrong = {**PACE, "options": [{"label": "Bad", "values": {"photo_interval": "2 minutes"}}]}
    outside = {**PACE, "fields": ["photo_interval"]}
    for preset in (unknown, wrong, outside):
        with pytest.raises(ValidationError):
            Manifest.model_validate({**PHOTOS, "presets": [preset]})


@pytest.mark.parametrize("preset, typed, minutes", [
    ("often", "", 5), ("normal", "", 15), ("calm", "", 60), ("custom", "30", 30), ("calm", "30", 60)])
def test_refresh_presets_set_exact_minutes(tmp_path, preset, typed, minutes):
    app, client = make(tmp_path)
    response = client.post("/household/refresh", headers=auth(), follow_redirects=True,
                           data={"csrf": csrf(client), "preset": preset, "refresh": typed})
    assert app.extensions["displays"].configs.load().site.refresh_minutes == minutes
    assert f"Layouts refresh every {minutes if minutes < 60 else 1} {'min' if minutes < 60 else 'hour'}" in response.text
    expected = {5: "often", 15: "normal", 60: "calm"}.get(minutes, "custom")
    assert f'name="preset" value="{expected}" checked' in response.text


def test_setup_finds_names_and_saves_the_tv(tmp_path):
    app, client = make(tmp_path)
    page = client.get("/tv/main/setup", headers=auth()).text
    assert 'aria-current="step"><span class="setup-number">1</span>Find the TV' in page
    assert "data-autostart" in page and "Search a different network" in page and "Enter the address yourself" in page
    token = csrf(client)
    response = client.post("/tv/main/setup", headers=auth(), data={"csrf": token, "tv_name": "Den", "tv_ip": ""})
    assert response.status_code == 400 and "Choose your TV, or enter its address yourself." in response.text
    response = client.post("/tv/main/setup", headers=auth(), data={
        "csrf": token, "tv_name": "Den", "tv_ip": "192.0.2.10", "tv_mac": "AA:BB:CC:DD:EE:01"})
    assert response.status_code == 302 and response.location.endswith("/tv/main/setup")
    cfg = app.extensions["configs"].load()
    assert (cfg.tv_name, cfg.tv.ip, cfg.tv.mac) == ("Den", "192.0.2.10", "AA:BB:CC:DD:EE:01")
    page = client.get("/tv/main/setup", headers=auth()).text
    assert "Pair Den" in page and ">Change the address<" in page and "data-autostart" not in page
    assert "Find the TV" in client.get("/tv/main/setup?step=find", headers=auth()).text


def test_setup_pairs_then_offers_plans(tmp_path, monkeypatch):
    app, client = make(tmp_path)
    app.extensions["configs"].mutate(lambda raw: (raw.update(tv_name="Den"), raw["tv"].update(ip="192.0.2.10")))
    jobs, controller = app.extensions["jobs"], app.extensions["controller"]

    def refuse():
        raise ValueError("no answer")
    monkeypatch.setattr(controller, "pair", refuse)
    response = client.post("/tv/main/setup/pair", headers=auth(), data={"csrf": csrf(client)})
    assert response.status_code == 302 and "/tv/main/setup?job=" in response.location
    jobs.drain()
    page = client.get(response.location, headers=auth()).text
    assert "Den did not answer. Check that it is on and that the address is right, then try again." in page
    assert ">Try again<" in page and ">Change the address<" in page
    # The TV answered, but nobody selected Allow.

    def no_allow():
        raise ValueError("Pairing was not completed. Press Allow on the TV and try again.")
    monkeypatch.setattr(controller, "pair", no_allow)
    response = client.post("/tv/main/setup/pair", headers=auth(), data={"csrf": csrf(client)})
    jobs.drain()
    page = client.get(response.location, headers=auth()).text
    assert "Den answered, but Allow was not selected on the TV." in page and "did not answer" not in page
    monkeypatch.setattr(controller, "pair", lambda: None)
    response = client.post("/tv/main/setup/pair", headers=auth(), data={"csrf": csrf(client)})
    jobs.drain()
    page = client.get(response.location, headers=auth()).text
    assert "Den is paired" in page and "Choose what it shows" in page
    # Home and the sidebar agree that Den is paired.
    assert app.extensions["displays"].get("main").library.state("tv_status")["paired"] is True
    assert ">Pair Den</a>" not in client.get("/", headers=auth()).text
    # A new address needs pairing again.
    client.post("/tv/main/setup", headers=auth(), data={"csrf": csrf(client), "tv_name": "Den", "tv_ip": "192.0.2.11"})
    assert "Pair Den" in client.get("/tv/main/setup", headers=auth()).text


def test_add_tv_opens_the_setup_and_home_links_pairing_to_it(tmp_path):
    app, client = make(tmp_path)
    page = client.get("/tvs/new", headers=auth()).text
    assert "data-autostart" in page and "Enter the address yourself" in page
    response = client.post("/tvs/new", headers=auth(), data={"csrf": csrf(client), "name": "Den", "tv_ip": "192.0.2.12"})
    assert response.status_code == 302 and response.location.endswith("/setup")
    home = client.get("/", headers=auth()).text
    assert "/setup\">Pair Den</a>" in home


def checklist(client):
    page = client.get("/", headers=auth()).text
    return page.split('id="checklist-title"')[1].split("</section>")[0] if "checklist-title" in page else ""


def test_home_checklist_follows_each_step(three_apps, tmp_path):  # noqa: F811
    app, client = make(tmp_path)
    configs, library = app.extensions["configs"], app.extensions["displays"].get("main").library
    text = checklist(client)
    assert "Set up your first TV" in text and "1 of 5" in text
    assert 'class="next"' in text and "/tv/main/setup\">Start<" in text
    assert "data-browser-zone hidden" in text and 'value="UTC">Keep UTC<' in text
    configs.mutate(lambda raw: raw["tv"].update(ip="192.0.2.10", mac="AA:BB:CC:DD:EE:01"))
    library.set_state("tv_status", {"paired": True})
    assert "3 of 5" in checklist(client)
    client.post("/home/checklist/timezone", headers=auth(), data={"csrf": csrf(client), "timezone": "america/new_york"})
    assert app.extensions["displays"].configs.load().timezone == "America/New_York"
    assert "4 of 5" in checklist(client)
    configs.mutate(lambda raw: raw["app_schedule"]["week"]["default"].update(wake="07:00"))
    assert "4 of 5" in checklist(client)  # a turn-on time alone schedules no app
    configs.mutate(lambda raw: raw["app_schedule"]["week"]["default"].update(
        blocks=[{"app": "news", "start": "07:00", "end": "09:00"}]))
    text = checklist(client)
    assert "5 of 5" in text and "Setup is done" in text and "This list goes away tomorrow." in text
    # A day later the list is gone.
    library.set_state("checklist_done_at", "2000-01-01T00:00:00+00:00")
    assert checklist(client) == ""


def test_keep_utc_and_hide_the_list(tmp_path):
    app, client = make(tmp_path)
    assert "Art Frames uses UTC." in client.get("/household", headers=auth()).text
    client.post("/home/checklist/timezone", headers=auth(), data={"csrf": csrf(client), "timezone": "UTC"})
    assert "Set your timezone" in checklist(client) and "1 of 3" in checklist(client)
    response = client.post("/home/checklist/timezone", headers=auth(), data={"csrf": csrf(client), "timezone": "Mars/Base"},
                           follow_redirects=True)
    assert "Art Frames does not know that timezone." in response.text
    response = client.post("/home/checklist/dismiss", headers=auth(), data={"csrf": csrf(client)}, follow_redirects=True)
    assert "The setup list is hidden." in response.text and checklist(client) == ""


def test_saves_say_when_the_change_shows(three_apps, tmp_path):  # noqa: F811
    app, client = make(tmp_path)
    response = client.post("/household/timezone", headers=auth(), data={"csrf": csrf(client), "timezone": "Europe/London"},
                           follow_redirects=True)
    assert "Saved. Schedules use Europe/London now." in response.text
    response = client.post("/household/artwork", headers=auth(), follow_redirects=True, data={
        "csrf": csrf(client), "enabled": "on", "order": "shuffle", "change_at": "04:00"})
    assert "Saved. The next artwork comes at 4:00 AM." in response.text
    store = apps.find("news").store(tmp_path / "data")

    def save_news():
        return client.post("/layouts/news", headers=auth(), follow_redirects=True, data={
            "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "location": "Lisbon"}).text
    assert "Saved.</p>" in save_news()  # no TV shows News yet
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule=rest("news")))
    assert "Saved. TVs that show News change at their next update, within 15 min." in save_news()


def test_quiet_footer_and_plain_app_rows(three_apps, tmp_path):  # noqa: F811
    _, client = make(tmp_path)
    page = client.get("/layouts", headers=auth()).text
    assert "Scheduler running" not in page
    assert 'Not on any TV yet. <a href="/tv/main/schedule#add-news">Add it to' in page
