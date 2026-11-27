"""Installed layouts, and which layout each TV shows at a given time.

Every layout is a Layout with an ID, a settings scope, a socket and an artwork need. Only where
the first layout lives comes from the installer: its manifest is in `sites/installed`, named
by the DAYFRAME_LAYOUT_* environment, and it runs as the `website` service. More layouts are
listed in DAYFRAME_APPS as "<id>=<settings scope>" pairs; each has its manifest in
`<sites>/apps/<id>/layout.json` and its own socket.

Layouts run only on a static Compose profile: its website service gives each layout a socket and
sets DAYFRAME_DEFAULT_SITE_SOCKET. A browser profile installs no layouts; a stale layout entry in
its environment is ignored.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import names
from .config import (
    APP_ID,
    DEFAULT_SWAP_SECONDS,
    SCOPE,
    WEEK_MINUTES,
)
from .env import env, require
from .layouts import LayoutStore, Manifest, load_manifest

log = logging.getLogger(__name__)
_manifests = {}  # path -> ((mtime_ns, size), Manifest)
_warned = set()  # failures already logged; installed() runs every 5 s per TV
# The built-in base layer's ID. An installed layout with this ID is refused (see installed()), so a block,
# a hold, a stored rotation image or a preview record that names it always means the built-in Artwork.
ARTWORK_ID = "artwork"


def _warn_once(key, message, *args):
    """Log a failure once, until it changes (a new key)."""
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def _stamp(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@dataclass(frozen=True)
class App:
    id: str
    scope: str  # the key for the layout's saved values, shared and per TV
    manifest_path: Path
    socket: str | None
    manifest: Manifest = field(compare=False)
    artwork: bool = False  # the layout shows the day's artwork
    builtin: bool = False  # the built-in Artwork: no website, socket, settings or renderer

    @property
    def name(self):
        return self.manifest.name

    def store(self, root):
        """The layout's settings store."""
        return LayoutStore(root, self)


# The base layer: the day's artwork, full frame, composed in this process (see artwork.py). Outside the
# layout blocks, every TV with installed layouts shows it.
ARTWORK = App(ARTWORK_ID, ARTWORK_ID, Path(), None, Manifest(id=ARTWORK_ID, name="Artwork", artwork=True),
              artwork=True, builtin=True)


def _manifest(path, app_id):
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _manifests.get(path)
    if cached and cached[0] == stamp and (app_id is None or cached[1].id == app_id):
        return cached[1]
    manifest = load_manifest(path, app_id)
    _manifests[path] = (stamp, manifest)
    return manifest


def starter_socket():
    """The `website` service's socket: the first layout's, or the starter website's without an installed layout."""
    return env("DEFAULT_SITE_SOCKET") or None


def supported():
    """Whether this host can run layouts: only a static profile has a website service."""
    return starter_socket() is not None


def _first():
    """The layout in `sites/installed`, with the ID, scope and artwork need the installer recorded."""
    path = Path(require("LAYOUT_MANIFEST"))
    manifest = _manifest(path, env("LAYOUT_ID") or None)
    if manifest.id == ARTWORK_ID:
        raise ValueError("The layout ID artwork is reserved for the built-in Artwork")
    # The scope outlives a layout ID rename: the installer keeps the first scope it chose.
    scope = env("LAYOUT_CONFIG_ID") or manifest.id
    if not re.fullmatch(SCOPE, scope):
        raise ValueError("Invalid installed layout settings scope")
    artwork = env("LAYOUT_ARTWORK", "")
    if artwork not in {"", "0", "1"}:
        raise ValueError("Invalid installed layout artwork capability")
    return App(manifest.id, scope, path, starter_socket(), manifest,
               artwork=artwork == "1" if artwork else manifest.artwork)


def usable(cfg):
    """The installed layouts a TV can show: none when it shows a custom website folder."""
    return [] if cfg.site.path else installed()


