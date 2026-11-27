"""Suggested fixes: guesses about TV settings from stored analytics history only."""

import base64
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from art_frames import analytics
from art_frames.web import create_app

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
HOUR, MINUTE = 3600, 60
PASSWORD = "a-long-test-password-not-a-real-secret"


def midnight(days_ago):
    return NOW - 12 * HOUR - days_ago * 86400


def nights(count, *, standby_minutes=20, deep=True, wake="confirmed"):
    """Each day: wake into Art Mode at 7 AM, standby at 10 PM, then deep standby after standby_minutes."""
    transitions, events = [], []
    for days_ago in range(count, 0, -1):
        day = midnight(days_ago)
        transitions.append((day + 7 * HOUR, "awake"))
        if wake:
            events.append((day + 7 * HOUR, "wake", wake))
        transitions.append((day + 22 * HOUR, "standby"))
        if deep:
            transitions.append((day + 22 * HOUR + standby_minutes * MINUTE, "deep"))
    transitions.append((midnight(0) + 7 * HOUR, "awake"))  # this morning
    if wake:
        events.append((midnight(0) + 7 * HOUR, "wake", wake))
    return transitions, events


def ids(found):
    return [item.id for item in found]


def by_id(found, finding_id):
    return next(item for item in found if item.id == finding_id)


# --- 1. Deep standby that needs a fix ---

def test_deep_standby_after_standby_suggests_the_network_settings():
    transitions, events = nights(7, wake="failed")
    found = analytics.diagnose(transitions, [], events, NOW, UTC)
    fix = by_id(found, "deep-standby")
    assert fix.kind == "fix" and fix.confidence == "high"
    assert fix.evidence.startswith("Seen on 7 of the last 7 nights.")
    assert "answered in standby for about 20 minutes, then stopped answering" in fix.evidence
    assert "did not work on 7 of 7 mornings" in fix.evidence
    # The mornings fail, so matching TV settings do not mean that nothing needs fixing.
    assert fix.steps_intro == "Check these settings."
    assert "nothing needs fixing" not in fix.steps_intro + fix.note
    assert fix.note.endswith("If these match, check the MAC address and wake broadcast address in this TV's Art Frames "
                             "settings. The MAC must be for the TV's active connection (Wi-Fi or Ethernet).")
    assert fix.action == {"label": "Open TV settings", "page": "settings", "anchor": "connection"}
    assert fix.steps[0].startswith("Power On with Mobile: On.") and "Network → Expert Settings" in fix.steps[0]
    assert fix.steps[1].startswith("Auto Power Off: Off.") and "Power and Energy Saving" in fix.steps[1]
    assert "Eco Solution" in fix.steps[1] and len(fix.steps) == 2
    assert "even with the right settings" in fix.summary and "wrong" not in fix.summary
    assert "Art Frames cannot read or change these settings." in fix.note


def test_deep_standby_without_morning_data_keeps_the_plain_checklist():
    transitions, _ = nights(7, wake=None)
    fix = by_id(analytics.diagnose(transitions, [], [], NOW, UTC), "deep-standby")
    assert fix.steps_intro == analytics.NETWORK_CHECK and fix.action is None


def test_deep_standby_straight_from_art_mode_says_so():
    transitions = []
    for days_ago in range(6, 0, -1):
        transitions += [(midnight(days_ago) + 7 * HOUR, "awake"), (midnight(days_ago) + 23 * HOUR, "deep")]
    found = analytics.diagnose(transitions, [], [], NOW, UTC)
    assert "stopped answering soon after it turned off" in by_id(found, "deep-standby").evidence


def test_a_tv_that_answers_all_night_in_standby_gets_no_network_fix():
    transitions, events = nights(7, deep=False)
    assert "deep-standby" not in ids(analytics.diagnose(transitions, [], events, NOW, UTC))
    assert "deep-standby-normal" not in ids(analytics.diagnose(transitions, [], events, NOW, UTC))


def test_short_network_blips_are_not_deep_standby():
    transitions = []
    for days_ago in range(7, 0, -1):
        day = midnight(days_ago)
        transitions += [(day + 22 * HOUR, "standby"), (day + 23 * HOUR, "deep"), (day + 23 * HOUR + 5 * MINUTE, "standby")]
    assert analytics.diagnose(transitions, [], [], NOW, UTC) == []


def test_deep_standby_on_too_few_nights_gives_nothing():
    transitions, events = nights(2, wake="failed")
    assert analytics.diagnose(transitions, [], events, NOW, UTC) == []  # under 3 days of data


# --- 4. Deep standby that is normal ---

