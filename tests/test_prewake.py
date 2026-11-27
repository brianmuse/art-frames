"""The wake-time image goes to a TV in standby in the 2 minutes before its scheduled wake.

The TV gets it with show=False, so the screen stays off. At the wake, the TV only selects it. Every TV here
is a fake: no test contacts a TV, a sensor or a NAS.
"""
# ruff: noqa: F811

import hashlib
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_app_schedule import rest, three_apps  # noqa: F401  (three_apps is a fixture)
from test_controller import set_wake
from test_screen_power import IP, StandbyTV, standby_device

from art_frames import controller as controller_module
from art_frames import site
from art_frames.config import Config
from art_frames.controller import PREWAKE_TRIES, Controller
from art_frames.television import DemoTelevision, DisplayError

WAKE_IMAGE, NIGHT_IMAGE = b"wake image", b"night image"


def at(clock, seconds=0):
    return datetime.fromisoformat(f"2026-10-26T{clock}:00").replace(tzinfo=ZoneInfo("UTC")) + timedelta(seconds=seconds)


class Frame:
    """A fake Frame TV for the controller. `power` is "on", "standby", "off" or None (no answer).

    `calls` records every change: ("upload", image), ("select", content_id, show), "wake".
    """

    def __init__(self, power="standby"):
        self.power, self.playing = power, False
        self.current = "night-id"
        self.calls, self.count = [], 0
        self.during_upload = None  # runs between the pre-wake upload and its power recheck
        self.prewake_error = None  # raised by prewake_load after starting()

    def status(self):
        if self.power is None:
            return {"awake": False, "playing": False, "paired": True, "known": False, "answered": False}
        if self.power in {"standby", "off"}:
            return {"awake": False, "playing": False, "paired": True, "known": True, "answered": True}
        return {"awake": not self.playing, "playing": self.playing, "paired": True, "known": True, "answered": True}

    def wake(self, *, checks=6):
        self.calls.append("wake")
        if self.playing:
            return False
        self.power = "on"
        return True

    def _upload(self, image, record_upload):
        self.count += 1
        content_id = f"tv-{self.count}"
        self.calls.append(("upload", image))
        record_upload(content_id)
        return content_id

    def prewake_load(self, image, starting, record_upload):
        result = {"outcome": "not-standby", "power": self.power, "power_after": None, "current": None}
        if self.power != "standby":
            return result
        starting()
        if self.prewake_error:
            raise self.prewake_error
        content_id = self._upload(image, record_upload)
        if self.during_upload:
            self.during_upload()
        if self.power != "standby":
            return {**result, "outcome": "turned-on", "power_after": self.power}
        self.calls.append(("select", content_id, False))
        self.current = content_id
        return {**result, "outcome": "selected", "power_after": self.power, "current": True, "seconds": 12.3}

    def select(self, content_id):
        if self.power != "on" or self.playing:
            raise DisplayError("not-art-mode")
        self.calls.append(("select", content_id, True))
        self.current = content_id
        return True

    def publish(self, image, owned_ids, record_upload, keep=()):
        if self.power != "on" or self.playing:
            return None
        content_id = self._upload(image, record_upload)
        self.calls.append(("select", content_id, True))
        self.current = content_id
        return content_id, [*[item for item in owned_ids if item in keep], content_id], True

    def current_artwork(self):
        return self.current if self.power == "on" and not self.playing else None

    def remove_old(self, owned_ids, selected, keep=()):
        return [item for item in owned_ids if item == selected or item in keep]


