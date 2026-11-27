"""Bounded public HTTPS requests with DNS pinning; never fetch NAS/private services."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urljoin, urlsplit


class FetchError(ValueError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def public_address(host: str, port: int) -> str:
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise FetchError("The source must resolve exclusively to public internet addresses")
    return addresses[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int):
        super().__init__(host, port, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Use the checked address for the socket, and the original hostname for SNI
        # and certificate verification. A second DNS lookup would allow rebinding.
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch(url: str, *, hosts: set[str] | None = None, limit: int = 25_000_000) -> bytes:
    for _ in range(4):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            raise FetchError("Only public HTTPS URLs on port 443 are supported")
        host = parts.hostname.lower()
        if hosts is not None and host not in hosts:
            raise FetchError("The image host is not approved for this museum")
        address = public_address(host, 443)
        conn = PinnedHTTPS(host, address, 443)
        try:
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            conn.request(
                "GET",
                target,
                headers={
                    "User-Agent": "ArtFrames/0.1 (self-hosted art display)",
                    "Accept-Encoding": "identity",
                },
            )
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise FetchError("Source returned an invalid redirect")
                url = urljoin(url, location)
                continue  # every redirected address is validated and pinned again
            if response.status != 200:
                raise FetchError(f"Source returned HTTP {response.status}", status=response.status)
            length = response.getheader("Content-Length")
            if length and int(length) > limit:
                raise FetchError("Source exceeds the download size limit")
            data = response.read(limit + 1)
            if len(data) > limit:
                raise FetchError("Source exceeds the download size limit")
            return data
        finally:
            conn.close()
    raise FetchError("Source returned too many redirects")


def get_json(url: str, *, hosts: set[str]) -> dict:
    return json.loads(fetch(url, hosts=hosts, limit=4_000_000))
