"""Every TV channel connection has a total deadline, so a chatty TV cannot hold a wait open forever."""

import base64
import hashlib
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from samsungtvws.event import IGNORE_EVENTS_AT_STARTUP
from websocket import ABNF

from art_frames.config import Config, private_write
from art_frames.television import ART_SECONDS, Remote, Television
from art_frames.tv_artwork import ArtworkSocket, BoundedArt


def flooding_tv(stop, *, startup=False):
    """A local fake TV that never replies. It opens the channel, then sends unrelated events forever.

    With startup=True it never finishes opening: it sends startup events that the library skips.
    """
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(10)
    if startup:
        opening, noise = [], {"event": IGNORE_EVENTS_AT_STARTUP[0]}
    else:
        opening, noise = [{"event": "ms.channel.connect", "data": {}}, {"event": "ms.channel.ready"}], \
            {"event": "ms.channel.clientConnect"}

    def serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(4096)
                key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                           if line.lower().startswith(b"sec-websocket-key"))
                accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                for message in opening:
                    conn.sendall(ABNF.create_frame(json.dumps(message), ABNF.OPCODE_TEXT).format())
                frame = ABNF.create_frame(json.dumps(noise), ABNF.OPCODE_TEXT).format()
                try:
                    while not stop.is_set():
                        conn.sendall(frame)
                        time.sleep(0.02)
                except OSError:
                    continue  # the client closed the channel
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server, thread


def local_tv(server, tmp_path, power="on"):
    cfg = Config(tv={"ip": "192.168.1.50"}).tv
    # The fake TV listens on this computer; settings checks would refuse its address and port.
    device = Television(cfg.model_copy(update={"ip": "127.0.0.1", "port": server.getsockname()[1]}), tmp_path)
    private_write(device.token, b"synthetic-token")
    real_remote = device.remote

    def remote(timeout=None):
        connection = real_remote(timeout=timeout)
        connection.rest_device_info = lambda: {"device": {"PowerState": power}}
        return connection
    device.remote = remote
    return device


def bounded_call(fn, limit=10):
    """Run fn in a thread. Fail, instead of hanging the tests, when it has not ended after `limit` seconds."""
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as error:  # noqa: BLE001 - handed to the test thread
            box["error"] = error
    worker = threading.Thread(target=run, daemon=True)
    start = time.monotonic()
    worker.start()
    worker.join(limit)
    assert not worker.is_alive(), "the call did not stop at its deadline"
    box["seconds"] = time.monotonic() - start
    return box


@pytest.fixture
def chatty_tv(request):
    stop = threading.Event()
    server, thread = flooding_tv(stop, startup=getattr(request, "param", False))
    yield server
    stop.set()
    server.close()
    thread.join(5)


@pytest.mark.parametrize("path", ["status", "current_artwork"])
def test_endless_unrelated_events_cannot_hold_an_art_check_open(tmp_path, monkeypatch, chatty_tv, path):
    device = local_tv(chatty_tv, tmp_path)
    monkeypatch.setattr("art_frames.television.ART_SECONDS", 2)
    box = bounded_call(getattr(device, path))
    # Each frame comes well inside the 8 s reply timeout, so only the total deadline ends the wait.
    assert box["seconds"] < 6 and "error" not in box
    if path == "status":
        assert box["result"] == {"awake": False, "playing": False, "paired": True, "known": False, "answered": True}
    else:
        assert box["result"] is None


def test_publish_connection_stops_at_its_deadline(tmp_path, monkeypatch, chatty_tv):
    device = local_tv(chatty_tv, tmp_path)
    monkeypatch.setattr("art_frames.television.UPLOAD_SECONDS", 2)
    box = bounded_call(lambda: device.publish(b"jpeg", [], lambda _: None))
    assert box["seconds"] < 6 and isinstance(box.get("error"), Exception)  # fails like a lost connection


