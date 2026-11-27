"""ESPHome presence and light sensors: the client, the readings it keeps, and how Art Frames picks them.

Each TV can have one sensor (config.Sensor). A Link keeps one connection to it on its own thread:
1. It tries the native API (aioesphomeapi), with the encryption key when the TV's settings have one.
2. When the API does not answer, it reads the sensor's web server instead (/events, server-sent events).
3. When both fail, it waits and tries again, longer each time (BACKOFF).

The Link keeps the last value of each binary_sensor and sensor, and when it arrived. A reading counts
only while the link is live (see Readings.fresh). Missing or stale readings never change the TV:
sensor_control.py checks this before every action.

Privacy: Art Frames never stores, logs or shows a MAC address, the encryption key or text sensors.
The device info keeps only DEVICE_FIELDS.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import math
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .config import MAC_SUFFIX, without_mac  # noqa: F401  (pages and discovery use them from here)

log = logging.getLogger(__name__)
# The ESPHome and mDNS libraries can log device names, addresses and MAC addresses. Keep them silent.
for _name in ("aioesphomeapi", "zeroconf"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

# A reading is stale when the link has not heard from the sensor for this long. Then Art Frames holds the
# TV's brightness and does nothing.
STALE_SECONDS = 120
# The native API link asks for the device info this often, so a quiet sensor still counts as live.
HEARTBEAT_SECONDS = 30
CONNECT_TIMEOUT = 10
# The web server sends a ping event every few seconds; no line for this long means the link is lost.
WEB_TIMEOUT = 45
MAX_LINE = 4096
# Limits for one sensor, so a broken or hostile sensor cannot fill the memory.
MAX_EVENT = 16 * 1024  # bytes of data in one web server event
MAX_ENTITIES = 256  # readings kept; a new id after this is ignored
MAX_SAMPLES = 1000  # light samples kept for each reading
# Seconds between tries after a failure: 5, 10, 20 ... up to 5 minutes, with some jitter.
BACKOFF = (5, 300)
# The log says when the link drops and when it comes back, at most one drop line in this many seconds. Drops
# in between are counted, and the next drop line gives their number.
LINK_LOG_GAP = 10 * 60
# The light samples kept, in seconds. The median uses the last 2 minutes (sensor_curve.WINDOW).
HISTORY_SECONDS = 900
DOMAINS = ("binary_sensor", "sensor")
DEVICE_FIELDS = ("name", "friendly_name", "manufacturer", "model", "project_name", "project_version")
PRESENCE_CLASSES = {"occupancy": 4, "presence": 3, "motion": 1}
LIGHT_UNITS = {"lx", "lux"}
NO_ANSWER = ("The sensor does not answer. Check that it has power and Wi-Fi. "
             "If its address changed, use Find sensors.")
# Why a link is not live, in plain words for pages. A code goes to the log; a library message never does.
LINK_ERRORS = {
    "": "Not connected yet.",
    "no-answer": NO_ANSWER,
    "key-needed": "The sensor needs its encryption key.",
    "key-wrong": "The encryption key does not match the sensor.",
    "key-not-used": "The sensor does not use an encryption key. Remove the key.",
    "web-failed": "The sensor's web server does not answer.",
    "lost": NO_ANSWER,
}


@dataclass(frozen=True)
class Entity:
    """A binary_sensor or sensor of the device. object_id is ESPHome's id, such as "ambient_light"."""

    domain: str
    object_id: str
    name: str = ""
    device_class: str = ""
    unit: str = ""

    @property
    def label(self):
        return self.name or self.object_id.replace("_", " ").capitalize()


# Vendor profiles, keyed by the start of the device's project_name (mDNS TXT "project_name", ESPHome's
# esphome: project: name:). They give a display name and suggested readings. A device without a profile
# still works: pick_presence() and pick_light() fall back to device classes and units.
# Each entry cites the vendor's own ESPHome YAML, where the project name is set. The entity ids are the
# entity names in snake case, as ESPHome makes them.


