import io
import json
import struct

import pytest

from art_frames import site_service
from art_frames.ipc import MAX_MESSAGE, encode
from renderer.pipe import read_pipe


def test_website_service_sends_only_snapshot_and_closes(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, endpoint):
            assert endpoint == "/private-example/site.sock"

        def request(self, method, path, body, headers):
            calls.append((method, path, json.loads(body), headers))

        def getresponse(self):
            return self

        status = 200

        def getheader(self, name, default):
            return "text/html"

        def read(self, maximum):
            return b"<h1>My site</h1>"

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(site_service, "WebsiteConnection", Connection)
    context = {"now": "2030-01-02T08:00:00+00:00", "data": {"headline": "My site"}}
    assert site_service.request("/private-example/site.sock", "/index.html", context) == (
        "text/html",
        b"<h1>My site</h1>",
    )
    assert calls == [("POST", "/index.html", context, {"Content-Type": "application/json"}), "closed"]


@pytest.mark.parametrize("status,body", [(202, b"pending"), (404, b"missing"), (200, b"too many bytes")])
def test_website_service_rejects_error_and_oversized_response(monkeypatch, status, body):
    closed = []

    class Connection:
        def __init__(self, endpoint):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self

        def getheader(self, *args):
            return "text/css"

        def read(self, maximum):
            assert maximum == 9
            return body

        def close(self):
            closed.append(True)

    Connection.status = status
    monkeypatch.setattr(site_service, "MAX_ASSET", 8)
    monkeypatch.setattr(site_service, "WebsiteConnection", Connection)
    with pytest.raises(ValueError):
        site_service.request("example.sock", "/asset.css")
    assert closed == [True]


def test_oversized_snapshot_never_opens_website_connection(monkeypatch):
    monkeypatch.setattr(site_service, "MAX_CONTEXT", 8)
    monkeypatch.setattr(site_service, "WebsiteConnection", lambda endpoint: pytest.fail("Must not connect"))
    with pytest.raises(ValueError, match="size limit"):
        site_service.request("example.sock", "/index.html", {"data": "too large"})


def test_pipe_rejects_length_before_reading_body():
    class HeaderOnly(io.BytesIO):
        def read(self, size):
            assert size == 4
            return super().read(size)

    with pytest.raises(ValueError, match="too large"):
        read_pipe(HeaderOnly(struct.pack("!I", MAX_MESSAGE + 1)))
    assert read_pipe(io.BytesIO(encode({"type": "fetch"}))) == {"type": "fetch"}
    for data in (b"", b"\x00\x00", struct.pack("!I", 8) + b"{}"):
        with pytest.raises(ValueError, match="Incomplete"):
            read_pipe(io.BytesIO(data))
