"""The per-TV pages after the UX pass: Now, Sensor, Picture, Health, TV settings and Schedule.

Every TV and sensor here is a fake (DemoTelevision, a Link with no clients): no test contacts a device.
"""
# ruff: noqa: F811

import html
import re
import time

from test_sensors import client, connected  # noqa: F401  (client is a fixture)
from test_web import auth, csrf

from art_frames import sensor_control

HOST = "192.168.1.148"


def page(browser, path):
    return html.unescape(browser.get(path, headers=auth()).get_data(as_text=True))


def connect(browser, host=HOST):
    return browser.post("/tv/main/sensor/connect", headers=auth(), data={"csrf": csrf(browser), "host": host})


def turn_on(app, **fields):
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(enabled=True, **fields))


def set_mac(app, mac="AA:BB:CC:DD:EE:01"):
    app.extensions["configs"].mutate(lambda raw: raw["tv"].update(ip="192.168.1.50", mac=mac))


def status(app, **fields):
    library = app.extensions["library"]
    library.set_state("tv_status", {"paired": True, "known": True, "awake": True, "answered": True, **fields})
    library.set_state("tv_status_at", time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))


# A first sensor dims after 5 minutes, not at once.


def test_a_first_sensor_dims_after_five_minutes(client):
    app, browser = client
    connect(browser)
    assert app.extensions["configs"].load().sensor.away_after == 5


def test_reconnecting_keeps_the_saved_dim(client):
    app, browser = client
    connect(browser)
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(away_after=1))
    connect(browser)
    assert app.extensions["configs"].load().sensor.away_after == 1


# Now: Resume on the Now page.


def test_now_offers_resume_while_the_sensor_is_paused(client):
    app, browser = client
    connect(browser)
    turn_on(app)
    status(app)
    now = time.time()
    app.extensions["library"].set_state(sensor_control.CONTROL, {"sent": 4, "paused_at": now - 60,
                                                                 "paused_until": now + 3600})
    body = page(browser, "/tv/main")
    assert "Sensor paused until" in body and "You changed the brightness on the TV." in body
    form = re.search(r'<form method="post" action="/tv/main/sensor/resume"[^>]*>(.*?)</form>', body, re.S).group(1)
    assert 'name="back" value="now"' in form and "Resume now" in form
    response = browser.post("/tv/main/sensor/resume", headers=auth(), data={"csrf": csrf(browser), "back": "now"})
    assert response.status_code == 302 and response.location.endswith("/tv/main")
    assert not app.extensions["library"].state(sensor_control.CONTROL).get("paused_until")


def test_now_has_no_resume_when_the_sensor_follows_the_room(client):
    app, browser = client
    connect(browser)
    turn_on(app)
    status(app)
    app.extensions["library"].set_state(sensor_control.CONTROL, {"sent": 4})
    assert "Resume now" not in page(browser, "/tv/main")


def test_resume_without_back_returns_to_the_sensor_page(client):
    app, browser = client
    connect(browser)
    response = browser.post("/tv/main/sensor/resume", headers=auth(), data={"csrf": csrf(browser)})
    assert response.location.endswith("/tv/main/sensor#sensor-status")


# Health: a count in the title, and real next steps for a failed turn-on.


def wake_failed(app, code):
    app.extensions["library"].set_state(sensor_control.CONTROL, {
        "wake_failures": 2, "wake_at": time.time() - 300, "wake_result": code})


def health_row(body, row_id):
    return re.search(rf'<li class="health-row [^"]*" id="check-{row_id}">(.*?)</li>\s*(?=<li class="health-row|</ul>)',
                     body, re.S).group(1)


def test_health_title_counts_problems(client):
    app, browser = client
    connect(browser)
    set_mac(app)
    turn_on(app)
    status(app, awake=False)
    wake_failed(app, "not-confirmed")
    body = page(browser, "/tv/main/health")
    assert re.search(r'id="checks-title">\d+ problems?</h2>', body)
    assert "did not turn on for someone</h2>" not in body


