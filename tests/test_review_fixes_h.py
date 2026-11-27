"""Fixes from the security, saboteur and UX reviews of the household pass. Each test fails without its fix."""

import html
from pathlib import Path

import pytest
from test_app_schedule import three_apps, write  # noqa: F401  (a fixture)
from test_household_ux import PACE, PHOTOS
from test_web import PASSWORD, auth, csrf

from art_frames import apps, tv_recovery
from art_frames.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "art_frames" / "static"


def make(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(tv_name="Living Room"))
    return app, app.test_client()


def test_static_files_before_sign_in_are_only_the_sign_in_files(tmp_path):
    _, client = make(tmp_path)
    for name in ("style.css", "favicon.svg", "Inter-Regular.ttf", "Inter-SemiBold.ttf"):
        assert client.get(f"/static/{name}").status_code == 200
    # Other static files, and missing ones, need a password. Nothing shows the app shell or a TV name.
    for path in ("/static/app.js", "/static/missing.css"):
        response = client.get(path)
        assert response.status_code == 401
        assert 'class="sidebar"' not in response.text and "Living Room" not in response.text


def test_an_error_before_sign_in_is_plain(tmp_path):
    app, _ = make(tmp_path)
    with app.test_request_context("/static/missing.css"):
        from werkzeug.exceptions import NotFound
        response = app.handle_user_exception(NotFound())
        assert 'class="sidebar"' not in str(response) and "Living Room" not in str(response)


def test_sign_in_from_an_untrusted_host_is_not_a_500(tmp_path):
    _, client = make(tmp_path)
    response = client.get("/", headers={"Host": "evil.example"})
    assert response.status_code in (400, 401)
    assert 'class="sidebar"' not in response.text


def test_a_signed_in_404_keeps_the_app_shell(tmp_path):
    _, client = make(tmp_path)
    response = client.get("/static/missing.css", headers=auth())
    assert response.status_code == 404 and 'class="sidebar"' in response.text


# 5, 6, 7: preset fields sit under Custom, never under Advanced; options carry their values for app.js.

def test_preset_fields_sit_under_custom_not_advanced(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", PHOTOS)
    _, client = make(tmp_path)
    page = client.get("/layouts/photos", headers=auth()).text
    custom = page.split('class="preset-custom stack">')[1].split("</fieldset>")[0]
    assert 'name="photo_interval"' in custom and 'name="sync_interval"' in custom
    assert 'id="settings-advanced"' not in page  # its only advanced field belongs to the preset
    assert page.count('name="sync_interval"') == 1


def test_preset_options_carry_their_values_for_app_js(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", PHOTOS)
    _, client = make(tmp_path)
    page = html.unescape(client.get("/layouts/photos", headers=auth()).text)
    assert ('data-values=\'{"photo_interval": "5 minutes", "sync_interval": "Every hour"}\' '
            'name="preset.pace" value="2"') in page
    household = html.unescape(client.get("/household", headers=auth()).text)
    assert 'data-values=\'{"refresh": "5"}\' name="preset" value="often"' in household
    script = (STATIC / "app.js").read_text()
    assert 'document.querySelectorAll("[data-preset]")' in script and "JSON.parse(choice.dataset.values)" in script


def test_a_preset_is_one_column(three_apps, tmp_path):  # noqa: F811
    css = (STATIC / "style.css").read_text()
    assert ".preset-group { display: flex; flex-direction: column;" in css
    # The group must not also carry the card row's grid class, or Custom sits beside the cards.
    write(three_apps / "apps" / "photos" / "layout.json", PHOTOS)
    _, client = make(tmp_path)
    page = client.get("/layouts/photos", headers=auth()).text
    group = page.split("data-preset>")[0].rsplit("<fieldset", 1)[1]
    assert "preset-group" in group and "preset-cards" not in group


# 9: the checklist counts only scheduled apps, skips upgraded households and never comes back once done.

def checklist(client):
    page = client.get("/", headers=auth()).text
    return page.split('class="card settings-panel checklist"')[1].split("</section>")[0] if "checklist-title" in page else ""


def test_the_checklist_skips_an_app_no_schedule_uses(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", {"id": "photos", "name": "Photos", "settings": [
        {"key": "album_url", "label": "Album", "type": "secret"}]})
    _, client = make(tmp_path)
    text = checklist(client)
    assert "Finish setting up Photos" not in text and "Finish layout setup" in text


def test_an_upgraded_household_never_sees_the_checklist(tmp_path):
    app, client = make(tmp_path)
    app.extensions["configs"].mutate(lambda raw: (raw["tv"].update(ip="192.0.2.10", mac="AA:BB:CC:DD:EE:01"),
                                                  raw["app_schedule"]["week"]["default"].update(wake="07:00")))
    app.extensions["displays"].get("main").library.set_state("tv_status", {"paired": True})
    assert checklist(client) == ""
    app.extensions["configs"].mutate(lambda raw: raw["app_schedule"]["week"]["default"].update(wake=None))
    assert checklist(client) == ""  # still hidden: the first visit decided


def test_a_done_checklist_never_comes_back(tmp_path):
    app, client = make(tmp_path)
    library = app.extensions["displays"].get("main").library
    assert "Set up your first TV" in checklist(client)
    library.set_state("checklist_done_at", "2999-01-01T00:00:00+00:00")  # done once, earlier
    assert checklist(client) == ""


