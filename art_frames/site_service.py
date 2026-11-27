"""An optional website's HTTP contract over a private, local Unix socket."""

import http.client
import json
import socket

from .names import SITE_HOST

MAX_ASSET = 25_000_000
MAX_CONTEXT = 40_000_000


class WebsitePending(ValueError):
    """The layout is preparing data; keep the existing preview until it is ready."""


class WebsiteConnection(http.client.HTTPConnection):
    def __init__(self, endpoint):
        super().__init__(SITE_HOST, timeout=20)
        self.endpoint = endpoint

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.endpoint)


def request(endpoint, path, context=None):
    body = None if context is None else json.dumps(context).encode()
    if body is not None and len(body) > MAX_CONTEXT:
        raise ValueError("Website page data exceeds its size limit")
    connection = WebsiteConnection(endpoint)
    try:
        connection.request(
            "GET" if body is None else "POST",
            path,
            body=body,
            headers={} if body is None else {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if response.status == 202:
            raise WebsitePending("The website is preparing its data")
        if response.status != 200:
            raise ValueError("The website service could not render this page or asset")
        result = response.read(MAX_ASSET + 1)
        if len(result) > MAX_ASSET:
            raise ValueError("Website asset exceeds its size limit")
        return response.getheader("Content-Type", "application/octet-stream"), result
    except (OSError, http.client.HTTPException) as error:
        raise ValueError("The default website service is unavailable; check its container") from error
    finally:
        connection.close()