def test_a_failed_turn_on_has_steps_and_turn_on_now(client):
    app, browser = client
    connect(browser)
    set_mac(app)
    turn_on(app)
    status(app, awake=False)
    wake_failed(app, "not-confirmed")
    row = health_row(page(browser, "/tv/main/health"), "sensor-wake")
    assert "Frame 1 did not turn on for someone" in row or "did not turn on for someone" in row
    assert "<li>Make sure the TV is plugged in.</li>" in row
    assert "Expert Settings › Power On with Mobile.</li>" in row
    assert "<li>Select Turn on now to test.</li>" in row
    wake = re.search(r'<form method="post" action="/wake\?tv=main">(.*?)</form>', row, re.S).group(1)
    assert 'name="back" value="health"' in wake and "Turn on now" in wake
    assert "Open TV settings" in row and "Open sensor settings" not in row


def test_a_turn_on_without_a_mac_points_to_tv_settings(client):
    app, browser = client
    connect(browser)
    turn_on(app)
    status(app, awake=False)
    wake_failed(app, "missing-mac")
    row = health_row(page(browser, "/tv/main/health"), "sensor-wake")
    assert "<li>Add the TV's MAC address in TV settings.</li>" in row
    assert "/wake?" not in row and "Open TV settings" in row


def test_a_turn_on_blocked_by_the_network_links_the_hosting_docs(client):
    app, browser = client
    connect(browser)
    set_mac(app)
    turn_on(app)
    status(app, awake=False)
    wake_failed(app, "network")
    row = health_row(page(browser, "/tv/main/health"), "sensor-wake")
    assert "host networking" in row and "docs/hosting.md" in row and "Turn on now" in row


def test_turn_on_now_from_health_returns_to_health(client):
    app, browser = client
    calls = []
    app.extensions["controller"].wake_screen = lambda: calls.append("wake") or "published"
    response = browser.post("/wake", headers=auth(), data={"csrf": csrf(browser), "back": "health"})
    assert response.status_code == 302 and "/tv/main/health" in response.location
    assert calls == ["wake"]


# One term for the scheduled wake: turn-on time.


def test_the_scheduled_wake_is_the_turn_on_time_everywhere(client):
    app, browser = client
    set_mac(app)
    body = page(browser, "/tv/main")
    assert "Turn on now" in body and "Wake TV" not in body and "Turn-on time" in body
    schedule = browser.get("/tv/main/schedule", headers=auth())
    if schedule.status_code == 200:
        text = html.unescape(schedule.get_data(as_text=True))
        assert "Turn on in Art Mode" not in text and "Turn-on time" in text


# The sidebar names the Sensor page.


def test_the_sidebar_has_a_sensor_item(client):
    app, browser = client
    body = page(browser, "/tv/main")
    item = re.search(r'<a class="sidebar-menu-button" href="/tv/main/sensor"[^>]*>(.*?)</a>', body, re.S).group(1)
    assert "<span>Sensor</span>" in item and "Add" in item
    connect(browser)
    body = page(browser, "/tv/main/sensor")
    current = re.search(r'<a class="sidebar-menu-button" href="/tv/main/sensor" aria-current="page">(.*?)</a>',
                        body, re.S)
    assert current and "Add" not in current.group(1)
    assert 'href="/tv/main/settings" aria-current' not in body


# The page-level Advanced part: a heading, then folds that remember their state and open for a result.


def fold_tag(body, fold_id):
    return re.search(rf'<details class="[^"]*" id="{fold_id}"[^>]*>', body).group(0)


def test_every_tv_page_has_one_advanced_section(client):
    app, browser = client
    connect(browser)
    set_mac(app)  # with an address, TV settings keeps it under Advanced
    for path, folds, lead in (
            ("/tv/main/picture", ("picture-settings", "tv-details", "tests"),
             "Tools for unusual setups. Most homes never need them."),
            ("/tv/main/sensor", ("advanced",), "Tools for unusual setups. Most homes never need them."),
            ("/tv/main/settings", ("connection",), "Address and recovery. Change these only if the TV moved.")):
        body = page(browser, path)
        assert body.count('id="advanced-heading">Advanced</h2>') == 1, path
        assert lead in body
        start = body.index('class="advanced-section"')
        for fold in folds:
            assert body.index(f'id="{fold}"') > start, (path, fold)
            assert "data-fold-remember" in fold_tag(body, fold)


def test_a_picture_test_result_opens_the_tests_fold(client):
    app, browser = client
    assert " open" not in fold_tag(page(browser, "/tv/main/picture"), "tests")
    response = browser.post("/screen/display/screen-off", headers=auth(), data={"csrf": csrf(browser)},
                            follow_redirects=True)
    body = html.unescape(response.get_data(as_text=True))
    assert " open" in fold_tag(body, "tests")
    assert " open" not in fold_tag(body, "picture-settings")