def installed():
    """Every installed layout, the first layout first. A layout with a broken manifest is left out."""
    result = []
    first = env("LAYOUT_MANIFEST")
    if not supported():
        if first or env("APPS", "").strip():
            _warn_once(("profile",), "Ignored the installed layouts: layouts need a static Compose profile")
        return result
    if first:
        try:
            result.append(_first())
        except (ValueError, OSError) as error:
            _warn_once((first, _stamp(Path(first)), type(error).__name__),
                       "The first installed layout is unavailable: %s", type(error).__name__)
    sites = Path(env("SITES_DIR", "sites"))
    sockets = env("APPS_SOCKET_DIR", names.APPS_SOCKET_DIR)
    for pair in env("APPS", "").split():
        app_id, _, scope = pair.partition("=")
        if (not re.fullmatch(APP_ID, app_id) or not re.fullmatch(SCOPE, scope) or app_id == ARTWORK_ID
                or app_id in {app.id for app in result}):
            _warn_once(("entry", pair), "Ignored an invalid entry in DAYFRAME_APPS")
            continue
        path = sites / "apps" / app_id / "layout.json"
        try:
            manifest = _manifest(path, app_id)
        except (ValueError, OSError) as error:
            _warn_once((str(path), _stamp(path), type(error).__name__),
                       "Layout %s is unavailable: %s", app_id, type(error).__name__)
            continue
        result.append(App(app_id, scope, path, f"{sockets}/{app_id}/site.sock", manifest, artwork=manifest.artwork))
    return result


def earlier_scopes(app):
    """The scopes the layout had under earlier names of its source, from the installer's
    .earlier-scopes file next to its manifest. Only "<first 40 characters of the ID>-<32 hex>"."""
    path = app.manifest_path.parent / names.EARLIER_SCOPES_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return []
        lines = path.read_text().split()
    except (OSError, UnicodeDecodeError):
        return []
    pattern = re.escape(app.id[:40]) + "-[0-9a-f]{32}"
    return [line for line in lines if re.fullmatch(pattern, line) and line != app.scope]


def keep_tv_options(configs, available):
    """Give each TV back its options for a layout whose source was renamed.

    Each TV keeps its options for a layout under the layout's scope, and a renamed source gives
    the layout a new scope. For each TV with no options under the new scope, copy them from an
    earlier scope (earlier_scopes). Saved options are never replaced, and a second run does
    nothing. scripts/keep-layout-settings.sh does the same for the layout's own settings.
    Returns how many TV options were copied.
    """
    earlier = {app.id: earlier_scopes(app) for app in available}
    if not any(earlier.values()):
        return 0
    copied = []

    def update(raw):
        copied.clear()
        for name, values in [(raw["tv_name"], raw["app_settings"]),
                             *((tv["name"], tv["app_settings"]) for tv in raw["displays"])]:
            for app in available:
                old = next((scope for scope in earlier[app.id] if scope in values), None)
                if old is None or app.scope in values:
                    continue
                values[app.scope] = dict(values[old])
                if len(values) > 8:  # TVOptions.app_settings keeps at most eight layouts
                    del values[old]
                copied.append((app.id, name))

    cfg = configs.load()
    pending = [cfg.app_settings, *(tv.app_settings for tv in cfg.displays)]
    if not any(app.scope not in values and any(scope in values for scope in earlier[app.id])
               for values in pending for app in available):
        return 0
    configs.mutate(update)
    for app_id, name in copied:
        log.info("Layout %s: restored the options of TV %s from an earlier install.", app_id, name)
    return len(copied)


def sources(cfg, available=None):
    """What a TV's schedule can show: the built-in Artwork first, then the installed layouts (`available`, by
    default usable(cfg)). Empty for a custom website folder, or without an installed layout: the starter
    website shows then, as before."""
    available = [app for app in (usable(cfg) if available is None else available) if not app.builtin]
    if cfg.site.path or not available:
        return []
    return [ARTWORK, *available]


def find(app_id, available=None):
    available = installed() if available is None else available
    return next((app for app in available if app.id == app_id), None)


def configure(cfg, app):
    """This TV's settings with the site entry and artwork need of `app`. None keeps `cfg`."""
    if app is None or app.builtin:
        return cfg
    cfg = cfg.model_copy(deep=True)
    cfg.site.entry = app.manifest.entry
    cfg.site.providers = ["art"] if app.artwork else []
    return cfg


