"""TV channel connections with a total deadline, and bounded, read-only artwork retrieval."""

import io
import ipaddress
import socket
import time
import warnings

from PIL import Image
from samsungtvws import helper
from samsungtvws.art import SamsungTVArt
from samsungtvws.event import (
    IGNORE_EVENTS_AT_STARTUP,
    MS_CHANNEL_CONNECT_EVENT,
    MS_CHANNEL_READY_EVENT,
    MS_CHANNEL_UNAUTHORIZED,
)
from samsungtvws.exceptions import ConnectionFailure, UnauthorizedError
from samsungtvws.helper import get_ssl_context
from websocket import create_connection

MAX_THUMBNAIL = 5_000_000
CHUNK = 65536


class ArtworkSocket:
    """A socket that checks a total deadline before each send and receive.

    The library's own timeout starts again on each frame, so a TV that keeps sending
    unrelated events could hold a reply loop open forever. This wrapper stops that.
    `step` is the longest wait for one send or receive (None: up to the deadline).
    `max_bytes` caps all bytes received; None means no cap.
    """

    def __init__(self, sock, deadline, *, step=5, max_bytes=8_000_000):
        self.sock, self.deadline, self.step = sock, deadline, step
        # The default cap covers the HTTP upgrade, JSON/base64 replies and startup frames.
        self.remaining_bytes = max_bytes

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def _bound(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("TV art channel timed out")
        self.sock.settimeout(remaining if self.step is None else min(remaining, self.step))

    def send(self, data, *args):
        self._bound()
        return self.sock.send(data, *args)

    def sendall(self, data, *args):
        # Send a large upload in parts, so each part checks the deadline again.
        view = memoryview(data)
        if len(view) <= CHUNK:
            self._bound()
            return self.sock.sendall(data, *args)
        for start in range(0, len(view), CHUNK):
            self._bound()
            self.sock.sendall(view[start:start + CHUNK], *args)
        return None

    def recv(self, size, *args):
        self._bound()
        if self.remaining_bytes is None:
            return self.sock.recv(min(size, CHUNK), *args)
        if self.remaining_bytes <= 0:
            raise ValueError("Art channel messages exceed size limit")
        data = self.sock.recv(min(size, self.remaining_bytes, CHUNK), *args)
        self.remaining_bytes -= len(data)
        return data


class BoundedConnection:
    """Opens a TV websocket channel like SamsungTVWSConnection.open, but within `self.deadline`.

    The library's startup loop reads until the TV sends a non-startup event. Its socket timeout
    starts again on each frame, so a TV that keeps sending startup events could hold it forever.
    Here every send and receive on the channel checks the same total deadline.
    """

    MAX_MESSAGE_BYTES = None  # no cap on the main socket: content lists and replies can be large

    def _connect_bounded(self):
        """Connect and read the startup events. Sets and returns self.connection."""
        sock = socket.create_connection((self.host, self.port), timeout=self._remaining())
        try:
            if self._is_ssl_connection():
                sock = get_ssl_context().wrap_socket(sock)
            bounded = ArtworkSocket(sock, self.deadline, step=self.timeout, max_bytes=self.MAX_MESSAGE_BYTES)
            connection = create_connection(
                self._format_websocket_url(self.endpoint), timeout=self._remaining(),
                connection="Connection: Upgrade", socket=bounded, redirect_limit=0,
            )
        except Exception:
            sock.close()
            raise
        self.connection = connection
        try:
            event = None
            while event is None or event in IGNORE_EVENTS_AT_STARTUP:
                response = helper.process_api_response(connection.recv())
                event = response.get("event", "*")
                self._websocket_event(event, response)
            if event == MS_CHANNEL_UNAUTHORIZED:
                raise UnauthorizedError(response)
            if event != MS_CHANNEL_CONNECT_EVENT:
                raise ConnectionFailure(response)
            self._check_for_token(response)
            return connection
        except Exception:
            self.close()
            raise

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("TV channel timed out")
        return remaining if self.timeout is None else min(remaining, self.timeout)


class BoundedArt(BoundedConnection, SamsungTVArt):
    """The TV's art channel, with a total deadline of `seconds` for all the work it does.

    It opens and works like SamsungTVArt, and each reply still waits at most `timeout` seconds.
    Once the deadline passes, each send and receive raises TimeoutError (or a library error that
    wraps it), like any other network failure. The upload and thumbnail socket has the same deadline.
    """

    def __init__(self, *args, seconds, **kwargs):
        super().__init__(*args, **kwargs)
        self.deadline = time.monotonic() + seconds

    def open(self):
        if self.connection:
            return self.connection
        connection = self._connect_bounded()
        try:
            event, frame = self._recv_frame()
            if event != MS_CHANNEL_READY_EVENT:
                raise ConnectionFailure(frame)
            return connection
        except Exception:
            self.close()
            raise

    def _recv_frame(self):
        if self.connection:
            self.connection.settimeout(self._remaining())
        return super()._recv_frame()

    def _connect_d2d(self, host, port, secured):
        sock = socket.create_connection((host, port), timeout=self._remaining())
        try:
            return get_ssl_context().wrap_socket(sock) if secured else sock
        except Exception:
            sock.close()
            raise

    def _open_d2d_socket(self, conn_info):
        # The upload and thumbnail socket. Each send and receive checks the same total deadline.
        sock = self._connect_d2d(conn_info["ip"], int(conn_info["port"]), conn_info.get("secured", False))
        return ArtworkSocket(sock, self.deadline, step=self.timeout, max_bytes=None)


class ArtworkReader(BoundedArt):
    """Read-only artwork checks: 15 seconds in all, with byte caps and a fixed thumbnail address."""

    MAX_MESSAGE_BYTES = 8_000_000  # HTTP upgrade, JSON/base64 and startup frames

    def __init__(self, *args, **kwargs):
        super().__init__(*args, seconds=15, **kwargs)
        self.remaining_bytes = MAX_THUMBNAIL + 65536

    def _open_d2d_socket(self, conn_info):
        # A thumbnail reply must not redirect the server to another LAN service.
        if ipaddress.ip_address(conn_info["ip"]) != ipaddress.ip_address(self.host):
            raise ValueError("Unexpected thumbnail address")
        if not 1 <= int(conn_info["port"]) <= 65535:
            raise ValueError("Invalid thumbnail port")
        return self._connect_d2d(self.host, int(conn_info["port"]), conn_info.get("secured", False))

    def _recv_exact(self, sock, size):
        if size < 0 or size > self.remaining_bytes:
            raise ValueError("Thumbnail exceeds size limit")
        self.remaining_bytes -= size
        data = bytearray()
        while len(data) < size:
            sock.settimeout(self._remaining())
            chunk = sock.recv(min(size - len(data), 65536))
            if not chunk:
                raise OSError("Incomplete thumbnail")
            data.extend(chunk)
        return bytes(data)


def thumbnail_jpeg(data):
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_THUMBNAIL:
        raise ValueError("Invalid thumbnail")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP"]) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or image.width * image.height > 8_500_000:
                raise ValueError("Invalid thumbnail dimensions")
            image.thumbnail((1920, 1080))
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=90)
            return output.getvalue()
