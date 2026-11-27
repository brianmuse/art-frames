"""The shared timezone picker: alias search data, the macro on the Settings page, and alias normalization on save."""

import json
import re
from datetime import UTC, datetime

import pytest

from art_frames import timezones
from art_frames.web import create_app


@pytest.mark.parametrize(
    ("typed", "zone"),
    [
        ("est", "America/New_York"), ("edt", "America/New_York"), ("Eastern", "America/New_York"),
        ("pst", "America/Los_Angeles"), ("PDT", "America/Los_Angeles"), ("mdt", "America/Denver"),
        ("Mountain", "America/Denver"), ("mt", "America/Denver"), ("Mst", "America/Phoenix"),
        ("arizona", "America/Phoenix"), ("hst", "Pacific/Honolulu"), ("NST", "America/St_Johns"),
        ("bst", "Europe/London"), ("cet", "Europe/Berlin"), ("ist", "Asia/Kolkata"),
        ("AEDT", "Australia/Sydney"), ("z", "UTC"),
        (" america/new_york ", "America/New_York"), ("Asia/Tokyo", "Asia/Tokyo"),
    ],
)
def test_aliases_normalize_to_iana_names_in_any_case(typed, zone):
    assert timezones.normalize(typed) == zone


@pytest.mark.parametrize("stored", ["GMT", "EST", "MST", "HST", "Zulu"])
def test_names_zoneinfo_knows_stay_exactly_as_entered(stored):
    # The form posts the stored value back on every save, so a legacy zone must never move.
    assert timezones.normalize(stored) == stored


def test_choices_are_built_once_per_half_hour():
    first = timezones.choices(datetime(2026, 1, 15, 12, 5, tzinfo=UTC))
    assert timezones.choices(datetime(2026, 1, 15, 12, 29, tzinfo=UTC)) is first
    assert timezones.choices(datetime(2026, 1, 15, 12, 30, tzinfo=UTC)) is not first


def test_half_hour_offset_changes_show_at_once():
    # Lord Howe Island moves from UTC+10:30 to UTC+11 at 15:30 UTC on 3 October 2026.
    def lord_howe(minute):
        options = timezones.choices(datetime(2026, 10, 3, 15, minute, tzinfo=UTC))
        return next(option["label"] for option in options if option["value"] == "Australia/Lord_Howe")
    assert lord_howe(29).endswith("UTC+10:30")
    assert lord_howe(31).endswith("UTC+11")


def test_unknown_values_are_left_for_validation():
    assert timezones.normalize("Not/A_Timezone") == "Not/A_Timezone"


def test_choices_list_current_zones_with_common_ones_first():
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    options = timezones.choices(now)
    values = [option["value"] for option in options]
    assert values[:5] == ["America/New_York", "America/Chicago", "America/Denver", "America/Phoenix", "America/Los_Angeles"]
    assert values.index("Europe/London") < values.index("Asia/Tokyo") < values.index("Africa/Cairo")
    assert options[0]["label"] == "Eastern Time (EST/EDT), New York, UTC−5"
    assert next(o for o in options if o["value"] == "Asia/Kolkata")["label"].endswith("UTC+5:30")
    assert len(values) == len(set(values)) and "UTC" in values
    for legacy in ("EST", "EST5EDT", "US/Eastern", "Etc/GMT+5", "Asia/Calcutta", "Europe/Kiev"):
        assert legacy not in values
    assert {"Europe/Oslo", "Europe/Kyiv", "America/Argentina/Buenos_Aires"} <= set(values)


def test_offsets_follow_daylight_saving_time():
    assert timezones.offset("America/New_York", datetime(2026, 7, 1, tzinfo=UTC)) == "UTC−4"
    assert timezones.offset("America/New_York", datetime(2026, 1, 1, tzinfo=UTC)) == "UTC−5"


def settings_post(client, timezone):
    with client.session_transaction() as session:
        token = session["csrf"]
    return client.post("/settings", data={
        "csrf": token, "tv_name": "Den", "tv_ip": "192.168.1.50",
        "site_entry": "index.html", "site_refresh": "15", "timezone": timezone,
        "site_data": "{}", "wake_at": "07:00", "change_at": "00:00", "art_enabled": "on", "sources": ["met"],
    }, follow_redirects=True)


def test_settings_page_uses_the_timezone_picker(tmp_path):
    client = create_app(tmp_path, demo=True, testing=True).test_client()
    page = client.get("/household").text
    field = re.search(r'<label class="timezone-field" data-timezone-field>.*?</label>', page, re.S)
    assert field, "Household settings renders timezone_field()"
    assert '<input name="timezone" value="UTC" list="timezone-zones"' in field.group()
    assert '<option value="America/Los_Angeles" label="Pacific Time (PST/PDT), Los Angeles, UTC' in field.group()
    assert 'data-keywords="PST PDT PT Pacific"' in field.group()
    assert '<option value="Asia/Tokyo" label="Japan Time (JST), Tokyo, UTC+9"' in field.group()
    assert page.count('name="timezone"') == 1


@pytest.mark.parametrize(("typed", "zone"), [("pst", "America/Los_Angeles"), ("est", "America/New_York")])
def test_saving_an_alias_stores_the_iana_name(tmp_path, typed, zone):
    app = create_app(tmp_path, demo=True, testing=True)
    client = app.test_client()
    client.get("/tv/main/settings")
    page = settings_post(client, typed).text
    assert "Saved." in page
    assert app.extensions["configs"].load().timezone == zone
    assert json.loads((tmp_path / "settings.json").read_text())["timezone"] == zone


def test_saving_an_unknown_timezone_is_rejected_with_a_clear_message(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    client = app.test_client()
    client.get("/tv/main/settings")
    page = settings_post(client, "Mars/Olympus").text
    assert "timezone: Value error, Use an IANA timezone, such as America/New_York." in page
    assert app.extensions["configs"].load().timezone == "UTC"


def test_resaving_a_stored_legacy_zone_keeps_it(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    configs = app.extensions["configs"]
    configs.mutate(lambda raw: raw.update(timezone="GMT"))
    client = app.test_client()
    page = client.get("/household").text
    assert '<input name="timezone" value="GMT"' in page
    assert "Saved." in timezone_post(client, "GMT").text
    assert configs.load().timezone == "GMT"
    assert json.loads((tmp_path / "settings.json").read_text())["timezone"] == "GMT"
    assert "Saved." in timezone_post(client, "pst").text
    assert configs.load().timezone == "America/Los_Angeles"


def timezone_post(client, timezone):
    with client.session_transaction() as session:
        token = session["csrf"]
    return client.post("/household/timezone", data={"csrf": token, "timezone": timezone}, follow_redirects=True)


def test_household_timezone_card_saves_only_the_timezone(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    configs = app.extensions["configs"]
    before = configs.load()
    client = app.test_client()
    client.get("/household")
    assert "Saved." in timezone_post(client, "pst").text
    after = configs.load()
    assert after.timezone == "America/Los_Angeles"
    assert after.model_dump(exclude={"timezone"}) == before.model_dump(exclude={"timezone"})


def test_household_timezone_card_keeps_the_typed_value_on_an_error(tmp_path):
    app = create_app(tmp_path, demo=True, testing=True)
    client = app.test_client()
    client.get("/household")
    with client.session_transaction() as session:
        token = session["csrf"]
    response = client.post("/household/timezone", data={"csrf": token, "timezone": "Mars/Olympus"})
    assert response.status_code == 400
    assert 'value="Mars/Olympus"' in response.text and "Pick a timezone from the list" in response.text
    assert app.extensions["configs"].load().timezone == "UTC"
