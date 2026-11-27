import json
import threading

import pytest

from art_frames import tv_discovery as discovery


def device(**changes):
    return {
        "type": "Samsung SmartTV",
        "device": {
            "name": "Old room name",
            "modelName": "QN65LS03D",
            "FrameTVSupport": "true",
            "networkType": "wireless",
            "wifiMac": "aa:bb:cc:dd:ee:ff",
            **changes,
        },
    }


@pytest.mark.parametrize(
    "network",
    [
        "0.0.0.0/0",
        "10.0.0.0/8",
        "192.168.0.0/23",
        "127.0.0.1/32",
        "169.254.0.0/24",
        "8.8.8.0/24",
        "::1/128",
        "fd00::/120",
        "example.com",
        "",
    ],
)
def test_only_small_rfc1918_networks_are_searched(network, monkeypatch):
    monkeypatch.setattr(discovery, "_probe", lambda ip: pytest.fail("Invalid network probed"))
    with pytest.raises(ValueError):
        discovery.TVDiscovery().scan(network)


def test_results_use_probed_ip_frame_first_and_no_settings_side_effects(monkeypatch):
    calls = []

    def probe(ip):
        calls.append(ip)
        return discovery._device(
            ip, device(FrameTVSupport="true" if ip.endswith("2") else "false", ip="8.8.8.8")
        )

    monkeypatch.setattr(discovery, "_probe", probe)
    finder = discovery.TVDiscovery()
    results = finder.scan("192.168.1.1/30")
    assert set(calls) == {"192.168.1.1", "192.168.1.2"}
    assert [item["ip"] for item in results] == ["192.168.1.2", "192.168.1.1"]
    assert results[0]["mac"] == "AA:BB:CC:DD:EE:FF"
    with pytest.raises(discovery.DiscoveryBusy, match="30 seconds"):
        finder.scan("192.168.1.0/30")


def test_one_scan_at_a_time(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def probe(ip):
        started.set()
        assert release.wait(3)
        return None

    monkeypatch.setattr(discovery, "_probe", probe)
    finder = discovery.TVDiscovery()
    thread = threading.Thread(target=lambda: finder.scan("192.168.1.1/32"))
    thread.start()
    try:
        assert started.wait(3)
        with pytest.raises(discovery.DiscoveryBusy, match="already running"):
            finder.scan("192.168.1.1/32")
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert finder.lock.acquire(blocking=False)
    finder.lock.release()


def test_wired_mac_is_not_guessed_and_untrusted_metadata_is_bounded():
    result = discovery._device("192.168.1.2", device(name="<script>\n" + "x" * 200, networkType="wired"))
    assert result["mac"] == ""
    assert len(result["name"]) == 100 and "\n" not in result["name"]
    assert discovery._device("192.168.1.2", {"type": "Another device", "device": {}}) is None
    assert discovery._device("192.168.1.2", []) is None


class Peer:
    def __init__(self, wire):
        self.wire = wire
        self.sent = []
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        chunk, self.wire = self.wire[:size], self.wire[size:]
        return chunk


def probe_wire(monkeypatch, wire):
    peer = Peer(wire)

    def connect(address, timeout):
        assert address == ("192.168.1.2", 8001) and timeout == 0.5
        return peer

    monkeypatch.setattr(discovery.socket, "create_connection", connect)
    return discovery._probe("192.168.1.2"), peer


def test_probe_uses_only_fixed_read_only_endpoint(monkeypatch):
    body = json.dumps(device()).encode()
    result, peer = probe_wire(
        monkeypatch, b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    assert result["frame"] is True
    assert len(peer.sent) == 1 and peer.sent[0].startswith(b"GET /api/v2/ HTTP/1.1\r\n")
    assert all(0 < timeout <= 1 for timeout in peer.timeouts)


@pytest.mark.parametrize(
    "wire",
    [
        b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/admin\r\n\r\n",
        b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 32769,
        b"x" * 65537,
        b"HTTP/1.1 200 OK\r\n\r\n{}",
        b"HTTP/1.1 200 OK\r\n\r\nnot-json",
        b"garbage",
    ],
)
def test_redirects_oversized_invalid_and_unrelated_responses_ignored(monkeypatch, wire):
    result, peer = probe_wire(monkeypatch, wire)
    assert result is None
    assert len(peer.sent) == 1


def test_slow_peer_cannot_extend_absolute_deadline(monkeypatch):
    ticks = iter(index * 0.3 for index in range(10))
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(ticks))
    result, peer = probe_wire(monkeypatch, b"x" * 65537)
    assert result is None
    assert 0 < len(peer.wire) < 65537


def test_network_suggestion_uses_tv_without_route_lookup(monkeypatch):
    monkeypatch.setattr(discovery.socket, "socket", lambda *args: pytest.fail("Unnecessary route lookup"))
    assert discovery.suggested_network("192.168.50.12") == "192.168.50.0/24"
    assert discovery.suggested_network("::1") == ""