@pytest.mark.parametrize("chatty_tv", [True], indirect=True)
def test_endless_startup_events_cannot_hold_a_key_send(tmp_path, monkeypatch, chatty_tv):
    """The remote channel never finishes opening, so no key goes out, and screen_on fails like a network error."""
    device = local_tv(chatty_tv, tmp_path, power="standby")
    monkeypatch.setattr("art_frames.television.REMOTE_SECONDS", 2)
    box = bounded_call(lambda: device.remote(timeout=1).send_key("KEY_POWER"))
    assert box["seconds"] < 6 and isinstance(box.get("error"), Exception)
    state = {"answers": True, "power": "standby", "art_mode": None}
    box = bounded_call(lambda: device.screen_on(signal=False, state=state))
    assert box["seconds"] < 8 and isinstance(box.get("error"), Exception)  # the 3 s reply timeout plus 2 s
    assert "key" not in device.phases  # the key was never sent


def test_remote_channel_deadline_is_the_reply_timeout_plus_fifteen_seconds(monkeypatch):
    sockets = []
    monkeypatch.setattr("art_frames.tv_artwork.socket.create_connection",
                        lambda *a, **kw: sockets.append(kw["timeout"]) or (_ for _ in ()).throw(OSError("offline")))
    for timeout, seconds in ((3, 18), (120, 135)):
        remote = Remote(host="192.168.1.50", port=8002, name="Art Frames", timeout=timeout)
        before = time.monotonic()
        with pytest.raises(OSError):
            remote.open()
        assert before + seconds <= remote.deadline <= time.monotonic() + seconds


def test_remote_art_matches_the_library_settings_and_has_a_deadline(tmp_path):
    remote = Remote(host="192.168.1.50", port=8002, name="Art Frames", timeout=3, token_file=str(tmp_path / "t"))
    before = time.monotonic()
    art = remote.art()
    plain = super(Remote, remote).art()
    assert isinstance(art, BoundedArt)
    for name in ("host", "port", "name", "timeout", "token", "token_file", "key_press_delay", "endpoint"):
        assert getattr(art, name) == getattr(plain, name)
    assert before + ART_SECONDS <= art.deadline <= time.monotonic() + ART_SECONDS
    assert remote.art(seconds=300).deadline > art.deadline


def stalled_d2d(monkeypatch):
    """A fake upload socket where each part of the image takes one second (of a fake clock)."""
    clock = [0.0]
    monkeypatch.setattr("art_frames.tv_artwork.time.monotonic", lambda: clock[0])
    timeouts, sent, closed = [], [], []

    def send(data):
        clock[0] += 1
        sent.append(bytes(data))
    raw = SimpleNamespace(settimeout=timeouts.append, sendall=send, close=lambda: closed.append(True))
    monkeypatch.setattr("art_frames.tv_artwork.socket.create_connection", lambda *a, **kw: raw)
    return timeouts, sent, closed


def test_upload_socket_is_bounded_by_the_same_deadline(monkeypatch):
    timeouts, sent, _ = stalled_d2d(monkeypatch)
    art = BoundedArt("192.168.1.50", timeout=8, seconds=5)
    sock = art._open_d2d_socket({"ip": "192.168.1.50", "port": "40000"})
    assert isinstance(sock, ArtworkSocket) and sock.deadline == art.deadline
    with pytest.raises(TimeoutError):
        sock.sendall(b"x" * (65536 * 20))  # a large image that would take 20 seconds
    assert len(sent) == 5 and all(0 < value <= 8 for value in timeouts)


def test_library_upload_sends_through_the_bounded_socket(monkeypatch):
    """BoundedArt.upload() is the library's own upload; its image data goes through the bounded socket."""
    timeouts, sent, closed = stalled_d2d(monkeypatch)
    art = BoundedArt("192.168.1.50", timeout=8, seconds=10)
    art.get_api_version = lambda: "4.3.4.0"
    art._send_art_request = lambda *a, **kw: {"conn_info": {"ip": "192.168.1.50", "port": 40000, "key": "k"}}
    art._wait_for_d2d = lambda **kw: pytest.fail("the upload must stop at its deadline")
    with pytest.raises(TimeoutError):
        art.upload(b"x" * (65536 * 40), file_type="JPEG", matte="none", portrait_matte="none")
    assert 5 <= len(sent) <= 10 and closed == [True]