@dataclass(frozen=True)
class Profile:
    prefix: str
    name: str
    presence: tuple[str, ...] = ()
    light: tuple[str, ...] = ()
    notes: str = ""


PROFILES = (
    # https://raw.githubusercontent.com/ApolloAutomation/MSR-1/main/Integrations/ESPHome/MSR-1.yaml
    # Also "ApolloAutomation.MSR-1_BLE". Readings from Core.yaml in the same folder.
    Profile("ApolloAutomation.MSR-1", "Apollo Automation MSR-1", ("radar_target", "radar_zone_occupancy"),
            ("ltr390_light",)),
    # https://raw.githubusercontent.com/ApolloAutomation/MSR-2/main/Integrations/ESPHome/MSR-2.yaml
    # Also "ApolloAutomation.MSR-2_BLE" and "ApolloAutomation.MSR-2_Factory".
    Profile("ApolloAutomation.MSR-2", "Apollo Automation MSR-2", ("radar_target", "radar_zone_occupancy"),
            ("ltr390_light",)),
    # https://raw.githubusercontent.com/ApolloAutomation/MTR-1/main/Integrations/ESPHome/MTR-1.yaml
    Profile("ApolloAutomation.MTR-1", "Apollo Automation MTR-1", ("ld2450_presence",), ("ltr390_light",)),
    # https://raw.githubusercontent.com/EverythingSmartHome/everything-presence-one/main/common/everything-presence-one-base.yaml
    Profile("Everything Smart Technology.Everything Presence One", "Everything Presence One", ("occupancy",),
            ("illuminance",), "Occupancy combines the mmWave and PIR sensors."),
    # https://raw.githubusercontent.com/EverythingSmartHome/everything-presence-lite/main/common/everything-presence-lite-base.yaml
    # The vendor part has no spaces here, unlike the Presence One.
    Profile("EverythingSmartTechnology.Everything Presence Lite", "Everything Presence Lite", ("occupancy",),
            ("illuminance",)),
    # https://raw.githubusercontent.com/screekworkshop/screek-human-sensor/main/1u/yaml/human-sensor-1u-github.yaml
    # https://raw.githubusercontent.com/screekworkshop/screek-human-sensor/main/2a/yaml/human-sensor-2a-stable-github.yaml
    Profile("Screek.Human_Presence_Sensor", "SCREEK Human Presence Sensor", ("any_presence", "presence"),
            ("illuminance",), "The 1U has no light sensor; the 2A has one."),
    # https://raw.githubusercontent.com/screekworkshop/screek-human-sensor/main/1w/yaml/human-sensor-1w-github.yaml
    Profile("Screek.Human_Sensor", "SCREEK Human Sensor", ("presence",), (),
            "The 1W and 1M have no light sensor, so only presence works."),
    # https://raw.githubusercontent.com/Seeed-Studio/xiao-esphome-projects/main/projects/seeedstudio-mr60bha2-kit/seeedstudio-mr60bha2-kit.yaml
    Profile("seeedstudio.mr60bha2_kit", "Seeed Studio MR60BHA2 kit", ("person_information",),
            ("seeed_mr60bha2_illuminance",)),
    # https://raw.githubusercontent.com/Seeed-Studio/xiao-esphome-projects/main/projects/seeedstudio-mr60fda2-kit/seeedstudio-mr60fda2-kit.yaml
    Profile("seeedstudio.mr60fda2_kit", "Seeed Studio MR60FDA2 kit", (), ("seeed_mr60fda2_illuminance",),
            "Its presence reading is text, so Art Frames uses only its light reading."),
    # https://raw.githubusercontent.com/akamatis-smart-devices/esphome-firmware/main/c3-wroom-02-ld2410-aht20-ltr329/src/firmware.yaml
    # Firmware 1.3.0 on a real device names its readings "presence" and "ambient_light".
    Profile("Akamatis.SmartHomeDevice", "Akamatis Smart Home Device", ("presence", "any_presence"),
            ("ambient_light", "01_ambient_light", "illuminance")),
    # https://raw.githubusercontent.com/akamatis-smart-devices/esphome-config/main/skopos/skopos.yaml
    Profile("Akamatis.HumanPresenceSensor", "Akamatis Presence Sensor"),
)


