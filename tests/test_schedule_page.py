"""The Schedule, Picture, TV settings and Household settings pages: one card per job, each with its own Save."""

import html
import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_app_schedule import three_apps  # noqa: F401  (a fixture)
from test_web import PASSWORD, auth, csrf

from art_frames import apps
from art_frames.config import day_names, rule_label, span_label
from art_frames.rotation import Hold, Rotation
from art_frames.web import create_app

WEEKDAY_MORNINGS = {"apps": ["news"], "start": "07:00", "end": "10:00", "days": [0, 1, 2, 3, 4]}


@pytest.fixture
def site(three_apps, tmp_path):  # noqa: F811
    """A test app with Morning, News and Photos installed, News on weekday mornings."""
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw.update(app_schedule={"windows": [WEEKDAY_MORNINGS]}))
    return app, app.extensions["configs"], app.test_client()


def post(client, path, data, **options):
    return client.post(path, headers=auth(), data={"csrf": csrf(client), **data}, **options)


def version(client):
    """The schedule version that the Schedule page's forms post back."""
    page = client.get("/tv/main/schedule", headers=auth()).text
    return re.search(r'name="schedule_version" value="([0-9a-f]+)"', page).group(1)


def paired(app):
    """Show now needs a paired TV outside demo mode."""
    app.extensions["library"].set_state("tv_status", {"paired": True, "known": True})


# Plain words

def test_rules_and_days_read_as_plain_words():
    assert day_names(range(7)) == "Every day"
    assert day_names([0, 1, 2, 3, 4]) == "Mon to Fri"
    assert day_names([5, 6]) == "Sat and Sun"
    assert day_names([0, 2, 4]) == "Mon, Wed, Fri"
    assert rule_label("07:00", "10:00", [0, 1, 2, 3, 4]) == "7:00 to 10:00 AM, Mon to Fri"
    assert rule_label("18:00", "02:00", range(7)) == "6:00 PM to 2:00 AM, every day"
    assert rule_label("", "10:00", [0]) == "? to 10:00 AM, Mon"  # no double space for a missing time
    assert span_label("22:00", "24:00") == "10:00 PM to 12:00 AM"
    assert span_label("00:00", "07:00") == "12:00 to 7:00 AM"


# The timeline

def plan(configs, blocks=(), wake=None, days=None, swap=None):
    """Save a schedule: every day's blocks and wake, and days with their own plan. Artwork shows outside the blocks."""
    week = {"default": {"blocks": list(blocks), "wake": wake}, "days": days or {}}
    if swap:
        week["swap_seconds"] = swap
    configs.mutate(lambda raw: (raw["tv"].update(ip="192.168.1.50", mac="AA:BB:CC:DD:EE:01"),
                                raw.update(app_schedule={"week": week})))


def block(app, start, end):
    return {"app": app, "start": start, "end": end}


def editor(page, card):
    """One editor of the Schedule page, from id="card" to its end."""
    rest = page[page.index(f'id="{card}"'):]
    return rest[:rest.index("</section>")]


def strip(page):
    """The "On the TV" lane of the timeline, as (when, what) pairs."""
    part = page[page.index('class="lane lane-strip"'):]
    part = part[:part.index("</div>\n    </div>")]
    return [tuple(item.split(": ", 1)) for item in re.findall(r'<span class="segment [^"]*"[^>]* title="([^"]+)"',
                                                                 html.unescape(part))]


BASE_LAYER = "Artwork shows when no layout is scheduled."


STALE_TEXT = "This schedule changed after the page opened. Here is the latest version."


def stale(client, response, location="/tv/main/schedule?day=every#timeline"):
    """A refused form opens the latest page at the timeline, where the note shows once."""
    assert response.status_code == 302 and response.location.endswith(location)
    page = client.get(response.location, headers=auth()).text
    return page.count(STALE_TEXT) == 1


