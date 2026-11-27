"""Bounded framed input for the disposable HTML/CSS rendering process."""

import struct

from art_frames.ipc import MAX_MESSAGE, decode


def read_pipe(stream):
    header = stream.read(4)
    if len(header) != 4:
        raise ValueError("Incomplete render frame")
    length = struct.unpack("!I", header)[0]
    if length > MAX_MESSAGE:
        raise ValueError("Render frame is too large")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("Incomplete render frame")
    return decode(body)