def test_tv_settings_simple_view_is_name_pairing_and_sensor(client):
    app, browser = client
    set_mac(app)
    body = page(browser, "/tv/main/settings")
    advanced = body.index('class="advanced-section"')
    for card in ('id="name"', 'id="pairing"', 'id="sensor"'):
        assert body.index(card) < advanced
    assert body.index('name="tv_ip"') > advanced and body.index('id="address-recovery"') > advanced
    assert "Address: 192.168.1.50 · MAC set" in body and "AA:BB:CC:DD:EE:01" not in body[:advanced]
    assert " open" not in fold_tag(body, "connection")


def test_the_address_fold_opens_when_the_address_is_missing(client):
    app, browser = client
    assert " open" in fold_tag(page(browser, "/tv/main/settings"), "connection")


def test_saving_the_name_keeps_the_address(client):
    app, browser = client
    set_mac(app)
    response = browser.post("/tv/main/settings", headers=auth(), data={"csrf": csrf(browser), "tv_name": "Den TV"})
    assert response.status_code == 302 and response.location.endswith("#name")
    cfg = app.extensions["configs"].load()
    assert (cfg.tv_name, cfg.tv.ip, cfg.tv.mac) == ("Den TV", "192.168.1.50", "AA:BB:CC:DD:EE:01")


def test_saving_the_address_keeps_the_name(client):
    app, browser = client
    browser.post("/tv/main/settings", headers=auth(), data={"csrf": csrf(browser), "tv_name": "Den"})
    response = browser.post("/tv/main/settings", headers=auth(), data={
        "csrf": csrf(browser), "tv_ip": "192.168.1.70", "tv_mac": "", "broadcast": "255.255.255.255"})
    assert response.status_code == 302 and response.location.endswith("#connection")
    cfg = app.extensions["configs"].load()
    assert (cfg.tv_name, cfg.tv.ip) == ("Den", "192.168.1.70")


def test_a_bad_address_shows_its_error_in_the_open_fold(client):
    app, browser = client
    response = browser.post("/tv/main/settings", headers=auth(), data={
        "csrf": csrf(browser), "tv_ip": "not an ip", "tv_mac": "", "broadcast": "255.255.255.255"})
    body = html.unescape(response.get_data(as_text=True))
    assert response.status_code == 400 and " open" in fold_tag(body, "connection")
    assert body.index('role="alert"') > body.index('id="connection"')


def test_paired_tv_offers_pair_again(client):
    app, browser = client
    set_mac(app)
    status(app)
    body = page(browser, "/tv/main/settings")
    assert ">Pair again</button>" in body and "Paired" in body


def test_health_puts_history_below_the_separator(client):
    app, browser = client
    body = page(browser, "/tv/main/health")
    assert body.index('id="checks"') < body.index('class="separator')


def test_health_folds_the_ok_checks_and_keeps_problems_in_view(client):
    app, browser = client
    connect(browser)
    set_mac(app)
    turn_on(app)
    status(app, awake=False)
    wake_failed(app, "not-confirmed")
    body = page(browser, "/tv/main/health")
    fold = body.index('id="checks-ok"')
    rows = re.findall(r'<li class="health-row ([^"]*)" id="check-([^"]+)"', body)
    problems = [name for tone, name in rows if tone != "tone-ok"]
    assert problems, "a failed turn-on shows at least one problem row"
    for name in problems:
        assert body.index(f'id="check-{name}"') < fold
    assert re.search(r'<summary><span>\d+ checks? OK</span>', body)
    assert "<summary><span>All " not in body



# Room presets on the Sensor page.


def tv_range(app, low, high):
    cfg = app.extensions["configs"].load()
    app.extensions["library"].set_state("art_display", {
        "device": app.extensions["controller"]._identity(cfg), "min": low, "max": high, "brightness": low})


def sensor_cfg(app):
    return app.extensions["configs"].load().sensor


def version(browser):
    body = page(browser, "/tv/main/sensor")
    return re.search(r'name="sensor_version" value="([0-9a-f]+)"', body).group(1)


def pick(browser, **fields):
    return browser.post("/tv/main/sensor/room", headers=auth(),
                        data={"csrf": csrf(browser), "sensor_version": version(browser), "offset": "0", **fields})


