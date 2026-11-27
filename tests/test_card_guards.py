"""Guards of the per-card saves: older pages, locked fields, settings a card shows only sometimes, and the TV."""

import re

import pytest
from test_app_schedule import three_apps  # noqa: F401  (a fixture)
from test_schedule_page import post, site, version  # noqa: F401  (site is a fixture)
from test_web import PASSWORD, auth, csrf

from art_frames import apps, tv_recovery
from art_frames.tv_recovery import AddressRecovery
from art_frames.web import create_app, options_fingerprint

TV_METHODS = ("preview_site", "send_preview", "show_app", "check_artwork", "display_settings", "set_brightness",
              "set_light_sensor", "set_option", "screen_off", "check_screen", "screen_on", "standby_load", "pair",
              "tick", "wake_screen", "wake_if_due", "swap_if_due", "run_wake", "run_once")


def test_tv_settings_keeps_an_address_found_after_the_page_opened_and_clears_the_proposal(site, monkeypatch):  # noqa: F811
    app, configs, client = site
    configs.mutate(lambda raw: raw["tv"].update(ip="192.168.1.148"))  # address recovery saved a new address
    cleared = []
    recovery = app.extensions["controller"].recovery
    if recovery is not None:
        monkeypatch.setattr(recovery, "clear_proposal", lambda cfg: cleared.append(cfg.tv.ip))
    else:
        monkeypatch.setattr(tv_recovery, "clear_proposal", lambda library, cfg: cleared.append(cfg.tv.ip))
    old = {"tv_name": "Frame 1", "tv_ip": "192.168.1.50", "tv_ip_shown": "192.168.1.50", "tv_mac": "", "broadcast": ""}
    assert post(client, "/tv/main/settings", old).status_code == 302
    assert configs.load().tv.ip == "192.168.1.148" and cleared == ["192.168.1.148"]
    assert post(client, "/tv/main/settings", {**old, "tv_ip": "192.168.1.70"}).status_code == 302
    assert configs.load().tv.ip == "192.168.1.70" and cleared[-1] == "192.168.1.70"
    response = post(client, "/tv/main/settings", {**old, "tv_ip": "8.8.8.8"})
    assert response.status_code == 400 and len(cleared) == 2  # a failed save keeps the proposal


def test_installed_apps_lock_the_website_folder_and_entry(site):  # noqa: F811
    _, configs, client = site
    before = configs.load().site
    response = post(client, "/household/website", {"path": "elsewhere", "entry": "other.html",
                                                  "ready_selector": "#ready", "data": '{"city": "Lisbon"}'})
    assert response.status_code == 302 and response.location.endswith("#website")
    after = configs.load().site
    assert (after.path, after.entry) == (before.path, before.entry)
    assert (after.ready_selector, after.data) == ("#ready", {"city": "Lisbon"})


@pytest.mark.parametrize("data", ["{not json", "[1, 2]", '"text"'])
def test_website_page_data_must_be_a_json_object(site, data):  # noqa: F811
    _, configs, client = site
    before = configs.load()
    response = post(client, "/household/website", {"path": "", "entry": "index.html", "data": data})
    assert response.status_code == 400 and "Page data must be a JSON object" in response.text
    assert configs.load() == before