def test_the_empty_timeline_shows_artwork_all_day(site):
    _, configs, client = site
    plan(configs)
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert strip(page) == [("All day", "Artwork")]
    assert BASE_LAYER in page and "make them take turns.</span> Artwork shows the rest of the day." in page
    # No rest-of-the-time choice: Artwork is always the base layer.
    assert 'id="usually-shows"' not in page and "The rest of the time" not in page and "Over the day" not in page
    assert 'data-app="artwork"' not in page
    # The chips sit in the empty start, and the turns line shows only where apps take turns.
    assert 'class="app-chips"' not in page and "take turns every" not in page
    assert "Add turn-on time" in page and 'href="#wake-form"' in page and "data-wake=" not in page
    # A chip and an Add form for each app. The forms work without JS: they open by their #id.
    for app_id in ("morning", "news", "photos"):
        assert f'href="#add-{app_id}" data-app="{app_id}"' in page
        form = editor(page, f"add-{app_id}")
        assert f'action="/tv/main/schedule/every/blocks#add-{app_id}"' in form
        assert 'name="start" step="900" value="07:00"' in form and "data-cancel>Cancel</a>" in form
    assert "editor open" not in page
    # The tabs: Every day, then each day. No day has its own plan yet.
    assert re.search(r'href="/tv/main/schedule\?day=every#timeline" aria-current="page">Every day', page)
    assert 'href="/tv/main/schedule?day=mon#timeline"' in page and 'class="custom-dot"' not in page
    assert client.get("/tv/main/schedule?day=someday", headers=auth()).status_code == 404
    assert client.get("/tv/nope/schedule", headers=auth()).status_code == 404