def test_deep_standby_with_working_mornings_is_normal():
    transitions, events = nights(7)
    found = analytics.diagnose(transitions, [], events, NOW, UTC)
    assert "deep-standby" not in ids(found)
    info = by_id(found, "deep-standby-normal")
    assert ids(found) == ["deep-standby-normal"]  # one card for the overnight drop, not two
    assert info.kind == "info" and info.title == "The TV leaves the network overnight. This is normal."
    assert info.summary.startswith("Your TV stops answering on the network a while after it goes to standby. "
                                   "Many Frame TVs do this to save power, even with the right settings.")
    assert "The morning wake still works" in info.summary
    assert "woke the TV or sent an image on 7 of 7 mornings" in info.evidence
    assert info.steps_intro == analytics.NETWORK_CHECK and info.steps == analytics.NETWORK_STEPS


def test_an_image_sent_after_the_night_counts_as_a_working_morning():
    transitions, _ = nights(5, wake=None)
    events = [(midnight(days_ago) + 7.5 * HOUR, "delivery", "published") for days_ago in range(4, -1, -1)]
    assert "deep-standby-normal" in ids(analytics.diagnose(transitions, [], events, NOW, UTC))


def test_one_working_morning_is_not_enough_to_call_it_normal():
    transitions, _ = nights(5, wake=None)
    events = [(midnight(0) + 7 * HOUR, "wake", "confirmed")]
    assert ids(analytics.diagnose(transitions, [], events, NOW, UTC)) == ["deep-standby"]


# --- 2. Art Mode Sleep After timer ---

