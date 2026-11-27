import socket

import pytest

from art_frames.network import FetchError, fetch, public_address


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "https://example.com:5001",
        "https://127.0.0.1:443",
    ],
)
def test_unsafe_fetches_are_rejected(url):
    with pytest.raises((ValueError, OSError)):
        fetch(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "192.168.1.70", "169.254.169.254", "::1", "fc00::1"])
def test_dns_private_addresses_are_rejected(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, (address, 443))]
    )
    with pytest.raises(FetchError):
        public_address("attacker.example", 443)


def test_mixed_public_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (None, None, None, None, ("1.1.1.1", 443)),
            (None, None, None, None, ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(FetchError):
        public_address("attacker.example", 443)


def test_museum_host_allowlist_applies_before_network():
    with pytest.raises(FetchError):
        fetch("https://example.com/file", hosts={"images.metmuseum.org"})
