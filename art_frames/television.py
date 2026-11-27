"""Explicit TV operations. Routine status and image updates never send wake packets.

Artwork changes need confirmed power on and Art Mode. Besides the owner's standby test (standby_load), the one
exception is prewake_load: just before a scheduled wake, a TV in confirmed standby gets the wake-time image
with show=False.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import re
import socket
import time
import uuid
from pathlib import Path
from typing import NamedTuple

from PIL import Image
from samsungtvws import SamsungTVWS
from samsungtvws.exceptions import ResponseError

from .names import TV_CLIENT_NAME
from .tv_artwork import ArtworkReader, BoundedArt, BoundedConnection, thumbnail_jpeg

logging.getLogger("samsungtvws").setLevel(logging.WARNING)


class WakeError(ValueError):
    """Fixed reason code; never expose SDK responses or pairing credentials."""


class DisplayError(ValueError):
    """Fixed reason code for Art Mode display actions; never carries SDK text."""

    def __init__(self, code, low=None, high=None):
        super().__init__(code)
        self.code, self.low, self.high = code, low, high


class Readback(NamedTuple):
    """What was asked, what the TV reports afterwards, and the value before the change."""

    requested: int | str
    reported: int | str | None
    before: int | str | None
    acknowledged: bool
    settings: dict


class ScreenOn(NamedTuple):
    """How the screen was turned on, how long it took, and the result code."""

    method: str  # "power key" or "wake signal"
    seconds: int
    result: str  # confirmed, not-confirmed, tv-mode or watching
    # presence_wake only: when the key went out and when the TV confirmed Art Mode (time.monotonic()), or None.
    key_at: float | None = None
    confirmed_at: float | None = None


POWER_STATES = {"on", "standby", "off"}
# Sensor control's wake signal: this many bursts, this many seconds apart (about 40 s in all).
PRESENCE_WAKE_TRIES, PRESENCE_WAKE_WAIT = 3, 13
PRESENCE_WAKE_LIMIT = 45  # seconds: the longest presence_wake waits for the screen, key included
# After a wake signal, the TV must answer in standby for this long before the one short KEY_POWER: a Frame that
# is still starting up often answers in standby for a moment before it reaches Art Mode by itself.
STANDBY_DWELL = 4
# Seconds to wait for Art Mode after the power key. Each check can take about 9 s
# (three 3 s replies), so the key path holds the display lock for about 55 s at most
# and the wake-signal path for about a minute. The controller refuses both near a scheduled wake.
SCREEN_ON_WAIT = 30
# After the key, check for Art Mode every FAST_POLL seconds for the first FAST_SECONDS, then every second.
FAST_POLL, FAST_SECONDS = 0.5, 5
BRIGHTNESS_RANGE = (0, 50)  # used when the TV does not report its own limits
# Art Mode options. samsungtvws 3.0.6 sends these values as strings ("off", "5", "1" ...).
MOTION_TIMER_CHOICES = ("off", "5", "15", "30", "60", "120", "240")  # Sleep After, in minutes
SLEEP_AFTER_MINUTES = tuple(int(choice) for choice in MOTION_TIMER_CHOICES if choice != "off")
MOTION_SENSITIVITY_RANGE = (1, 3)
COLOR_TEMPERATURE_RANGE = (-5, 5)  # used when the TV does not report its own limits
OPTION_SETTERS = {"motion_timer": "set_motion_timer", "motion_sensitivity": "set_motion_sensitivity",
                  "color_temperature": "set_color_temperature"}
MAX_SETTING_ITEMS = 40
READBACK_TRIES = 3
DISPLAY_TIMEOUT = 3  # seconds per TV reply; bounds how long the display lock is held
SAMPLE_BUDGET = 10  # seconds; an analytics sample starts no new read after this
STANDBY_SETTLE = 5  # seconds to wait before reading the power state again after the standby test
BLANK_SIZE = (3840, 2160)  # the black image of the soft blank test, at the TV's art size
# Total time limits for one art channel connection, in seconds. Each reply still has its own
# shorter timeout; these stop a TV that keeps sending other events from holding a reply wait forever.
ART_SECONDS = 20  # state checks: status, current artwork, probe, sample, wake, cleanup
# Art Mode setting reads and changes, and rotation swaps. A change with all its read-backs can take
# about 65 s when each reply is slow, so the limit leaves room for that.
DISPLAY_SECONDS = 120
# An upload (the check, the image data, then the selection and its confirmation), and the soft blank
# and its undo: both select an image and then change the brightness on one connection.
UPLOAD_SECONDS = 300
# The remote-control channel (keys and pairing): opening it and the key sends that follow must end
# within the reply timeout plus this many seconds. Pairing waits up to 120 s for the person to press Allow.
REMOTE_SECONDS = 15


@functools.lru_cache(maxsize=1)
def black_jpeg():
    """A pure-black JPEG at the TV's art size, for the soft blank test. It is about 50 KB."""
    buffer = io.BytesIO()
    Image.new("RGB", BLANK_SIZE).save(buffer, "JPEG", quality=85, optimize=True)
    return buffer.getvalue()


def _number(value):
    """A TV-reported whole number, or None; text such as "5" is accepted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if -9999 <= value <= 9999 else None
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,4}", value.strip()):
        return int(value.strip())
    return None


def _text(value):
    """A short printable string for display; never trusted as markup (templates escape it)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return "".join(c for c in str(value) if c.isprintable())[:40]


def _on_off(value):
    value = str(value).strip().lower() if value is not None else ""
    return value if value in {"on", "off"} else None


# Only these device-info fields are kept. The reply also holds the TV's IP,
# MAC address, network name and unique IDs, which Art Frames never stores.
ABOUT_FIELDS = {"modelName": "model_name", "model": "model_code", "firmwareVersion": "firmware", "OS": "os"}


def parse_about(info):
    """Reduce rest_device_info() to the TV's model and software versions."""
    device = info.get("device") if isinstance(info, dict) else None
    device = device if isinstance(device, dict) else {}
    return {key: _text(device.get(field)) for field, key in ABOUT_FIELDS.items()}