def levels(app):
    return [(point.lux, point.level) for point in sensor_cfg(app).curve.points]


def test_a_new_sensor_asks_what_kind_of_room(client):
    app, browser = client
    connect(browser)
    body = page(browser, "/tv/main/sensor")
    room = body[body.index('id="room"'):body.index('id="away"')]
    assert "What kind of room is this?" in room and ">Turn on sensor control</button>" in room
    assert ">Save only</button>" in room
    for name, line in (("Living room", "Bright by day, soft at night. Blank after 30 min, off after 1 hour."),
                       ("Bedroom", "Very dim at night. Blank after 10 min, off after 30 min. Does not turn on after "
                                   "21:30."),
                       ("Dark home theater", "Never bright. Blank after 5 min, off after 30 min.")):
        assert f"<strong>{name}</strong><span class=\"small\">{line}</span>" in room
    assert room.count('class="room-spark"') == 3
    assert "Art Frames has not read this TV's brightness range yet, so a room uses 0 to 10." in room
    # U1: no room was picked yet, so the card does not say Custom. Living room starts selected.
    assert "Custom" not in room and "Reset to" not in room
    assert 'value="living" checked' in room and room.count(" checked") == 1
    # B1: only the main button carries enable; Enter presses the hidden Save first.
    assert 'type="hidden" name="enable"' not in room
    assert room.index('class="sr-only" type="submit"') < room.index('name="room"')
    assert '<button class="button" type="submit" name="enable" value="on">Turn on sensor control</button>' in room


def test_enter_and_reset_never_turn_on_sensor_control(client):
    app, browser = client
    connect(browser)
    tv_range(app, 0, 10)
    # Enter in the form: the hidden Save posts the room without enable.
    response = pick(browser, room="living")
    assert response.status_code == 302 and not sensor_cfg(app).enabled
    assert "Saved. Sensor control is still off." in page(browser, "/tv/main/sensor")
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(blank_after=15))
    # Reset, even with enable in the post, keeps sensor control off and asks nothing of the TV.
    response = pick(browser, reset="living", enable="on")
    assert response.status_code == 302 and not sensor_cfg(app).enabled and sensor_cfg(app).blank_after == 30
    assert not (app.extensions["library"].state(sensor_control.SAMSUNG) or {}).get("want")


def test_a_different_sensor_asks_for_its_room_again(client):
    app, browser = client
    connect(browser)
    pick(browser, room="bedroom", enable="on")
    assert app.extensions["library"].state("sensor_room")
    connect(browser, host=HOST)  # the same sensor keeps its room
    assert app.extensions["library"].state("sensor_room")
    connect(browser, host="192.168.1.231")
    assert app.extensions["library"].state("sensor_room") is None
    room = page(browser, "/tv/main/sensor")
    room = room[room.index('id="room"'):room.index('id="away"')]
    assert "What kind of room is this?" in room and "Custom" not in room


def test_a_room_picked_before_the_range_was_read_offers_to_apply_it(client):
    app, browser = client
    connect(browser)
    pick(browser, room="living", enable="on")  # range unknown: 0-10
    assert app.extensions["library"].state("sensor_room") == {"room": "living", "range": [0, 10]}
    tv_range(app, 0, 50)
    room = page(browser, "/tv/main/sensor")
    room = room[room.index('id="room"'):room.index('id="away"')]
    assert "Custom" not in room and 'value="living" checked' in room
    assert ('Living room was set before Art Frames read this TV. <button class="button link sm" type="submit" '
            'name="reset" value="living">Apply it for this TV</button>') in room
    response = pick(browser, reset="living")
    assert response.status_code == 302 and levels(app) == [(1.0, 10), (20.0, 20), (150.0, 30), (800.0, 50)]
    assert app.extensions["library"].state("sensor_room") == {"room": "living", "range": [0, 50]}
    room = page(browser, "/tv/main/sensor")
    assert "was set before Art Frames read this TV" not in room


def test_a_changed_room_on_a_new_range_stays_custom(client):
    app, browser = client
    connect(browser)
    pick(browser, room="living", enable="on")
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(blank_after=15))
    tv_range(app, 1, 10)
    room = page(browser, "/tv/main/sensor")
    room = room[room.index('id="room"'):room.index('id="away"')]
    assert "was set before Art Frames read this TV" not in room
    assert "Now: <strong>Custom (based on Living room)</strong>." in room


