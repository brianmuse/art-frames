import hashlib
import io
import json
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from art_frames.config import Config
from art_frames.controller import Controller
from art_frames.television import Television
from art_frames.tv_artwork import MAX_THUMBNAIL, ArtworkReader, ArtworkSocket, thumbnail_jpeg


class Art:
    def __init__(self, current="old", thumbnail=None):
        self.current = current
        self.thumbnail = thumbnail
        self.deleted = []
        self.queries = 0

    def get_artmode(self): return "on"
    def get_current(self): return {"content_id": self.current}
    def upload(self, *args, **kwargs): return "new"
    def select_image(self, *args, **kwargs): pass
    def delete_list(self, values): self.deleted.extend(values)
    def get_thumbnail(self, content_id):
        self.queries += 1
        assert content_id == self.current
        return self.thumbnail
    def close(self): pass


def test_unconfirmed_selection_keeps_previous_artwork_and_does_not_report_success(tmp_path, monkeypatch):
    device = Television(Config(tv={"ip": "192.168.1.50"}).tv, tmp_path)
    art = Art()
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    monkeypatch.setattr("art_frames.television.time.sleep", lambda _: None)
    recorded = []
    assert device.publish(b"jpeg", ["old"], recorded.append) == ("new", ["old", "new"], False)
    assert recorded == ["new"] and not art.deleted


@pytest.mark.parametrize("current,confirmed", [("new", True), ("old", False)])
def test_lost_selection_ack_still_verifies_instead_of_reuploading(tmp_path, monkeypatch, current, confirmed):
    device = Television(Config(tv={"ip": "192.168.1.50"}).tv, tmp_path)
    art = Art(current=current)
    def lost_ack(*args, **kwargs): raise TimeoutError("Lost selection acknowledgement")
    art.select_image = lost_ack
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    monkeypatch.setattr("art_frames.television.time.sleep", lambda _: None)
    assert device.publish(b"jpeg", ["old"], lambda _: None)[2] is confirmed
    assert bool(art.deleted) is confirmed


def test_websocket_budget_applies_before_buffering_and_to_startup_deadline(monkeypatch):
    sizes = []
    raw = SimpleNamespace(settimeout=lambda _: None, recv=lambda size: sizes.append(size) or b"x" * size)
    sock = ArtworkSocket(raw, time.monotonic() + 15)
    sock.remaining_bytes = 4
    assert sock.recv(100_000_000) == b"xxxx" and sizes == [4]
    with pytest.raises(ValueError, match="size"):
        sock.recv(1)
    sock.deadline = 0
    with pytest.raises(TimeoutError):
        sock.recv(1)
    reader = ArtworkReader("192.168.1.50", timeout=5)
    reader.deadline = 0
    monkeypatch.setattr("art_frames.tv_artwork.create_connection", lambda *a, **kw: pytest.fail("Expired startup opened a socket"))
    with pytest.raises(TimeoutError):
        reader.open()


def test_ignored_startup_frames_share_overall_deadline(monkeypatch):
    from samsungtvws.event import IGNORE_EVENTS_AT_STARTUP
    from websocket import ABNF, WebSocket, WebSocketTimeoutException
    clock = [0]
    wire = bytearray(ABNF.create_frame(json.dumps({"event": IGNORE_EVENTS_AT_STARTUP[0]}), ABNF.OPCODE_TEXT).format() * 100)
    def recv(size):
        clock[0] += 1
        data = bytes(wire[:size])
        del wire[:size]
        return data
    monkeypatch.setattr("art_frames.tv_artwork.time.monotonic", lambda: clock[0])
    raw = SimpleNamespace(settimeout=lambda _: None, gettimeout=lambda: 5, recv=recv,
                          close=lambda: None, shutdown=lambda _: None, _closed=False)
    monkeypatch.setattr("art_frames.tv_artwork.socket.create_connection", lambda *a, **kw: raw)
    def connect(*args, **kwargs):
        assert kwargs["redirect_limit"] == 0
        ws = WebSocket()
        ws.sock = kwargs["socket"]
        return ws
    monkeypatch.setattr("art_frames.tv_artwork.create_connection", connect)
    reader = ArtworkReader("192.168.1.50", timeout=5)
    reader.deadline = 5
    with pytest.raises(WebSocketTimeoutException):
        reader.open()
    assert clock[0] <= 5 and len(wire) > 0