def setup(configs, library, *, power="standby", refresh=15):
    cfg = Config(wake={"enabled": True}, site={"providers": ["art"], "refresh_minutes": refresh},
                 tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"})
    configs.save(cfg)
    frame, captures = Frame(power), []

    def capture(cfg, library, now, **kwargs):
        captures.append(now)
        return WAKE_IMAGE if now.strftime("%H:%M") >= "07:00" else NIGHT_IMAGE

    controller = Controller(configs, library, tv_factory=lambda _: frame, capture=capture)
    return controller, frame, captures


def device_of(configs):
    return configs.load().tv.identity


def uploads(frame):
    return [call for call in frame.calls if call[0] == "upload"]


def test_a_standby_tv_gets_the_wake_image_once_before_the_wake_then_only_selects_it(configs, library, caplog):
    caplog.set_level(logging.INFO, logger="art_frames")
    controller, frame, captures = setup(configs, library)
    assert controller.tick(at("06:57")) == "asleep"  # the image is prepared, but 3 min is too early
    assert captures == [at("07:00")] and frame.calls == []
    assert controller.tick(at("06:58")) == "asleep"  # 2 min before the wake
    assert frame.calls == [("upload", WAKE_IMAGE), ("select", "tv-1", False)]
    controller.tick(at("06:58", 30))
    controller.tick(at("06:59"))
    assert frame.calls == [("upload", WAKE_IMAGE), ("select", "tv-1", False)]  # once per wake
    device = device_of(configs)
    assert library.state("owned_images:" + device) == ["tv-1"]
    assert library.state("last_display") is None and library.state("display_key:" + device) is None
    assert "TV main: uploaded the 07:00 image in standby before the wake" in caplog.text
    assert "power after: standby), 12.3 s" in caplog.text
    # At the wake: no render, no upload; the TV selects the image it already holds.
    assert controller.tick(at("07:00")) == "selected"
    assert frame.calls[2:] == ["wake", ("select", "tv-1", True)]
    assert captures == [at("07:00")]
    assert library.state("display_key:" + device) == site.revision(configs.load(), library, at("07:00"))
    assert library.state("last_display")["image_sha256"] == hashlib.sha256(WAKE_IMAGE).hexdigest()
    assert library.state("tv_artwork")["content_id"] == "tv-1"
    assert controller.tick(at("07:01")) == "unchanged"
    assert len(uploads(frame)) == 1
    assert "AA:BB" not in caplog.text and IP not in caplog.text


@pytest.mark.parametrize("power,playing", [("on", False), ("on", True), (None, False), ("off", False)])
def test_no_pre_wake_upload_unless_the_tv_is_in_standby(configs, library, caplog, power, playing):
    caplog.set_level(logging.INFO, logger="art_frames")
    controller, frame, _ = setup(configs, library, power=power)
    frame.playing = playing
    controller.tick(at("06:58"))
    controller.tick(at("06:59"))
    assert not [call for call in frame.calls if call[0] == "select" and call[2] is False]
    assert "wake" not in frame.calls
    record = library.state("prewake_day:" + device_of(configs)) or {}
    assert not record.get("content_id") and not record.get("tries")
    if power == "on" and not playing:
        assert uploads(frame) == [("upload", NIGHT_IMAGE)]  # the normal update for 06:58
    else:
        assert not uploads(frame)
    if not playing:  # a programme also stops the preparation, so it logs nothing
        assert "did not upload the 07:00 image before the wake" in caplog.text


@pytest.mark.parametrize("moment", [at("06:50"), at("06:57", 59), at("06:59", 1), at("07:00"), at("07:03")])
def test_no_pre_wake_upload_outside_the_two_minutes_or_too_close_to_the_wake(configs, library, moment):
    controller, frame, _ = setup(configs, library)
    controller.tick(moment)
    assert not [call for call in frame.calls if call[0] == "select" and call[2] is False]


def test_a_tv_turned_on_during_the_upload_gets_no_selection(configs, library, caplog):
    caplog.set_level(logging.INFO, logger="art_frames")
    controller, frame, _ = setup(configs, library)
    frame.during_upload = lambda: setattr(frame, "power", "on")  # someone presses the remote
    controller.tick(at("06:58"))
    assert frame.calls == [("upload", WAKE_IMAGE)]
    assert "but did not select it: the TV left standby" in caplog.text
    controller.tick(at("06:58", 30))
    assert len(uploads(frame)) == 1 or uploads(frame)[1] == ("upload", NIGHT_IMAGE)  # the normal path only


def test_a_restart_does_not_upload_again(configs, library):
    controller, frame, captures = setup(configs, library)
    controller.tick(at("06:58"))
    restarted = Controller(configs, library, tv_factory=lambda _: frame, capture=controller.capture)
    restarted.tick(at("06:59"))
    assert len(uploads(frame)) == 1
    # The restarted controller has no prepared image in memory: it uses its saved copy, renders nothing at the wake.
    restarted._prepared = None
    assert restarted.tick(at("07:00")) == "selected"
    assert len(uploads(frame)) == 1 and frame.calls[-1] == ("select", "tv-1", True)
    assert captures == [at("07:00"), at("07:00")]


def test_failed_tries_are_counted_before_the_upload_and_stop(configs, library):
    controller, frame, _ = setup(configs, library)
    frame.prewake_error = OSError("the TV stopped answering")
    for moment in (at("06:58"), at("06:58", 30), at("06:59"), at("06:59", 30)):
        restarted = Controller(configs, library, tv_factory=lambda _: frame, capture=controller.capture)
        restarted.tick(moment)
    assert library.state("prewake_day:" + device_of(configs))["tries"] == PREWAKE_TRIES
    assert not uploads(frame)
    frame.prewake_error = None
    assert controller.tick(at("07:00")) == "published"  # the old path: wake, then upload
    assert frame.calls == ["wake", ("upload", WAKE_IMAGE), ("select", "tv-1", True)]


def test_a_tv_that_does_not_reach_art_mode_at_the_wake_gets_no_selection(configs, library):
    controller, frame, _ = setup(configs, library)
    controller.tick(at("06:58"))
    frame.wake = lambda **kwargs: False  # a programme started: the wake leaves it
    frame.power, frame.playing = "on", True
    assert controller.tick(at("07:00")) == "watching"
    assert frame.calls == [("upload", WAKE_IMAGE), ("select", "tv-1", False)]


def test_a_selection_the_tv_does_not_confirm_falls_back_to_the_upload(configs, library, monkeypatch):
    controller, frame, captures = setup(configs, library)
    controller.tick(at("06:58"))
    monkeypatch.setattr(frame, "select", lambda content_id: False)
    assert controller.tick(at("07:00")) == "published"
    assert uploads(frame) == [("upload", WAKE_IMAGE), ("upload", WAKE_IMAGE)] and captures == [at("07:00")]


def test_a_rotation_at_the_wake_keeps_the_old_path(configs, library, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="art_frames")
    controller, frame, _ = setup(configs, library)
    monkeypatch.setattr(controller, "plan", lambda cfg, now, available=None: SimpleNamespace(
        app=None, apps=[], rotate=True, key="blocks"))
    controller.tick(at("06:58"))
    assert frame.calls == []
    assert "did not upload the 07:00 image before the wake: the schedule shows a rotation at the wake" in caplog.text


def test_the_owners_test_blank_keeps_the_old_path(configs, library):
    controller, frame, _ = setup(configs, library)
    library.set_state("soft_blank:" + device_of(configs), {"content_id": None, "brightness": 5, "owner": "test"})
    controller.tick(at("06:58"))
    assert frame.calls == []


def wake_at_0701(configs, refresh=2):
    """A 07:01 wake with a 2-minute refresh: the periods start at 07:00 and 07:02, so 07:02:10 is in the next one."""
    cfg = configs.load()
    cfg.site.refresh_minutes = refresh
    set_wake(cfg, "07:01")
    configs.save(cfg)
    return cfg


def test_the_wake_key_holds_when_the_delivery_falls_into_the_next_refresh_period(configs, library):
    controller, frame, captures = setup(configs, library)
    cfg = wake_at_0701(configs)
    assert site.revision(cfg, library, at("07:01")) != site.revision(cfg, library, at("07:02", 10))
    controller.tick(at("06:59", 30))
    assert uploads(frame) == [("upload", WAKE_IMAGE)]
    assert controller.tick(at("07:02", 10)) == "selected"
    assert uploads(frame) == [("upload", WAKE_IMAGE)] and captures == [at("07:01")]
    assert library.state("display_key:" + device_of(configs)) == site.revision(cfg, library, at("07:01"))
    # The next pass renders the new refresh period as usual (the fake renders the same bytes).
    assert controller.tick(at("07:02", 40)) == "same-image"
    assert captures == [at("07:01"), at("07:02", 40)]


def test_the_prepared_image_is_used_across_the_refresh_period_without_a_pre_wake_upload(configs, library):
    controller, frame, captures = setup(configs, library, power=None)  # no answer before the wake
    wake_at_0701(configs)
    controller.tick(at("07:00"))
    assert controller.tick(at("07:02", 10)) == "published"
    assert uploads(frame) == [("upload", WAKE_IMAGE)] and captures == [at("07:01")]


def test_a_delivery_more_than_one_refresh_period_after_the_wake_renders_its_own_time(configs, library):
    controller, frame, captures = setup(configs, library)
    wake_at_0701(configs)
    controller.tick(at("06:59", 30))
    controller.capture = lambda cfg, library, now, **kwargs: captures.append(now) or b"late image"
    # The first delivery runs 2 min 10 s after the wake (a late wake or a failed first try).
    assert controller.tick(at("07:03", 10)) == "published"
    assert uploads(frame)[1:] == [("upload", b"late image")]
    assert ("select", "tv-1", True) not in frame.calls


def test_the_pre_wake_image_is_never_selected_again_after_a_delivery(configs, library):
    controller, frame, _ = setup(configs, library)
    wake_at_0701(configs)
    controller.tick(at("06:59", 30))
    assert controller.tick(at("07:01", 10)) == "selected"
    device = device_of(configs)
    assert library.state("prewake_day:" + device)["used"] is True
    assert controller._prewake_record(device, at("07:01").date()) is None
    controller.capture = lambda cfg, library, now, **kwargs: f"render {now:%H%M%S}".encode()
    frame.publish = lambda image, owned, record, keep=(): (frame.calls.append(("upload", image)), record("tv-9"),
                                                         ("tv-9", [*owned, "tv-9"], False))[2]  # not confirmed
    assert controller.tick(at("07:02", 10)) == "unconfirmed"
    assert controller.tick(at("07:02", 40), force=True) == "unconfirmed"
    assert frame.calls.count(("select", "tv-1", True)) == 1
    assert uploads(frame)[-1] == ("upload", b"render 070240")


def test_a_change_after_the_pre_wake_upload_sends_the_new_image_at_the_wake(configs, library):
    controller, frame, _ = setup(configs, library)
    controller.tick(at("06:58", 10))
    assert uploads(frame) == [("upload", WAKE_IMAGE)]
    cfg = configs.load()
    cfg.site.data = {"changed": True}
    configs.save(cfg)
    controller.capture = lambda cfg, library, now, **kwargs: b"new settings"
    assert controller.tick(at("07:00")) == "published"
    assert uploads(frame) == [("upload", WAKE_IMAGE), ("upload", b"new settings")]
    assert ("select", "tv-1", True) not in frame.calls


def test_a_rotation_at_an_unaligned_wake_renders_its_other_app_after_the_first(three_apps, configs, library):
    # A 06:59 wake with a 15-minute refresh: the first delivery at 07:00:10 is in the next refresh period.
    cfg = Config(site={"providers": [], "refresh_minutes": 15}, wake={"enabled": True},
                 tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"}, app_schedule=rest("news", "photos"))
    set_wake(cfg, "06:59")
    configs.save(cfg)
    frame, renders = Frame(), []

    def capture(cfg, library, now, *, app=None, **kwargs):
        renders.append((app.id, now))
        return f"{app.id}:{now:%H%M%S}".encode()

    controller = Controller(configs, library, tv_factory=lambda _: frame, capture=capture)
    controller.tick(at("06:50"))
    assert renders == [("news", at("06:59"))] and frame.calls == []  # a rotation gets no pre-wake upload
    assert controller.tick(at("07:00", 10)) == "published"
    assert renders == [("news", at("06:59"))] and uploads(frame) == [("upload", b"news:065900")]
    for seconds in (40, 70, 100):
        controller.tick(at("07:00", seconds))
    assert "photos" in [app_id for app_id, _ in renders[1:]]


def test_an_unaligned_wake_and_refresh_keep_the_same_key(configs, library):
    # A 7-minute refresh does not divide 06:45: the wake falls inside a refresh period.
    controller, frame, captures = setup(configs, library, refresh=7)
    cfg = configs.load()
    set_wake(cfg, "06:45")
    configs.save(cfg)
    controller.capture = lambda cfg, library, now, **kwargs: captures.append(now) or WAKE_IMAGE
    controller.tick(at("06:43"))
    assert frame.calls == [("upload", WAKE_IMAGE), ("select", "tv-1", False)]
    assert controller.tick(at("06:45", 20)) == "selected"
    assert captures == [at("06:45")] and len(uploads(frame)) == 1


def test_a_wake_at_the_start_of_an_app_block_gets_that_apps_image(three_apps, configs, library):
    week = rest("photos", windows=[{"apps": ["news"], "start": "06:45", "end": "09:00"}])
    cfg = Config(site={"providers": [], "refresh_minutes": 15}, wake={"enabled": True},
                 tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"}, app_schedule=week)
    set_wake(cfg, "06:45")
    configs.save(cfg)
    frame, renders = Frame(), []

    def capture(cfg, library, now, *, app=None, **kwargs):
        renders.append((app.id, now))
        return f"{app.id}:{now:%H%M}".encode()

    controller = Controller(configs, library, tv_factory=lambda _: frame, capture=capture)
    controller.tick(at("06:43"))
    assert renders == [("news", at("06:45"))]
    assert frame.calls == [("upload", b"news:0645"), ("select", "tv-1", False)]
    assert controller.tick(at("06:45")) == "selected"
    assert renders == [("news", at("06:45"))] and len(uploads(frame)) == 1
    assert controller.last_app == "News"


def test_the_sensors_blank_gives_way_to_the_pre_wake_image(configs, library, monkeypatch):
    controller, frame, _ = setup(configs, library)
    device = device_of(configs)
    library.set_state("soft_blank:" + device, {"content_id": "night-id", "brightness": 5, "owner": "sensor"})
    controller.tick(at("06:58"))
    assert frame.calls == [("upload", WAKE_IMAGE), ("select", "tv-1", False)]
    undone = []
    monkeypatch.setattr(controller, "_undo_sensor_blank",
                        lambda tv, device, cfg, held: undone.append(held["content_id"]) or
                        library.delete_state("soft_blank:" + device) or True)
    assert controller.tick(at("07:00")) == "selected"
    assert undone == ["tv-1"] and len(uploads(frame)) == 1


# The TV side: Television.prewake_load against a fake samsungtvws connection.


class PrewakeTV(StandbyTV):
    """StandbyTV with a current-artwork reply and traps for a key, Art Mode change or brightness change."""

    def remote(self, **kwargs):
        device = super().remote(**kwargs)
        art = device.art()
        art.get_current = lambda: {"content_id": "standby-id"}
        art.set_brightness = lambda value: self.calls.append(("set_brightness", value))
        device.art = lambda **_: art
        return device


def test_the_tv_uploads_and_selects_without_showing_in_standby(tmp_path, monkeypatch):
    fake = PrewakeTV(["standby"])
    device = standby_device(tmp_path, monkeypatch, fake)
    steps = []
    result = device.prewake_load(b"jpeg", lambda: steps.append("starting"), steps.append)
    assert result == {"outcome": "selected", "power": "standby", "power_after": "standby", "current": True,
                      "seconds": 0.0}
    assert fake.calls == ["get_artmode", ("upload", b"jpeg"), ("select", "standby-id", False)]
    assert steps == ["starting", "standby-id"] and not fake.keys


@pytest.mark.parametrize("power", ["on", "off", "", None])
def test_the_tv_sends_nothing_unless_it_reports_standby(tmp_path, monkeypatch, power):
    fake = PrewakeTV([power])
    device = standby_device(tmp_path, monkeypatch, fake)
    result = device.prewake_load(b"jpeg", lambda: pytest.fail("no try starts"), lambda _: pytest.fail("no upload"))
    assert result["outcome"] == "not-standby" and not fake.calls and not fake.keys


def test_the_tv_turned_on_during_the_upload_gets_no_selection(tmp_path, monkeypatch):
    fake = PrewakeTV(["standby", "on"])
    device = standby_device(tmp_path, monkeypatch, fake)
    result = device.prewake_load(b"jpeg", lambda: None, lambda _: None)
    assert result["outcome"] == "turned-on" and result["power_after"] == "on"
    assert fake.calls == ["get_artmode", ("upload", b"jpeg")] and not fake.keys


@pytest.mark.parametrize("fake,outcome,calls", [
    (PrewakeTV(["standby"], answers=False), "no-art", ["get_artmode"]),
    (PrewakeTV(["standby"], upload_fails=True), "upload-failed", ["get_artmode", ("upload", b"jpeg")]),
    (PrewakeTV(["standby"], select_fails=True), "no-reply",
     ["get_artmode", ("upload", b"jpeg"), ("select", "standby-id", False)]),
])
def test_the_tv_reports_each_outcome_and_never_sends_a_key(tmp_path, monkeypatch, fake, outcome, calls):
    device = standby_device(tmp_path, monkeypatch, fake)
    started = []
    assert device.prewake_load(b"jpeg", lambda: started.append(1), lambda _: None)["outcome"] == outcome
    assert fake.calls == calls and not fake.keys
    assert started == ([] if outcome == "no-art" else [1])


def test_demo_prewake_load():
    tv = DemoTelevision()
    assert tv.prewake_load(b"jpeg", lambda: pytest.fail("on"), lambda _: None)["outcome"] == "not-standby"
    tv.screen_off()
    assert tv.prewake_load(b"jpeg", lambda: None, lambda _: None)["outcome"] == "selected"


def test_the_window_is_two_minutes_and_ends_before_the_wake():
    assert controller_module.PREWAKE_SECONDS == 120 and controller_module.PREWAKE_MARGIN == 60


def test_a_slow_render_counts_toward_the_last_minute(configs, library, monkeypatch):
    controller, frame, _ = setup(configs, library)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(controller_module, "monotonic", lambda: clock.now)
    original = controller.capture

    def slow(*args, **kwargs):
        clock.now += 40  # the render takes 40 s
        return original(*args, **kwargs)

    controller.capture = slow
    controller.tick(at("06:58", 30))  # 90 s before the wake when the pass starts, 50 s after the render
    assert frame.calls == []
    controller.tick(at("06:58", 50))  # prepared now: no render, 70 s before the wake
    assert frame.calls == [("upload", WAKE_IMAGE), ("select", "tv-1", False)]
