"""Bounded JSON frames over a private Unix socket shared only by the two containers."""

import errno
import json
import struct
import time
from pathlib import Path

MAX_MESSAGE = 40_000_000
# JPEG quality for the 3840x2160 image sent to the TV. 85 is about 30% smaller than 92
# with no visible change to text, so the morning upload finishes sooner.
JPEG_QUALITY = 85


def describe(error, *own_files):
    """Our fixed ValueError text, or only the class name: library messages may quote page markup or CSS."""
    trace = error.__traceback__
    while trace is not None and trace.tb_next is not None:
        trace = trace.tb_next
    own = {Path(__file__).resolve().parent, *(Path(name).resolve().parent for name in own_files)}
    if type(error) is ValueError and trace is not None and Path(trace.tb_frame.f_code.co_filename).resolve().parent in own:
        return f"ValueError: {str(error)[:200]}"
    if isinstance(error, OSError) and error.errno in errno.errorcode:
        # The error code (for example ENOSPC) says why without quoting a path or page text.
        return f"{type(error).__name__} {errno.errorcode[error.errno]}"
    return type(error).__name__


def encode(message):
    raw = json.dumps(message, separators=(",", ":")).encode()
    if len(raw) > MAX_MESSAGE:
        raise ValueError("Render message exceeds its size limit")
    return struct.pack("!I", len(raw)) + raw


def decode(raw):
    message = json.loads(raw)
    if not isinstance(message, dict):
        raise ValueError("Invalid render message")
    return message


def receive(sock, deadline):
    def exact(size):
        parts = bytearray()
        while len(parts) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Website render exceeded its deadline")
            sock.settimeout(remaining)
            piece = sock.recv(min(size - len(parts), 65536))
            if not piece:
                raise ConnectionError("Renderer disconnected")
            parts.extend(piece)
        return parts

    size = struct.unpack("!I", exact(4))[0]
    if size > MAX_MESSAGE:
        raise ValueError("Render message exceeds its size limit")
    return decode(exact(size))


async def receive_async(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if size > MAX_MESSAGE:
        raise ValueError("Render message exceeds its size limit")
    return decode(await reader.readexactly(size))


async def send_async(writer, message):
    writer.write(encode(message))
    await writer.drain()