def test_picking_a_room_turns_on_sensor_control_in_one_step(client):
    app, browser = client
    connect(browser)
    tv_range(app, 0, 50)
    response = pick(browser, room="theater", enable="on")
    assert response.status_code == 302 and response.location.endswith("#room")
    sensor = sensor_cfg(app)
    assert sensor.enabled and levels(app) == [(0.5, 0), (5.0, 5), (50.0, 15), (300.0, 30)]
    assert (sensor.away_after, sensor.blank_after, sensor.hours_start, sensor.hours_end) == (1, 5, "08:00", "23:00")
    assert app.extensions["library"].state("sensor_room") == {"room": "theater", "range": [0, 50]}
    body = page(browser, "/tv/main/sensor")
    assert "Sensor control is on. The TV follows Dark home theater within a minute." in body
    room = body[body.index('id="room"'):body.index('id="away"')]
    assert 'value="theater" checked' in room and "Custom" not in room and ">Save</button>" in room
    assert 'aria-checked="true"' in body


def test_save_only_keeps_sensor_control_off(client):
    app, browser = client
    connect(browser)
    pick(browser, room="living", enable="on", save_only="1")
    assert not sensor_cfg(app).enabled
    assert levels(app) == [(1.0, 2), (20.0, 4), (150.0, 6), (800.0, 10)]  # 0-10 until the range is read
    assert "Saved. Sensor control is still off." in page(browser, "/tv/main/sensor")


def test_a_change_after_a_preset_shows_custom_and_reset(client):
    app, browser = client
    connect(browser)
    tv_range(app, 0, 10)
    pick(browser, room="bedroom")
    app.extensions["configs"].mutate(lambda raw: raw["sensor"].update(blank_after=15))
    room = page(browser, "/tv/main/sensor")
    room = room[room.index('id="room"'):room.index('id="away"')]
    assert "Now: <strong>Custom (based on Bedroom)</strong>." in room and " checked" not in room
    assert 'name="reset" value="bedroom">Reset to Bedroom</button>' in room
    assert "This changes Blank after 15 min → 10 min." in room
    response = pick(browser, reset="bedroom")
    assert response.status_code == 302 and sensor_cfg(app).blank_after == 10
    assert "Saved. Sensor control is still off." in page(browser, "/tv/main/sensor")


def test_darker_or_brighter_keeps_the_preset(client):
    app, browser = client
    connect(browser)
    tv_range(app, 0, 10)
    pick(browser, room="living")
    response = pick(browser, offset="-1")
    assert response.status_code == 302 and sensor_cfg(app).curve.offset == -1
    assert levels(app) == [(1.0, 2), (20.0, 4), (150.0, 6), (800.0, 10)]
    body = page(browser, "/tv/main/sensor")
    assert "Saved. Sensor control is still off." in body
    assert 'value="living" checked' in body


def test_an_unknown_room_is_refused(client):
    app, browser = client
    connect(browser)
    response = pick(browser, room="garage")
    assert response.status_code == 400 and "Pick a room from the list." in html.unescape(response.get_data(as_text=True))
    assert levels(app) == []


def test_what_it_does_describes_the_saved_settings(client):
    app, browser = client
    connect(browser)
    pick(browser, room="bedroom")
    body = page(browser, "/tv/main/sensor")
    for line in ("The TV turns on when someone comes in, from 07:00 to 21:30.",
                 "When the room is empty, it dims after 1 min, blanks after 10 min and turns off about 5 min after 30 min.",
                 "After 21:30, it turns off 5 min after the room is empty."):
        assert f"<li>{line}</li>" in body


def test_the_points_fold_has_the_chart_and_the_table(client):
    app, browser = client
    connect(browser)
    tv_range(app, 0, 10)
    pick(browser, room="living")
    body = page(browser, "/tv/main/sensor")
    fold = body[body.index('id="points"'):body.index('id="advanced"')]
    assert "4 points · room light to brightness, and the timing" in fold
    assert fold.count('class="curve-dot"') == 4 and "<title>800 lux → 10</title>" in fold
    assert 'name="lux_3" type="number" min="0.1" step="any" value="800"' in fold
    assert "Art Frames brightens after about 30 seconds" in fold
    assert " open" not in fold_tag(body, "points")