def test_artwork_supply_changes_only_when_the_card_shows_it(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    configs, client = app.extensions["configs"], app.test_client()
    fields = {"enabled": "on", "order": "shuffle", "change_at": "00:00", "sources": ["met"]}
    assert post(client, "/household/artwork", {**fields, "supply_shown": "1", "supply": "on"}).status_code == 302
    assert configs.load().site.providers == ["art"]
    assert post(client, "/household/artwork", fields).status_code == 302  # a page without the switch
    assert configs.load().site.providers == ["art"]
    assert post(client, "/household/artwork", {**fields, "supply_shown": "1"}).status_code == 302
    assert configs.load().site.providers == []


def test_no_card_save_contacts_the_tv(site, monkeypatch):  # noqa: F811
    app, configs, client = site
    controller = app.extensions["controller"]

    def no_tv(*args, **kwargs):
        raise AssertionError("A settings save must not contact the TV")
    controller.tv_factory = no_tv
    for name in TV_METHODS:
        if hasattr(controller, name):
            monkeypatch.setattr(controller, name, no_tv)
    configs.mutate(lambda raw: raw["tv"].update(ip="192.168.1.50", mac="AA:BB:CC:DD:EE:01"))
    block = {"app": "photos", "start": "18:00", "end": "21:00"}
    for path, data in (
            ("/tv/main/schedule/turns", {"swap": "120"}),
            ("/tv/main/schedule/every/blocks", block),
            ("/tv/main/schedule/every/blocks/0", {"start": "07:00", "end": "10:00"}),
            ("/tv/main/schedule/mon/customize", {}),
            ("/tv/main/schedule/mon/blocks/0/delete", {}),
            ("/tv/main/schedule/mon/reset", {}),
            ("/tv/main/schedule/every/wake", {"at": "06:30"}),
            ("/tv/main/schedule/every/wake/delete", {}),
            ("/tv/main/picture/mats", {"mats": "never"}),
            ("/tv/main/settings", {"tv_name": "Frame 1", "tv_ip": "192.168.1.50", "tv_mac": "AA:BB:CC:DD:EE:01"}),
            ("/household/timezone", {"timezone": "UTC"}),
            ("/household/refresh", {"refresh": "30"}),
            ("/household/artwork", {"order": "shuffle", "change_at": "00:00", "sources": ["met"]}),
            ("/household/website", {"path": "", "entry": "index.html", "data": "{}"}),
            ("/household/follow-shared", {"display": "nope"})):
        response = post(client, path, {"schedule_version": version(client), **data})
        assert response.status_code == 302, (path, response.status_code)


def test_follow_shared_links_a_tv_to_the_household_website(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    displays, client = app.extensions["displays"], app.test_client()
    second = displays.add("Den", "192.168.1.51")
    displays.get(second).configs.mutate(lambda raw: raw["site"].update(refresh_minutes=5), inherit_site=False)
    own = next(item for item in displays.configs.load().displays if item.id == second)
    assert own.site is not None and own.site.refresh_minutes == 5
    page = client.get("/household", headers=auth()).text
    assert "Den: every 5 min." in page and "Use household settings" in page
    page = client.get(f"/tv/{second}/schedule", headers=auth()).text
    assert "Den uses its own website settings, so it refreshes every 5 min." in page
    response = post(client, "/household/follow-shared", {"display": second})
    assert response.status_code == 302 and response.location.endswith("#website")
    assert next(item for item in displays.configs.load().displays if item.id == second).site is None
    response = post(client, "/household/follow-shared", {"display": "f" * 32}, follow_redirects=True)
    assert "This TV no longer exists." in response.text


def test_a_tv_with_its_own_website_keeps_it_when_its_schedule_saves(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    displays, client = app.extensions["displays"], app.test_client()
    second = displays.add("Den", "192.168.1.51")
    displays.get(second).configs.mutate(lambda raw: raw["site"].update(refresh_minutes=5), inherit_site=False)
    displays.get(second).configs.mutate(lambda raw: raw["tv"].update(mac="AA:BB:CC:DD:EE:02"))
    page = client.get(f"/tv/{second}/schedule", headers=auth()).text
    drawn = re.search(r'name="schedule_version" value="([0-9a-f]+)"', page).group(1)
    response = post(client, f"/tv/{second}/schedule/every/wake", {"at": "06:30", "schedule_version": drawn})
    assert response.status_code == 302
    own = next(item for item in displays.configs.load().displays if item.id == second).site
    assert own is not None and own.refresh_minutes == 5


def own_copy(displays, second):
    """Den keeps its own website settings, equal to the household's."""
    displays.get(second).configs.mutate(lambda raw: None, inherit_site=False)
    own = next(item for item in displays.configs.load().displays if item.id == second).site
    assert own is not None and own == displays.configs.load().site
    return own


def test_app_tv_settings_keep_a_tv_s_own_website_settings(site):  # noqa: F811
    app, _, client = site
    displays = app.extensions["displays"]
    second = displays.add("Den", "192.168.1.51")
    own = own_copy(displays, second)
    response = client.post("/site/tv-settings", headers=auth(), data={
        "csrf": csrf(client), "manifest": "morning", "options": options_fingerprint(apps.find("morning").manifest), "tvs": ["main", second]})
    assert response.status_code == 302
    assert next(item for item in displays.configs.load().displays if item.id == second).site == own


def test_address_recovery_keeps_a_tv_s_own_website_settings(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    displays = app.extensions["displays"]
    second = displays.add("Den", "192.168.1.51", "AA:BB:CC:DD:EE:01")
    own = own_copy(displays, second)
    screen = displays.get(second)
    recovery = AddressRecovery(screen.configs, screen.library, find=lambda mac, networks: [],
                               probe=lambda ip: None)
    assert recovery._move(screen.configs.load().tv, "192.168.1.77") == "updated"
    den = next(item for item in displays.configs.load().displays if item.id == second)
    assert den.tv.ip == "192.168.1.77" and den.site == own
    # A linked TV stays linked.
    third = displays.add("Office", "192.168.1.52", "AA:BB:CC:DD:EE:02")
    office = displays.get(third)
    AddressRecovery(office.configs, office.library, find=lambda mac, networks: [], probe=lambda ip: None)._move(
        office.configs.load().tv, "192.168.1.78")
    assert next(item for item in displays.configs.load().displays if item.id == third).site is None


def test_follow_shared_reports_a_failed_save_under_its_card(tmp_path, monkeypatch):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    displays, client = app.extensions["displays"], app.test_client()
    second = displays.add("Den", "192.168.1.51")
    own_copy(displays, second)

    def broken(change):
        raise OSError("disk full")
    monkeypatch.setattr(displays.configs, "mutate", broken)
    response = post(client, "/household/follow-shared", {"display": second})
    assert response.status_code == 400 and "The settings could not be saved. Try again." in response.text
    assert 'name="path"' in response.text  # the website card keeps the saved values


def test_deeply_nested_page_data_is_refused(site):  # noqa: F811
    _, configs, client = site
    before = configs.load()
    response = post(client, "/household/website", {"path": "", "entry": "index.html", "data": "[" * 100000})
    assert response.status_code == 400 and "Page data must be a JSON object" in response.text
    assert configs.load() == before


def test_find_art_highlights_artwork_in_the_sidebar(site):  # noqa: F811
    _, _, client = site
    page = client.get("/discover?page=2", headers=auth()).text
    current = re.findall(r'class="sidebar-menu-button" href="[^"]*" aria-current="page">.*?<span>([^<]+)</span>', page)
    assert current == ["Artwork"]
