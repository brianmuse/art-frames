"""One scheduler owns the TV; presentation follows the clock, wake is a separate event."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep
from zoneinfo import ZoneInfo

from . import analytics, apps, artwork, site
from .config import APP_ID, private_write, wake_time
from .rotation import KEYS as ROTATION_KEYS
from .rotation import Hold, Rotation, held_plan, hold_until_next_change, screen_off
from .site import art_day as art_day
from .site_service import WebsitePending
from .television import MOTION_TIMER_CHOICES, DemoTelevision, DisplayError, Readback, Television, black_jpeg
from .tv_recovery import AddressRecovery

log = logging.getLogger(__name__)
PACKAGE = Path(__file__).resolve().parent
# The renderer runs one job and starts its clock when the job begins, so the
# controller sends one capture at a time across all TVs.
RENDER_LOCK = threading.Lock()
SCHEDULED_WAIT, INTERACTIVE_WAIT = 90, 5
# A direct call waits this long for a scheduler pass (often a slow TV status check) to finish.
ACTION_WAIT = 15
# The delivery pass trusts a confirmed scheduled wake for this long instead of reading the TV status again.
HANDOFF_SECONDS = 30
# A scheduler pass waits this long for a running rotation swap, so a swap never makes it skip.
SWAP_WAIT = 20
# In this many seconds before a scheduled wake, a TV in confirmed standby gets the wake-time image without the
# screen coming on (see _prewake). At the wake, the TV then only selects it: no render and no upload.
PREWAKE_SECONDS = 120
# No pre-wake upload starts later than this many seconds before the wake. An upload can take up to a minute
# and holds the TV lock, so a later start could delay the wake itself.
PREWAKE_MARGIN = 60
# The pre-wake upload starts at most this many times per wake, so a restart cannot repeat it without end.
PREWAKE_TRIES = 2
# Plain words for the pre-wake log line, per Television.prewake_load outcome.
PREWAKE_SKIPS = {
    "not-standby": "the TV does not report standby",
    "no-art": "the TV's art channel does not answer in standby",
    "upload-failed": "the upload failed",
}
# The steps of a morning image, in order, with the words the timing log uses.
TIMING = (("answered", "TV answered"), ("art_mode", "Art Mode confirmed"), ("upload_start", "upload started"),
          ("upload", "upload done"), ("selected", "selected"), ("confirmed", "confirmed"))
# A queued job sets these for its own thread: how long to wait for the TV, and what to call once it has it.
CALLER = threading.local()
# Fixed, safe text per stage; the cause goes only to the log.
ERRORS = {
    "status": "A display update failed; Art Frames will retry",
    "wake": "The scheduled wake did not complete; Art Frames will retry",
    "render": "The website could not be rendered; Art Frames will retry",
    "publish": "The TV did not accept the update; Art Frames will retry",
}
# One plain sentence per delivery result. The result code stays in brackets for searching.
DELIVERY = {
    "published": "the TV shows the new image",
    "unconfirmed": "the image was sent, but the TV did not confirm it shows it",
    "unchanged": "the TV already shows the current image",
    "same-image": "the new render matches the image the TV shows; nothing was uploaded",
    "selected": "the TV shows the layout's stored image; nothing was rendered or uploaded",
    "changed": "the TV left Art Mode or stopped answering before the switch; Art Frames will retry",
    "asleep": "the TV is off; waiting for it to turn on",
    "unknown": "the TV does not answer; waiting",
    "watching": "a TV programme is playing; waiting",
    "unpaired": "the TV is not paired",
    "blanked": "the screen is blanked for a test; Undo blank shows artwork again",
    # Sensor control's blank: no test, so no Undo blank hint. Someone coming in shows the artwork again.
    "room-empty": "the screen is blank because the room is empty; it comes back when someone comes in",
    # The same result while sensor control's room dark rule holds the blank (see _note).
    "room-dark": "the room is dark, so the screen stays blank; it comes back when the light is on and someone "
                 "is there",
    "backoff": "waiting before the next render after a failure",
    "pending": "the website is not ready yet",
    "failed": "the update failed; see the next line",
}
DEVICE_KEYS = ("wake_day:", "wake_missed:", "presence:", "display_key:", "owned_images:", "screen_off_at:",
               "soft_blank:", "soft_blank_image:", "prewake_day:", "wake_dark:", *ROTATION_KEYS)
def app_copy(root, app_id):
    """Where this TV keeps a copy of the image it stores for one layout, so the Now page can show the
    picture after a rotation swap. None for an ID that is not a layout ID."""
    if not isinstance(app_id, str) or not re.fullmatch(APP_ID, app_id):
        return None
    return Path(root) / f"tv-upload-{app_id}.jpg"


def keep_app_copy(root, app_id, image):
    """Save `app_id`'s copy of the image the rotation stores for it, and remove the copies of layouts
    that are no longer installed: at most one file per installed layout.

    Call it after the image is stored (rotation.store), never while publishing: the copy is only for
    the Now page, so a disk error is logged and never affects the upload or the TV.
    """
    path = app_copy(root, app_id)
    if path is None:
        return
    try:
        digest = hashlib.sha256(image).hexdigest()
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            private_write(path, image)
        ids = {item.id for item in apps.installed()} | {apps.ARTWORK_ID, app_id}
        for old in Path(root).glob("tv-upload-*.jpg"):
            if old.name[len("tv-upload-"):-len(".jpg")] not in ids:
                old.unlink(missing_ok=True)
    except OSError as error:
        log.warning("Could not save the picture copy of %s: %s", app_id, error.strerror or type(error).__name__)


DEVICE_RECORDS = ("last_upload", "tv_artwork", "last_display", "art_display", "standby_test")


class Busy(ValueError):
    """Another display operation or website render is running."""


class AppsFailed(ValueError):
    """No layout of a rotation rendered. Each layout backs off on its own, so the TV-wide backoff waits."""


def _locked():
    """Tell a waiting job (see jobs.py) that it now holds the TV."""
    if callable(getattr(CALLER, "locked", None)):
        CALLER.locked()


def _own_message(error):
    """Art Frames' own fixed message, or None: library messages can contain calendar URLs or tokens."""
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    own = trace is not None and Path(trace.tb_frame.f_code.co_filename).resolve().is_relative_to(PACKAGE)
    if own and isinstance(error, (ValueError, OSError)) and str(error):
        return str(error)
    return None


def describe(error):
    """For the log: Art Frames' own message, else only the error type."""
    return _own_message(error) or type(error).__name__


def plain(error):
    """For the web pages: Art Frames' own message, else a fixed sentence."""
    text = _own_message(error)
    return text.rstrip(".") + "." if text else "The layout could not be rendered."


def _shown(value):
    """Log only numbers and on/off; anything else the TV reports is 'unknown'."""
    return value if isinstance(value, int) or value in {"on", "off", "standby"} else "unknown"


def _loggable_option(value):
    """Log only a number or a Sleep After choice; anything else is 'unknown'."""
    return value if isinstance(value, int) or value in MOTION_TIMER_CHOICES else "unknown"