def test_overlapping_blocks_take_turns_on_the_strip(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00"), block("photos", "09:00", "12:00")])
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert strip(page) == [("12:00–7:00 AM", "Artwork"), ("7:00–9:00 AM", "News"),
                           ("9:00–10:00 AM", "News + Photos take turns"), ("10:00 AM–12:00 PM", "Photos"),
                           ("12:00 PM–12:00 AM", "Artwork")]
    assert "Over the day" not in page and "they take turns every 5 min" in page
    assert re.search(r'<span class="segment artwork" [^>]*title="12:00–7:00 AM: Artwork">', page)
    # A lane for each app with blocks. Each block links to its edit form.
    assert re.search(r'<a class="block hue-\d" href="#block-0" data-left="29\.1667" data-width="12\.5" '
                     r'data-block="0" data-start="07:00" data-end="10:00"', page)
    assert 'href="#block-1"' in page and 'data-lane="news"' in page and 'data-lane="photos"' in page
    assert 'data-lane="morning"' not in page and "Drag a layout onto the day." not in page
    # A block's label is short, so it fits: "7–10 AM".
    assert '<span class="block-text">7–10 AM</span>' in page
    form = editor(page, "block-0")
    assert 'id="block-0-title">Edit News block</h2>' in form
    assert '<option value="news" selected>News</option>' in form and '<option value="photos" >Photos</option>' in form
    assert 'action="/tv/main/schedule/every/blocks/0#block-0"' in form
    assert 'name="start" step="900" value="07:00"' in form and 'name="end" step="900" value="10:00"' in form
    assert "Remove this block?" in form and "data-cancel>Cancel</a>" in form


def test_adding_a_block(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")])
    before = configs.load()
    # The fallback form: 12:00 AM as the end is midnight, the end of the day.
    response = post(client, "/tv/main/schedule/every/blocks",
                    {"app": "photos", "start": "18:00", "end": "00:00", "schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=every#timeline")
    after = configs.load()
    assert [(item.app, item.start, item.end) for item in after.app_schedule.week.default.blocks] == [
        ("news", "07:00", "10:00"), ("photos", "18:00", "24:00")]
    assert after.model_dump(exclude={"app_schedule"}) == before.model_dump(exclude={"app_schedule"})
    page = html.unescape(client.get(response.location, headers=auth()).text)
    assert "Added Photos, 6:00 PM to 12:00 AM." in page
    assert ("6:00 PM–12:00 AM", "Photos") in strip(page)


def test_a_wrong_block_keeps_the_typed_times(site):
    _, configs, client = site
    plan(configs)
    before = configs.load()
    drawn = version(client)
    response = post(client, "/tv/main/schedule/every/blocks",
                    {"app": "photos", "start": "10:00", "end": "09:00", "schedule_version": drawn})
    assert response.status_code == 400
    assert 'class="card settings-panel editor open" id="add-photos"' in response.text
    form = editor(response.text, "add-photos")
    assert "Pick an end time after the start time." in form and 'value="10:00"' in form and 'value="09:00"' in form
    for data, text in (({"app": "photos", "start": "", "end": "09:00"}, "Pick a start time."),
                       ({"app": "photos", "start": "08:00", "end": "25:00"}, "Pick an end time."),
                       ({"app": "nope", "start": "08:00", "end": "09:00"}, "Choose an installed layout.")):
        response = post(client, "/tv/main/schedule/every/blocks", {**data, "schedule_version": drawn})
        assert response.status_code == 400 and text in response.text, text
    assert configs.load() == before


def test_editing_and_removing_a_block(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00"), block("photos", "18:00", "21:00")])
    response = post(client, "/tv/main/schedule/every/blocks/1",
                    {"start": "17:15", "end": "22:45", "schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=every#timeline")
    blocks = configs.load().app_schedule.week.default.blocks
    assert [(item.app, item.start, item.end) for item in blocks] == [
        ("news", "07:00", "10:00"), ("photos", "17:15", "22:45")]
    assert "Saved: 5:15 to 10:45 PM." in html.unescape(client.get(response.location, headers=auth()).text)
    # The editor's app select moves a block to another app.
    response = post(client, "/tv/main/schedule/every/blocks/1",
                    {"app": "morning", "start": "17:15", "end": "22:45", "schedule_version": version(client)})
    assert response.status_code == 302 and configs.load().app_schedule.week.default.blocks[1].app == "morning"
    response = post(client, "/tv/main/schedule/every/blocks/0/delete", {"schedule_version": version(client)})
    assert response.status_code == 302
    assert [item.app for item in configs.load().app_schedule.week.default.blocks] == ["morning"]
    assert "Removed the block." in client.get(response.location, headers=auth()).text


def test_a_form_from_an_older_page_saves_nothing(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")])
    drawn = version(client)
    assert post(client, "/tv/main/schedule/every/blocks/0/delete", {"schedule_version": drawn}).status_code == 302
    before = configs.load()
    for path, data in (("/tv/main/schedule/every/blocks/0", {"start": "06:00", "end": "08:00"}),
                       ("/tv/main/schedule/every/blocks/0/delete", {}),
                       ("/tv/main/schedule/every/wake", {"at": "06:30"}),
                       ("/tv/main/schedule/every/blocks", {"app": "photos", "start": "06:00", "end": "08:00"}),
                       ("/tv/main/schedule/turns", {"swap": "120"})):
        assert stale(client, post(client, path, {"schedule_version": drawn, **data})), path
    # A block past the end is from an older page too.
    assert stale(client, post(client, "/tv/main/schedule/every/blocks/4",
                              {"schedule_version": version(client), "start": "06:00", "end": "08:00"}))
    assert configs.load() == before


def test_a_day_uses_every_days_plan_until_it_is_customized(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")], wake="07:00")
    page = client.get("/tv/main/schedule?day=sat", headers=auth()).text
    assert "Saturday uses every day's plan." in page and 'href="#customize">Customize Saturday…' in page
    assert ("7:00–10:00 AM", "News") in strip(page)
    # Its timeline shows every day's plan, but cannot change it: no chips, no editors.
    assert 'class="app-chip"' not in page and 'id="block-0"' not in page and "timeline readonly" in page
    # A change from an older page says what to do first.
    before = configs.load()
    response = post(client, "/tv/main/schedule/sat/blocks",
                    {"app": "photos", "start": "18:00", "end": "21:00", "schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=sat#timeline")
    assert "Saturday uses every day's plan. Customize it first." in html.unescape(
        client.get(response.location, headers=auth()).text)
    assert configs.load() == before
    # Customize copies every day's plan.
    response = post(client, "/tv/main/schedule/sat/customize", {"schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=sat#timeline")
    week = configs.load().app_schedule.week
    assert week.days["sat"] == week.default and week.custom(5) and not week.custom(6)
    page = html.unescape(client.get(response.location, headers=auth()).text)
    assert "Saturday has its own plan now." in page and "Use Every day's plan" in page
    # Use every day's plan asks first.
    assert "Remove Saturday's own plan? Saturday will use every day's plan." in page
    assert re.search(r'day=sat#timeline" aria-current="page">Sat<span class="custom-dot"', page)
    # A change to Saturday changes only Saturday.
    response = post(client, "/tv/main/schedule/sat/blocks",
                    {"app": "photos", "start": "18:00", "end": "21:00", "schedule_version": version(client)})
    assert response.status_code == 302
    week = configs.load().app_schedule.week
    assert [item.app for item in week.days["sat"].blocks] == ["news", "photos"]
    assert [item.app for item in week.default.blocks] == ["news"] and week.day(6) == week.default
    response = post(client, "/tv/main/schedule/sat/wake", {"at": "09:00", "schedule_version": version(client)})
    assert response.status_code == 302
    week = configs.load().app_schedule.week
    assert (week.day(5).wake, week.day(6).wake) == ("09:00", "07:00")
    # Use every day's plan removes the day's own plan.
    response = post(client, "/tv/main/schedule/sat/reset", {"schedule_version": version(client)})
    assert response.status_code == 302 and configs.load().app_schedule.week.days["sat"] is None
    assert "Saturday uses every day's plan again." in html.unescape(client.get(response.location, headers=auth()).text)
    for path in ("/tv/main/schedule/every/customize", "/tv/main/schedule/every/reset", "/tv/main/schedule/sun1/blocks"):
        assert post(client, path, {"schedule_version": version(client)}).status_code == 404


def test_the_turn_on_time(site):
    _, configs, client = site
    plan(configs)
    before = configs.load()
    response = post(client, "/tv/main/schedule/every/wake", {"at": "06:30", "schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=every#timeline")
    after = configs.load()
    assert all(after.app_schedule.week.day(weekday).wake == "06:30" for weekday in range(7))
    assert after.model_dump(exclude={"app_schedule"}) == before.model_dump(exclude={"app_schedule"})
    page = client.get(response.location, headers=auth()).text
    assert "Frame 1 turns on at 6:30 AM." in page and 'data-wake="06:30"' in page
    assert "Remove the turn-on time?" in editor(page, "wake-form")
    response = post(client, "/tv/main/schedule/every/wake", {"at": "6:30", "schedule_version": version(client)})
    assert response.status_code == 400 and "Pick a turn-on time." in editor(response.text, "wake-form")
    assert 'class="card settings-panel editor open" id="wake-form"' in response.text
    response = post(client, "/tv/main/schedule/every/wake/delete", {"schedule_version": version(client)})
    assert response.status_code == 302 and not configs.load().app_schedule.week.has_wake()
    page = client.get(response.location, headers=auth()).text
    assert "Removed the turn-on time." in page and "Add turn-on time" in page
    # A turn-on time needs the TV's address.
    configs.mutate(lambda raw: raw["tv"].update(mac=""))
    response = post(client, "/tv/main/schedule/every/wake", {"at": "06:30", "schedule_version": version(client)})
    assert response.status_code == 400
    assert "A turn-on time needs the TV's IP and MAC address" in html.unescape(editor(response.text, "wake-form"))


def test_there_is_no_rest_of_the_time_to_save(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")])
    before = configs.load()
    response = post(client, "/tv/main/schedule/default", {"apps": ["photos"], "schedule_version": version(client)})
    assert response.status_code in (404, 405) and configs.load() == before


def test_taking_turns_saves_the_switch_time(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00"), block("photos", "08:00", "09:00")])
    response = post(client, "/tv/main/schedule/turns", {"swap": "120", "schedule_version": version(client)})
    assert response.status_code == 302 and configs.load().app_schedule.week.swap_seconds == 120
    assert "take turns every 2 min" in client.get(response.location, headers=auth()).text
    before = configs.load()
    response = post(client, "/tv/main/schedule/turns", {"swap": "5", "schedule_version": version(client)})
    assert response.status_code == 400 and "Take turns:" in editor(response.text, "turns")
    assert configs.load() == before


def test_an_older_weekday_rule_becomes_every_days_plan(site):
    _, configs, client = site  # site saves News on weekday mornings, as an older time rule
    week = configs.load().app_schedule.week
    # The most common day, Mon to Fri, is every day's plan. Sat and Sun get their own, empty plan.
    assert [(item.app, item.start, item.end) for item in week.default.blocks] == [("news", "07:00", "10:00")]
    assert [week.custom(day) for day in range(7)] == [False] * 5 + [True] * 2 and week.days["sat"].blocks == []
    page = html.unescape(client.get("/tv/main/schedule?day=every", headers=auth()).text)
    assert page.count('class="custom-dot"') == 2 and "Used on Mon to Fri. Sat and Sun have their own plans." in page
    page = html.unescape(client.get("/tv/main/schedule?day=wed", headers=auth()).text)
    assert "Wednesday uses every day's plan." in page and ("7:00–10:00 AM", "News") in strip(page)
    page = html.unescape(client.get("/tv/main/schedule?day=sun", headers=auth()).text)
    assert "Sunday has its own plan." in page and 'href="#copy-plan">Copy to other days…' in page


def test_a_custom_website_has_no_timeline_to_change(tmp_path):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    app.extensions["configs"].mutate(lambda raw: raw["site"].update(path="my-site"))
    client = app.test_client()
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "Frame 1 shows your custom website all the time. Blocks on the timeline work only with layouts." in page
    assert re.search(r'<a href="/household[^"]*#website">Change the website</a>', page)
    assert 'id="usually-shows"' not in page and 'class="app-chip"' not in page and BASE_LAYER not in page
    assert post(client, "/tv/main/schedule/every/blocks", {"app": "morning"}).status_code == 404


def test_schedule_with_no_app(tmp_path, monkeypatch):
    app = create_app(tmp_path / "data", password=PASSWORD, testing=True)
    client = app.test_client()
    monkeypatch.setattr(apps, "usable", lambda cfg: [])
    monkeypatch.setattr(apps, "supported", lambda: True)
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "Install a layout to choose what Frame 1 shows." in page and "How to install layouts" in page
    monkeypatch.setattr(apps, "supported", lambda: False)
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "No layouts are installed, so Frame 1 shows the starter website." in html.unescape(page)
    assert "How to install layouts" in page and "static profile" not in page


# Show now and the hold banner

def test_show_now_menu_and_hold_banner_on_now_home_and_schedule(site):
    app, configs, client = site
    paired(app)
    for path in ("/tv/main", "/"):
        page = client.get(path, headers=auth()).text
        menu = page[page.index('class="dropdown show-now"'):]
        menu = menu[:menu.index("</details>")]
        assert "For how long?" in menu and "For 1 hour" in menu and "Until I resume" in menu
        assert re.search(r"Until .+ \(next change\)|For 4 hours \(until .+\)", menu) and "Pick a layout" in menu
        assert 'name="app" value="news"' in menu and 'action="/screen/show-app?tv=main"' in menu
        assert "hold-banner" not in page
    Rotation(app.extensions["library"], configs.load().tv.identity).set_hold(Hold("photos"))
    for path in ("/tv/main", "/", "/tv/main/schedule"):
        page = client.get(path, headers=auth()).text
        assert "Holding Photos until you resume." in page and "Resume schedule" in page, path
    # The banner names the hold, so the schedule line says what comes after it.
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert re.search(r'<span class="label-inline">Then</span> [^<]+, after the hold\.</p>', page)
    page = client.get("/tv/main", headers=auth()).text
    assert re.search(NEXT + r"When you resume the schedule</a>", page)
    Rotation(app.extensions["library"], configs.load().tv.identity).set_hold(Hold("photos", datetime(2099, 1, 1, tzinfo=UTC)))
    page = client.get("/tv/main", headers=auth()).text
    assert re.search(NEXT + r"[^<]+ on \w{3} at [^<]+ \(after the hold\)</a>", page)


# The Now page's side panel: the label over the value.
NEXT = r'<p class="now-label">Next</p>\s*<p class="now-value"><a [^>]*>'


def test_a_timed_hold_names_the_plan_at_its_end_on_now_and_schedule(site):
    app, configs, client = site
    paired(app)
    zone = ZoneInfo(configs.load().timezone)
    end = datetime.combine(datetime.now(zone).date() + timedelta(days=1), time(12, 0), zone)
    # Every day at noon a rule shows Photos; Morning shows the rest of the day.
    configs.mutate(lambda raw: raw.update(app_schedule={"default": {"apps": ["morning"]}, "windows": [
        {"apps": ["photos"], "start": "11:00", "end": "13:00", "days": list(range(7))}]}))
    Rotation(app.extensions["library"], configs.load().tv.identity).set_hold(Hold("news", end))
    photos = apps.find("photos", apps.installed()).name
    page = html.unescape(client.get("/tv/main", headers=auth()).text)
    assert re.search(NEXT + rf"{photos} on \w{{3}} at 12:00 PM \(after the hold\)</a>", page)
    page = html.unescape(client.get("/tv/main/schedule", headers=auth()).text)
    assert f'<span class="label-inline">Then</span> {photos}, after the hold.</p>' in page


def test_an_unpaired_tv_has_no_show_now_and_home_says_how_to_pair(site):
    _, _, client = site
    page = client.get("/", headers=auth()).text
    assert 'class="dropdown show-now"' not in page
    # Home says it once: the health line, with the fix as a button.
    assert page.count("is not paired") == 1 and "Pair Frame 1 to show apps on it" not in page
    # While the setup checklist shows, its Start is the one primary button, so the tile's Pair is outline.
    style = "button outline sm" if "checklist-title" in page else "button sm"
    assert re.search(rf'Frame 1 is not paired</span><a class="{style}" href="[^"]+">Pair Frame 1</a>', page)


def test_show_now_goes_through_the_controller_and_returns_to_its_page(site, monkeypatch):
    app, _, client = site
    calls = []
    monkeypatch.setattr(app.extensions["controller"], "show_app", lambda app_id, until: calls.append((app_id, until)))
    for back, target in (("home", "/?job="), ("schedule", "/tv/main/schedule?job="), ("", "/tv/main?job=")):
        response = post(client, "/screen/show-app?tv=main", {"app": "photos", "hold": "resumed", "back": back})
        assert response.status_code == 302 and response.location.startswith(target), (back, response.location)
    assert response.location.endswith("&view=tv")
    assert calls and all(call == ("photos", None) for call in calls)


def test_resume_returns_to_the_page_it_came_from(site):
    app, configs, client = site
    rotation = Rotation(app.extensions["library"], configs.load().tv.identity)
    for back, target in (("home", "/"), ("schedule", "/tv/main/schedule"), ("", "/tv/main?view=tv")):
        rotation.set_hold(Hold("photos", datetime(2099, 1, 1, tzinfo=UTC)))
        response = post(client, "/screen/resume?tv=main", {"back": back})
        assert response.status_code == 302 and response.location == target, back
    page = client.get("/", headers=auth()).text
    assert "The schedule decides what Frame 1 shows again." in page


# Moved pages and per-card saves elsewhere

def test_the_display_page_moved_to_picture(site):
    _, _, client = site
    response = client.get("/tv/main/display?x=1", headers=auth())
    assert response.status_code == 301 and response.location == "/tv/main/picture?x=1"
    assert client.get("/tv/nope/display", headers=auth()).status_code == 404


def test_mats_save_on_the_picture_page(site):
    _, configs, client = site
    before = configs.load()
    response = post(client, "/tv/main/picture/mats", {"mats": "never"})
    assert response.status_code == 302 and response.location.endswith("/tv/main/picture#mats")
    after = configs.load()
    assert after.mats == "never" and after.model_dump(exclude={"mats"}) == before.model_dump(exclude={"mats"})
    post(client, "/tv/main/picture/mats", {"mats": "sometimes"})
    assert configs.load().mats == "never"


def test_old_settings_anchors_point_to_their_new_pages(site):
    _, _, client = site
    page = client.get("/tv/main/settings", headers=auth()).text
    moves = re.search(r"data-hash-moves='([^']+)'", page).group(1)
    for anchor, target in (("app-schedule", "/tv/main/schedule"), ("wake", "/tv/main/schedule#wake-form"),
                           ("mats", "/tv/main/picture#mats"), ("artwork", "#artwork")):
        assert f'"{anchor}": "' in moves and target in moves


def test_tv_settings_saves_only_the_name_and_connection(site):
    _, configs, client = site
    before = configs.load()
    response = post(client, "/tv/main/settings", {"tv_name": "Living Room", "tv_ip": "192.168.1.148",
                                                  "tv_mac": "", "broadcast": ""})
    assert response.status_code == 302 and response.location.endswith("/tv/main/settings#connection")
    after = configs.load()
    assert (after.tv_name, after.tv.ip, after.tv.broadcast) == ("Living Room", "192.168.1.148", "255.255.255.255")
    assert after.app_schedule == before.app_schedule and after.wake == before.wake and after.mats == before.mats


def test_tv_settings_keeps_the_typed_values_on_an_error(site):
    _, configs, client = site
    before = configs.load()
    response = post(client, "/tv/main/settings", {"tv_name": "Den", "tv_ip": "not an address", "tv_mac": ""})
    assert response.status_code == 400
    assert 'value="Den"' in response.text and 'value="not an address"' in response.text
    assert configs.load() == before


def test_household_cards_save_only_their_part(site):
    _, configs, client = site
    before = configs.load()
    response = post(client, "/household/refresh", {"refresh": "30"})
    assert response.status_code == 302 and "#refresh" in response.location
    after = configs.load()
    assert after.site.refresh_minutes == 30
    assert after.model_dump(exclude={"site"}) == before.model_dump(exclude={"site"})
    response = post(client, "/household/refresh", {"refresh": "nope"})
    assert response.status_code == 400 and configs.load().site.refresh_minutes == 30
    response = post(client, "/household/artwork", {"order": "sequential", "change_at": "05:00", "sources": ["met"]})
    assert response.status_code == 302
    art = configs.load().art
    assert (art.enabled, art.order, art.change_at, art.sources) == (False, "sequential", "05:00", ["met"])
    assert configs.load().site == after.site.model_copy(update={"refresh_minutes": 30})


def test_household_page_has_one_card_per_job(site):
    _, _, client = site
    response = client.get("/household", headers=auth())
    assert response.status_code == 200
    for card in ("timezone", "refresh", "artwork", "website", "museum-sources"):
        assert f'id="{card}"' in response.text
    assert "Ready selector and page data. For custom websites." in response.text and "Website build folder" not in response.text


# Days with their own plan

def test_customize_gives_the_picked_days_their_own_plan(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")], days={"mon": {"blocks": []}})
    # Started from Saturday: Saturday and Sunday are picked. Monday has its own plan already: it can't be picked.
    form = editor(client.get("/tv/main/schedule?day=sat", headers=auth()).text, "customize")
    assert 'name="days" value="5" checked' in form and 'name="days" value="6" checked' in form
    assert 'name="days" value="0"  disabled' in form and 'name="days" value="2" >' in form
    # Started from a weekday: only that day.
    form = editor(client.get("/tv/main/schedule?day=tue", headers=auth()).text, "customize")
    assert 'name="days" value="1" checked' in form and 'value="5" checked' not in form
    # A day that has its own plan already keeps it.
    response = post(client, "/tv/main/schedule/sat/customize",
                    {"days_shown": "1", "days": ["0", "5", "6"], "schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=sat#timeline")
    week = configs.load().app_schedule.week
    assert week.days["sat"] == week.days["sun"] == week.default and week.days["mon"].blocks == []
    assert "have their own plan now." in client.get(response.location, headers=auth()).text
    # No day picked: nothing changes.
    before = configs.load()
    response = post(client, "/tv/main/schedule/tue/customize", {"days_shown": "1", "schedule_version": version(client)})
    assert response.status_code == 400 and "Pick at least one day." in editor(response.text, "customize")
    assert configs.load() == before


def test_a_days_plan_copies_to_other_days(site):
    _, configs, client = site
    plan(configs, days={"sat": {"blocks": [block("photos", "08:00", "12:00")], "wake": "09:00"}})
    page = html.unescape(client.get("/tv/main/schedule?day=sat", headers=auth()).text)
    assert "Saturday has its own plan." in page and 'name="days" value="5"  disabled' in editor(page, "copy-plan")
    response = post(client, "/tv/main/schedule/sat/copy", {"days": ["5", "6"], "schedule_version": version(client)})
    assert response.status_code == 302 and response.location.endswith("/tv/main/schedule?day=sat#timeline")
    week = configs.load().app_schedule.week
    assert week.days["sun"] == week.days["sat"] and week.day(6).wake == "09:00" and not week.custom(4)
    assert "Copied Saturday's plan to Sunday. Change each day on its own." in html.unescape(
        client.get(response.location, headers=auth()).text)
    assert "Later changes apply to one day only." in html.unescape(editor(page, "copy-plan"))
    response = post(client, "/tv/main/schedule/sat/copy", {"days": ["5"], "schedule_version": version(client)})
    assert response.status_code == 400 and "Pick at least one other day." in response.text
    # Only a day with its own plan copies it.
    response = post(client, "/tv/main/schedule/mon/copy", {"days": ["1"], "schedule_version": version(client)})
    assert response.status_code == 302 and not configs.load().app_schedule.week.custom(1)


def test_the_page_opens_on_today_when_today_has_its_own_plan(site):
    _, configs, client = site
    today = datetime.now(UTC).strftime("%a").lower()
    plan(configs)
    assert re.search(r'day=every#timeline" aria-current="page">Every day',
                     client.get("/tv/main/schedule", headers=auth()).text)
    plan(configs, days={today: {"blocks": [block("photos", "08:00", "12:00")]}})
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert re.search(rf'day={today}#timeline" aria-current="page">', page)
    # The Every day tab still opens every day's plan.
    page = client.get("/tv/main/schedule?day=every", headers=auth()).text
    assert re.search(r'day=every#timeline" aria-current="page">Every day', page)


def test_a_note_says_once_that_apps_take_turns_at_one_speed(site):
    _, configs, client = site
    configs.mutate(lambda raw: raw.update(app_schedule={"windows": [
        {"apps": ["news", "photos"], "swap_seconds": 120, "start": "07:00", "end": "09:00"},
        {"apps": ["news", "photos"], "swap_seconds": 600, "start": "18:00", "end": "20:00"}]}))
    note = "Your older time rules used different speeds. Layouts now take turns at one speed on every day: every 2 min."
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert note in page
    assert re.search(r'class="turns-note-actions"><a href="#turns">Change</a>', page)
    response = post(client, "/tv/main/schedule/turns-note", {"day": "every"})
    assert response.status_code == 302 and configs.load().app_schedule.turns_note is False
    assert note not in client.get("/tv/main/schedule", headers=auth()).text


def test_the_turns_line_shows_only_where_apps_take_turns(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")])
    assert "take turns every" not in client.get("/tv/main/schedule", headers=auth()).text
    # A saved rest-of-the-time list from an older version is ignored: it never makes apps take turns.
    configs.mutate(lambda raw: raw["app_schedule"].update(default={"apps": ["morning", "photos"]}))
    assert "take turns every" not in client.get("/tv/main/schedule", headers=auth()).text
    plan(configs, [block("news", "07:00", "10:00"), block("photos", "09:00", "11:00")])
    page = client.get("/tv/main/schedule", headers=auth()).text
    assert "Where two or more layouts share a time, they take turns every 5 min." in page


def test_a_day_takes_at_most_24_blocks_from_the_page(site):
    _, configs, client = site
    plan(configs, [block("news", f"{hour:02d}:00", f"{hour:02d}:30") for hour in range(24)])
    response = post(client, "/tv/main/schedule/every/blocks",
                    {"app": "photos", "start": "08:00", "end": "09:00", "schedule_version": version(client)})
    assert response.status_code == 400
    assert "This day has 24 blocks. Use at most 24. Remove a block first." in response.text
    assert len(configs.load().app_schedule.week.default.blocks) == 24


def test_a_block_moves_only_to_an_installed_app(site):
    _, configs, client = site
    plan(configs, [block("news", "07:00", "10:00")])
    response = post(client, "/tv/main/schedule/every/blocks/0",
                    {"app": "gone", "start": "07:00", "end": "10:00", "schedule_version": version(client)})
    assert response.status_code == 400 and "Choose an installed layout." in editor(response.text, "block-0")
    assert [item.app for item in configs.load().app_schedule.week.default.blocks] == ["news"]
    response = post(client, "/tv/main/schedule/every/blocks/0",
                    {"app": "photos", "start": "07:00", "end": "10:00", "schedule_version": version(client)})
    assert response.status_code == 302
    assert [item.app for item in configs.load().app_schedule.week.default.blocks] == ["photos"]


def test_the_empty_day_text_fits_touch_and_pointer(site):
    _, configs, client = site
    plan(configs)
    page = client.get("/tv/main/schedule?day=every", headers=auth()).text
    empty = page[page.index('class="empty-start"'):]
    assert '<span class="hint-pointer">Drag a layout onto the day.' in empty
    assert '<span class="hint-touch">Select a layout to add it at set times. Add two layouts at the same time' in empty