def split_words(text):
    """ "SmartHomeDevice" -> "Smart Home Device", "Human_Presence_Sensor" -> "Human Presence Sensor".

    A run of capitals or a model code stays whole: "MSR-2", "LD2410".
    """
    text = re.sub(r"[_]+", " ", text or "")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z0-9])(?=[A-Z][a-z])", " ", text)
    return " ".join(text.split())


def parse_project(project_name):
    """ESPHome's "Vendor.Product" project name as (vendor, product) in plain words. Missing parts are ""."""
    vendor, _, product = (project_name or "").strip().partition(".")
    if not product:
        return "", split_words(vendor)
    return split_words(vendor), split_words(product)


def profile_for(project_name):
    """The vendor profile whose prefix starts project_name (the longest one), or None."""
    name = (project_name or "").casefold()
    matches = [item for item in PROFILES if name.startswith(item.prefix.casefold())]
    return max(matches, key=lambda item: len(item.prefix), default=None)


def product_name(project_name, friendly_name=""):
    """What pages call the device: "Akamatis Smart Home Device". Without a project name, its friendly name."""
    profile = profile_for(project_name)
    if profile:
        return profile.name
    vendor, product = parse_project(project_name)
    words = " ".join(part for part in (vendor, product) if part)
    return words or _text(friendly_name) or "ESPHome sensor"


def _words(entity):
    return f"{entity.object_id} {entity.name}".lower().replace("_", " ")


def _presence_score(entity):
    words = _words(entity)
    keyword = "presence" in words or "occupancy" in words or "occupied" in words
    if entity.device_class not in PRESENCE_CLASSES and not keyword:
        return 0
    score = PRESENCE_CLASSES.get(entity.device_class, 0) + (3 if keyword else 0)
    score += 1 if "any" in words.split() else 0
    # A moving or still target, a zone or a PIR sensor is part of the picture, not the whole room.
    score -= 4 if re.search(r"\b(moving|still|move)\b", words) else 0
    score -= 4 if re.search(r"\b(zone|zout|gate)\d*\b", words) else 0
    score -= 1 if "pir" in words.split() else 0
    return max(score, 1)


def _light_score(entity):
    if entity.device_class != "illuminance" and entity.unit.lower() not in LIGHT_UNITS:
        return 0
    words = _words(entity)
    return 2 + (1 if re.search(r"illuminance|ambient|light|lux", words) else 0)


def _pick(entities, domain, override, suggested, score):
    own = {item.object_id: item for item in entities if item.domain == domain}
    if override:
        return override  # the person's choice, even before the sensor lists it
    for object_id in suggested:
        if object_id in own:
            return object_id
    ranked = sorted((item for item in own.values() if score(item) > 0), key=lambda item: (-score(item), item.object_id))
    return ranked[0].object_id if ranked else ""


def pick_presence(entities, project_name="", override=""):
    """The binary_sensor that says someone is in the room, as an object_id, or ""."""
    profile = profile_for(project_name)
    return _pick(entities, "binary_sensor", override, profile.presence if profile else (), _presence_score)


def pick_light(entities, project_name="", override=""):
    """The sensor that reports room light in lux, as an object_id, or ""."""
    profile = profile_for(project_name)
    return _pick(entities, "sensor", override, profile.light if profile else (), _light_score)


def presence_choices(entities):
    """Binary sensors a person can pick for presence: the likely ones first."""
    items = [item for item in entities if item.domain == "binary_sensor"]
    return sorted(items, key=lambda item: (-_presence_score(item), item.label))


