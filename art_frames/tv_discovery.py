"""Explicit, bounded Samsung device-info searches on a small private IPv4 network."""

from __future__ import annotations

import http.client
import io
import ipaddress
import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def search_network(value):
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError as error:
        raise ValueError("Enter a private IPv4 network, such as 192.168.1.0/24.") from error
    if (
        network.version != 4
        or network.num_addresses > 256
        or not any(network.subnet_of(private) for private in PRIVATE_NETWORKS)
    ):
        raise ValueError("Search a private IPv4 network with at most 256 addresses (/24 or smaller).")
    return network


def suggested_network(tv_ip=""):
    try:
        if tv_ip:
            return str(search_network(f"{tv_ip}/24"))
        # UDP connect selects a route without sending any packet.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            return str(search_network(f"{sock.getsockname()[0]}/24"))
    except (ValueError, OSError):
        return ""


class _BufferedResponse:
    def __init__(self, data):
        self.data = data

    def makefile(self, mode):
        return io.BytesIO(self.data)


def _device(ip, payload):
    if not isinstance(payload, dict) or payload.get("type") != "Samsung SmartTV":
        return None
    device = payload.get("device")
    if not isinstance(device, dict):
        return None

    def label(value):
        return "".join(char for char in str(value or "") if char.isprintable())[:100]

    mac = str(device.get("wifiMac", ""))
    if device.get("networkType") != "wireless" or not re.fullmatch(
        r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac
    ):
        mac = ""
    frame = str(device.get("FrameTVSupport", "")).lower()
    return {
        "ip": ip,
        "name": label(device.get("name") or payload.get("name")) or "Samsung TV",
        "model": label(device.get("modelName")),
        "mac": mac.upper(),
        "frame": {"true": True, "false": False}.get(frame),
    }


def _probe(ip):
    # A hard one-second deadline covers connect, headers and body, including slow peers.
    deadline = time.monotonic() + 1
    try:
        wire = bytearray()
        with socket.create_connection((ip, 8001), timeout=0.5) as sock:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            sock.sendall(
                f"GET /api/v2/ HTTP/1.1\r\nHost: {ip}:8001\r\nAccept: application/json\r\n"
                "Accept-Encoding: identity\r\nConnection: close\r\n\r\n".encode()
            )
            while len(wire) <= 65536:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(min(8192, 65537 - len(wire)))
                except TimeoutError:
                    break
                if not chunk:
                    break
                wire.extend(chunk)
        if len(wire) > 65536:
            return None
        with http.client.HTTPResponse(_BufferedResponse(bytes(wire))) as response:
            response.begin()
            if response.status != 200:
                return None  # never follow redirects or connect to an advertised URL
            body = response.read(32769)
            if len(body) > 32768:
                return None
        return _device(ip, json.loads(body))
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return None


def _sweep(network):
    with ThreadPoolExecutor(max_workers=12) as pool:
        return [result for result in pool.map(_probe, map(str, network.hosts())) if result]


ARP_TABLE = Path("/proc/net/arp")
MAC_PATTERN = r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"


def arp_table(path=ARP_TABLE):
    """Return the kernel neighbour table as {private IPv4 address: upper-case MAC}.

    Only complete entries are kept. Returns None when the table does not exist or
    cannot be read (for example, on macOS).
    """
    try:
        with Path(path).open() as table:
            lines = table.read(262144).splitlines()[1:]
    except OSError:
        return None
    found = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 4 or fields[2] == "0x0" or not re.fullmatch(MAC_PATTERN, fields[3]):
            continue  # 0x0 marks an incomplete entry
        try:
            address = ipaddress.IPv4Address(fields[0])
        except ValueError:
            continue
        if any(address in private for private in PRIVATE_NETWORKS):
            found.setdefault(fields[0], fields[3].upper())
    return found


def arp_addresses(mac, path=ARP_TABLE):
    """Return private IPv4 addresses that the kernel neighbour table maps to this MAC.

    Returns None when the table does not exist or cannot be read.
    """
    table = arp_table(path)
    if table is None:
        return None
    return [ip for ip, entry in table.items() if entry == mac.upper()]


def find_mac(mac, networks, *, arp_path=ARP_TABLE):
    """Return [(ip, confirmed)] for each address where a Samsung TV with this MAC answers. Read-only.

    A device can claim any MAC in its device-info reply, so only the kernel neighbour
    table confirms a match. The table is tried first: each entry for this MAC must also
    answer the device-info request, so a stale entry is not trusted. If that gives no
    answer, each bounded private network is swept and the table is read again, because
    the sweep's connections fill it. Then:

    - A host that the table maps to this MAC is confirmed. This also finds a wired TV,
      which reports no Wi-Fi MAC.
    - A host that reports this MAC but that the table maps to another MAC is dropped.
    - A host that reports this MAC but has no table entry (no table on macOS, or Docker
      bridge networking) is returned unconfirmed. The caller must not apply it automatically.
    """
    mac = mac.upper()
    confirmed = []
    for ip in arp_addresses(mac, arp_path) or []:
        device = _probe(ip)
        # A wired TV reports no Wi-Fi MAC; the table entry then supplies the match.
        if device and device["mac"] in ("", mac):
            confirmed.append(ip)
    if confirmed:
        return [(ip, True) for ip in confirmed]
    answered = {}
    for value in networks:
        for device in _sweep(search_network(value)):
            answered.setdefault(device["ip"], device["mac"])
    table = arp_table(arp_path) or {}
    matches = []
    for ip, reported in answered.items():
        entry = table.get(ip)
        if entry == mac and reported in ("", mac):
            matches.append((ip, True))
        elif entry is None and reported == mac:
            matches.append((ip, False))
    return matches


class DiscoveryBusy(ValueError):
    pass


class TVDiscovery:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_started = float("-inf")

    def scan(self, value):
        network = search_network(value)
        if not self.lock.acquire(blocking=False):
            raise DiscoveryBusy("A TV search is already running. Please wait.")
        try:
            if time.monotonic() - self.last_started < 30:
                raise DiscoveryBusy("Please wait 30 seconds between TV searches.")
            self.last_started = time.monotonic()
            results = _sweep(network)
            return sorted(
                results, key=lambda item: (item["frame"] is not True, ipaddress.ip_address(item["ip"]))
            )
        finally:
            self.lock.release()