def test_a_points_error_opens_the_points_fold(client):
    app, browser = client
    connect(browser)
    response = browser.post("/tv/main/sensor/control", headers=auth(), data={
        "csrf": csrf(browser), "sensor_version": version(browser), "count": "0", "offset": "0",
        "new_lux": "5", "new_level": ""})
    body = html.unescape(response.get_data(as_text=True))
    assert response.status_code == 400 and " open" in fold_tag(body, "points")


def test_save_messages_say_when_the_change_works(client):
    app, browser = client
    connect(browser)
    browser.post("/tv/main/sensor/away", headers=auth(), data={
        "csrf": csrf(browser), "sensor_version": version(browser), "away_after": "5", "blank_after": "30",
        "hours_start": "07:00", "hours_end": "23:00"})
    assert "Saved. The room-empty timer starts again now." in page(browser, "/tv/main/sensor")


def test_removing_the_sensor_forgets_the_room(client):
    app, browser = client
    connect(browser)
    pick(browser, room="living")
    browser.post("/tv/main/sensor/remove", headers=auth(), data={"csrf": csrf(browser)})
    assert app.extensions["library"].state("sensor_room") is None


def test_the_sensor_switch_is_in_the_status_card(client):
    app, browser = client
    connect(browser)
    body = page(browser, "/tv/main/sensor")
    status = body[body.index('id="sensor-status"'):body.index('id="room"')]
    assert 'role="switch" aria-checked="false" aria-label="Sensor control"' in status
    assert "Art Frames leaves the TV alone while someone watches TV." in status
    assert body.count('id="sensor-control"') == 1


# Now: one status sentence under the title, and a note on the picture.


def headline(body):
    return re.search(r'<p class="page-header-description">(.*?)</p>', body, re.S).group(1)


def overlay(body):
    found = re.search(r'<div class="screen-overlay" role="status"><p>(.*?)</p></div>', body)
    return found.group(1) if found else None


def sensor_now(app, browser, tv_status, control, empty_since=None):
    connect(browser)
    turn_on(app)
    status(app, **tv_status)
    app.extensions["library"].set_state(sensor_control.CONTROL, control)
    if empty_since is not None:
        app.extensions["sensors"].memory["main"] = sensor_control.Memory(absent_since=empty_since)
    return page(browser, "/tv/main")