def light_choices(entities):
    items = [item for item in entities if item.domain == "sensor"]
    return sorted(items, key=lambda item: (-_light_score(item), item.label))


def _text(value, limit=80):
    if value is None or isinstance(value, (dict, list)):
        return ""
    return "".join(c for c in str(value) if c.isprintable())[:limit]




def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class Readings:
    """What a Link knows about its sensor. Thread-safe: the Link writes, pages and the control loop read."""

    clock: object = time.time
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    transport: str = ""  # "api" or "web" while connected, else ""
    connected: bool = False
    since: float | None = None  # when the current connection started
    last_seen: float | None = None  # the last message of any kind
    error: str = ""  # a LINK_ERRORS code
    device: dict = field(default_factory=dict)
    entities: dict = field(default_factory=dict)  # (domain, object_id) -> Entity
    values: dict = field(default_factory=dict)  # (domain, object_id) -> (value, at)
    history: dict = field(default_factory=dict)  # (domain, object_id) -> deque of (at, value), sensors only
    # (domain, object_id) -> when that binary sensor last turned on: someone came in.
    arrivals: dict = field(default_factory=dict)
    # Called with the object_id, on the sensor client's thread, when a binary sensor turns on. The hub sets it
    # to wake its loop at once. It must be quick and never contact a TV.
    on_arrival: object = field(default=None, repr=False)
    # Called with the transport after each connection starts. The Link sets it to log a reconnect.
    on_start: object = field(default=None, repr=False)

    def start(self, transport, device=None, entities=()):
        with self.lock:
            now = self.clock()
            self.transport, self.connected, self.since, self.last_seen, self.error = transport, True, now, now, ""
            if device:
                self.device.update({key: _device_text(key, device.get(key)) for key in DEVICE_FIELDS
                                    if device.get(key)})
            for entity in entities:
                if len(self.entities) < MAX_ENTITIES or (entity.domain, entity.object_id) in self.entities:
                    self.entities[(entity.domain, entity.object_id)] = entity
        if callable(self.on_start):
            self.on_start(transport)

    def add_entity(self, entity):
        with self.lock:
            known = self.entities.get((entity.domain, entity.object_id))
            if known is None and len(self.entities) >= MAX_ENTITIES:
                return
            if known is None or (not known.name and entity.name) or (not known.unit and entity.unit):
                self.entities[(entity.domain, entity.object_id)] = Entity(
                    entity.domain, entity.object_id, entity.name or (known.name if known else ""),
                    entity.device_class or (known.device_class if known else ""),
                    entity.unit or (known.unit if known else ""))

    def set_device(self, **fields):
        with self.lock:
            self.device.update({key: _device_text(key, value) for key, value in fields.items()
                                if key in DEVICE_FIELDS and value})

    def update(self, domain, object_id, value):
        """A new state. None means the sensor reports no value (ESPHome's "NA")."""
        if domain not in DOMAINS:
            return
        arrived = False
        with self.lock:
            now = self.clock()
            self.last_seen = now
            key = (domain, object_id)
            if key not in self.values and len(self.values) >= MAX_ENTITIES:
                return  # a new id after the limit is ignored
            if domain == "binary_sensor" and value is True and (self.values.get(key) or (None,))[0] is not True:
                self.arrivals[key] = now
                arrived = True
            self.values[key] = (value, now)
            if domain == "sensor":
                samples = self.history.setdefault(key, deque(maxlen=MAX_SAMPLES))
                samples.append((now, value))
                while samples and samples[0][0] < now - HISTORY_SECONDS and len(samples) > 1:
                    samples.popleft()
        if arrived and callable(self.on_arrival):
            self.on_arrival(object_id)

    def arrival(self, object_id):
        """When the binary sensor `object_id` last turned on, or None."""
        with self.lock:
            return self.arrivals.get(("binary_sensor", object_id))

    def seen(self):
        with self.lock:
            self.last_seen = self.clock()

    def lost(self, error="lost"):
        with self.lock:
            self.transport, self.connected, self.error = "", False, error if error in LINK_ERRORS else "lost"

    def fresh(self, now=None):
        """The link is live: connected, and it heard from the sensor within STALE_SECONDS."""
        now = self.clock() if now is None else now
        with self.lock:
            return self.connected and self.last_seen is not None and now - self.last_seen <= STALE_SECONDS

    def value(self, domain, object_id, now=None):
        """The current value, or None when it is missing or the link is stale."""
        if not object_id or not self.fresh(now):
            return None
        with self.lock:
            item = self.values.get((domain, object_id))
        return item[0] if item else None

    def samples(self, object_id):
        with self.lock:
            return list(self.history.get(("sensor", object_id), ()))

    def snapshot(self):
        with self.lock:
            return {"transport": self.transport, "connected": self.connected, "since": self.since,
                    "last_seen": self.last_seen, "error": self.error, "device": dict(self.device),
                    "entities": list(self.entities.values()), "values": dict(self.values)}


