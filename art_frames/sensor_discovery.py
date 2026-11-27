"""Find ESPHome sensors on the network with mDNS (_esphomelib._tcp).

mDNS needs the container on the host network (compose.static.host.yaml uses network_mode: host).
In Docker's bridge mode the search finds nothing; the page then asks for the sensor's address.
The TXT records also hold the device's MAC address; parse_service() never keeps it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import threading
import time

from . import sensors

SERVICE = "_esphomelib._tcp.local."
BROWSE_SECONDS = 3
# One search takes at most this long: the browse, then reading each found device in the time left.
SEARCH_SECONDS = 5
MAX_FOUND = 20
# Only these TXT records are kept.
TXT_FIELDS = ("friendly_name", "project_name", "project_version", "board", "platform", "version", "network")


class SearchBusy(ValueError):
    """A search is already running."""


def _decode(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return sensors._text(value)


def parse_service(name, addresses, port, properties):
    """One found device as a dict for pages, or None when it has no private IPv4 address.

    name is the mDNS instance name ("akamatis-smart-device-005301._esphomelib._tcp.local.").
    The result has "name", "host", "port", "friendly_name", "vendor", "product", "title" (what the page
    shows after "Found"), "encrypted" (the TXT has api_encryption) and the kept TXT_FIELDS.
    """
    txt = {_decode(key).lower(): _decode(value) for key, value in (properties or {}).items() if key is not None}
    host = ""
    for address in addresses or ():
        try:
            addr = ipaddress.ip_address(address)
        except ValueError:
            continue
        if addr.version == 4 and addr.is_private and not addr.is_loopback and not addr.is_link_local:
            host = str(addr)
            break
    if not host:
        return None
    instance = sensors._text(str(name).removesuffix(".").removesuffix(SERVICE.rstrip(".")).rstrip("."), 64)
    device_id = hashlib.sha256(instance.lower().encode()).hexdigest()[:16]  # see sensor_web.device_id
    instance = sensors.without_mac(instance)  # ESPHome's MAC suffix is part of the MAC address
    txt["friendly_name"] = sensors.without_mac(txt.get("friendly_name", ""))
    vendor, product = sensors.parse_project(txt.get("project_name", ""))
    found = {key: txt.get(key, "") for key in TXT_FIELDS}
    found.update(name=instance, host=host, port=int(port or 6053), vendor=vendor, product=product,
                 title=sensors.product_name(txt.get("project_name", ""), txt.get("friendly_name") or instance),
                 encrypted="api_encryption" in txt, device_id=device_id)
    return found


def browse(seconds=BROWSE_SECONDS, deadline_seconds=SEARCH_SECONDS):
    """Browse for ESPHome devices for a few seconds. Returns parse_service() dicts, sorted by title.

    The whole search ends after deadline_seconds: a device that does not answer in time is left out.
    """
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    names = set()

    class Listener(ServiceListener):
        def add_service(self, zc, type_, name):
            names.add(name)

        def update_service(self, zc, type_, name):
            names.add(name)

        def remove_service(self, zc, type_, name):
            pass

    deadline = time.monotonic() + deadline_seconds
    zc = Zeroconf()
    try:
        browser = ServiceBrowser(zc, SERVICE, Listener())
        time.sleep(seconds)
        browser.cancel()
        found = []
        for name in sorted(names)[:MAX_FOUND]:
            left = deadline - time.monotonic()
            if left < 0.1:
                break
            info = zc.get_service_info(SERVICE, name, timeout=int(min(1.5, left) * 1000))
            if info is None:
                continue
            item = parse_service(name, info.parsed_addresses(), info.port, info.properties)
            if item:
                found.append(item)
        return sorted(found, key=lambda item: (item["title"], item["name"]))
    finally:
        zc.close()


DEMO_FOUND = [
    parse_service("akamatis-smart-device._esphomelib._tcp.local.", ["192.168.1.148"], 6053, {
        b"friendly_name": b"Akamatis Smart Device", b"project_name": b"Akamatis.SmartHomeDevice",
        b"project_version": b"1.3.0", b"board": b"esp32-c3-devkitm-1", b"platform": b"ESP32"}),
    parse_service("apollo-msr-2._esphomelib._tcp.local.", ["192.168.1.120"], 6053, {
        b"friendly_name": b"Apollo MSR-2", b"project_name": b"ApolloAutomation.MSR-2",
        b"project_version": b"25.4.1", b"api_encryption": b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"}),
]


class Discovery:
    """One search at a time; the last result stays for the page."""

    def __init__(self, *, demo=False, browse_fn=None):
        self.demo = demo
        self.browse_fn = browse_fn or browse
        self.lock = threading.Lock()
        self.found = None  # None before the first search
        self.at = None
        self.failed = False

    def search(self):
        if not self.lock.acquire(blocking=False):
            raise SearchBusy("A search is already running.")
        try:
            try:
                self.found = [dict(item) for item in DEMO_FOUND] if self.demo else self.browse_fn()
                self.failed = False
            except Exception:
                self.found, self.failed = [], True  # no multicast here, such as Docker's bridge mode
            self.at = time.time()
            return self.found
        finally:
            self.lock.release()
