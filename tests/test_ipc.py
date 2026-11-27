import struct
import time

import pytest

from art_frames.ipc import MAX_MESSAGE, decode, encode, receive


class Chunks:
    def __init__(self, data):
        self.data = bytearray(data)

    def settimeout(self, value):
        pass

    def recv(self, size):
        result = self.data[: min(3, size)]
        del self.data[: len(result)]
        return bytes(result)


def test_frame_reassembles_partial_socket_reads():
    assert (
        receive(
            Chunks(encode({"type": "fetch", "url": "http://dayframe.local/index.html"})), time.monotonic() + 2
        )["type"]
        == "fetch"
    )


def test_oversized_frame_is_rejected_before_allocating_body():
    with pytest.raises(ValueError):
        receive(Chunks(struct.pack("!I", MAX_MESSAGE + 1)), time.monotonic() + 2)


def test_incomplete_frame_and_expired_deadline_fail():
    with pytest.raises(ConnectionError):
        receive(Chunks(b"\x00\x00"), time.monotonic() + 2)
    with pytest.raises(TimeoutError):
        receive(Chunks(encode({})), time.monotonic() - 1)


def test_protocol_requires_object_payload():
    with pytest.raises(ValueError):
        decode(b"[]")