@dataclass
class Plan:
    """What a TV shows now: one layout, or a rotation, and when that changes next.

    `apps` is empty for a custom website (`custom_site`) or when no layout is installed. Outside the
    blocks, `apps` is the built-in Artwork. `missing` lists the blocks' layout IDs that are no longer
    installed. When none of them is installed, `fallback` is True and `apps` holds Artwork instead.
    """

    apps: list[App]
    swap_seconds: int = DEFAULT_SWAP_SECONDS
    key: str = "default"  # names the active slot, so a new slot starts a new rotation
    next_change: datetime | None = None
    next_apps: list[App] = field(default_factory=list)
    custom_site: bool = False
    missing: list[str] = field(default_factory=list)
    fallback: bool = False

    @property
    def rotate(self):
        return len(self.apps) >= 2

    @property
    def app(self):
        """The layout shown now, or a rotation's first layout. None without an installed layout."""
        return self.apps[0] if self.apps else None


def week_minute(now):
    return now.weekday() * 1440 + now.hour * 60 + now.minute


def apps_at(week, minute):
    """The layout IDs of the blocks at `minute` of the week (Monday 00:00 is 0). Empty: Artwork shows."""
    minute %= WEEK_MINUTES
    return week.day(minute // 1440).apps_at(minute % 1440)


def _edges(week):
    """The minutes of the week where what shows can change: each block edge, and each midnight."""
    return sorted({day * 1440 + edge for day in range(7) for edge in [0, *week.day(day).edges()]} - {WEEK_MINUTES})


def stretch(week, now):
    """(start, next change) of the time around `now` with the same layouts, in minutes of the week from
    `now`'s week. start is None when the layouts never change. next change is None then too."""
    minute = week_minute(now)
    shown, edges = apps_at(week, minute), _edges(week)
    later = sorted(((edge - minute) % WEEK_MINUTES or WEEK_MINUTES) for edge in edges)
    step = next((step for step in later if apps_at(week, minute + step) != shown), None)
    if step is None:
        return None, None
    earlier = sorted((minute - edge) % WEEK_MINUTES for edge in edges)
    back = next(back for back in earlier if apps_at(week, minute - back - 1) != shown)
    return minute - back, minute + step


def next_change(schedule, now):
    """The next time the shown layouts change, or None when they never do."""
    _, change = stretch(schedule.week, now)
    if change is None:
        return None
    return now.replace(second=0, microsecond=0) + timedelta(minutes=change - week_minute(now))


def _slot_apps(ids, available):
    """The installed layouts of these IDs. A missing layout is left out; with none left, the first source (Artwork)."""
    by_id = {app.id: app for app in available}
    return [by_id[item] for item in ids if item in by_id] or [available[0]]


def _slot_ids(schedule, now):
    """(layout IDs, switch seconds, rotation key) at `now`: the blocks' layouts, else Artwork.

    The saved `default` (the older "rest of the time" choice) is ignored: Artwork is the base layer."""
    week = schedule.week
    ids = apps_at(week, week_minute(now))
    if ids:
        start, _ = stretch(week, now)
        return ids, week.swap_seconds, f"blocks:{0 if start is None else start % WEEK_MINUTES}:{','.join(ids)}"
    return [ARTWORK_ID], week.swap_seconds, "default"


def plan(cfg, now, available=None):
    """What the TV shows at `now`. `available` are the installed layouts (default usable(cfg)); Artwork joins them.

    The day's own plan or every day's plan decides. No block at `now`: Artwork. One block: its layout.
    Two or more blocks: their layouts take turns.
    """
    available = sources(cfg, available)
    if not available:
        return Plan([], key="site", custom_site=bool(cfg.site.path))
    schedule = cfg.app_schedule
    ids, swap, key = _slot_ids(schedule, now)
    result = Plan(_slot_apps(ids, available), swap, key=key)
    installed_ids = {app.id for app in available}
    result.missing = [item for item in ids if item not in installed_ids]
    result.fallback = len(result.missing) == len(ids)
    result.next_change = next_change(schedule, now)
    if result.next_change is not None:
        following, _, _ = _slot_ids(schedule, result.next_change)
        result.next_apps = _slot_apps(following, available)
    return result


def uses(cfg, app, available=None):
    """Whether this TV's schedule shows the installed layout `app` at any time: in one of its blocks."""
    if not sources(cfg, available):
        return False
    return any(block.app == app.id for weekday in range(7) for block in cfg.app_schedule.week.day(weekday).blocks)