def art_sessions(lengths, starts=(8, 13, 18)):
    """Art Mode sessions of the given minutes, each ending in standby, at varied times of day."""
    transitions = [(midnight(8), "standby")]
    for index, minutes in enumerate(lengths):
        begin = midnight(7 - index // len(starts)) + starts[index % len(starts)] * HOUR
        transitions += [(begin, "awake"), (begin + minutes * MINUTE, "standby")]
    return transitions


def test_art_mode_ending_after_a_fixed_time_points_at_sleep_after():
    transitions = art_sessions([30, 30.5, 29.5, 31, 30, 30, 29, 30, 30.5])
    fix = by_id(analytics.diagnose(transitions, [], [], NOW, UTC), "sleep-after")
    assert fix.title == "Art Mode turns off after 30 minutes" and fix.confidence == "high"
    assert fix.evidence.startswith("Seen 9 of the 9 times Art Mode turned off in the last 14 days, on 3 different days.")
    assert "lasted 29 to 31 minutes" in fix.evidence
    assert fix.steps[-1] == "Set Sleep After to Off."
    assert "Settings → Art Mode" in fix.steps[1]
    assert "Motion Detection" in fix.note and "Night Mode" in fix.note
    assert fix.action is None  # no stored read shows a Sleep After control on the TV page
    fix = by_id(analytics.diagnose(transitions, [], [], NOW, UTC, display={"motion_timer": "30"}), "sleep-after")
    assert fix.action == {"label": "Set Sleep After to Off", "anchor": "art-mode-options"}


@pytest.mark.parametrize("display", [{}, {"brightness": 5}, {"motion_timer": None}])
def test_sleep_after_link_needs_a_read_that_reports_the_timer(display):
    fix = by_id(analytics.diagnose(art_sessions([30] * 6), [], [], NOW, UTC, display=display), "sleep-after")
    assert fix.action is None


def test_sleep_after_card_hides_once_the_tv_reports_the_timer_off():
    found = analytics.diagnose(art_sessions([30] * 9), [], [], NOW, UTC, display={"motion_timer": "off"})
    assert "sleep-after" not in ids(found)


def test_sleep_after_counts_only_art_mode_after_the_last_change():
    transitions = art_sessions([30] * 9)
    changed = midnight(6) + 12 * HOUR  # sessions are on days 7, 6 and 5 ago, at 8 AM, 1 PM and 6 PM
    found = analytics.diagnose(transitions, [], [], NOW, UTC, sleep_set_at=changed)
    fix = by_id(found, "sleep-after")
    assert fix.evidence.startswith("Seen 5 of the 5 times")
    late = midnight(5) + 12 * HOUR  # only two sessions on one day are left: not enough
    assert "sleep-after" not in ids(analytics.diagnose(transitions, [], [], NOW, UTC, sleep_set_at=late))


def test_sleep_after_change_in_the_file_resets_the_history(tmp_path):
    transitions = art_sessions([30] * 9)
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    seed(before, transitions, events=[(midnight(1), "setting", "motion_timer:not-art-mode")])
    assert "sleep-after" in ids(analytics.findings(before, "UTC", now=NOW))  # a refused change resets nothing
    seed(after, transitions, events=[(midnight(4) + 12 * HOUR, "setting", "motion_timer:done")])
    assert "sleep-after" not in ids(analytics.findings(after, "UTC", now=NOW))


def test_a_timer_dayframe_cannot_set_names_the_closest_choice():
    fix = by_id(analytics.diagnose(art_sessions([10] * 6), [], [], NOW, UTC), "sleep-after")
    assert fix.title == "Art Mode turns off after 10 minutes"
    assert "Art Frames cannot set 10 minutes; its closest Sleep After choice is 5 minutes." in fix.summary
    fix = by_id(analytics.diagnose(art_sessions([30] * 6), [], [], NOW, UTC), "sleep-after")
    assert "closest" not in fix.summary


def test_detected_timer_lengths_come_from_the_tv_choices():
    from art_frames.television import MOTION_TIMER_CHOICES
    settable = {int(choice) for choice in MOTION_TIMER_CHOICES if choice != "off"}
    assert settable <= set(analytics.DETECTABLE_SLEEP_MINUTES)
    assert set(analytics.DETECTABLE_SLEEP_MINUTES) - settable == {10, 180}


def test_hour_timers_read_in_hours():
    fix = by_id(analytics.diagnose(art_sessions([120, 121, 119, 120, 118, 122]), [], [], NOW, UTC), "sleep-after")
    assert fix.title == "Art Mode turns off after 2 hours"


def test_art_mode_sessions_of_varied_length_are_not_a_timer():
    transitions = art_sessions([17, 45, 80, 133, 200, 23, 70, 95, 160])
    assert "sleep-after" not in ids(analytics.diagnose(transitions, [], [], NOW, UTC))


def test_watching_tv_after_art_mode_is_not_a_timer():
    transitions = [(midnight(8), "standby")]
    for days_ago in range(6, 0, -1):
        begin = midnight(days_ago) + 8 * HOUR
        transitions += [(begin, "awake"), (begin + 30 * MINUTE, "playing"), (begin + 2 * HOUR, "standby")]
    assert "sleep-after" not in ids(analytics.diagnose(transitions, [], [], NOW, UTC))


def test_two_matching_sessions_are_not_enough():
    transitions = art_sessions([30, 30]) + [(midnight(1), "standby")]
    assert "sleep-after" not in ids(analytics.diagnose(sorted(transitions), [], [], NOW, UTC))


# --- 3. Brightness sensor ---

def readings(values, sensor=None, days=4):
    per_day = -(-len(values) // days)
    return [(midnight(days - index // per_day) + 9 * HOUR + (index % per_day) * 15 * MINUTE, value, sensor)
            for index, value in enumerate(values)]


def standing_history():
    return [(midnight(8), "awake")]  # enough tracked days for every detector


def test_brightness_that_never_changes_means_the_sensor_looks_off():
    found = analytics.diagnose(standing_history(), readings([7] * 16), [], NOW, UTC)
    info = by_id(found, "brightness-sensor")
    assert info.title == "The brightness sensor looks off" and info.kind == "info" and info.confidence == "medium"
    assert info.evidence == "Brightness stayed at 7 in all 16 Art Mode samples on 4 days."
    assert info.steps[-1] == "Set Brightness Sensor to On." and "Brightness Optimization" in info.note


def test_brightness_that_changes_often_means_the_sensor_looks_on():
    found = analytics.diagnose(standing_history(), readings([3, 5, 7, 4] * 4), [], NOW, UTC)
    info = by_id(found, "brightness-sensor")
    assert info.title == "The brightness sensor looks on" and info.steps[-1] == "Set Brightness Sensor to Off."
    assert "from 3 to 7" in info.evidence


def test_the_reported_sensor_setting_wins():
    found = analytics.diagnose(standing_history(), readings([7] * 16, sensor=1), [], NOW, UTC)
    info = by_id(found, "brightness-sensor")
    assert info.title == "The brightness sensor is on" and info.confidence == "high"
    assert info.evidence.startswith("The TV reported the sensor on in its last sample.")


def test_one_rare_change_says_nothing_about_the_sensor():
    found = analytics.diagnose(standing_history(), readings([7] * 15 + [8]), [], NOW, UTC)
    assert "brightness-sensor" not in ids(found)


def test_too_few_brightness_samples_say_nothing():
    assert analytics.diagnose(standing_history(), readings([7] * 6), [], NOW, UTC) == []
    assert analytics.diagnose(standing_history(), readings([7] * 16, days=2), [], NOW, UTC) == []


# --- Not enough data overall ---

def test_no_findings_without_enough_tracked_time():
    assert analytics.diagnose([], [], [], NOW, UTC) == []
    unknown_only = [(midnight(10), "unknown")]
    assert analytics.diagnose(unknown_only, readings([7] * 16), [], NOW, UTC) == []


def test_fixes_come_before_information():
    transitions, events = nights(7, wake="failed")
    found = analytics.diagnose(transitions, readings([7] * 16), events, NOW, UTC)
    assert ids(found) == ["deep-standby", "brightness-sensor"]


# --- Reading the file and showing the fixes ---

def seed(root, transitions, samples=(), events=()):
    def write(db):
        db.executemany("INSERT INTO transitions VALUES (?, ?)", transitions)
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 1, 100, 'on', 1, 0, ?, ?, 0, NULL)",
                       samples)
        db.executemany("INSERT INTO events VALUES (?, ?, ?, NULL)", events)
    analytics.store(root).write(write)


def test_findings_read_old_unknown_nights_as_deep_standby(tmp_path):
    """An older file has "unknown" for each silent night. The unanswered samples show it was deep standby."""
    transitions, _ = nights(6, wake=None)
    transitions = [(at, "unknown" if state == "deep" else state) for at, state in transitions]
    silent = [(at + index * 900,) for at, state in transitions if state == "unknown" for index in range(1, 30)]

    def write(db):
        db.executemany("INSERT INTO transitions VALUES (?, ?)", transitions)
        db.executemany(f"INSERT INTO samples ({', '.join(analytics.SAMPLE_FIELDS)}) VALUES (?, 0, 3000, 'unknown', "
                       "NULL, NULL, NULL, NULL, NULL, NULL)", silent)
    analytics.store(tmp_path).write(write)
    assert ids(analytics.findings(tmp_path, "UTC", now=NOW)) == ["deep-standby"]


def test_findings_never_raise_on_a_damaged_or_missing_file(tmp_path, caplog):
    assert analytics.findings(tmp_path, "UTC", now=NOW) == []
    (tmp_path / analytics.FILE).write_bytes(b"not a database " * 200)
    with caplog.at_level("WARNING"):
        assert analytics.findings(tmp_path, "UTC", now=NOW) == []
        assert analytics.findings(tmp_path, "UTC", now=NOW) == []
        analytics.report(tmp_path, "UTC", 7, now=NOW)
    assert caplog.text.count("could not be read") == 1  # once per damaged file, shared with report()


def auth():
    return {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path, password=PASSWORD, testing=True)


def test_tv_page_shows_nothing_without_enough_data(app):
    assert "Suggested fixes" not in app.test_client().get("/tv/main", headers=auth()).text


def remember_display(app, **values):
    identity = app.extensions["configs"].load().tv.identity
    app.extensions["library"].set_state("art_display", {**values, "device": identity, "checked_at": "2026-10-20T12:00:00+00:00"})


def test_tv_and_analytics_pages_show_suggested_fixes(app, tmp_path):
    shift = time.time() - NOW  # the pages read up to the real now
    seed(tmp_path, [(at + shift, state) for at, state in art_sessions([30, 30.5, 29.5, 31, 30, 30])])
    remember_display(app, motion_timer="30")
    client = app.test_client()
    # Suggested fixes live on Health; the Now page keeps to what the TV shows.
    assert "Suggested fixes" not in client.get("/tv/main", headers=auth()).text
    text = client.get("/tv/main/health", headers=auth()).text
    assert "Suggested fixes" in text and "Art Mode turns off after 30 minutes" in text
    assert "Why we think this:" in text and "<ol><li>Put the TV in Art Mode.</li>" in text
    assert 'class="button secondary" href="/tv/main/picture#art-mode-options">Set Sleep After to Off</a>' in text


def test_sleep_after_link_and_card_follow_the_stored_read(app, tmp_path):
    shift = time.time() - NOW
    seed(tmp_path, [(at + shift, state) for at, state in art_sessions([30] * 6)])
    client = app.test_client()
    text = client.get("/tv/main/health", headers=auth()).text
    assert "Art Mode turns off after 30 minutes" in text and "Set Sleep After to Off</a>" not in text
    remember_display(app, motion_timer="off")
    assert "Art Mode turns off after 30 minutes" not in client.get("/tv/main/health", headers=auth()).text


def test_tv_page_shows_only_fixes_and_analytics_shows_all(app, tmp_path):
    shift = time.time() - NOW
    transitions, events = nights(7)  # a normal overnight drop: an information card
    seed(tmp_path, [(at + shift, state) for at, state in transitions], events=[(at + shift, k, o) for at, k, o in events])
    client = app.test_client()
    assert "This is normal" not in client.get("/tv/main", headers=auth()).text
    assert "The TV leaves the network overnight. This is normal." in client.get("/tv/main/health", headers=auth()).text


def test_failing_mornings_link_to_the_tv_settings_page(app, tmp_path):
    shift = time.time() - NOW
    transitions, events = nights(7, wake="failed")
    seed(tmp_path, [(at + shift, state) for at, state in transitions], events=[(at + shift, k, o) for at, k, o in events])
    text = app.test_client().get("/tv/main/health", headers=auth()).text
    assert 'class="button secondary" href="/tv/main/settings#connection">Open TV settings</a>' in text