def _device_text(key, value):
    """A kept device field as plain text. The names lose ESPHome's MAC suffix."""
    text = _text(value)
    return without_mac(text) if key in {"name", "friendly_name"} else text


def _device_fields(info):
    """Only DEVICE_FIELDS of aioesphomeapi's DeviceInfo. Its MAC address and other fields are dropped."""
    return {key: getattr(info, key, "") for key in DEVICE_FIELDS}


class NativeClient:
    """The ESPHome native API (port 6053), through aioesphomeapi."""

    transport = "api"

    def __init__(self, host, port, key=""):
        self.host, self.port, self.key = host, port, key

    def run(self, readings, stopping):
        asyncio.run(self._run(readings, stopping))

    async def _run(self, readings, stopping):
        from aioesphomeapi import APIClient, BinarySensorInfo, SensorInfo

        # provide_time=False: Art Frames never sets the sensor's clock.
        client = APIClient(self.host, self.port, None, client_info="Art Frames", noise_psk=self.key or None,
                           provide_time=False)
        lost = asyncio.Event()

        async def on_stop(expected_disconnect):
            lost.set()
        await asyncio.wait_for(client.connect(on_stop=on_stop, login=False, log_errors=False), CONNECT_TIMEOUT)
        try:
            info = await asyncio.wait_for(client.device_info(), CONNECT_TIMEOUT)
            listed, _ = await asyncio.wait_for(client.list_entities_services(), CONNECT_TIMEOUT)
            keys, entities = {}, []
            for item in listed:
                domain = "binary_sensor" if isinstance(item, BinarySensorInfo) else "sensor" if isinstance(item, SensorInfo) else None
                if domain is None or not item.object_id:
                    continue
                entity = Entity(domain, _text(item.object_id, 64), _text(item.name), _text(item.device_class, 40),
                                _text(getattr(item, "unit_of_measurement", ""), 16))
                keys[item.key] = entity
                entities.append(entity)
            readings.start(self.transport, _device_fields(info), entities)

            def on_state(state):
                entity = keys.get(getattr(state, "key", None))
                if entity is None:
                    return
                if getattr(state, "missing_state", False):
                    value = None
                elif entity.domain == "binary_sensor":
                    value = bool(state.state)
                else:
                    value = _number(state.state)
                readings.update(entity.domain, entity.object_id, value)
            client.subscribe_states(on_state)
            beat = time.monotonic()
            while not stopping.is_set() and not lost.is_set():
                await asyncio.sleep(1)
                if time.monotonic() - beat >= HEARTBEAT_SECONDS:
                    await asyncio.wait_for(client.device_info(), CONNECT_TIMEOUT)
                    readings.seen()
                    beat = time.monotonic()
            if lost.is_set() and not stopping.is_set():
                raise ConnectionError("lost")
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception:
                pass