def test_the_checklist_is_not_done_before_an_app_can_be_chosen(three_apps, tmp_path, monkeypatch):  # noqa: F811
    app, client = make(tmp_path)
    library = app.extensions["displays"].get("main").library
    checklist(client)  # the first visit: a new household sees the list
    app.extensions["configs"].mutate(lambda raw: (raw["tv"].update(ip="192.0.2.10"), raw.update(timezone="America/New_York")))
    app.extensions["displays"].configs.mutate(lambda raw: raw.update(timezone="America/New_York"))
    library.set_state("tv_status", {"paired": True})
    real = apps.usable
    monkeypatch.setattr(apps, "usable", lambda *args, **kwargs: [])  # no app is installed yet
    checklist(client)
    assert library.state("checklist_done_at") is None
    monkeypatch.setattr(apps, "usable", real)  # an app arrives later
    text = checklist(client)
    assert "Choose when layouts show on Living Room" in text and library.state("checklist_done_at") is None


# 10: Keep UTC hides the UTC banner.

def test_keep_utc_hides_the_banner(tmp_path):
    _, client = make(tmp_path)
    assert "Art Frames uses UTC." in client.get("/household", headers=auth()).text
    client.post("/home/checklist/timezone", headers=auth(), data={"csrf": csrf(client), "timezone": "UTC"})
    assert "Art Frames uses UTC." not in client.get("/household", headers=auth()).text


# 11, 12: preset refresh minutes are refresh choices; only ASCII digits count as numbers.

def test_a_preset_refresh_must_be_a_refresh_choice():
    from pydantic import ValidationError

    from art_frames.layouts import Manifest
    odd = {**PACE, "options": [{**PACE["options"][0], "refresh_minutes": 7}]}
    with pytest.raises(ValidationError):
        Manifest.model_validate({**PHOTOS, "presets": [odd]})


def test_unicode_digits_are_not_numbers(three_apps, tmp_path):  # noqa: F811
    write(three_apps / "apps" / "photos" / "layout.json", PHOTOS)
    _, client = make(tmp_path)
    store = apps.find("photos").store(tmp_path / "data")
    response = client.post("/layouts/photos", headers=auth(), data={
        "csrf": csrf(client), "manifest": store.fingerprint(store.manifest()), "preset.pace": "²",
        "photo_interval": "30 minutes", "sync_interval": "Every hour"})
    assert response.status_code == 302 and store.values(store.manifest())["photo_interval"] == "30 minutes"
    assert client.post("/layouts/photos/refresh", headers=auth(), data={"csrf": csrf(client), "minutes": "⁵"}).status_code == 400


# 14: the setup's Find step clears an address-recovery proposal, like TV settings.

def test_the_find_step_clears_a_recovery_proposal(tmp_path, monkeypatch):
    app, client = make(tmp_path)
    cleared = []
    recovery = app.extensions["controller"].recovery
    if recovery is not None:
        monkeypatch.setattr(recovery, "clear_proposal", lambda cfg: cleared.append(cfg.tv.ip))
    else:
        monkeypatch.setattr(tv_recovery, "clear_proposal", lambda library, cfg: cleared.append(cfg.tv.ip))
    client.post("/tv/main/setup", headers=auth(), data={"csrf": csrf(client), "tv_name": "Den", "tv_ip": "192.0.2.12"})
    assert cleared == ["192.0.2.12"]


# 17-21: copy and small layout changes.

def test_refresh_cards_show_their_minutes(tmp_path):
    _, client = make(tmp_path)
    page = client.get("/household", headers=auth()).text
    assert "Every 5 min. For photos and clocks." in page and "Every 15 min. Good for most layouts." in page
    assert "Every hour. For art and dashboards that change slowly." in page and "Choose the minutes yourself." in page


def test_plans_explain_their_bars_and_weekends(three_apps, tmp_path):  # noqa: F811
    _, client = make(tmp_path)
    page = client.get("/tv/main/plans", headers=auth()).text
    assert 'class="small plan-legend"' in page and "Artwork" in page and "Calendar hours" not in page
    from art_frames import starters
    assert starters.find("weekdays").line.endswith("No turn-on time on weekends.")


def test_first_run_home_has_one_primary_button(tmp_path):
    _, client = make(tmp_path)
    page = client.get("/", headers=auth()).text
    assert 'class="button outline sm" type="submit" name="timezone" value="" data-browser-zone' in page
    assert '<a class="button outline sm" href="/tv/main/setup">Pair Living Room</a>' in page
    assert page.count('<a class="button sm"') == 1  # the next step's Start
    script = (STATIC / "app.js").read_text()
    assert 'button.textContent = `Use ${zone.split("/").pop().replace(/_/g, " ")} time`;' in script
    assert "if (location.hash === `#${row.id}`) row.open = true;" in script


# Fix review: Deny or a timed-out prompt on the TV raises a samsungtvws ConnectionFailure. The TV answered.

def test_a_denied_or_timed_out_pair_says_allow_was_not_selected(tmp_path, monkeypatch):
    from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
    app, client = make(tmp_path)
    app.extensions["configs"].mutate(lambda raw: (raw.update(tv_name="Den"), raw["tv"].update(ip="192.0.2.10")))
    jobs, controller = app.extensions["jobs"], app.extensions["controller"]

    def pair_page(error):
        def fail():
            raise error
        monkeypatch.setattr(controller, "pair", fail)
        response = client.post("/tv/main/setup/pair", headers=auth(), data={"csrf": csrf(client)})
        jobs.drain()
        return client.get(response.location, headers=auth()).text
    for error in (UnauthorizedError({"event": "ms.channel.unauthorized"}),
                  ConnectionFailure({"event": "ms.channel.timeOut"})):
        page = pair_page(error)
        assert "Den answered, but Allow was not selected on the TV." in page and "did not answer" not in page
    for error in (ConnectionRefusedError(61, "refused"), TimeoutError("timed out"), OSError("no route")):
        assert "Den did not answer." in pair_page(error)