def parse_display_settings(payload):
    """Reduce get_artmode_settings() to a small dict; unknown shapes give empty values."""
    nested = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(nested, str):
        try:
            nested = json.loads(nested)
        except ValueError:
            nested = None
    entries = [item for item in nested if isinstance(item, dict)] if isinstance(nested, list) else []
    by_name = {str(item.get("item")): item for item in entries}
    brightness = by_name.get("brightness", {})
    color = by_name.get("color_temperature", {})
    # Night Mode has no fixed item name across models; keep the first item that names it.
    night = next((item for name, item in by_name.items() if "night" in name.lower()), None)
    return {
        "brightness": _number(brightness.get("value")),
        "min": _number(brightness.get("min")),
        "max": _number(brightness.get("max")),
        "sensor": _on_off(by_name.get("brightness_sensor_setting", {}).get("value")),
        "color_temperature": _number(color.get("value")),
        "color_temperature_min": _number(color.get("min")),
        "color_temperature_max": _number(color.get("max")),
        # None means the TV does not list the item; the page then hides it.
        "motion_timer": _motion_timer(by_name["motion_timer"].get("value")) if "motion_timer" in by_name else None,
        # The Sleep After values the TV lists ("valid_values"), or () when it lists none.
        "motion_timer_choices": _timer_choices(by_name.get("motion_timer", {}).get("valid_values")),
        "motion_sensitivity": _number(by_name.get("motion_sensitivity", {}).get("value")),
        "night_mode": (_on_off(night.get("value")) or _text(night.get("value")) or "unknown") if night else None,
        "items": [
            {key: _text(item.get(key)) for key in ("item", "value", "min", "max")}
            for item in entries[:MAX_SETTING_ITEMS]
        ],
    }


def _motion_timer(value):
    """The TV's Sleep After value as one of MOTION_TIMER_CHOICES, or "unknown"."""
    value = str(value).strip().lower() if value is not None else ""
    return value if value in MOTION_TIMER_CHOICES else "unknown"


def _timer_choices(values):
    """The Sleep After values a TV lists that Art Frames knows, in MOTION_TIMER_CHOICES order."""
    listed = {str(value).strip().lower() for value in values} if isinstance(values, list) else set()
    return tuple(choice for choice in MOTION_TIMER_CHOICES if choice in listed)


def shortest_sleep_after(settings):
    """The shortest Sleep After the TV lists, such as "5". When the TV lists none, the shortest value of
    MOTION_TIMER_CHOICES (Samsung's documented list); the readback after the change confirms it."""
    listed = [choice for choice in settings.get("motion_timer_choices") or () if choice != "off"]
    return min(listed, key=int) if listed else str(min(SLEEP_AFTER_MINUTES))


def color_temperature_range(settings):
    low, high = settings.get("color_temperature_min"), settings.get("color_temperature_max")
    low = COLOR_TEMPERATURE_RANGE[0] if low is None else low
    high = COLOR_TEMPERATURE_RANGE[1] if high is None else high
    return (low, high) if low <= high else COLOR_TEMPERATURE_RANGE


def check_option(field, value, settings):
    """Check an Art Mode option before any TV change. Returns the value the library sends."""
    if field == "motion_timer":
        if value not in MOTION_TIMER_CHOICES:
            raise DisplayError("invalid")
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise DisplayError("invalid")
    if field == "motion_sensitivity":
        if settings.get("motion_sensitivity") is None:
            raise DisplayError("not-reported")  # the TV does not list it, so it has no motion sensor setting
        low, high = MOTION_SENSITIVITY_RANGE
    elif field == "color_temperature":
        low, high = color_temperature_range(settings)
    else:
        raise DisplayError("invalid")
    if not low <= value <= high:
        raise DisplayError("out-of-range", low, high)
    return value


def brightness_range(settings):
    low, high = settings.get("min"), settings.get("max")
    low = BRIGHTNESS_RANGE[0] if low is None else low
    high = BRIGHTNESS_RANGE[1] if high is None else high
    return (low, high) if low <= high else BRIGHTNESS_RANGE


def check_brightness(value, settings):
    low, high = brightness_range(settings)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DisplayError("out-of-range", low, high)
    return value


class Remote(BoundedConnection, SamsungTVWS):
    """SamsungTVWS with total deadlines on the remote-control channel and the art channel."""

    def open(self):
        if self.connection:
            return self.connection
        # The deadline starts when the channel opens; a key send right after it shares the deadline.
        self.deadline = time.monotonic() + (self.timeout or 0) + REMOTE_SECONDS
        return self._connect_bounded()

    def art(self, seconds=ART_SECONDS):
        """Like SamsungTVWS.art(), with the same settings, but with a total deadline of `seconds`.

        Television opens every art channel here (artwork_reader has its own 15 s limit), so no art
        reply wait can block forever. The deadline starts now.
        """
        return BoundedArt(self.host, token=self.token, token_file=self.token_file, port=self.port,
                          timeout=self.timeout, key_press_delay=self.key_press_delay, name=self.name,
                          seconds=seconds)