def native_error(error):
    """A LINK_ERRORS code for an aioesphomeapi failure. Its message is never kept: it can name the device."""
    try:
        from aioesphomeapi import (
            EncryptionPlaintextAPIError,
            InvalidEncryptionKeyAPIError,
            RequiresEncryptionAPIError,
        )
    except ImportError:
        return "no-answer"
    if isinstance(error, RequiresEncryptionAPIError):
        return "key-needed"
    if isinstance(error, InvalidEncryptionKeyAPIError):
        return "key-wrong"
    if isinstance(error, EncryptionPlaintextAPIError):
        return "key-not-used"
    if isinstance(error, ConnectionError) and str(error) == "lost":
        return "lost"
    return "no-answer"


def parse_event(event, data, readings):
    """Apply one server-sent event of ESPHome's web server to readings.

    "state" events carry {"id": "<domain>-<object_id>", "value", "state", "name", "uom"}; the first event
    of an entity has its name and unit. "ping" carries {"title": friendly name}. Text sensors (which can
    hold a MAC address), logs and every other domain are ignored.
    """
    readings.seen()
    if event not in {"state", "ping"} or not data:
        return
    try:
        payload = json.loads(data)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if event == "ping":
        readings.set_device(friendly_name=payload.get("title"))
        return
    domain, _, object_id = str(payload.get("id", "")).partition("-")
    if domain not in DOMAINS or not re.fullmatch(r"[a-z0-9_]{1,64}", object_id):
        return
    readings.add_entity(Entity(domain, object_id, _text(payload.get("name")), "", _text(payload.get("uom"), 16)))
    value = payload.get("value")
    if domain == "binary_sensor":
        value = value if isinstance(value, bool) else None
    else:
        value = _number(value)
    readings.update(domain, object_id, value)


class WebClient:
    """The fallback: ESPHome's web_server, through its /events stream. It has no device classes."""

    transport = "web"

    def __init__(self, host, port=80):
        self.host, self.port = host, port

    def run(self, readings, stopping):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=WEB_TIMEOUT)
        try:
            connection.request("GET", "/events", headers={"Accept": "text/event-stream"})
            response = connection.getresponse()
            if response.status != 200:
                raise ConnectionError("web-failed")
            readings.start(self.transport)
            event, data, size = "", [], 0
            while not stopping.is_set():
                line = response.fp.readline(MAX_LINE)
                if not line:
                    raise ConnectionError("lost")
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    if size <= MAX_EVENT:
                        parse_event(event, "\n".join(data), readings)
                    event, data, size = "", [], 0
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    size += len(line) - 5
                    if size > MAX_EVENT:
                        data = []  # too big: drop this event; the next blank line starts a new one
                        readings.seen()
                    else:
                        data.append(line[5:].strip())
                else:
                    readings.seen()  # "id:", "retry:" and comments still show the link is live
        finally:
            connection.close()


class DemoClient:
    """Demo mode: a made-up Akamatis sensor. No network access."""

    transport = "api"
    ENTITIES = (Entity("binary_sensor", "presence", "Presence", "occupancy"),
                Entity("binary_sensor", "moving_target", "Moving Target", "motion"),
                Entity("binary_sensor", "still_target", "Still Target", "occupancy"),
                Entity("sensor", "ambient_light", "Ambient Light", "illuminance", "lx"),
                Entity("sensor", "detection_distance", "Detection Distance", "distance", "cm"))

    def __init__(self, *args, **kwargs):
        pass

    def run(self, readings, stopping):
        readings.start(self.transport, {"name": "akamatis-smart-device",
                                        "friendly_name": "Akamatis Smart Device",
                                        "project_name": "Akamatis.SmartHomeDevice", "project_version": "1.3.0"},
                       self.ENTITIES)
        step = 0
        while not stopping.is_set():
            readings.update("binary_sensor", "presence", True)
            readings.update("binary_sensor", "moving_target", step % 4 == 0)
            readings.update("binary_sensor", "still_target", True)
            readings.update("sensor", "ambient_light", round(120 + 40 * math.sin(step / 8), 1))
            readings.update("sensor", "detection_distance", 180.0)
            step += 1
            stopping.wait(5)