def test_upgrade_header_is_bounded_before_websocket_frames(monkeypatch):
    from websocket import WebSocketTimeoutException
    from websocket._http import read_headers
    clock = [0]
    def trickle(size):
        clock[0] += 1
        return b"x"
    raw = SimpleNamespace(settimeout=lambda _: None, gettimeout=lambda: 5, recv=trickle)
    monkeypatch.setattr("art_frames.tv_artwork.time.monotonic", lambda: clock[0])
    sock = ArtworkSocket(raw, 4)
    with pytest.raises(WebSocketTimeoutException):
        read_headers(sock)
    assert clock[0] == 4
    clock[0] = 0
    sock = ArtworkSocket(raw, 100)
    sock.remaining_bytes = 3
    with pytest.raises(ValueError, match="size"):
        read_headers(sock)
    assert clock[0] == 3


def test_reader_opens_through_bounded_socket_and_saves_token(tmp_path):
    import base64
    import socket
    import threading

    from websocket import ABNF
    server = socket.create_server(("127.0.0.1", 0))
    def tv():
        conn, _ = server.accept()
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                request += conn.recv(4096)
            key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            for message in ({"event": "ms.channel.connect", "data": {"token": "12345678"}}, {"event": "ms.channel.ready"}):
                conn.sendall(ABNF.create_frame(json.dumps(message), ABNF.OPCODE_TEXT).format())
            conn.recv(4096)
    thread = threading.Thread(target=tv, daemon=True)
    thread.start()
    token_file = tmp_path / "token"
    reader = ArtworkReader("127.0.0.1", port=server.getsockname()[1], token_file=str(token_file), timeout=5)
    try:
        connection = reader.open()
        assert isinstance(connection.sock, ArtworkSocket) and connection.connected
        assert token_file.read_text() == "12345678"
    finally:
        reader.close()
        thread.join(5)
        server.close()


def test_readback_uses_matching_upload_or_tv_thumbnail(tmp_path, monkeypatch, image_bytes):
    device = Television(Config(tv={"ip": "192.168.1.50"}).tv, tmp_path)
    art = Art(thumbnail=image_bytes)
    monkeypatch.setattr(device, "artwork_reader", lambda: art)
    assert device.read_artwork("old", b"exact uploaded bytes") == ("old", b"exact uploaded bytes", "upload")
    assert art.queries == 0
    content_id, image, source = device.read_artwork("different", b"old bytes")
    assert (content_id, source, art.queries) == ("old", "thumbnail", 1)
    assert Image.open(io.BytesIO(image)).format == "JPEG"


def test_readback_rejects_artwork_changed_mid_check(tmp_path, monkeypatch, image_bytes):
    device = Television(Config(tv={"ip": "192.168.1.50"}).tv, tmp_path)
    art = Art(thumbnail=image_bytes)
    values = iter([{"content_id": "old"}, {"content_id": "changed"}])
    art.get_current = lambda: next(values)
    monkeypatch.setattr(device, "artwork_reader", lambda: art)
    with pytest.raises(ValueError, match="changed"):
        device.read_artwork()


def test_thumbnail_transport_cannot_redirect_or_allocate_unbounded_data():
    reader = ArtworkReader("192.168.1.50", timeout=5)
    with pytest.raises(ValueError, match="address"):
        reader._open_d2d_socket({"ip": "127.0.0.1", "port": 80})
    with pytest.raises(ValueError, match="port"):
        reader._open_d2d_socket({"ip": "192.168.1.50", "port": 65536})
    with pytest.raises(ValueError, match="size"):
        reader._recv_exact(None, MAX_THUMBNAIL + 65537)
    reader.deadline = 0
    with pytest.raises(TimeoutError):
        reader._remaining()
    with pytest.raises(ValueError):
        thumbnail_jpeg(b"x" * (MAX_THUMBNAIL + 1))