class Controller:
    def __init__(self, configs, library, *, demo=False, tv_factory=None, capture=None, recovery=None):
        self.configs, self.library, self.demo = configs, library, demo
        # Finds the TV again after a DHCP address change; the search runs on its own thread.
        self.recovery = recovery or (None if demo else AddressRecovery(configs, library))
        self.lock = threading.Lock()
        self._prepared = None
        self._reported = None
        self._result = None
        self._stage = "status"
        self._handoff = None  # a scheduled wake that wake() just confirmed; see _wake
        self._pass_started = None  # monotonic() when the running delivery pass started; see _prewake
        # Sensor control's one-shot brightness step after a wake (sensor_control.CurveStep), or None. Each sensor
        # control pass sets it; see _curve_step.
        self.curve_step = None
        self._demo_display = {}  # in-memory demo Art Mode settings for this TV
        self._last_app = None  # the layout the last delivery pass rendered, for the activity record (see last_app)
        self._swapping = threading.Event()  # set while a rotation swap tries or holds the TV lock
        self._swap_failed = False  # log a failed rotation swap once, until a swap works again
        self.tv_factory = tv_factory or (
            lambda cfg: DemoTelevision(self._demo_display) if demo else Television(cfg.tv, configs.root)
        )
        self.capture = capture or site.capture

    @property
    def last_app(self):
        """The name of the layout the last delivery pass rendered or showed, or None."""
        return self._last_app

    def _capture(self, cfg, now, *, wait, app, purpose="scheduled", **options):
        """Render `app` (an apps.App, or None without an installed layout). The built-in Artwork is composed
        here, with no renderer, so it never waits for another render.

        `purpose` tells the layout why it renders: "scheduled", "prepare", "rotation" (a
        background render of a layout that is not on screen) or "preview" (a management preview,
        which must not move the layout's own state forward). `options` go to site.capture().
        """
        if app is not None and app.builtin:
            return artwork.compose(cfg, self.library, now)
        if not RENDER_LOCK.acquire(timeout=max(wait, getattr(CALLER, "wait", 0))):
            CALLER.timed_out = True
            raise Busy("Another website render is running")
        try:
            return self.capture(cfg, self.library, now, demo=self.demo, app=app, purpose=purpose,
                                display=getattr(self.configs, "id", "main"), **options)
        finally:
            RENDER_LOCK.release()

    def preview_site(self, *, app_id=None, test=False):
        """Render a preview of the layout this TV shows now, or of the installed layout `app_id`.

        The layout gets render_purpose "preview", so it does not move its own state forward (a
        photo rotation does not skip a photo).
        """
        wait = getattr(CALLER, "wait", 0)
        if not (self.lock.acquire(timeout=wait) if wait else self.lock.acquire(blocking=False)):
            CALLER.timed_out = True
            raise Busy("Another update is running")
        try:
            _locked()
            cfg = self.configs.load()
            now = datetime.now(ZoneInfo(cfg.timezone))
            available = apps.sources(cfg)
            app = apps.find(app_id, available) if app_id else self.plan(cfg, now, available).app
            if app_id and app is None:
                raise ValueError("That layout is not installed")
            try:
                image = self._capture(apps.configure(cfg, app), now, wait=INTERACTIVE_WAIT, app=app,
                                      purpose="preview")
            except (Busy, WebsitePending):
                raise
            except Exception as error:
                # Health's "Test <app>" (`test`) records its result apart from the TV's renders.
                if test and app_id:
                    Rotation(self.library, self._identity(cfg)).record_test(app.id, now, plain(error))
                raise
            if test and app_id:
                Rotation(self.library, self._identity(cfg)).record_test(app.id, now)
            private_write(self.configs.root / "screen-preview.jpg", image)
            # Which layout made the preview, next to it: a preview send shows and holds that layout.
            private_write(self.configs.root / "screen-preview.json", json.dumps({
                "app": app.id if app else None, "image_sha256": hashlib.sha256(image).hexdigest(),
                "at": now.isoformat()}).encode())
            return image
        finally:
            self.lock.release()

    def send_preview(self, revision):
        """Send the saved preview. During a rotation or a hold, or for a layout the schedule does not
        show now, a confirmed send holds the preview's layout until the next schedule change, at most
        4 hours (see rotation.hold_until_next_change)."""
        if not isinstance(revision, str) or len(revision) != 64 or any(c not in "0123456789abcdef" for c in revision):
            return "preview-changed"
        return self.tick(force=True, preview_revision=revision)

    def preview_app(self, revision=None):
        """The ID of the layout that made the saved preview, or None. With `revision`, only for that image."""
        path = self.configs.root / "screen-preview.json"
        try:
            saved = json.loads(path.read_bytes()) if path.exists() else {}
        except (ValueError, OSError):
            return None
        if not isinstance(saved, dict) or revision is not None and saved.get("image_sha256") != revision:
            return None
        return saved.get("app") if isinstance(saved.get("app"), str) else None

    def show_app(self, app_id, until, now=None):
        """Render `app_id` for this TV now, publish it, then hold it on screen.

        `until` is a datetime, or None to hold until resume(). The hold starts only after the
        TV takes the image. Like any update, it never wakes the TV or leaves a programme.
        """
        def run():
            cfg = self._load()
            return self._tick(now, force=True, scheduled_wake=False, cfg=cfg, show=Hold(app_id, until))
        return self._exclusive(run)

    def resume(self):
        """End a hold: the schedule decides what the TV shows again, from the next pass."""
        Rotation(self.library, self._identity(self.configs.load())).resume()

    def rotation_status(self, now=None):
        """The schedule's state for the web pages, a rotation.RotationStatus. Never writes or contacts the TV."""
        cfg = self.configs.load()
        now = (now or datetime.now(ZoneInfo(cfg.timezone))).astimezone(ZoneInfo(cfg.timezone))
        available, device = apps.sources(cfg), self._identity(cfg)
        return Rotation(self.library, device).status(self.plan(cfg, now, available), now, available,
                                                     blocked=self.swap_block(cfg, device, now))

    def _identity(self, cfg):
        """The TV identity that holds the live per-TV state. It changes only in a pass (see _device)."""
        return self.library.state("tv_identity") or cfg.tv.identity

    def _device(self, cfg):
        """Carry pairing and per-TV state to a new identity; never delete the old one (rollback)."""
        device, stored = cfg.tv.identity, self.library.state("tv_identity")
        previous = stored or cfg.tv.address_identity
        if device != previous:
            tokens = self.configs.root / "tokens"
            # A stored marker means the previous identity holds the live state, so it replaces
            # any older snapshot left under this identity (for example after a settings revert).
            if (tokens / f"{previous}.txt").exists() and (stored or not (tokens / f"{device}.txt").exists()):
                private_write(tokens / f"{device}.txt", (tokens / f"{previous}.txt").read_bytes())
            for prefix in DEVICE_KEYS:
                old, new = self.library.state(prefix + previous), self.library.state(prefix + device)
                if old is None:
                    continue
                if stored or new is None:
                    self.library.set_state(prefix + device, old)
                elif isinstance(new, list) and isinstance(old, list):
                    # First migration after pairing again: keep every owned ID so cleanup still works.
                    self.library.set_state(prefix + device, list(dict.fromkeys([*old, *new])))
            for key in DEVICE_RECORDS:
                record = self.library.state(key)
                if isinstance(record, dict) and record.get("device") == previous:
                    self.library.set_state(key, {**record, "device": device})
        if stored != device:
            # The marker makes this run once per change, so IDs deleted later are not merged back.
            self.library.set_state("tv_identity", device)
        return device

    def _record_status(self, status, cfg):
        """Store the TV's state for the web pages, and when it was read.

        The scheduler reads an unchanged state every 30 s, so the time is saved
        only when the state changes or the saved time is 5 minutes old.
        """
        now = datetime.now(ZoneInfo(cfg.timezone))
        stamp = self.library.state("tv_status_at")
        if self.library.state("tv_status") != status or not stamp or now - datetime.fromisoformat(stamp) >= timedelta(minutes=5):
            self.library.set_state("tv_status_at", now.isoformat())
        self.library.set_state("tv_status", status)
        analytics.record_status(self, status)

    def _remember_artwork(self, device, content_id, image, source, now):
        digest = hashlib.sha256(image).hexdigest()
        path = self.configs.root / "tv-artwork.jpg"
        known = self.library.state("tv_artwork", {}).get("image_sha256") == digest
        if not (known and path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == digest):
            private_write(path, image)
        self.library.set_state("tv_artwork", {
            "device": device, "content_id": content_id, "checked_at": now.isoformat(),
            "image_sha256": digest, "source": source,
        })

    def _remove_old(self, tv, device, selected):
        """A later confirmation of our upload also finishes the cleanup a slow TV missed.

        The stored images of the layouts in use stay on the TV (see Rotation.images).
        """
        key = "owned_images:" + device
        owned = self.library.state(key, [])
        keep = Rotation(self.library, device).keep(content_id=selected)
        if selected in owned and len(owned) > 1 and hasattr(tv, "remove_old"):
            self.library.set_state(key, tv.remove_old(owned, selected, keep))

    def _refresh_all(self, device):
        """Make the next delivery pass render again, for the single layout and for each layout of a rotation."""
        self.library.set_state("display_key:" + device, None)
        Rotation(self.library, device).refresh_all()

    def check_artwork(self):
        def run():
            cfg = self.configs.load()
            device = self._device(cfg)
            tv = self.tv_factory(cfg)
            status = tv.status()
            self._record_status(status, cfg)
            for condition, result in ((not status["paired"], "unpaired"),
                                      (not status.get("known"), "unknown"),
                                      (status["playing"], "watching"),
                                      (not status["awake"], "asleep")):
                if condition:
                    return result
            upload = self.library.state("last_upload", {})
            known_id, known_image = None, None
            image = self._saved_upload(upload.get("image_sha256")) if upload.get("device") == device else None
            if image is not None:
                known_id, known_image = upload.get("content_id"), image
            try:
                content_id, image, source = tv.read_artwork(known_id, known_image)
                self._remember_artwork(device, content_id, image, source, datetime.now(ZoneInfo(cfg.timezone)))
            except Exception:
                return "check-failed"
            if known_id is not None and content_id == known_id:
                self._remove_old(tv, device, content_id)
            return "checked"
        return self._exclusive(run)

    def _display_action(self, action):
        """Run an Art Mode display action under the display lock; record the latest reading."""
        def run():
            cfg = self.configs.load()
            device = self._device(cfg)
            result = action(self.tv_factory(cfg))
            self._record_display(result.settings if isinstance(result, Readback) else result, device, cfg)
            return result
        result = self._exclusive(run)
        if result == "busy":
            raise Busy("Another display operation is running")
        return result

    def _record_display(self, settings, device, cfg):
        """Store the latest Art Mode settings reading for the Picture page."""
        previous = self.library.state("art_display") or {}
        # Keep the last model reading when a change or a failed read brings none.
        if not any((settings.get("about") or {}).values()) and previous.get("device") == device and previous.get("about"):
            settings = {**settings, "about": previous["about"]}
        self.library.set_state("art_display", {
            **settings, "device": device,
            "checked_at": datetime.now(ZoneInfo(cfg.timezone)).isoformat(),
        })

    def display_settings(self):
        return self._display_action(lambda tv: tv.display_settings())

    def set_brightness(self, value):
        result = self._display_action(lambda tv: tv.set_brightness(value))
        log.info("TV %s: Art Mode brightness %s -> %s (TV reports %s)", getattr(self.configs, "id", "main"),
                 _shown(result.before), result.requested, _shown(result.reported))
        return result

    def set_light_sensor(self, on):
        result = self._display_action(lambda tv: tv.set_light_sensor(on))
        log.info("TV %s: Art Mode light sensor %s -> %s (TV reports %s)", getattr(self.configs, "id", "main"),
                 _shown(result.before), result.requested, _shown(result.reported))
        return result

    def set_option(self, field, value):
        """Change one Art Mode option (Sleep After, motion sensitivity, color temperature).

        The TV runs it only in Art Mode and reads it back. Each try is an analytics event:
        "done" when the TV reports the new value, else "not-done", the refusal code,
        "busy" or "failed" (the TV did not answer).
        """
        try:
            result = self._display_action(lambda tv: tv.set_option(field, value))
        except DisplayError as error:
            analytics.record_setting(self, field, error.code)
            raise
        except Busy:
            analytics.record_setting(self, field, "busy")
            raise
        except Exception:
            analytics.record_setting(self, field, "failed")
            raise
        outcome = "done" if result.reported == result.requested else "not-done"
        analytics.record_setting(self, field, outcome)
        log.info("TV %s: Art Mode %s %s -> %s (TV reports %s)", getattr(self.configs, "id", "main"),
                 field.replace("_", " "), _loggable_option(result.before), result.requested, _loggable_option(result.reported))
        return result

    def _screen_action(self, action, *, power=False):
        """Screen off/on tests run under the display lock, like wake. Returns (result, device, cfg).

        A power change can hold the lock for about a minute, so it is refused from the
        10-minute preparation before a scheduled wake until the grace window ends.
        """
        def run():
            cfg = self.configs.load()
            if power:
                in_window, _, prepare_due = self._wake_window(cfg, datetime.now(ZoneInfo(cfg.timezone)))
                if in_window or prepare_due:
                    raise DisplayError("wake-window")
            device = self._device(cfg)
            return action(self.tv_factory(cfg), device, cfg), device, cfg
        result = self._exclusive(run)
        if result == "busy":
            raise Busy("Another display operation is running")
        return result

    def _since_off(self, device, cfg):
        stamp = self.library.state("screen_off_at:" + device)
        if not stamp:
            return None
        seconds = (datetime.now(ZoneInfo(cfg.timezone)) - datetime.fromisoformat(stamp)).total_seconds()
        return max(0, int(seconds))

    def screen_off(self):
        def run(tv, device, cfg):
            probe = tv.screen_off()
            self.library.set_state("screen_off_at:" + device, datetime.now(ZoneInfo(cfg.timezone)).isoformat())
            return probe
        probe, _, _ = self._screen_action(run, power=True)
        log.info("TV %s: screen off sent (TV answers: %s, power %s)", getattr(self.configs, "id", "main"),
                 "yes" if probe["answers"] else "no", _shown(probe["power"]))
        return probe

    def check_screen(self):
        """Read-only probe; holds the lock only for the probe, like a status check."""
        probe, device, cfg = self._screen_action(lambda tv, device, cfg: tv.probe())
        probe = {**probe, "since_off": self._since_off(device, cfg)}
        log.info("TV %s: screen check: answers %s, power %s, Art Mode %s, %s s after screen off",
                 getattr(self.configs, "id", "main"), "yes" if probe["answers"] else "no",
                 _shown(probe["power"]), _shown(probe["art_mode"]), _shown(probe["since_off"]))
        return probe

    def screen_on(self):
        def run(tv, device, cfg):
            result = tv.screen_on()
            if result.result in {"confirmed", "tv-mode"}:
                self.library.delete_state("screen_off_at:" + device)  # the screen is on again
            return result
        result, _, _ = self._screen_action(run, power=True)
        name = getattr(self.configs, "id", "main")
        if result.result == "confirmed":
            log.info("TV %s: screen on by %s in %s s", name, result.method, result.seconds)
        else:
            log.info("TV %s: screen on by %s: %s after %s s", name, result.method, result.result, result.seconds)
        return result

    def presence_wake(self, stop=None):
        """Sensor control: turn the screen on for someone who came in (Television.presence_wake).

        It holds the display lock like Screen on, is refused near a scheduled wake, and stores the TV's
        state after it, so sensor control sees Art Mode at once.
        """
        def run(tv, device, cfg):
            result = tv.presence_wake(stop=stop) if stop is not None else tv.presence_wake()
            if result.result in {"confirmed", "tv-mode"}:
                self.library.delete_state("screen_off_at:" + device)
            self._record_status(tv.status(), cfg)
            if result.result == "confirmed":
                self._curve_step(tv, device, cfg, "presence")  # the first look is at the room's level
            return result
        result, _, _ = self._screen_action(run, power=True)
        log.info("TV %s: screen on for someone in the room by %s: %s after %s s", getattr(self.configs, "id", "main"),
                 result.method, result.result, result.seconds)
        return result

    def blank_record(self, device=None):
        """The soft blank record {content_id, brightness, at}, or None when the screen is not blanked.

        A bad record counts as not blanked, so it never pauses the schedule. Without `device`, it reads
        the TV identity that holds the live per-TV state.
        """
        device = device or self._identity(self.configs.load())
        record = self.library.state("soft_blank:" + device)
        if not isinstance(record, dict):
            return None
        brightness = record.get("brightness")
        if isinstance(brightness, bool) or not isinstance(brightness, int):
            return None
        content_id = record.get("content_id")
        return {**record, "content_id": content_id if isinstance(content_id, str) and content_id else None}

    def soft_blank(self, now=None, owner="test"):
        """Show a black image at the lowest brightness, only in confirmed Art Mode.

        `owner` is "test" (the owner's Tests button) or "sensor" (sensor control: the room is empty). The
        record keeps it, so sensor control undoes only its own blank. A test blank over the sensor's takes it
        over. The first blank saves the image and brightness the TV shows; a second blank keeps them.
        While the record is set, the schedule selects no artwork (see swap_block and _deliver).
        Returns the TV's result with "saved": the record.
        """
        def run(tv, device, cfg):
            key, image_key, owned_key = "soft_blank:" + device, "soft_blank_image:" + device, "owned_images:" + device
            black_id = self.library.state(image_key)
            if not isinstance(black_id, str) or black_id not in self.library.state(owned_key, []):
                black_id = None  # cleanup deleted it from the TV: upload a new one

            def remember(content_id, brightness):
                held = self.blank_record(device)
                if held is not None:
                    # Already blanked: keep the image and brightness saved the first time.
                    if owner == "test" and held.get("owner") == "sensor":
                        self.library.set_state(key, {**self.library.state(key), "owner": "test"})
                    return
                moment = now or datetime.now(ZoneInfo(cfg.timezone))
                self.library.set_state(key, {
                    "content_id": None if content_id == self.library.state(image_key) else content_id,
                    "brightness": brightness, "at": moment.isoformat(), "owner": owner,
                })

            def record(content_id):
                # Owned like any upload, so a later confirmed update deletes it from the TV.
                self.library.set_state(owned_key, list(dict.fromkeys([*self.library.state(owned_key, []), content_id])))
                self.library.set_state(image_key, content_id)
            result = tv.soft_blank(black_jpeg(), black_id, remember, record)
            if result["readback"] is not None:
                self._record_display(result["readback"].settings, device, cfg)
            return {**result, "saved": self.blank_record(device)}
        result, _, _ = self._screen_action(run)
        readback = result["readback"]
        log.info("TV %s: soft blank: black image %s, brightness %s (TV reports %s), saved brightness %s",
                 getattr(self.configs, "id", "main"), "shown" if result["shown"] else "not confirmed",
                 _shown(readback.requested if readback else None), _shown(readback.reported if readback else None),
                 _shown((result["saved"] or {}).get("brightness")))
        return result

    def undo_blank(self, owner=None, brightness=None):
        """Select the saved image and restore the saved brightness, only in confirmed Art Mode.

        With `brightness`, that level comes back instead of the saved one: sensor control passes the curve's
        level for the room light now.

        With `owner`, only a blank of that owner is undone: sensor control never undoes the owner's test blank
        ("not-blanked"). The owner's Undo blank (no owner) undoes either. The result has the blank's "owner".
        The record is cleared only after the TV ran the undo; a refusal keeps it. When the saved
        image is gone, the next update renders and sends the current image again.
        """
        def run(tv, device, cfg):
            saved = self.blank_record(device)
            if saved is None or (owner is not None and saved.get("owner", "test") != owner):
                raise DisplayError("not-blanked")
            level = saved["brightness"] if brightness is None else brightness
            result = tv.undo_blank(saved["content_id"], level)
            self.library.delete_state("soft_blank:" + device)
            if not result["restored"]:
                self._refresh_all(device)
            self._record_display(result["readback"].settings, device, cfg)
            return {**result, "brightness": level, "owner": saved.get("owner", "test")}
        result, _, _ = self._screen_action(run)
        log.info("TV %s: soft blank undone: image %s, brightness %s (TV reports %s)", getattr(self.configs, "id", "main"),
                 {True: "restored", False: "not confirmed"}.get(result["restored"], "not saved"),
                 _shown(result["readback"].requested), _shown(result["readback"].reported))
        return result

    def _undo_sensor_blank(self, tv, device, cfg, held):
        """Undo sensor control's blank inside a delivery that holds the TV, and tell sensor control.

        True when the TV ran the undo. A failure is logged and keeps the blank (False). The empty-room count
        starts again now, so the morning image gets the full time before the next blank.
        """
        from . import sensor_control  # sensor_control imports this module

        name = getattr(self.configs, "id", "main")
        step = self.curve_step
        try:
            # The curve's level for the room light now, else the level from before the blank.
            level = step.level(None, "scheduled") if step is not None else None
        except Exception as error:
            log.warning("TV %s: the room light could not be read for the scheduled wake (%s)", name,
                        type(error).__name__)
            level = None
        try:
            result = tv.undo_blank(held["content_id"], held["brightness"] if level is None else level)
        except Exception as error:
            log.warning("TV %s: the scheduled wake could not undo the sensor's blank (%s)", name,
                        getattr(error, "code", None) or type(error).__name__)
            return False
        self.library.delete_state("soft_blank:" + device)
        self._record_display(result["readback"].settings, device, cfg)
        level = result["readback"].reported
        now = datetime.now().timestamp()
        sensor_control.merge(self.library, sensor_control.CONTROL, blanked=False, empty_again=True,
                             **({"sent": level, "sent_at": now, "unconfirmed": 0} if isinstance(level, int) else {}))
        log.info("TV %s: the scheduled wake undid the sensor's blank", name)
        return True

    def _dark_state(self):
        """True while sensor control's saved state says the room is dark, for the words of a blank."""
        from . import sensor_control  # sensor_control imports this module

        return bool((self.library.state(sensor_control.CONTROL) or {}).get("dark"))

    def _room_dark(self):
        """True when sensor control says the room is dark (CurveStep.dark). A failed check is not dark."""
        step = self.curve_step
        if step is None:
            return False
        try:
            return bool(step.dark())
        except Exception as error:
            log.warning("TV %s: the room light could not be checked (%s)", getattr(self.configs, "id", "main"),
                        type(error).__name__)
            return False

    def _curve_step(self, tv, device, cfg, reason):
        """Sensor control's one-shot step right after a wake: the curve's level for the room light now.

        The caller holds the TV and the TV just confirmed Art Mode. A blank holds its own brightness, and
        control that is off, paused or without a light reading changes nothing (see sensor_control.CurveStep).
        The TV reads power and Art Mode again before the change. A failure keeps the level and never stops the
        wake. Returns the level set, or None.
        """
        step = self.curve_step
        if step is None or self.blank_record(device) is not None:
            return None
        name = getattr(self.configs, "id", "main")
        try:
            settings = tv.display_settings()  # confirms power and Art Mode
            self._record_display(settings, device, cfg)
            level = step.level(settings, reason)
            if level is None:
                return None
            before, readback = settings.get("brightness"), None
            if level != before:  # never send a level the TV already has
                readback = tv.set_brightness(level)  # confirms power and Art Mode again
                self._record_display(readback.settings, device, cfg)
                log.info("TV %s: Art Mode brightness %s -> %s (TV reports %s)", name, _shown(readback.before),
                         readback.requested, _shown(readback.reported))
            step.applied(level, readback, before, reason)
            return level
        except Exception as error:
            log.warning("TV %s: the brightness for the room light was not set after the wake (%s)", name,
                        getattr(error, "code", None) or type(error).__name__)
            return None

    def _saved_preview(self):
        path = self.configs.root / "screen-preview.jpg"
        if not path.exists():
            return None
        with path.open("rb") as handle:
            image = handle.read(site.MAX_ASSET + 1)
        return image if len(image) <= site.MAX_ASSET else None

    def standby_load(self):
        """Owner-run test: load the next image while the TV is in standby, without turning the screen on.

        It uses the saved preview, or the prepared morning image. It is refused near a
        scheduled wake, like the screen power tests. It never sends a key or a wake packet.
        """
        def run(tv, device, cfg):
            image = self._saved_preview() or (self._prepared[1] if self._prepared else None)
            if image is None:
                raise DisplayError("no-image")
            key = "owned_images:" + device

            def record(content_id):
                # Owned like any upload, so the next confirmed update deletes it from the TV.
                self.library.set_state(key, list(dict.fromkeys([*self.library.state(key, []), content_id])))
            result = tv.standby_load(image, record)
            self.library.set_state("standby_test", {
                **result, "device": device, "checked_at": datetime.now(ZoneInfo(cfg.timezone)).isoformat(),
            })
            return result
        result, _, _ = self._screen_action(run, power=True)
        yes_no = lambda value: {True: "yes", False: "no"}.get(value, "unknown")  # noqa: E731
        log.info("TV %s: standby load test: art channel answered %s, upload worked %s, selection %s, "
                 "still in standby %s (power %s), %.1f s", getattr(self.configs, "id", "main"),
                 yes_no(result["answered"]), yes_no(result["uploaded"]), result["selected"] or "not sent",
                 yes_no(result["stayed"]), _shown(result["power_after"]), result["seconds"])
        return result

    def pair(self):
        """Ask the TV for a pairing token; the person accepts on the TV."""
        def run():
            self.tv_factory(self.configs.load()).pair()
            return "paired"
        if self._exclusive(run) == "busy":
            raise Busy("Another display operation is running")

    def tick(self, now=None, *, force=False, preview_revision=None):
        return self._exclusive(lambda: self._tick(now, force=force, preview_revision=preview_revision),
                               wait=None if force else self._pass_wait())

    def _pass_wait(self):
        """A scheduler pass skips a busy TV, but waits for a rotation swap: a skipped pass could
        let a wake handoff expire."""
        return SWAP_WAIT if self._swapping.is_set() else 0

    def _exclusive(self, action, *, wait=None):
        """Run one TV operation at a time. The scheduler passes wait=0 and skips a busy pass."""
        wait = getattr(CALLER, "wait", ACTION_WAIT) if wait is None else wait
        deadline = monotonic() + wait
        acquired = self.lock.acquire(timeout=wait) if wait else self.lock.acquire(blocking=False)
        if not acquired:
            CALLER.timed_out = True
            return "busy"
        try:
            self._stage = "status"  # a failure before the action starts is a status failure, not a stale stage
            with (self.configs.root / "scheduler.lock").open("a") as handle:
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if monotonic() >= deadline:
                            CALLER.timed_out = True
                            return "busy"
                        sleep(0.25)
                _locked()
                return action()
        finally:
            self.lock.release()

    def wake_screen(self):
        def run():
            cfg = self.configs.load()
            self._device(cfg)
            try:
                woke = self.tv_factory(cfg).wake()
            except Exception as error:
                if self.recovery:
                    self.recovery.wake_failed(cfg, error)  # the TV may answer at a new address
                raise
            if not woke:
                return "watching"
            return self._tick(None, force=True, scheduled_wake=False, cfg=cfg)
        return self._exclusive(run)

    def wake_if_due(self, now=None):
        """The scheduler's cheap first pass: only a due wake contacts the TV."""
        def run():
            self._stage = "wake"
            cfg = self._load()
            moment = (now or datetime.now(ZoneInfo(cfg.timezone))).astimezone(ZoneInfo(cfg.timezone))
            device = self._device(cfg)
            # Only this first pass retries a missed wake, so each pass retries at most once.
            if not self._wake_due(cfg, moment, device, retry=True):
                return None
            if self.library.state("wake_dark:" + device) == moment.date().isoformat() and self._room_dark():
                return None  # still dark: the first pass recorded the skip; no TV read, no wake event
            tv = self.tv_factory(cfg)
            status = self._wake(tv, tv.status(), moment, device, cfg)
            self._record_status(status, cfg)
            return status
        return self._exclusive(run, wait=self._pass_wait())

    @staticmethod
    def _wake_window(cfg, now):
        # Each day has its own wake time, or none (config.wake_time).
        today, tomorrow = now.date(), now.date() + timedelta(days=1)
        at = wake_time(cfg, today)
        wake_at = datetime.combine(today, time.fromisoformat(at or "00:00"), tzinfo=now.tzinfo)
        offset = (now - wake_at).total_seconds()
        in_window = bool(at) and 0 <= offset < cfg.wake.grace_minutes * 60
        if at and now < wake_at:
            prepare_at, upcoming = wake_at, at
        else:
            upcoming = wake_time(cfg, tomorrow)
            prepare_at = datetime.combine(tomorrow, time.fromisoformat(upcoming or "00:00"), tzinfo=now.tzinfo)
        prepare_due = bool(upcoming) and 0 < (prepare_at - now).total_seconds() <= 10 * 60
        return in_window, prepare_at, prepare_due

    def _wake_due(self, cfg, now, device, *, retry=False):
        # An unconfirmed wake is retried once per scheduler pass until the grace window ends.
        today = now.date().isoformat()
        return self._wake_window(cfg, now)[0] and (
            self.library.state("wake_day:" + device) != today
            or (retry and self.library.state("wake_missed:" + device) == today)
        )

    def _wake(self, tv, status, now, device, cfg):
        today = now.date().isoformat()
        retry = self.library.state("wake_missed:" + device) == today
        if retry and status.get("known") and status["awake"]:
            # The TV reached Art Mode after an earlier attempt; no further packets.
            self._late_wake(cfg, now, device)
            return status
        if self._room_dark():
            # Sensor control's room dark rule (Sensor.dark_below): no artwork in a dark room. The day is not
            # done: each pass in the wake window checks again, so the wake runs once the light is back. When
            # the window ends in the dark, the sensor turns the screen on when the light comes on with someone
            # there. The result is saved and logged once per day.
            if self.library.state("wake_dark:" + device) != today:
                self.library.set_state("wake_dark:" + device, today)
                self._wake_result("Wake skipped because the room is dark")
            return status
        # Persist before the effect: a process restart must not wake the room twice.
        self.library.set_state("wake_day:" + device, today)
        self.library.delete_state("wake_missed:" + device)
        if status["playing"]:
            self._wake_result("Wake skipped because a TV programme was in progress")
            return status
        if not status["paired"]:
            self._wake_result("Wake skipped because the TV has not been paired")
            return status
        # Wake and content readiness are separate: failed delivery must retry
        # even if pre-wake artwork used the same refresh revision. Render
        # backoff from before the wake must not delay the wake-time image.
        self._refresh_all(device)
        self.library.delete_state("render_backoff")
        woke = None
        try:
            # A retry sends one burst and checks briefly, so other TVs and web actions are not held up.
            woke = tv.wake(checks=1) if retry else tv.wake()
            self._wake_result("Wake confirmed at " + now.strftime("%H:%M") if woke else "Wake skipped because a TV programme was in progress")
        except Exception as error:
            self.library.set_state("wake_missed:" + device, today)
            if self.recovery:
                self.recovery.wake_failed(cfg, error)  # the TV may answer at a new address
            # Log the first miss only; a retry runs every scheduler pass in the window.
            self._wake_result("Wake was attempted; the TV did not confirm Art Mode", quiet=retry)
            if not retry:
                log.warning("Scheduled wake did not complete; Art Frames retries during the wake window")
        if woke is not True:
            return tv.status()
        # wake() has just confirmed Art Mode: another status read only delays the morning image.
        # The delivery pass that follows uses this status, and publish() still checks the TV itself.
        status = {"awake": True, "playing": False, "paired": True, "known": True, "answered": True}
        # Sensor control's one-shot step: the room's level before the morning image comes.
        self._curve_step(tv, device, cfg, "scheduled")
        self._handoff = {"device": device, "status": status, "at": monotonic(),
                         "phases": dict(getattr(tv, "phases", None) or {})}
        return status

    def _tick(self, now, *, force, preview_revision=None, scheduled_wake=True, cfg=None, show=None):
        self._stage = "status"
        self._pass_started = monotonic()  # the render before a pre-wake upload can take a while (see _prewake)
        cfg = self._load(cfg)
        now = (now or datetime.now(ZoneInfo(cfg.timezone))).astimezone(ZoneInfo(cfg.timezone))
        device = self._device(cfg)
        tv = self.tv_factory(cfg)
        scheduled = scheduled_wake and preview_revision is None
        # Only the scheduler's own pass uses a just-confirmed wake; a manual send or update
        # reads the status itself and leaves the handoff for that pass.
        handoff = self._take_handoff(device) if scheduled and not force else None
        status = handoff["status"] if handoff else tv.status()
        if scheduled and self._wake_due(cfg, now, device):
            self._stage = "wake"
            status = self._wake(tv, status, now, device, cfg)
            handoff = None if force else self._take_handoff(device)
        self._arrived(cfg, now, device, status)
        self._record_status(status, cfg)
        if self.recovery:
            # Starts a background address search after a failed wake or a 24-hour outage.
            self.recovery.observe(cfg, status)
        if not status["paired"]:
            return "unpaired"
        try:
            return self._deliver(tv, cfg, now, device, status, force=force, scheduled=scheduled,
                                 preview_revision=preview_revision, woke=handoff, show=show)
        except Busy:
            return "busy"
        except (WebsitePending, AppsFailed):
            raise
        except Exception:
            if not force and self._stage in {"render", "publish"}:
                self._back_off(cfg, now)
            raise

    def _load(self, cfg=None):
        """Load settings, first applying a finished address search (the caller holds the TV lock)."""
        cfg = cfg or self.configs.load()
        if self.recovery and self.recovery.apply(cfg):
            cfg = self.configs.load()
        return cfg

    def plan(self, cfg, now, available=None):
        """apps.plan() for this TV: its blocks' layouts, else the built-in Artwork."""
        return apps.plan(cfg, now, available)

    def _take_handoff(self, device):
        """The status a scheduled wake just confirmed, once, if it is recent and for this TV."""
        handoff, self._handoff = self._handoff, None
        if handoff and handoff["device"] == device and monotonic() - handoff["at"] < HANDOFF_SECONDS:
            return handoff
        return None

    def _log_timing(self, phases, result):
        """One line per scheduled wake delivery: the seconds each step took, from the wake packet on."""
        last = start = phases.get("packet")
        if start is None:
            return  # no wake packet was timed (a demo or test TV)
        steps = []
        for key, label in TIMING:
            if key in phases:
                steps.append(f"{label} +{phases[key] - last:.1f} s")
                last = phases[key]
            else:
                steps.append(f"{label} not reached")
        log.info("TV %s: morning image timing from the wake packet: %s; %.1f s in all [%s]",
                 getattr(self.configs, "id", "main"), ", ".join(steps), last - start, result)

    def _wake_result(self, text, *, quiet=False):
        """Save the wake result for the web page and log it, unless a retry repeats it."""
        self.library.set_state("wake_result", text)
        analytics.record_wake(self, text)
        if not quiet:
            log.info("TV %s: %s", getattr(self.configs, "id", "main"), text)

    def _late_wake(self, cfg, now, device):
        """Record a TV that reached Art Mode after a missed wake, and send its image at once."""
        # An unreachable TV is not a render failure: do not delay the morning image.
        self.library.delete_state("render_backoff")
        self.library.delete_state("wake_missed:" + device)
        self._refresh_all(device)
        self._wake_result("Wake not confirmed at " + (wake_time(cfg, now.date()) or now.strftime("%H:%M"))
                               + "; the TV reached Art Mode at " + now.strftime("%H:%M"))

    def _arrived(self, cfg, now, device, status):
        """A TV that returns to Art Mode gets a fresh image; it never wakes or changes mode.

        Only the delivery path writes this record. One unknown reply is a blip, not an absence.
        """
        key = "presence:" + device
        seen = self.library.state(key) or {}
        if status.get("known") and status["awake"]:
            today = now.date().isoformat()
            # A wake the dark room held (wake_dark) is not late when the TV comes on later that day.
            if (self.library.state("wake_missed:" + device) == today
                    and self.library.state("wake_dark:" + device) != today):
                self._late_wake(cfg, now, device)
            elif not seen.get("awake", True) and (seen.get("away") or seen.get("unknown", 0) >= 2):
                # A selection the TV did not confirm before it slept is retried now.
                self._refresh_all(device)
            record = {"awake": True}
        elif status.get("known"):
            record = {"awake": False, "away": True}  # confirmed standby or a programme
        else:
            record = {"awake": False, "away": bool(seen.get("away")), "unknown": min(seen.get("unknown", 0) + 1, 2)}
        if record != seen:
            self.library.set_state(key, record)

    def _backing_off(self, now):
        backoff = self.library.state("render_backoff")
        return bool(backoff) and now < datetime.fromisoformat(backoff["next_at"])

    def _back_off(self, cfg, now):
        failures = (self.library.state("render_backoff") or {}).get("failures", 0) + 1
        minutes = min(2 ** min(failures - 1, 11), cfg.site.refresh_minutes)
        self.library.set_state("render_backoff", {"failures": failures, "next_at": (now + timedelta(minutes=minutes)).isoformat()})

    def _prepare(self, cfg, prepare_at, wait):
        """Render the wake-time presentation while the screen is still asleep.

        It uses the layout the schedule shows at wake time; for a rotation, its first layout.
        """
        app = self.plan(cfg, prepare_at).app
        cfg = apps.configure(cfg, app)
        # The revision uses prepare_at, the wake time the layout renders for.
        ready_key = site.revision(cfg, self.library, prepare_at, app=app)
        if self._prepared is None or self._prepared[0] != ready_key:
            self._prepared = (ready_key, self._capture(cfg, prepare_at, wait=wait, app=app, purpose="prepare"))

    @staticmethod
    def _wake_at(cfg, now):
        """Today's scheduled wake as a datetime in `now`'s time zone, or None on a day without a wake."""
        at = wake_time(cfg, now.date())
        return datetime.combine(now.date(), time.fromisoformat(at), tzinfo=now.tzinfo) if at else None

    def _wake_key(self, cfg, now, app):
        """The revision of `app` at today's wake time, or None. `cfg` is already configured for the layout.

        The key counts refresh periods (site.revision), so a delivery that runs a little after the wake can
        fall into the next period. The image made for the wake keeps the key of the wake time. That holds only
        for one refresh period after the wake: a later delivery (a late wake, or a failed first try) renders
        the image for its own time instead of sending an old one.
        """
        wake_at = self._wake_at(cfg, now)
        if wake_at is None or not 0 <= (now - wake_at).total_seconds() < cfg.site.refresh_minutes * 60:
            return None
        return site.revision(cfg, self.library, wake_at, app=app)

    def _prewake_record(self, device, day):
        """The image uploaded in standby before the wake on `day` (see _prewake), while the TV still holds it.

        A dict with "content_id", "image_sha256", "key" and "app", or None.
        """
        record = self.library.state("prewake_day:" + device)
        if (not isinstance(record, dict) or record.get("day") != day.isoformat() or not record.get("content_id")
                or record.get("used")):
            return None
        return record if record["content_id"] in self.library.state("owned_images:" + device, []) else None

    def _wake_ready(self, cfg, now, device, app, key):
        """In the wake window: the image made for the wake time, as (key, image, content ID or None), or None.

        The image is the prepared render, or the image uploaded in standby before the wake (its copy is
        tv-upload.jpg, so it survives a restart). The content ID is set when the TV holds that upload. None
        when neither matches the layout's revision now or at the wake time, or the TV already shows it.
        """
        record = self._prewake_record(device, now.date())
        if record is not None and record.get("app") != (app.id if app is not None else None):
            record = None
        prepared = self._prepared
        keys = {prepared[0] if prepared else None, record.get("key") if record else None} - {None}
        if not keys:
            return None
        ready = key if key in keys else self._wake_key(cfg, now, app)
        if ready not in keys or ready == self.library.state("display_key:" + device):
            return None
        content_id = record["content_id"] if record is not None and record.get("key") == ready else None
        image = prepared[1] if prepared and prepared[0] == ready else None
        if image is None and content_id is not None:
            image = self._saved_upload(record.get("image_sha256"))
        if image is not None and content_id is not None and hashlib.sha256(image).hexdigest() != record.get("image_sha256"):
            content_id = None  # another image under the same key: upload this one
        return (ready, image, content_id) if image is not None else None

    def _prewake_used(self, device):
        """A delivery in the wake window recorded an image: the image uploaded before the wake is never
        selected again, so a later pass cannot switch the TV back to it."""
        key = "prewake_day:" + device
        record = self.library.state(key)
        if isinstance(record, dict) and record.get("content_id") and not record.get("used"):
            self.library.set_state(key, {**record, "used": True})

    def _saved_upload(self, digest):
        """The bytes of tv-upload.jpg when they match `digest`, else None."""
        path = self.configs.root / "tv-upload.jpg"
        try:
            with path.open("rb") as handle:
                image = handle.read(site.MAX_ASSET + 1)
        except OSError:
            return None
        return image if len(image) <= site.MAX_ASSET and hashlib.sha256(image).hexdigest() == digest else None

    def _prewake(self, tv, cfg, now, wake_at, device, status):
        """Upload the wake-time image to a TV in confirmed standby, in the PREWAKE_SECONDS before its wake.

        Besides the owner's standby test, this is the one exception to the rule that a TV in standby gets no
        artwork. The image becomes the TV's current artwork with show=False, so the screen stays off. At the
        wake, the delivery selects it (see _wake_ready and _show), with no render and no upload.

        It never sends a key, a wake packet or a brightness change. A TV that is on, plays a programme, does
        not answer or is in an unknown state gets nothing here: the wake and the normal upload follow. The
        TV reads its power state itself before the upload and again before the selection (prewake_load).
        No upload starts in the last PREWAKE_MARGIN seconds, so it does not delay the wake. `now` is the time
        the pass started; the prepare render before this can take a while, so the time since then counts too.
        It starts at most PREWAKE_TRIES (2) times per wake and TV. The record "prewake_day:<device>" is saved
        before each upload starts, so a restart counts too; a failure tries again on the next pass.
        It is limited to a wake whose schedule shows one layout: a rotation keeps the old path.
        """
        load = getattr(tv, "prewake_load", None)
        elapsed = monotonic() - self._pass_started if self._pass_started is not None else 0
        if load is None or self._prepared is None or not (
                PREWAKE_MARGIN <= (wake_at - now).total_seconds() - elapsed <= PREWAKE_SECONDS):
            return
        state_key = "prewake_day:" + device
        day, clock = wake_at.date().isoformat(), wake_at.strftime("%H:%M")
        record = self.library.state(state_key)
        if not isinstance(record, dict) or record.get("day") != day:
            record = {"day": day, "tries": 0}
        if record.get("content_id") or record.get("tries", 0) >= PREWAKE_TRIES:
            return  # done for this wake, or no tries left
        plan = self.plan(cfg, wake_at)
        held = self.blank_record(device)
        reason = None
        if not status.get("paired"):
            reason = "the TV is not paired"
        elif not status.get("known"):
            reason = "the TV does not answer or its state is unknown"
        elif status["awake"]:
            reason = "the TV is on; the normal update sends the image"
        elif plan.rotate:
            reason = "the schedule shows a rotation at the wake"
        elif held is not None and held.get("owner") != "sensor":
            reason = "the screen is blanked for a test"
        elif self._room_dark():
            # The image would replace the black one as the TV's current artwork: the remote would show it.
            reason = "the room is dark"
        if reason is not None:
            self._prewake_skipped(record, state_key, clock, reason)
            return
        key, image = self._prepared
        digest, app = hashlib.sha256(image).hexdigest(), plan.app
        app_id = app.id if app is not None else None
        owned_key = "owned_images:" + device

        def starting():
            # Saved before the upload: a restart during the upload counts this try.
            record.update(tries=record.get("tries", 0) + 1, skip=None)
            self.library.set_state(state_key, record)

        def stored(content_id):
            # Owned like any upload, so a later confirmed update deletes it from the TV.
            self.library.set_state(owned_key, list(dict.fromkeys([*self.library.state(owned_key, []), content_id])))
            private_write(self.configs.root / "tv-upload.jpg", image)
            self.library.set_state("last_upload", {"device": device, "content_id": content_id, "at": now.isoformat(),
                                                   "image_sha256": digest, "app": app_id})
            record.update(content_id=content_id, image_sha256=digest, key=key, app=app_id)
            self.library.set_state(state_key, record)

        try:
            result = load(image, starting, stored)
        except Exception as error:
            self._prewake_skipped(record, state_key, clock, f"the TV call failed ({type(error).__name__})")
            return
        outcome, name = result.get("outcome"), getattr(self.configs, "id", "main")
        seconds = result.get("seconds")
        seconds = float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else 0.0
        if outcome in {"selected", "no-reply"}:
            current = {True: "yes", False: "no"}.get(result.get("current"), "not reported")
            log.info("TV %s: uploaded the %s image in standby before the wake (selected without showing it: %s; "
                     "current artwork: %s; power after: %s), %.1f s", name, clock, "sent" if outcome == "selected" else
                     "no reply", current, _shown(result.get("power_after")), seconds)
        elif outcome == "turned-on":
            log.info("TV %s: uploaded the %s image in standby before the wake, but did not select it: the TV left "
                     "standby (power %s), %.1f s", name, clock, _shown(result.get("power_after")), seconds)
        else:
            reason = PREWAKE_SKIPS.get(outcome, "the TV did not take it")
            if outcome == "not-standby":
                reason += f" (power {_shown(result.get('power'))})"
            self._prewake_skipped(record, state_key, clock, reason)

    def _prewake_skipped(self, record, state_key, clock, reason):
        """Log why the pre-wake upload did not happen, once per reason and wake."""
        if record.get("skip") == reason:
            return
        record["skip"] = reason
        self.library.set_state(state_key, record)
        log.info("TV %s: did not upload the %s image before the wake: %s", getattr(self.configs, "id", "main"),
                 clock, reason)

    def _deliver(self, tv, cfg, now, device, status, *, force, scheduled, preview_revision, woke=None, show=None):
        """One delivery pass. `show` is a rotation.Hold: show that layout now, then hold it."""
        self._stage = "render"
        in_window, prepare_at, prepare_due = self._wake_window(cfg, now)
        wait = INTERACTIVE_WAIT if force else SCHEDULED_WAIT
        if scheduled and prepare_due and not status["playing"] and (force or not self._backing_off(now)):
            self._prepare(cfg, prepare_at, wait)
            self._prewake(tv, cfg, now, prepare_at, device, status)
        if not (prepare_due or in_window):
            self._prepared = None
        if not status.get("known", False):
            return "unknown"
        if status["playing"]:
            return "watching"
        if not status["awake"]:
            # A TV in standby gets no artwork here. The one exception is the pre-wake upload above (_prewake).
            return "asleep"
        held = self.blank_record(device)
        if (held is not None and held.get("owner") == "sensor" and (woke is not None or scheduled and in_window)
                and not self._room_dark()):
            # The scheduled wake turned the screen on: sensor control's blank (room empty) gives way to it.
            # In a dark room (Sensor.dark_below), the blank stays.
            # A failed undo keeps the blank; the next pass in the wake window tries again. The image uploaded
            # before the wake comes back in place of the saved one, so the old image does not flash first.
            early = self._prewake_record(device, now.date()) if in_window else None
            if early is not None and early.get("key") != self.library.state("display_key:" + device):
                held = {**held, "content_id": early["content_id"]}
            if self._undo_sensor_blank(tv, device, cfg, held):
                held = None
            elif woke is not None:
                self._handoff = woke
        if held is not None:
            # The owner's test blank holds the screen until Undo blank ("blanked"). Sensor control's blank
            # holds it until someone comes in ("room-empty").
            return "room-empty" if held.get("owner") == "sensor" else "blanked"
        rotation = Rotation(self.library, device)
        available = apps.sources(cfg)
        rotation.end_hold(now, available)  # a hold ended at its time, or its layout was removed
        schedule = self.plan(cfg, now, available)
        plan = rotation.apply_hold(schedule, now, available)
        if show is not None:
            shown = apps.find(show.app, available)
            if shown is None or not schedule.apps:
                raise ValueError("That layout is not installed")
            plan = held_plan(schedule, shown)
        # Forget the images of layouts this TV no longer shows. A hold keeps the schedule's images for later.
        rotation.images({app.id for app in [*schedule.apps, *plan.apps]})
        # A hold or a preview send keeps a rotation's stored images on the TV for later.
        if plan.rotate and preview_revision is None:
            started = monotonic()
            result = self._rotate(tv, cfg, now, device, plan, rotation, force=force, wait=wait, in_window=in_window)
            if result in {"published", "unconfirmed", "same-image", "changed"}:
                analytics.record_delivery(self, result, monotonic() - started)
            if result != "changed" and not prepare_due:
                self._prepared = None
            return result
        app = plan.app
        if preview_revision is not None:
            # A preview send shows the layout that made the preview, when it is still installed.
            app = apps.find(self.preview_app(preview_revision), available) or app
        self._last_app = app.name if app is not None else None
        cfg = apps.configure(cfg, app)
        key = site.revision(cfg, self.library, now, app=app)
        # In the wake window, the image made for the wake time: (its key, image, content ID on the TV or None).
        ready = self._wake_ready(cfg, now, device, app, key) if in_window and preview_revision is None else None
        if ready is not None:
            key = ready[0]
        if not force and key == self.library.state("display_key:" + device):
            return "unchanged"
        if not force and self._backing_off(now):
            return "backoff"
        if ready is not None and ready[2] is not None and not force:
            # The TV got this image in standby before the wake: select it, with no render and no upload.
            result = "failed"
            try:
                result = self._show(tv, now, device, key,
                                    {"content_id": ready[2], "image_sha256": hashlib.sha256(ready[1]).hexdigest()},
                                    image=ready[1], app=app, rotation=rotation)
            finally:
                if woke is not None and result is not None:  # None: the upload below logs the timing
                    self._log_timing({**woke["phases"], **(getattr(tv, "phases", None) or {})}, result)
            if result:
                if result != "changed" and not prepare_due:
                    self._prepared = None
                if result == "selected":
                    self._prewake_used(device)
                return result
        stored = rotation.images().get(app.id) if app is not None else None
        if (preview_revision is None and not force and stored and stored.get("key") == key
                and stored.get("content_id") in self.library.state("owned_images:" + device, [])):
            # A new slot whose layout has a fresh image on the TV (from a rotation): show it, no render.
            result = self._show(tv, now, device, key, stored)
            if result:
                return result
        if preview_revision is not None:
            path = self.configs.root / "screen-preview.jpg"
            if not path.exists():
                return "no-preview"
            with path.open("rb") as handle:
                image = handle.read(site.MAX_ASSET + 1)
            if len(image) > site.MAX_ASSET or hashlib.sha256(image).hexdigest() != preview_revision:
                return "preview-changed"
        elif ready is not None:
            image = ready[1]
        else:
            image = self._render(cfg, now, app, rotation, wait=wait)
        started = monotonic()  # delivery time covers the upload and selection, not the render
        # Right after a scheduled wake the image is new and the TV still shows yesterday's,
        # so skip the check for an image the TV already shows: it costs one more connection.
        result = "failed"
        try:
            result = self._publish(tv, now, device, key, image, "preview" if preview_revision else "website",
                                   reuse=woke is None, app=app, keep=rotation.keep(app_id=app.id if app else None))
        finally:
            if woke is not None:  # also when the upload fails, to show which step stopped
                self._log_timing({**woke["phases"], **(getattr(tv, "phases", None) or {})}, result)
        analytics.record_delivery(self, result, monotonic() - started)
        if result != "changed" and not prepare_due:
            self._prepared = None  # consumed; a failed upload retries the same image
        if in_window and result in {"published", "unconfirmed", "same-image"}:
            self._prewake_used(device)
        if show is not None and result in {"published", "unconfirmed", "same-image"}:
            rotation.set_hold(show)
        elif preview_revision is not None and app is not None and result in {"published", "same-image"}:
            if not schedule.rotate and schedule.app is not None and app.id == schedule.app.id:
                rotation.resume()  # the schedule's own layout is on screen again: a hold of another layout ends
            else:
                # A preview during a rotation, or of a layout the schedule does not show now.
                # Without a hold, the next swap or render pass would undo the preview the TV just confirmed.
                rotation.set_hold(Hold(app.id, hold_until_next_change(schedule, now)))
        return result

    def _render(self, cfg, now, app, rotation, *, wait, purpose="scheduled"):
        """Render one layout and record the result for its health on the web pages."""
        try:
            image = self._capture(cfg, now, wait=wait, app=app, purpose=purpose)
        except (Busy, WebsitePending):
            raise
        except Exception as error:
            if app is not None and rotation.record_failure(app.id, plain(error), now, cfg.site.refresh_minutes):
                log.warning("TV %s: %s could not render: %s", getattr(self.configs, "id", "main"), app.name, describe(error))
            raise
        if app is not None:
            rotation.record_success(app.id, now)
        return image

    def _show(self, tv, now, device, key, stored, *, image=None, app=None, rotation=None):
        """Select an image that is already on the TV. None when the TV does not confirm it.

        `stored` is a layout's stored image (a rotation's, {content_id, image_sha256}). With `image`, it is the
        image the TV got in standby before the wake (see _prewake): it is recorded like a confirmed upload.
        Television.select confirms power on and Art Mode just before the selection, so a TV in standby or
        playing a programme gets nothing ("changed").
        """
        self._stage = "publish"
        select = getattr(tv, "select", None)
        if select is None:
            return None
        try:
            shown = select(stored["content_id"])
        except DisplayError:
            return "changed"  # the TV left Art Mode; the next pass checks it again
        if not shown:
            return None  # render and upload instead
        if image is not None:
            if app is not None:  # before the cleanup, so the layout's older image is not kept
                rotation.store(app, stored["content_id"], stored["image_sha256"], key, now)
                keep_app_copy(self.configs.root, app.id, image)
            self._remove_old(tv, device, stored["content_id"])
            self._delivered(device, key, stored["content_id"], image, now, "website")
            log.info("TV %s: the TV shows the image it got before the wake; nothing was uploaded again",
                     getattr(self.configs, "id", "main"))
            return "selected"
        self.library.delete_state("render_backoff")
        self.library.set_state("display_key:" + device, key)
        self.library.set_state("last_display", {"kind": "website", "at": now.isoformat(), "device": device,
                                                "image_sha256": stored.get("image_sha256")})
        self._remove_old(tv, device, stored["content_id"])
        return "selected"

    def _rotate(self, tv, cfg, now, device, plan, rotation, *, force, wait, in_window):
        """A rotation's render pass: render and upload at most one layout whose image is out of date.

        Each layout renders on the refresh interval, never on a swap. The swap timer (swap_if_due)
        shows the stored images in turn. A layout that fails to render waits on its own
        backoff, and the pass tries the next layout.
        """
        self._stage = "render"
        owned = self.library.state("owned_images:" + device, [])
        stale, waiting = self._stale_apps(cfg, now, plan, rotation, owned, force=force)
        if not stale:
            return "backoff" if waiting else "unchanged"
        if not force and self._backing_off(now):
            return "backoff"
        app, key, entry, image = self._render_one(stale, now, plan, rotation, force=force, wait=wait, in_window=in_window)
        digest = hashlib.sha256(image).hexdigest()
        if entry.get("image_sha256") == digest and entry.get("content_id") in owned:
            # The TV already holds these exact bytes for this layout: nothing to upload.
            rotation.store(app, entry["content_id"], digest, key, now)
            keep_app_copy(self.configs.root, app.id, image)  # rewrites a missing or different copy
            self.library.delete_state("render_backoff")
            log.info("TV %s: %s image unchanged; skipped the upload", getattr(self.configs, "id", "main"), app.name)
            return "same-image"
        result = self._publish(tv, now, device, key, image, "website", reuse=False, app=app,
                               keep=rotation.keep(app_id=app.id), single=False)
        if result in {"published", "unconfirmed"}:
            rotation.record_shown(plan, app.id, now)
        return result

    def _stale_apps(self, cfg, now, plan, rotation, owned, *, force):
        """The rotation's layouts to render, as (app, settings, key, stored entry), and whether an
        out-of-date layout waits after a render failure. A forced update starts with the layout on screen."""
        images, shown = rotation.images(), rotation.showing(plan)
        order = sorted(plan.apps, key=lambda app: app.id != shown) if force else list(plan.apps)
        stale, waiting = [], False
        for app in order:
            app_cfg = apps.configure(cfg, app)
            key = site.revision(app_cfg, self.library, now, app=app)
            entry = images.get(app.id) or {}
            if not (force or entry.get("key") != key or entry.get("content_id") not in owned):
                continue
            if not force and rotation.failing(app.id, now):
                waiting = True
                continue
            stale.append((app, app_cfg, key, entry))
        return stale, waiting

    def _render_one(self, stale, now, plan, rotation, *, force, wait, in_window):
        """Render the first layout that works. Raise only when no layout rendered.

        A failed layout gets its own backoff (see _render); the TV-wide backoff is left alone.
        """
        pending = failure = None
        shown = rotation.showing(plan)
        for app, app_cfg, key, entry in stale[:1] if force else stale:
            self._last_app = app.name
            prepared = self._prepared if in_window else None
            # The wake key counts only for a layout that does not hold that image yet (see _wake_key).
            if prepared is not None and (prepared[0] == key or entry.get("key") != prepared[0]
                                         and prepared[0] == self._wake_key(app_cfg, now, app)):
                # The image made for the wake time, also when this pass falls into the next refresh period.
                return app, prepared[0], entry, prepared[1]
            purpose = "scheduled" if app.id == shown else "rotation"
            try:
                return app, key, entry, self._render(app_cfg, now, app, rotation, wait=wait, purpose=purpose)
            except Busy:
                raise
            except WebsitePending as error:
                pending = error  # this layout is preparing data; try the next one
            except Exception as error:
                if force:
                    raise
                failure = error  # this layout is broken; try the next one
        if failure is not None:
            raise AppsFailed("No layout of the rotation could be rendered") from failure
        raise pending

    def swap_if_due(self, now=None):
        """The swap timer: show the next layout of a rotation from its stored image.

        It never renders or uploads, and it never waits for the TV lock, so it never overlaps
        an upload. A scheduler pass waits for a running swap instead of skipping (see tick).
        It runs only when the last status reading says Art Mode, and select() checks power and
        Art Mode again. In standby, while watching, in an unknown state, after Screen off,
        during a wake or while a hold is active, it returns "skipped" without contacting the
        TV; the next timer tick (5 s later) checks again. A swap the TV refuses waits for the
        next swap time. It shows only images that match their layout's current revision.
        """
        cfg = self.configs.load()
        moment = (now or datetime.now(ZoneInfo(cfg.timezone))).astimezone(ZoneInfo(cfg.timezone))
        device = self._identity(cfg)
        rotation = Rotation(self.library, device)
        plan = rotation.apply_hold(self.plan(cfg, moment), moment, apps.sources(cfg))
        if not plan.rotate or not rotation.due(plan, moment):
            return None
        if self.swap_block(cfg, device, moment):
            return "skipped"
        self._swapping.set()
        try:
            return self._exclusive(lambda: self._swap(moment), wait=0)
        finally:
            self._swapping.clear()

    def swap_block(self, cfg, device, now, *, swapping=True):
        """Why a swap must not touch the TV now, or None. The swap timer and the Overview both use it.

        "swapping": another swap runs. "wake": a wake handoff waits, or a wake window or its
        prepare render is due. "blanked": the soft blank test holds the screen until Undo blank.
        "screen-off": Screen off was just sent (see rotation.screen_off).
        "watching", "standby" or "unknown": the last status reading is not Art Mode.
        """
        if swapping and self._swapping.is_set():
            return "swapping"
        handoff = self._handoff
        in_window, _, prepare_due = self._wake_window(cfg, now)
        if in_window or prepare_due or handoff is not None and monotonic() - handoff["at"] < HANDOFF_SECONDS:
            return "wake"
        if self.blank_record(device) is not None:
            return "blanked"
        if screen_off(self.library, device, now):
            return "screen-off"
        status = self.library.state("tv_status") or {}
        if not (status.get("paired") and status.get("known")):
            return "unknown"
        if status.get("playing"):
            return "watching"
        if not status.get("awake"):
            return "standby"
        return None

    def _swap(self, now):
        cfg = self.configs.load()
        device = self._device(cfg)
        rotation, available = Rotation(self.library, device), apps.sources(cfg)
        rotation.end_hold(now, available)  # a hold that just ended: swap back to the schedule now
        plan = rotation.apply_hold(self.plan(cfg, now, available), now, available)
        if not plan.rotate or not rotation.due(plan, now) or self.swap_block(cfg, device, now, swapping=False):
            return None
        images = rotation.images({app.id for app in plan.apps})
        owned = self.library.state("owned_images:" + device, [])
        # Only an image of the current revision: after a wake, last night's images are out of date.
        keys = {app.id: site.revision(apps.configure(cfg, app), self.library, now, app=app) for app in plan.apps}
        ready = rotation.ready(plan, owned, keys)
        current = rotation.showing(plan)
        ids = [app.id for app in ready]
        target = ready[(ids.index(current) + 1) % len(ready)] if current in ids else (ready[0] if ready else None)
        # One try per swap interval, whatever the result: no retries.
        rotation.restart_timer(plan, now)
        if target is None or target.id == current:
            return None  # the render pass has not stored a second current image yet
        try:
            shown = self.tv_factory(cfg).select(images[target.id]["content_id"])
        except DisplayError:
            rotation.record_swap(now, "skipped")
            return "skipped"  # not in Art Mode now: nothing was sent
        except Exception:
            shown = False
        if shown:
            rotation.record_shown(plan, target.id, now)
            rotation.record_swap(now, "done")
            # The TV confirmed the selection: the Now page and Home read what it shows from last_display.
            self.library.set_state("last_display", {"kind": "website", "at": now.isoformat(), "device": device,
                                                    "image_sha256": images[target.id].get("image_sha256"),
                                                    "app": target.id})
            if self._swap_failed:
                log.info("TV %s: rotation swaps work again", getattr(self.configs, "id", "main"))
                self._swap_failed = False
            return "swapped"
        rotation.drop(target.id)  # the TV may have lost this image; the next render pass uploads it again
        rotation.record_swap(now, "failed")
        if not self._swap_failed:
            log.warning("TV %s: a rotation swap to %s was not confirmed; Art Frames uploads that layout again",
                        getattr(self.configs, "id", "main"), target.name)
            self._swap_failed = True
        return "failed"

    def _publish(self, tv, now, device, key, image, kind, *, reuse=True, app=None, keep=(), single=True):
        """Upload and select an image. `single` is False for a rotation's upload: display_key
        belongs to single-layout delivery, so a rotation never writes it."""
        self._stage = "publish"
        digest = hashlib.sha256(image).hexdigest()
        ownership_key = "owned_images:" + device
        owned = self.library.state(ownership_key, [])
        last = self.library.state("last_upload") or {}
        current = getattr(tv, "current_artwork", None) if reuse else None
        rotation = Rotation(self.library, device)
        if (
            last.get("device") == device and last.get("image_sha256") == digest
            and last.get("content_id") in owned and current and current() == last["content_id"]
        ):
            # The TV confirms it already shows these exact bytes: skip the 4K upload and the
            # selection. Each refresh period renders again (a layout can show the time), so
            # without this check every period would send the same image again. A forced update
            # or a preview send also skips identical bytes that the TV confirms it shows.
            content_id = last["content_id"]
            if app is not None and kind == "website":
                rotation.store(app, content_id, digest, key, now)
                keep_app_copy(self.configs.root, app.id, image)
            self._remove_old(tv, device, content_id)
            self._delivered(device, key if single else None, content_id, image, now, kind)
            log.info("TV %s: image unchanged; skipped the upload", getattr(self.configs, "id", "main"))
            return "same-image"

        def record(content_id):
            owned.append(content_id)
            self.library.set_state(ownership_key, owned)
            private_write(self.configs.root / "tv-upload.jpg", image)
            self.library.set_state("last_upload", {
                "device": device, "content_id": content_id, "at": now.isoformat(),
                "image_sha256": digest, "app": app.id if app is not None else None,
            })

        result = tv.publish(image, list(owned), record, keep=list(keep))
        if not result:
            return "changed"
        content_id, remaining, confirmed = result
        self.library.set_state(ownership_key, remaining)
        if app is not None and kind == "website":
            rotation.store(app, content_id, digest, key, now)
            keep_app_copy(self.configs.root, app.id, image)  # after the TV calls, outside the publish
        if not confirmed:
            # An unconfirmed selection must not cause another upload every 30s.
            # A manual send or the next refresh period can retry it.
            self.library.delete_state("render_backoff")
            if single:
                self.library.set_state("display_key:" + device, key)
            return "unconfirmed"
        self._delivered(device, key if single else None, content_id, image, now, kind)
        return "published"

    def _delivered(self, device, key, content_id, image, now, kind):
        """The TV confirms it shows this image: record the revision, the artwork and the last display.

        `key` is None for a rotation's upload, which leaves display_key alone.
        """
        self.library.delete_state("render_backoff")
        if key is not None:
            self.library.set_state("display_key:" + device, key)  # this revision is delivered
        self._remember_artwork(device, content_id, image, "upload", now)
        self.library.set_state("last_display", {"kind": kind, "at": now.isoformat(), "device": device,
                                                "image_sha256": hashlib.sha256(image).hexdigest()})

    def run_wake(self):
        """Returns "woke" after a wake attempt, "failed", "busy" or None (no wake due)."""
        try:
            status = self.wake_if_due()
        except Exception as error:
            self._report("wake", error)
            return "failed"
        return "busy" if status == "busy" else None if status is None else "woke"

    def run_once(self):
        """One scheduler delivery pass; returns its result code, "pending" or "failed".

        _tick sets the stage once it holds the TV, so a skipped pass never changes a running job's step.
        """
        try:
            result = self.tick()
            self._note(result)
            if result in {"busy", "backoff"} or (
                result in {"unpaired", "unknown", "watching", "asleep"}
                and self._reported and self._reported[0] in {"render", "publish"}
            ):
                return result  # nothing was rendered, so an earlier render failure still stands
            self._report()
            return result
        except WebsitePending:
            self._note("pending")
            self._report()
            return "pending"
        except Exception as error:
            self._note("failed")
            self._report(self._stage, error)
            return "failed"

    def _note(self, result):
        """Log each change of delivery outcome once, so a missed image can be traced."""
        # A skipped upload logs its own line. Here it counts as "unchanged", so the log does
        # not switch between the two results every refresh period.
        result = "unchanged" if result == "same-image" else result
        if result != self._result and result != "busy":
            text = DELIVERY.get("room-dark" if result == "room-empty" and self._dark_state() else result, result)
            log.info("TV %s: delivery: %s [%s]", getattr(self.configs, "id", "main"), text, result)
            self._result = result

    def _report(self, stage=None, error=None):
        """Log a failure once per distinct cause, and log recovery once."""
        analytics.record_failure(self, stage)
        current = (stage, describe(error)) if stage else None
        name = getattr(self.configs, "id", "main")
        if current != self._reported:
            if current:
                log.warning("TV %s: %s failed: %s", name, *current)
            elif self._reported:
                log.info("TV %s: recovered after %s failure", name, self._reported[0])
            self._reported = current
        text = ERRORS[stage] if stage else ""
        if self.library.state("scheduler_error") != text:
            self.library.set_state("scheduler_error", text)
            self.library.set_state("scheduler_error_at", datetime.now().astimezone().isoformat() if text else None)