class Television:
    def __init__(self, cfg, root: Path):
        self.cfg = cfg
        self.token = root / "tokens" / f"{cfg.identity}.txt"
        # When each step of a wake or an image update finished (time.monotonic()); the controller logs these.
        self.phases = {}

    def remote(self, timeout=None):
        if not self.cfg.ip:
            raise ValueError("Set your TV's private IP address first")
        return Remote(
            host=self.cfg.ip,
            port=self.cfg.port,
            name=TV_CLIENT_NAME,
            timeout=timeout or self.cfg.timeout_seconds,
            token_file=str(self.token),
        )

    def pair(self):
        self.token.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        remote = self.remote(timeout=120)
        try:
            remote.open()  # the only action allowed to initiate a pairing prompt
        finally:
            remote.close()
        if not self.token.exists():
            raise ValueError("Pairing was not completed. Press Allow on the TV and try again.")
        self.token.chmod(0o600)

    def artwork_reader(self):
        return ArtworkReader(host=self.cfg.ip, port=self.cfg.port, name=TV_CLIENT_NAME,
                             timeout=5, token_file=str(self.token))

    @staticmethod
    def current_id(art):
        result = art.get_current()
        value = result.get("content_id") if isinstance(result, dict) else None
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value):
            raise ValueError("TV did not report its artwork")
        return value

    def read_artwork(self, known_id=None, known_image=None):
        art = self.artwork_reader()
        try:
            if art.get_artmode() != "on":
                raise ValueError("TV is not in Art Mode")
            content_id = self.current_id(art)
            matching_upload = content_id == known_id and known_image is not None
            image = known_image if matching_upload else thumbnail_jpeg(art.get_thumbnail(content_id))
            if self.current_id(art) != content_id or art.get_artmode() != "on":
                raise ValueError("TV artwork changed during check")
            return content_id, image, "upload" if matching_upload else "thumbnail"
        finally:
            try:
                art.close()
            except Exception:
                pass

    def current_artwork(self):
        """The selected content ID, or None unless Art Mode and the reply are confirmed."""
        art = None
        try:
            art = self.remote().art(seconds=ART_SECONDS)
            return self.current_id(art) if art.get_artmode() == "on" else None
        except Exception:
            return None
        finally:
            if art:
                try:
                    art.close()
                except Exception:
                    pass

    def status(self, *, timeout=None):
        """The TV's state. "answered": the TV replied on the network (REST). "known": its
        viewing state is certain. A TV can answer while its state stays unknown."""
        def state(awake=False, playing=False, *, known, answered=True):
            return {"awake": awake, "playing": playing, "paired": True, "known": known, "answered": answered}
        if not self.cfg.ip or not self.token.exists():
            return {"awake": False, "playing": False, "paired": False, "known": False, "answered": False}
        art = None
        answered = False
        try:
            device = self.remote(timeout=timeout) if timeout is not None else self.remote()
            info = device.rest_device_info().get("device", {})
            answered = True
            power = str(info.get("PowerState", "")).lower()
            if power in {"off", "standby"}:
                return state(known=True)
            art = device.art(seconds=ART_SECONDS)
            mode = art.get_artmode()
            if mode == "on":
                return state(awake=True, known=True)
            if mode == "off" and power == "on":
                return state(playing=True, known=True)
            return state(known=False)
        except Exception:
            return state(known=False, answered=answered)
        finally:
            if art:
                try:
                    art.close()
                except Exception:
                    pass

    # Art Mode display settings: read and change only while the TV confirms
    # it is on and in Art Mode. These never wake the TV, change mode or power.

    def _in_art_mode(self, action, *, remote=False, seconds=DISPLAY_SECONDS):
        """Run action(art) only after the TV confirms it is on and in Art Mode.

        With remote=True, the action also gets the remote-control connection: action(art, device).
        `seconds` is the total time limit of the art channel.
        """
        if not self.cfg.ip or not self.token.exists():
            raise DisplayError("unpaired")
        art = device = None
        try:
            try:
                device = self.remote(timeout=DISPLAY_TIMEOUT)
                info = device.rest_device_info().get("device", {})
                power = str(info.get("PowerState", "")).lower()
                if power in {"off", "standby"}:
                    raise DisplayError("not-art-mode")
                art = device.art(seconds=seconds)
                if art.get_artmode() != "on":
                    raise DisplayError("not-art-mode")
            except DisplayError:
                raise
            except Exception:
                raise DisplayError("not-art-mode") from None  # unknown state never permits a change
            return action(art, device) if remote else action(art)
        finally:
            for connection in (art, device if remote else None):
                if connection:
                    try:
                        connection.close()
                    except Exception:
                        pass

    @staticmethod
    def _read_settings(art):
        """A TV error reply means "not supported"; any other failure (connection, timeout) is raised."""
        try:
            settings = parse_display_settings(art.get_artmode_settings())
        except (ResponseError, AttributeError, TypeError, KeyError):
            # The library parses the payload itself; a malformed one also means "not reported".
            settings = parse_display_settings(None)
        if settings["brightness"] is None:
            try:
                settings["brightness"] = _number(art.get_brightness())
            except ResponseError:
                pass
        if settings["color_temperature"] is None:
            try:
                settings["color_temperature"] = _number(art.get_color_temperature())
            except ResponseError:
                pass
        return settings

    def display_settings(self):
        """Read-only: the TV's Art Mode settings, model and software versions."""
        def read(art, device):
            settings = self._read_settings(art)
            try:
                about = parse_about(device.rest_device_info())
            except Exception:
                about = parse_about(None)
            try:
                about["art_api"] = _text(art.get_api_version())
            except Exception:
                about["art_api"] = ""
            settings["about"] = about
            return settings
        return self._in_art_mode(read, remote=True)

    def _change(self, art, field, requested, send, current):
        # The settings read can take a while; confirm Art Mode again just before the change.
        if art.get_artmode() != "on":
            raise DisplayError("not-art-mode")
        acknowledged = True
        try:
            send()
        except ResponseError:
            raise DisplayError("rejected") from None
        except Exception:
            acknowledged = False  # a lost reply can still have changed the setting; read it back
        settings = None
        for attempt in range(READBACK_TRIES):
            if attempt:
                time.sleep(1)  # some TVs report the new value a moment later
            try:
                settings = self._read_settings(art)
            except Exception:
                break  # the TV stopped answering: do not wait for more timeouts
            if settings[field] == requested:
                break
        if settings is None:
            settings = {**current, field: None}  # nothing read back; the change is unconfirmed
        return Readback(requested, settings[field], current[field], acknowledged, settings)

    def set_brightness(self, value):
        """Set Art Mode brightness with the library's command, then read it back."""
        def run(art):
            current = self._read_settings(art)
            check_brightness(value, current)
            return self._change(art, "brightness", value, lambda: art.set_brightness(value), current)
        return self._in_art_mode(run)

    def set_light_sensor(self, on: bool):
        """Turn the TV's own brightness sensor on or off, then read it back."""
        requested = "on" if on is True else "off" if on is False else None
        if requested is None:
            raise DisplayError("invalid")

        def run(art):
            current = self._read_settings(art)
            return self._change(art, "sensor", requested, lambda: art.set_brightness_sensor_setting(requested), current)
        return self._in_art_mode(run)

    def set_option(self, field, value):
        """Change Sleep After, motion sensitivity or color temperature in Art Mode, then read it back.

        It runs only after the TV confirms it is on and in Art Mode, and never wakes it.
        """
        if field not in OPTION_SETTERS:
            raise DisplayError("invalid")

        def run(art):
            current = self._read_settings(art)
            checked = check_option(field, value, current)
            # The library documents string values for the motion settings; brightness-style numbers otherwise.
            sent = str(checked) if field == "motion_sensitivity" else checked
            return self._change(art, field, checked, lambda: getattr(art, OPTION_SETTERS[field])(sent), current)
        return self._in_art_mode(run)

    # Screen off and on: owner-run test controls. These change the TV's power.
    # A short KEY_POWER click on a Frame that is on switches Art Mode to TV mode,
    # so the short click is sent only after the TV confirms standby or off.
    # A network "hold" of KEY_POWER acts like a short click on a real Frame, so
    # the screen-off test sends KEY_POWEROFF once instead.

    def probe(self, *, with_art=True):
        """Read-only and fast: does the TV answer, and what are its power state and Art Mode?

        With with_art=False, it reads the power state only (one REST request) and opens no art channel: "art_mode"
        stays None. A presence wake needs only the power state before the key.
        """
        result = {"answers": False, "power": None, "art_mode": None}
        if not self.cfg.ip or not self.token.exists():
            raise DisplayError("unpaired")
        art = None
        try:
            device = self.remote(timeout=DISPLAY_TIMEOUT)
            info = device.rest_device_info().get("device", {})
            result["answers"] = True
            power = str(info.get("PowerState", "")).strip().lower()
            result["power"] = power if power in POWER_STATES else None
            if with_art:
                art = device.art(seconds=ART_SECONDS)
                result["art_mode"] = _on_off(art.get_artmode())
        except Exception:
            pass  # no answer, or no Art Mode reply; the fields stay as read so far
        finally:
            if art:
                try:
                    art.close()
                except Exception:
                    pass
        return result

    def sample(self, *, budget=SAMPLE_BUDGET):
        """Read-only reading for analytics, on one connection with DISPLAY_TIMEOUT per reply.

        In confirmed standby or off, the art channel stays closed. In Art Mode it also
        reads brightness, the light sensor, color temperature and the selected content ID.
        No new read starts once the budget (seconds) is spent; the fields read so far are kept.
        Never wakes the TV, changes its mode or sends a key.
        """
        result = {"answers": False, "power": None, "art_mode": None, "brightness": None,
                  "sensor": None, "color_temperature": None, "content_id": None}
        if not self.cfg.ip or not self.token.exists():
            return {**result, "paired": False}
        deadline = time.monotonic() + budget
        art = None
        try:
            device = self.remote(timeout=DISPLAY_TIMEOUT)
            info = device.rest_device_info().get("device", {})
            result["answers"] = True
            power = str(info.get("PowerState", "")).strip().lower()
            result["power"] = power if power in POWER_STATES else None
            if result["power"] in {"standby", "off"}:
                result["art_mode"] = "off"  # from REST alone; a Frame in standby can still report Art Mode
                return result
            if time.monotonic() >= deadline:
                return result
            art = device.art(seconds=ART_SECONDS)
            result["art_mode"] = _on_off(art.get_artmode())
            if result["art_mode"] != "on" or time.monotonic() >= deadline:
                return result
            settings = self._read_settings(art)
            result.update({key: settings[key] for key in ("brightness", "sensor", "color_temperature")})
            if time.monotonic() < deadline:
                result["content_id"] = self.current_id(art)
        except Exception:
            pass  # no answer, or a reply stopped; the fields stay as read so far
        finally:
            if art:
                try:
                    art.close()
                except Exception:
                    pass
        return result

    def screen_off(self):
        """Send KEY_POWEROFF once while the TV confirms Art Mode; then probe once."""
        def run(art, device):
            if art.get_artmode() != "on":  # confirm again just before the key
                raise DisplayError("not-art-mode")
            device.send_key("KEY_POWEROFF")
        self._in_art_mode(run, remote=True)
        return self.probe()

    def screen_on(self, *, signal=True, until=None, stop=None, state=None):
        """Turn the screen on from confirmed standby/off, or from no answer; never when it is on.

        With signal=False, a TV that does not answer gets no wake signal ("not-confirmed"). `until` is a
        time.monotonic() deadline for the wait after the key. When `stop()` is true just before the key, it
        sends nothing and raises DisplayError("stopped"). `state` is a probe() the caller just made, so this
        does not probe again; the power recheck on the key's connection always runs.
        Sets self.phases["key"] and self.phases["confirmed"] (time.monotonic()).
        """
        state = self.probe() if state is None else state
        start = time.monotonic()
        if not state["answers"] and not signal:
            return ScreenOn("power key", 0, "not-confirmed")
        if not state["answers"]:
            try:
                woke = self.wake()
            except WakeError as error:
                if str(error) in {"unpaired", "missing-mac", "network"}:
                    raise DisplayError(str(error)) from None
                woke = None
            seconds = round(time.monotonic() - start)
            if woke is False:
                return ScreenOn("wake signal", seconds, "watching")
            return ScreenOn("wake signal", seconds, "confirmed" if woke else "not-confirmed")
        if state["power"] not in {"standby", "off"}:
            raise DisplayError("screen-is-on")  # on, or a blank/unknown power state: never click
        device = self.remote(timeout=DISPLAY_TIMEOUT)
        try:
            # Read the power state again on this connection, just before the key: a short
            # click on a TV that came on in the meantime would switch Art Mode to TV mode.
            try:
                power = str(device.rest_device_info().get("device", {}).get("PowerState", "")).strip().lower()
            except Exception:
                return ScreenOn("power key", 0, "not-confirmed")  # it stopped answering: no key
            if power not in {"standby", "off"}:
                raise DisplayError("screen-is-on")
            if stop is not None and stop():
                raise DisplayError("stopped")
            device.send_key("KEY_POWER")
        finally:
            try:
                device.close()
            except Exception:
                pass
        start = self.phases["key"] = time.monotonic()
        end = start + SCREEN_ON_WAIT if until is None else min(start + SCREEN_ON_WAIT, until)
        while time.monotonic() < end:
            # Check often at first: delivery and the brightness step wait for the confirmed Art Mode. Read the
            # power state alone first; the art channel opens only once the TV answers and is no longer in standby.
            time.sleep(FAST_POLL if time.monotonic() - start < FAST_SECONDS else 1)
            state = self.probe(with_art=False)
            if not state["answers"] or state["power"] in {"standby", "off"}:
                continue
            state = self.probe()
            if state["power"] not in {"standby", "off"} and state["art_mode"] == "on":
                self.phases["confirmed"] = time.monotonic()
                return ScreenOn("power key", round(time.monotonic() - start), "confirmed")
            if state["power"] == "on" and state["art_mode"] == "off":
                return ScreenOn("power key", round(time.monotonic() - start), "tv-mode")
        return ScreenOn("power key", round(time.monotonic() - start), "not-confirmed")

    def presence_wake(self, *, tries=PRESENCE_WAKE_TRIES, wait=PRESENCE_WAKE_WAIT, stop=None):
        """Sensor control: turn the screen on for someone who came in. Never when the TV is on.

        A TV that answers in standby gets one short KEY_POWER through screen_on(), which reads the power
        state again on the key's connection. A TV that does not answer gets the wake signal: up to `tries`
        bursts, `wait` seconds apart. It stops as soon as the TV confirms Art Mode, and stops when the TV
        comes up in TV mode (someone watches: Art Frames leaves it). A TV that answers in standby after a
        wake signal, twice in a row, gets the one short KEY_POWER. It never falls back to wake(), and it
        ends within about PRESENCE_WAKE_LIMIT seconds, so it holds the display lock for a short time only.
        `stop()` is checked before each burst and just before the key: once it is true, the wake raises
        DisplayError("stopped") and sends nothing more.
        """
        start = time.monotonic()
        until = start + PRESENCE_WAKE_LIMIT
        self.phases.pop("key", None)
        self.phases.pop("confirmed", None)
        state = self.probe(with_art=False)  # the power state is all the key needs: no art channel, no wait
        if state["answers"]:
            if state["power"] not in {"standby", "off"}:
                raise DisplayError("screen-is-on")  # on, or a blank/unknown power state: never click
            return self._timed(self.screen_on(signal=False, until=until, stop=stop, state=state))
        if not self.cfg.mac:
            raise DisplayError("missing-mac")
        standby, standby_since = 0, None
        for _ in range(tries):
            if stop is not None and stop():
                raise DisplayError("stopped")
            if not self._send_magic():
                raise DisplayError("network")
            deadline = min(time.monotonic() + wait, until)
            while time.monotonic() < deadline:
                time.sleep(1)
                status = self.status(timeout=2)
                seconds = round(time.monotonic() - start)
                if status["playing"]:
                    return ScreenOn("wake signal", seconds, "tv-mode")
                if status["known"] and status["awake"]:
                    self.phases["confirmed"] = time.monotonic()
                    return self._timed(ScreenOn("wake signal", seconds, "confirmed"))
                if status["known"]:
                    standby += 1
                    standby_since = time.monotonic() if standby_since is None else standby_since
                else:
                    standby, standby_since = 0, None
                if standby >= 2 and time.monotonic() - standby_since >= STANDBY_DWELL:
                    # It has answered in standby for STANDBY_DWELL seconds: one short key, after a recheck.
                    result = self.screen_on(signal=False, until=until, stop=stop)
                    return self._timed(result._replace(seconds=round(time.monotonic() - start)))
        return ScreenOn("wake signal", round(time.monotonic() - start), "not-confirmed")

    def _timed(self, result):
        """The result with the times of the key and of the confirmed Art Mode, for the wake's log line."""
        return result._replace(key_at=self.phases.get("key"), confirmed_at=self.phases.get("confirmed"))

    # Soft blank: an owner-run test that keeps the TV in Art Mode but shows a black
    # image at the lowest brightness. It never changes power or mode, and never wakes the TV.

    @staticmethod
    def _still_art_mode(art, device):
        """Read power and Art Mode again just before a change. A Frame in standby can still report Art Mode."""
        try:
            power = str(device.rest_device_info().get("device", {}).get("PowerState", "")).strip().lower()
            mode = art.get_artmode()
        except Exception:
            raise DisplayError("not-art-mode") from None  # unknown state never permits a change
        if power in {"off", "standby"} or mode != "on":
            raise DisplayError("not-art-mode")

    def _show_and_confirm(self, art, device, content_id):
        """Select an image with show=True after an Art Mode recheck. True when the TV reports it."""
        self._still_art_mode(art, device)
        try:
            # On and in Art Mode, show=True only redraws the artwork (see publish).
            art.select_image(content_id, show=True)
        except Exception:
            pass  # a lost acknowledgement can still have selected the image; an unknown ID is not confirmed
        for attempt in range(3):
            try:
                if art.get_artmode() != "on":
                    raise DisplayError("not-art-mode")
                if self.current_id(art) == content_id:
                    return True
            except DisplayError:
                raise
            except Exception:
                return False
            if attempt < 2:
                time.sleep(1)
        return False

    def soft_blank(self, image, black_id, remember, record_upload):
        """Show a black image, then set the lowest brightness. Only in confirmed Art Mode.

        `black_id` is an earlier black upload to select again; when the TV does not show it, this
        uploads `image`. remember(content_id, brightness) runs once, just before the first selection,
        with the image and brightness the TV shows now. Returns {"black_id", "shown", "readback"}.
        The brightness changes only when the TV confirms the black image ("readback" is None otherwise).
        """
        def run(art, device):
            settings = self._read_settings(art)
            if settings["brightness"] is None:
                raise DisplayError("no-brightness")  # nothing to restore later: change nothing
            low = brightness_range(settings)[0]
            try:
                current = self.current_id(art)
            except Exception:
                current = None
            remembered = False

            def show(content_id):
                nonlocal remembered
                if not remembered:
                    self._still_art_mode(art, device)  # before saving, so a refusal leaves no blank record
                    remember(current, settings["brightness"])
                    remembered = True
                return self._show_and_confirm(art, device, content_id)
            content_id, shown = black_id, False
            if black_id:
                shown = show(black_id)
            if not shown:
                self._still_art_mode(art, device)
                content_id = art.upload(image, file_type="JPEG", matte="none", portrait_matte="none")
                record_upload(content_id)
                shown = show(content_id)
            if not shown:
                return {"black_id": content_id, "shown": False, "readback": None}
            self._still_art_mode(art, device)
            readback = self._change(art, "brightness", low, lambda: art.set_brightness(low), settings)
            return {"black_id": content_id, "shown": True, "readback": readback}
        return self._in_art_mode(run, remote=True, seconds=UPLOAD_SECONDS)

    def undo_blank(self, content_id, brightness):
        """Select the saved image again, then restore the saved brightness. Only in confirmed Art Mode.

        Returns {"restored", "readback"}: "restored" is None without a saved image, and False
        when the TV does not show it (it may have been deleted).
        """
        def run(art, device):
            settings = self._read_settings(art)
            check_brightness(brightness, settings)
            restored = self._show_and_confirm(art, device, content_id) if content_id else None
            self._still_art_mode(art, device)
            readback = self._change(art, "brightness", brightness, lambda: art.set_brightness(brightness), settings)
            return {"restored": restored, "readback": readback}
        # The same limit as soft_blank: the brightness must come back even on a slow TV.
        return self._in_art_mode(run, remote=True, seconds=UPLOAD_SECONDS)

    def _standby_upload(self, image, record_upload, *, timeout=None, starting=None, after=None):
        """The shared safety steps of the two paths that put artwork on a TV in standby (standby_load and
        prewake_load). Nothing else may change the artwork of a TV that is not on and in Art Mode.

        It sends nothing unless the REST power state reads "standby" (Art Mode may be on or off). It checks
        that the art channel answers, calls starting() (if given), uploads and calls record_upload(content_id).
        It reads the power state again and selects the image with show=False only while the TV still reports
        standby: a TV that someone turned on in the meantime gets no selection. Then after(art, steps) runs
        (if given) on the open connection, and the power state is read a last time. It never sends a key,
        never calls set_artmode or a brightness change and never sends a wake packet.

        Returns the steps: {"power", "start", "answered", "uploaded", "content_id", "selected", "power_after"}.
        "start" is time.monotonic() after the first power read, or None when the TV is not in standby.
        "selected" is None (not tried), "skipped" (the TV left standby), "sent" or "no-reply".
        """
        if not self.cfg.ip or not self.token.exists():
            raise DisplayError("unpaired")
        steps = {"power": None, "start": None, "answered": False, "uploaded": None, "content_id": None,
                 "selected": None, "power_after": None}
        device = self.remote(timeout=timeout) if timeout is not None else self.remote()

        def power():
            try:
                value = str(device.rest_device_info().get("device", {}).get("PowerState", "")).strip().lower()
            except Exception:
                return None
            return value if value in POWER_STATES else None

        art = None
        try:
            steps["power"] = power()
            if steps["power"] != "standby":
                return steps  # on, off or unknown: send nothing
            steps["start"] = time.monotonic()
            art = device.art(seconds=UPLOAD_SECONDS)
            try:
                art.get_artmode()  # does the art channel answer in standby? Its value does not matter here.
                steps["answered"] = True
            except ResponseError:
                steps["answered"] = True  # an error reply is still a reply
            except Exception:
                pass
            if steps["answered"]:
                if starting is not None:
                    starting()
                try:
                    steps["content_id"] = art.upload(image, file_type="JPEG", matte="none", portrait_matte="none")
                    record_upload(steps["content_id"])
                    steps["uploaded"] = True
                except Exception:
                    steps["uploaded"] = False
            if steps["uploaded"]:
                # The upload can take a while. Select only while the TV still reports standby: someone may
                # have turned it on with the remote, and that screen must not change.
                if power() != "standby":
                    steps["selected"] = "skipped"
                else:
                    try:
                        art.select_image(steps["content_id"], show=False)
                        steps["selected"] = "sent"
                    except Exception:
                        steps["selected"] = "no-reply"
            if after is not None:
                after(art, steps)
            steps["power_after"] = power()
            return steps
        finally:
            for connection in (art, device):
                if connection:
                    try:
                        connection.close()
                    except Exception:
                        pass

    def standby_load(self, image, record_upload):
        """Owner-run test: in confirmed standby, upload an image and select it with show=False.

        It finds out whether the art channel answers in standby, and whether using it
        turns the screen on. It never sends a key, never calls set_artmode and never
        sends a wake packet (see _standby_upload). Returns what happened and how long it took.
        """
        def settle(art, steps):
            time.sleep(STANDBY_SETTLE)  # give a TV that the upload woke time to report it

        steps = self._standby_upload(image, record_upload, after=settle)
        if steps["start"] is None:
            raise DisplayError("not-standby")  # on, off or unknown: nothing was sent
        power_after = steps["power_after"]
        return {"answered": steps["answered"], "uploaded": steps["uploaded"], "selected": steps["selected"],
                "power_after": power_after, "stayed": None if power_after is None else power_after == "standby",
                "seconds": round(time.monotonic() - steps["start"], 1)}

    def prewake_load(self, image, starting, record_upload):
        """Just before a scheduled wake: in confirmed standby, upload the wake-time image and make it the
        TV's current artwork with show=False, so the screen stays off.

        Besides the owner's standby test, this is the only path that changes the artwork of a TV in standby
        (see _standby_upload). It never sends a key, never calls set_artmode or a brightness change, and
        never sends a wake packet. starting() runs just before the upload; record_upload(content_id) runs as
        soon as the TV reports the new ID.

        Returns {"outcome", "power", "power_after", "current", "seconds"}. "outcome" is "not-standby" or
        "no-art" (nothing sent), "upload-failed", "turned-on" (uploaded, not selected), "no-reply" (selection
        sent, not acknowledged) or "selected". "current" is True when the TV reports the new image as its
        current artwork afterwards, False when it reports another one, None when it does not say.
        "seconds" is how long the upload and the selection took.
        """
        result = {"current": None, "seconds": None}

        def check(art, steps):
            if steps["selected"] in {"sent", "no-reply"}:
                try:
                    result["current"] = self.current_id(art) == steps["content_id"]
                except Exception:
                    pass
            result["seconds"] = round(time.monotonic() - steps["start"], 1)

        steps = self._standby_upload(image, record_upload, timeout=DISPLAY_TIMEOUT, starting=starting, after=check)
        if steps["start"] is None:
            outcome = "not-standby"
        elif not steps["answered"]:
            outcome = "no-art"
        elif not steps["uploaded"]:
            outcome = "upload-failed"
        else:
            outcome = {"skipped": "turned-on", "sent": "selected"}.get(steps["selected"], "no-reply")
        return {**result, "outcome": outcome, "power": steps["power"], "power_after": steps["power_after"]}

    def wake(self, *, checks=6):
        """Send Wake-on-LAN and confirm Art Mode; a retry passes fewer checks to bound its time."""
        status = self.status(timeout=2)
        if not status["paired"]:
            raise WakeError("unpaired")
        if status["playing"]:
            return False  # preserve an active TV programme even at the scheduled wake time
        if not self.cfg.mac:
            raise WakeError("missing-mac")
        if not self._send_magic():
            raise WakeError("network")
        self.phases["packet"] = time.monotonic()
        # Bounded startup retries belong to this one wake event, never to the
        # ordinary image-update loop. Later inactivity sleep is respected.
        for _ in range(checks):
            art = None
            try:
                status = self.status(timeout=2)
                self._mark_wake(status)
                if status["playing"]:
                    return False
                if status["known"] and status["awake"]:
                    return True
                if not status["known"]:
                    # A TV in deep standby is off the network; a single burst can be lost.
                    self._send_magic()
                    time.sleep(2)
                    continue
                # A confirmed standby state may be explicitly moved into Art Mode.
                # Ambiguous replies never authorize a mode change.
                art = self.remote(timeout=2).art(seconds=ART_SECONDS)
                art.set_artmode(True)
            except Exception:
                pass
            finally:
                if art:
                    try:
                        art.close()
                    except Exception:
                        pass
            time.sleep(2)
        status = self.status(timeout=2)
        self._mark_wake(status)
        if status["playing"]:
            return False
        if status["known"] and status["awake"]:
            return True
        raise WakeError("state-unknown" if not status["known"] else "not-confirmed")

    def _mark_wake(self, status):
        """Record when the TV first answered after the wake packet, and when it confirmed Art Mode."""
        if status["known"] or status.get("answered"):
            self.phases.setdefault("answered", time.monotonic())
        if status["known"] and status["awake"]:
            self.phases.setdefault("art_mode", time.monotonic())

    def _send_magic(self):
        """Send a magic-packet burst to the configured and limited broadcast; return the targets reached."""
        packet = b"\xff" * 6 + bytes.fromhex(self.cfg.mac.replace(":", "")) * 16
        sent = 0
        for target in dict.fromkeys((self.cfg.broadcast, "255.255.255.255")):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    for _ in range(3):
                        sock.sendto(packet, (target, 9))
                sent += 1
            except OSError:
                continue  # the other target can still reach the TV
        return sent

    @staticmethod
    def _delete_old(art, owned_ids, selected, keep=()):
        """Delete Art Frames' other uploads, except `keep` (the other layouts of a rotation).

        Keep them all unless the TV confirms the delete.
        """
        old = [item for item in owned_ids if item != selected and item not in keep]
        try:
            if not old or art.delete_list(old):
                return [item for item in owned_ids if item == selected or item in keep]
        except Exception:
            pass  # retain ownership so cleanup can be retried later
        return owned_ids

    def remove_old(self, owned_ids, selected, keep=()):
        """Clean up after a later confirmation; never wakes the TV or changes Art Mode."""
        if all(item == selected or item in keep for item in owned_ids):
            return owned_ids
        art = None
        try:
            art = self.remote().art(seconds=ART_SECONDS)
            return self._delete_old(art, owned_ids, selected, keep)
        except Exception:
            return owned_ids
        finally:
            if art:
                try:
                    art.close()
                except Exception:
                    pass

    def select(self, content_id):
        """Show an image Art Frames uploaded earlier (a rotation swap, or the image uploaded before a
        wake). No upload, no render.

        It runs only when the TV confirms power on and Art Mode (see _in_art_mode), so it
        never wakes the TV or interrupts a programme. True when the TV reports the image.
        """
        def run(art):
            try:
                # On and in Art Mode, show=True only redraws the artwork (see publish).
                art.select_image(content_id, show=True)
            except Exception:
                pass  # a lost acknowledgement can still have selected the image
            self.phases["selected"] = time.monotonic()
            for attempt in range(3):
                try:
                    if art.get_artmode() != "on":
                        return False
                    if self.current_id(art) == content_id:
                        self.phases["confirmed"] = time.monotonic()
                        return True
                except Exception:
                    return False
                if attempt < 2:
                    time.sleep(1)
            return False
        return self._in_art_mode(run)

    def publish(self, image: bytes, owned_ids: list[str], record_upload, keep=()):
        """Record per-device ownership before selection; retain IDs until cleanup confirms.

        `keep` lists other uploads to leave on the TV: the stored images of a rotation.
        """
        device = self.remote()
        art = device.art(seconds=UPLOAD_SECONDS)
        try:
            if art.get_artmode() != "on":
                return None
            self.phases["upload_start"] = time.monotonic()
            content_id = art.upload(image, file_type="JPEG", matte="none", portrait_matte="none")
            self.phases["upload"] = time.monotonic()
            record_upload(content_id)
            # Recheck after upload so a programme or standby in the meantime wins.
            # A Frame in standby can still report Art Mode, so check power too.
            try:
                power = str(device.rest_device_info().get("device", {}).get("PowerState", "")).lower()
            except Exception:
                return None  # unknown state never shows artwork
            if power in {"off", "standby"} or art.get_artmode() != "on":
                return None
            try:
                # The TV is on and in Art Mode, so show=True only redraws the artwork.
                # With show=False the TV reports the new image as current but can
                # keep the previous one on screen.
                art.select_image(content_id, show=True)
            except Exception:
                pass  # a lost acknowledgement can still have selected the image
            self.phases["selected"] = time.monotonic()
            confirmed = False
            try:
                for attempt in range(5):  # a slow TV can take a few seconds to switch
                    if art.get_artmode() != "on":
                        break
                    if self.current_id(art) == content_id:
                        confirmed = True
                        self.phases["confirmed"] = time.monotonic()
                        break
                    if attempt < 4:
                        time.sleep(1)
            except Exception:
                pass  # upload/selection is not proof that the TV displays this artwork
            owned = list(dict.fromkeys([*owned_ids, content_id]))
            if not confirmed:
                return content_id, owned, False
            return content_id, self._delete_old(art, owned, content_id, keep), True
        finally:
            try:
                art.close()
            except Exception:
                pass