def default_clients(settings):
    """The clients a Link tries, in order: the native API, then the web server.

    With an encryption key, only the native API: the web server is not encrypted, so a sensor that
    uses a key never falls back to it.
    """
    key = settings.key.get_secret_value() if hasattr(settings.key, "get_secret_value") else settings.key
    if key:
        return [NativeClient(settings.host, settings.port, key)]
    return [NativeClient(settings.host, settings.port, key), WebClient(settings.host, settings.web_port)]


class Link:
    """One sensor connection on its own thread, with reconnects and backoff."""

    def __init__(self, settings, *, clients=None, clock=time.time, name="sensor"):
        self.settings = settings
        self.readings = Readings(clock=clock)
        self.clients = clients if clients is not None else default_clients(settings)
        self.stopping = threading.Event()
        self.thread = None
        self.name = name
        self.failures = 0
        self.created = clock()  # Health says "connecting", not "not seen", during the first tries
        self.readings.on_start = self._started
        self.drop_logged = False  # the last drop has a log line, so its reconnect gets one too
        self.drop_logged_at = None
        self.quiet_drops = 0  # drops since the last drop line that had no line of their own

    @staticmethod
    def signature(settings):
        """What a running Link depends on: a change starts a new Link. The key counts, but is not kept."""
        key = settings.key.get_secret_value() if hasattr(settings.key, "get_secret_value") else settings.key
        return (settings.host, settings.port, settings.web_port, hash(key))

    def start(self):
        self.thread = threading.Thread(target=self._loop, name=f"art-frames-{self.name}", daemon=True)
        self.thread.start()
        return self

    def stop(self, timeout=0):
        self.stopping.set()
        if self.thread and timeout:
            self.thread.join(timeout)

    def run_once(self):
        """Try each client in order until one runs and stops. Returns True when one connected."""
        codes = []
        for client in self.clients:
            if self.stopping.is_set():
                return False
            try:
                client.run(self.readings, self.stopping)
                connected = True
            except Exception as error:
                connected = self.readings.connected
                codes.append(native_error(error) if client.transport == "api" else
                             "lost" if self.readings.connected else "web-failed")
            if connected:
                self.readings.lost("lost" if not self.stopping.is_set() else "")
                if not self.stopping.is_set():
                    self._dropped()
                return True
        # The native API's reason says more (such as a wrong key) than the fallback's.
        self.readings.lost(codes[0] if codes else "no-answer")
        log.info("Sensor %s: no connection (%s)", self.name, ", ".join(codes) or "no-answer")
        return False

    def _dropped(self):
        """Log a lost connection: at most one line in LINK_LOG_GAP seconds."""
        now = self.readings.clock()
        if self.drop_logged_at is not None and now - self.drop_logged_at < LINK_LOG_GAP:
            self.quiet_drops += 1
            return
        more = f" ({self.quiet_drops} more drops since the last line)" if self.quiet_drops else ""
        log.info("Sensor %s: connection lost; reconnecting%s", self.name, more)
        self.drop_logged, self.drop_logged_at, self.quiet_drops = True, now, 0

    def _started(self, transport):
        """Log a reconnect after a drop that has a log line."""
        if self.drop_logged:
            self.drop_logged = False
            log.info("Sensor %s: connection back (%s)", self.name, transport)

    def _loop(self):
        while not self.stopping.is_set():
            connected = self.run_once()
            self.failures = 0 if connected else self.failures + 1
            low, high = BACKOFF
            delay = low if connected else min(high, low * 2 ** min(self.failures - 1, 10))
            self.stopping.wait(delay * random.uniform(0.8, 1.2))