def test_successful_publish_and_check_are_separate_from_preview(configs, library, monkeypatch):
    cfg = Config(tv={"ip": "192.168.1.50"})
    configs.save(cfg)
    calls = []
    class Screen:
        def status(self): return {"paired": True, "known": True, "awake": True, "playing": False}
        def publish(self, image, owned, record, keep=()):
            record("new")
            return "new", ["new"], False
        def read_artwork(self, known_id, known_image):
            calls.append((known_id, known_image))
            return known_id, known_image, "upload"
    screen = Screen()
    controller = Controller(configs, library, tv_factory=lambda _: screen, capture=lambda *a, **kw: b"scheduled image")
    (configs.root / "screen-preview.jpg").write_bytes(b"different preview")
    assert controller.tick(force=True) == "unconfirmed"
    assert library.state("last_display") is None
    assert library.state("tv_artwork") is None
    assert controller.tick() == "unchanged"  # no repeated upload every scheduler tick
    assert controller.check_artwork() == "checked"
    assert calls == [("new", b"scheduled image")]
    assert (configs.root / "tv-artwork.jpg").read_bytes() == b"scheduled image"
    assert (configs.root / "screen-preview.jpg").read_bytes() == b"different preview"
    assert library.state("tv_artwork")["image_sha256"] == hashlib.sha256(b"scheduled image").hexdigest()
    before = library.state("tv_artwork")
    screen.read_artwork = lambda *args: (_ for _ in ()).throw(OSError("PRIVATE-REPLY"))
    assert controller.check_artwork() == "check-failed"
    assert library.state("tv_artwork") == before
    assert (configs.root / "tv-artwork.jpg").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("state,result", [
    ({"paired": False}, "unpaired"), ({"known": False}, "unknown"),
    ({"awake": False}, "asleep"), ({"playing": True}, "watching"),
])
def test_check_never_wakes_or_reads_while_asleep_or_watching(configs, library, state, result):
    configs.save(Config(tv={"ip": "192.168.1.50"}))
    status = {"paired": True, "known": True, "awake": True, "playing": False, **state}
    screen = SimpleNamespace(status=lambda: status)
    controller = Controller(configs, library, tv_factory=lambda _: screen)
    assert controller.check_artwork() == result
    assert not (configs.root / "tv-artwork.jpg").exists()


@pytest.mark.parametrize("shown,cleaned", [("new", True), ("other", False)])
def test_check_that_confirms_our_upload_finishes_cleanup(configs, library, shown, cleaned):
    configs.save(Config(tv={"ip": "192.168.1.50"}))
    removed = []
    class Screen:
        def status(self): return {"paired": True, "known": True, "awake": True, "playing": False}
        def publish(self, image, owned, record, keep=()):
            record("new")
            return "new", ["old", "new"], False  # the TV was slow to confirm
        def read_artwork(self, known_id, known_image): return shown, known_image, "upload"
        def remove_old(self, owned, selected, keep=()):
            removed.append((owned, selected))
            return [selected]
    controller = Controller(configs, library, tv_factory=lambda _: Screen(), capture=lambda *a, **kw: b"image")
    library.set_state("owned_images:" + configs.load().tv.identity, ["old"])
    assert controller.tick(force=True) == "unconfirmed"
    assert controller.check_artwork() == "checked"
    assert removed == ([(["old", "new"], "new")] if cleaned else [])
    assert library.state("owned_images:" + configs.load().tv.identity) == (["new"] if cleaned else ["old", "new"])


@pytest.mark.parametrize("method", ["send", "sendall"])
def test_artwork_socket_writes_are_bounded_by_the_deadline(method):
    timeouts, sent = [], []
    raw = SimpleNamespace(settimeout=timeouts.append, send=sent.append, sendall=sent.append)
    sock = ArtworkSocket(raw, time.monotonic() + 15)
    getattr(sock, method)(b"frame")
    assert sent == [b"frame"] and 0 < timeouts[0] <= 5
    sock.deadline = 0
    with pytest.raises(TimeoutError):
        getattr(sock, method)(b"late")
    assert sent == [b"frame"]


def encoded(size, fmt):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, fmt)
    return output.getvalue()


def test_thumbnail_accepts_only_allowlisted_bounded_images():
    assert thumbnail_jpeg(encoded((40, 30), "PNG"))[:2] == b"\xff\xd8"
    for fmt in ("GIF", "TIFF"):
        with pytest.raises((ValueError, OSError)):  # PIL reports unknown formats as OSError
            thumbnail_jpeg(encoded((40, 30), fmt))
    with pytest.raises(ValueError, match="dimensions"):
        thumbnail_jpeg(encoded((3000, 3000), "PNG"))