class DemoTelevision:
    """No network access, pairing, or real wake packets in demo mode."""

    def __init__(self, display=None):
        # The controller passes one dict per TV so demo settings last between requests.
        self.display = display if display is not None else {}
        self.display.setdefault("brightness", 5)
        self.display.setdefault("sensor", "off")
        self.display.setdefault("power", "on")

    def _require_art_mode(self):
        if self.display["power"] != "on":
            raise DisplayError("not-art-mode")

    def probe(self):
        on = self.display["power"] == "on"
        return {"answers": True, "power": self.display["power"], "art_mode": "on" if on else "off"}

    def sample(self, *, budget=10):
        result = {**self.probe(), "brightness": None, "sensor": None, "color_temperature": None, "content_id": None}
        if result["art_mode"] == "on":
            settings = self._settings()
            result.update({key: settings[key] for key in ("brightness", "sensor", "color_temperature")})
        return result

    def screen_off(self):
        self._require_art_mode()
        self.display["power"] = "standby"
        return self.probe()

    def screen_on(self):
        if self.display["power"] == "on":
            raise DisplayError("screen-is-on")
        self.display["power"] = "on"
        return ScreenOn("power key", 0, "confirmed")

    def presence_wake(self, **kwargs):
        result = self.screen_on()
        now = time.monotonic()
        return result._replace(key_at=now, confirmed_at=now)

    def _settings(self):
        return parse_display_settings({"data": json.dumps([
            {"item": "brightness", "value": str(self.display["brightness"]), "min": "0", "max": "10"},
            {"item": "brightness_sensor_setting", "value": self.display["sensor"]},
            {"item": "color_temperature", "value": str(self.display.get("color_temperature", 0)), "min": "-5", "max": "5"},
            {"item": "motion_timer", "value": self.display.get("motion_timer", "off"),
             "valid_values": list(MOTION_TIMER_CHOICES)},
            {"item": "motion_sensitivity", "value": str(self.display.get("motion_sensitivity", 2)), "min": "1", "max": "3"},
        ])})

    def set_option(self, field, value):
        if field not in OPTION_SETTERS:
            raise DisplayError("invalid")
        self._require_art_mode()
        before = self._settings()
        self.display[field] = check_option(field, value, before)
        after = self._settings()
        return Readback(self.display[field], after[field], before[field], True, after)

    def display_settings(self):
        self._require_art_mode()
        about = {"model_name": "Demo Frame", "model_code": "", "firmware": "", "os": "", "art_api": ""}
        return {**self._settings(), "about": about}

    def set_brightness(self, value):
        self._require_art_mode()
        before = self._settings()
        check_brightness(value, before)
        self.display["brightness"] = value
        return Readback(value, value, before["brightness"], True, self._settings())

    def set_light_sensor(self, on: bool):
        if not isinstance(on, bool):
            raise DisplayError("invalid")
        self._require_art_mode()
        before = self.display["sensor"]
        self.display["sensor"] = "on" if on else "off"
        return Readback(self.display["sensor"], self.display["sensor"], before, True, self._settings())

    def soft_blank(self, image, black_id, remember, record_upload):
        self._require_art_mode()
        before = self._settings()
        remember(self.display.get("content_id"), before["brightness"])
        if not black_id:
            black_id = "demo-" + str(uuid.uuid4())
            record_upload(black_id)
        self.display.update(content_id=black_id, brightness=before["min"])
        return {"black_id": black_id, "shown": True,
                "readback": Readback(before["min"], before["min"], before["brightness"], True, self._settings())}

    def undo_blank(self, content_id, brightness):
        self._require_art_mode()
        before = self._settings()
        check_brightness(brightness, before)
        self.display.update(content_id=content_id, brightness=brightness)
        return {"restored": True if content_id else None,
                "readback": Readback(brightness, brightness, before["brightness"], True, self._settings())}

    def standby_load(self, image, record_upload):
        if self.display["power"] != "standby":
            raise DisplayError("not-standby")
        record_upload("demo-" + str(uuid.uuid4()))
        return {"answered": True, "uploaded": True, "selected": "sent", "power_after": "standby", "stayed": True,
                "seconds": 0.0}

    def prewake_load(self, image, starting, record_upload):
        power = self.display["power"]
        if power != "standby":
            return {"outcome": "not-standby", "power": power, "power_after": None, "current": None, "seconds": None}
        starting()
        content_id = "demo-" + str(uuid.uuid4())
        record_upload(content_id)
        self.display["content_id"] = content_id
        return {"outcome": "selected", "power": power, "power_after": power, "current": True, "seconds": 0.0}

    def status(self, **kwargs):
        # The demo screen's power: Screen off and the night's Sleep After put it in standby.
        on = self.display["power"] == "on"
        return {"awake": on, "playing": False, "paired": True, "known": True, "answered": True, "demo": True}

    def pair(self):
        return None

    def wake(self, *, checks=6):
        return True

    def publish(self, image, owned_ids, record_upload, keep=()):
        content_id = "demo-" + str(uuid.uuid4())
        record_upload(content_id)
        return content_id, [*[item for item in owned_ids if item in keep], content_id], True

    def select(self, content_id):
        self._require_art_mode()
        return True

    def current_artwork(self):
        return None  # each demo send uploads, so there is nothing to reuse

    def read_artwork(self, known_id=None, known_image=None):
        if known_id is None or known_image is None:
            raise ValueError("No demo artwork has been sent")
        return known_id, known_image, "upload"