def at_clock(app, hour, minute):
    """A time stamp today at hour:minute in the household's timezone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(app.extensions["configs"].load().timezone)
    return datetime.now(zone).replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()


def test_now_says_the_screen_is_blank(client):
    app, browser = client
    body = sensor_now(app, browser, {}, {"sent": 0, "away": True, "blanked": True}, at_clock(app, 21, 40))
    assert "Screen blank: room empty since 21:40" in headline(body)
    assert overlay(body) == "<strong>Screen blank</strong> · Room empty since 21:40. It comes back when someone walks in."
    assert "has-overlay tone-dark" in body


def test_now_says_the_screen_is_dimmed(client):
    app, browser = client
    body = sensor_now(app, browser, {}, {"sent": 0, "away": True}, at_clock(app, 21, 32))
    assert "Dimmed: room empty since 21:32" in headline(body) and "tone-dim" in body
    assert overlay(body) == "<strong>Dimmed</strong> · Room empty since 21:32. It brightens when someone walks in."


def test_now_says_off_for_the_night(client):
    app, browser = client
    body = sensor_now(app, browser, {"awake": False}, {"night": True})
    assert "Off for the night. It turns on when someone comes in after 07:00." in headline(body)
    assert overlay(body) == "<strong>Off for the night</strong> · It turns on when someone comes in after 07:00."


def test_now_says_the_tv_is_off_because_the_room_is_empty(client):
    app, browser = client
    connect(browser)
    turn_on(app, hours_start="00:00", hours_end="00:00")  # all day: someone coming in turns it on now
    status(app, awake=False)
    app.extensions["library"].set_state(sensor_control.CONTROL, {"tv_off": True, "blanked": True, "away": True})
    app.extensions["sensors"].memory["main"] = sensor_control.Memory(absent_since=at_clock(app, 21, 10))
    body = page(browser, "/tv/main")
    assert "TV off: room empty since 21:10. It turns on when someone comes in." in headline(body)
    assert overlay(body) == "<strong>TV off</strong> · It turns on when someone comes in."
    assert "TV off: room empty" in body  # the Room block's word


def test_now_says_the_sensor_is_paused(client):
    app, browser = client
    until = at_clock(app, 23, 59)
    body = sensor_now(app, browser, {}, {"sent": 4, "paused_at": until - 7200, "paused_until": until})
    # U7: the headline and the Room block say it; the picture has no note that repeats it.
    assert "Sensor paused until 23:59" in headline(body) and overlay(body) is None
    assert body.count("You changed the brightness on the TV") == 1
    # U4: no suggestion, so no "Keep this level".
    assert "Keep this level" not in body and ">Resume now</button>" in body


def test_now_links_keep_this_level_to_the_offer(client):
    app, browser = client
    until = at_clock(app, 23, 59)
    body = sensor_now(app, browser, {}, {"sent": 4, "paused_at": until - 7200, "paused_until": until,
                                         "suggest": {"lux": 37.0, "level": 7}})
    assert '<a class="small" href="/tv/main/sensor#sensor-offer">Keep this level</a>' in body
    assert 'id="sensor-offer"' in page(browser, "/tv/main/sensor")


def test_now_says_the_brightness_is_held(client):
    app, browser = client
    body = sensor_now(app, browser, {}, {"sent": 4, "unconfirmed": sensor_control.MAX_UNCONFIRMED})
    assert "Holding the brightness: the TV did not confirm the last changes" in headline(body)
    assert "from room light" not in headline(body) and overlay(body) is None
    room = body[body.index('class="now-item now-room"'):]
    room = room[:room.index("</div>")]
    assert ">Holding the brightness</p>" in room and "Following room light" not in room
    assert ">Resume now</button>" in room and '<a class="small" href="/tv/main/health">Health</a>' in room
    assert "Keep this level" not in room


def test_now_puts_the_tv_state_before_the_pause(client):
    app, browser = client
    until = at_clock(app, 23, 59)
    body = sensor_now(app, browser, {"awake": False}, {"sent": 4, "paused_at": until - 7200, "paused_until": until})
    assert "TV off · sensor paused until 23:59" in headline(body)
    assert overlay(body).startswith("<strong>TV off</strong>")


def test_now_says_someone_is_watching(client):
    app, browser = client
    body = sensor_now(app, browser, {"playing": True}, {"sent": 4})
    assert "Someone is watching TV. Art Frames waits." in headline(body)
    assert overlay(body).startswith("<strong>Watching TV</strong>")


def test_now_names_the_brightness_from_room_light(client):
    app, browser = client
    body = sensor_now(app, browser, {}, {"sent": 6})
    assert "brightness 6 from room light" in headline(body) and overlay(body) is None


def test_now_without_a_sensor_says_the_tv_is_off(client):
    app, browser = client
    status(app, awake=False)
    body = page(browser, "/tv/main")
    assert headline(body).startswith("TV off")
    assert overlay(body) == "<strong>TV off</strong> · Art Frames shows this when the TV is on in Art Mode."


def test_now_without_a_sensor_in_art_mode_has_no_overlay(client):
    app, browser = client
    status(app)
    body = page(browser, "/tv/main")
    assert headline(body).startswith("Art Mode") or headline(body).startswith("Showing ")
    assert overlay(body) is None


def test_schedule_shows_the_sensor_hours_in_the_turn_on_row(client):
    app, browser = client
    assert "sensor-band" not in page(browser, "/tv/main/schedule")
    connect(browser)
    turn_on(app, hours_start="07:00", hours_end="23:00")
    body = page(browser, "/tv/main/schedule")
    wake = body[body.index('class="lane lane-wake"'):]
    band = re.search(r'<a class="sensor-band" href="/tv/main/sensor#away" data-left="([0-9.]+)" data-width="([0-9.]+)"',
                     wake)
    assert band and (float(band.group(1)), float(band.group(2))) == (29.167, 66.667)
    assert ("Sensor: turns the TV on when someone comes in, 07:00–23:00. After 23:00, the TV turns off 5 min "
            "after the room is empty.") in body


def test_sensor_hours_past_midnight_draw_two_bands(client):
    app, browser = client
    connect(browser)
    turn_on(app, hours_start="18:00", hours_end="02:00")
    bands = re.findall(r'class="sensor-band"[^>]*data-left="([0-9.]+)" data-width="([0-9.]+)"',
                       page(browser, "/tv/main/schedule"))
    assert [(float(a), float(b)) for a, b in bands] == [(75.0, 25.0), (0.0, 8.333)]


def test_now_puts_room_second_and_drops_the_picture_button(client):
    app, browser = client
    body = sensor_now(app, browser, {}, {"sent": 6})
    labels = re.findall(r'<p class="now-label">([^<]+)</p>', body)
    assert labels[:2] == ["On the TV", "Room"]
    assert "Picture: brightness and mats" not in body and ">Sensor settings</a>" in body


def test_the_sensor_page_says_what_hardware_works_before_a_sensor_is_added(client):
    _, browser = client
    body = page(browser, "/tv/main/sensor")
    assert "Works with ESPHome presence and light sensors, such as an mmWave sensor with a lux reading." in body
    connect(browser)
    assert "Works with ESPHome presence" not in page(browser, "/tv/main/sensor")


def test_room_saves_with_sensor_control_on_say_what_the_tv_follows(client):
    app, browser = client
    connect(browser)
    turn_on(app)
    pick(browser, room="bedroom")
    assert "Saved. The TV follows Bedroom within a minute." in page(browser, "/tv/main/sensor")
    pick(browser, offset="1")
    assert "Saved. The brightness follows the new points within a minute." in page(browser, "/tv/main/sensor")


def test_a_name_save_keeps_the_proposal_and_a_newer_address(client, monkeypatch):
    from art_frames import tv_recovery
    app, browser = client
    set_mac(app)
    cleared = []
    monkeypatch.setattr(tv_recovery, "clear_proposal", lambda *args: cleared.append(args))
    recovery = app.extensions["controller"].recovery
    if recovery is not None:
        monkeypatch.setattr(recovery, "clear_proposal", lambda *args: cleared.append(args))
    store = app.extensions["configs"]
    real = store.mutate

    def racing(change, *args, **kwargs):
        real(lambda raw: raw["tv"].update(ip="192.168.1.77"))  # recovery moves the TV after the page loaded
        return real(change, *args, **kwargs)
    monkeypatch.setattr(store, "mutate", racing)
    response = browser.post("/tv/main/settings", headers=auth(), data={"csrf": csrf(browser), "tv_name": "Den"})
    monkeypatch.setattr(store, "mutate", real)
    assert response.status_code == 302 and cleared == []
    cfg = store.load()
    assert (cfg.tv_name, cfg.tv.ip, cfg.tv.mac) == ("Den", "192.168.1.77", "AA:BB:CC:DD:EE:01")


def test_tv_settings_shows_a_missing_address_above_advanced(client):
    app, browser = client
    body = page(browser, "/tv/main/settings")
    assert "class=\"advanced-section\"" not in body
    assert body.index("id=\"pairing\"") < body.index("id=\"connection\"")
    assert "Save the TV's address first. Add it under Address below." in body
    set_mac(app)
    body = page(browser, "/tv/main/settings")
    assert body.index("class=\"advanced-section\"") < body.index("id=\"connection\"")
    assert "Address and recovery. Change these only if the TV moved." in body


def test_an_unknown_range_never_calls_a_pick_stale(client):
    app, browser = client
    connect(browser)
    tv_range(app, 0, 50)
    pick(browser, room="living", enable="on")
    assert app.extensions["library"].state("sensor_room") == {"room": "living", "range": [0, 50]}
    app.extensions["library"].delete_state("art_display")  # the range is unknown again: the page uses 0-10
    room = page(browser, "/tv/main/sensor")
    room = room[room.index('id="room"'):room.index('id="away"')]
    assert "was set before Art Frames read this TV" not in room and "Apply it for this TV" not in room


def test_tv_settings_has_one_advanced_section_and_the_mac_row_is_in_the_address_fold(client):
    app, browser = client
    for setup in (lambda: None, lambda: set_mac(app)):
        setup()
        body = page(browser, "/tv/main/settings")
        assert body.count('class="advanced-section"') <= 1
        assert '<span class="advanced-label">Advanced</span>' not in body
        fold = body.index('id="connection"')
        row = body.index('id="connection-advanced"')
        assert fold < row < body.index("</details>", body.index("</details>", row) + 1)
        assert '<span class="advanced-label">MAC and wake</span>' in body
