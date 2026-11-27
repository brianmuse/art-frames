"""The "Update available" notice. Art Frames never updates itself.

When the check is on, Art Frames asks GitHub for the newest release at most once a day. The
request carries no ID, no settings and no version: only a fixed User-Agent. The answer is
saved in <data>/updates.json.

The check is on by default for a new install. It is off by default for an install with a
password file, which is how installs from before this feature run, so an existing install
never starts calling out without its owner turning it on. ART_FRAMES_UPDATE_CHECK=1 or 0
overrides the switch on the Server page.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import private_write

log = logging.getLogger(__name__)

REPOSITORY = "brianmuse/art-frames"
RELEASES = f"https://github.com/{REPOSITORY}/releases"
LATEST_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
STATE_FILE = "updates.json"
DAY = 24 * 3600
TICK = 3600  # how often the loop looks at the clock
VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse(tag):
    """(major, minor, patch), or None for a tag that is not a plain release such as v1.2.3."""
    match = VERSION.match((tag or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def newer(tag, current=__version__):
    latest, now = parse(tag), parse(current)
    return bool(latest and now and latest > now)


def fetch_latest(url=LATEST_API, timeout=10, *, opener=urllib.request.urlopen):
    """The newest release from GitHub: {"tag": ..., "url": ...}, or None when there is no release yet.
    GitHub answers 404 for a repository with no published release."""
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Art-Frames"})
    try:
        with opener(request, timeout=timeout) as response:  # noqa: S310 (a fixed https URL)
            data = json.loads(response.read(200_000))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    tag = data.get("tag_name") if isinstance(data, dict) else None
    link = data.get("html_url") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not parse(tag):
        raise ValueError("GitHub answered without a release version")
    if not isinstance(link, str) or not link.startswith(RELEASES + "/"):
        link = RELEASES
    return {"tag": tag, "url": link}


def forced_setting(value):
    """ART_FRAMES_UPDATE_CHECK as True, False or None (not set)."""
    value = (value or "").strip().lower()
    if value in ("1", "on", "true", "yes"):
        return True
    if value in ("0", "off", "false", "no"):
        return False
    return None


class Updates:
    def __init__(self, root: Path, *, default_on, forced=None, fetch=fetch_latest, clock=time.time):
        self.path = root / STATE_FILE
        self.default_on, self.forced, self.fetch, self.clock = default_on, forced, fetch, clock
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = None

    def _state(self):
        try:
            value = json.loads(self.path.read_bytes())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, **changes):
        with self.lock:
            private_write(self.path, json.dumps({**self._state(), **changes}, indent=2).encode())

    @property
    def enabled(self):
        if self.forced is not None:
            return self.forced
        saved = self._state().get("check")
        return self.default_on if saved is None else bool(saved)

    def set_enabled(self, on):
        if self.forced is not None:
            raise ValueError("ART_FRAMES_UPDATE_CHECK sets this switch. Change it there.")
        self._save(check=bool(on))

    def view(self):
        """What the Server page and the sidebar show."""
        state = self._state()
        latest = state.get("latest") if self.enabled else None
        return {
            "enabled": self.enabled,
            "forced": self.forced is not None,
            "current": __version__,
            "latest": latest,
            "url": state.get("url") or RELEASES,
            "available": bool(latest) and newer(latest),
            "checked_at": state.get("checked_at") if self.enabled else None,
            "failed": bool(state.get("failed")) if self.enabled else False,
        }

    def due(self):
        return self.enabled and self.clock() - float(self._state().get("checked_at") or 0) >= DAY

    def check(self):
        """Ask GitHub now. Does nothing when the check is off. A failure is saved and logged, never raised."""
        if not self.enabled:
            return False
        try:
            latest = self.fetch()
        except Exception as error:
            log.info("The update check could not reach GitHub (%s)", type(error).__name__)
            self._save(checked_at=self.clock(), failed=True)
            return False
        if latest is None:
            self._save(latest=None, url=RELEASES, checked_at=self.clock(), failed=False)
            log.info("The update check found no Art Frames release yet")
            return True
        self._save(latest=latest["tag"], url=latest["url"], checked_at=self.clock(), failed=False)
        if newer(latest["tag"]):
            log.info("Art Frames %s is out. You run %s. See %s", latest["tag"], __version__, latest["url"])
        return True

    def _loop(self):
        wait = 60  # the first check waits a minute after start, so a restart loop never floods GitHub
        while not self.stopping.wait(wait):
            wait = TICK
            if self.due():
                self.check()

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="update-check", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
