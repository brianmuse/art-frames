"""Settings Export and Import: a .zip of the household's settings.

A backup always holds settings.json (every TV, schedule and option) and each layout's settings.
Private values stay out unless the person ticks "Include private values":

- the sensor encryption keys and the TV MAC addresses (in settings.json),
- layout values of type "secret", such as API keys and private calendar links,
- legacy-dashboard.json (older calendar settings),
- the TV pairing tokens. With them, the TVs don't ask to pair again after a move.

The password, the session key and the artwork library are never in a backup.

A backup without private values still holds the TV IP addresses: Art Frames needs them to
reach the TVs.

Import checks the whole file before it writes anything. It keeps the current settings as
settings.before-import-<time>.json, then writes each file atomically: layout settings and pairings
first, settings.json last. A private value that the backup leaves out (such as a sensor key or a
TV MAC address) keeps its current value.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import SCOPE, Config, private_write

FORMAT = "art-frames-backup"
FORMAT_VERSION = 1
MAX_ENTRIES = 500
MAX_FILE = 2_000_000
TOKEN_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}\.txt")
DISPLAY_ID = re.compile(r"[a-f0-9]{32}")


class BackupError(ValueError):
    """A backup Art Frames refuses. The message is a sentence for the page."""


def _blank_private(raw):
    """Remove the sensor keys and the TV MAC addresses from exported settings."""
    raw.setdefault("sensor", {})["key"] = ""
    raw.setdefault("tv", {})["mac"] = ""
    for display in raw.get("displays", []):
        display.setdefault("sensor", {})["key"] = ""
        display.setdefault("tv", {})["mac"] = ""


def _keep_private(raw, current):
    """Fill each sensor key and TV MAC address the backup left out with the current value."""
    def fill(target, source):
        for section, key in (("sensor", "key"), ("tv", "mac")):
            part = target.get(section)
            if part is None:
                part = target[section] = {}
            if isinstance(part, dict) and not part.get(key):
                part[key] = (source.get(section) or {}).get(key, "")

    fill(raw, current)
    by_id = {item.get("id"): item for item in current.get("displays", [])}
    for display in raw.get("displays", []):
        if isinstance(display, dict):
            fill(display, by_id.get(display.get("id"), {}))


def _token_files(root: Path):
    """(name in the zip, path) of each TV pairing token."""
    files = []
    for path in sorted((root / "tokens").glob("*.txt")):
        files.append((f"tokens/{path.name}", path))
    for path in sorted((root / "displays").glob("*/tokens/*.txt")):
        files.append((f"displays/{path.parent.parent.name}/tokens/{path.name}", path))
    return [(name, path) for name, path in files if path.is_file() and not path.is_symlink()]


def export(root: Path, configs, apps=(), *, private=False):
    """The backup as .zip bytes. `apps` are the installed layouts (apps.usable), for their secret fields."""
    raw = json.loads(configs.load().model_dump_json())
    if not private:
        _blank_private(raw)
    files = {"settings.json": json.dumps(raw, indent=2)}
    scopes = {item.scope: item for item in apps}
    for path in sorted((root / "layout-settings").glob("*/config.json")):
        scope = path.parent.name
        if path.is_symlink() or not re.fullmatch(SCOPE, scope) or path.stat().st_size > MAX_FILE:
            continue
        if private:
            files[f"layout-settings/{scope}/config.json"] = path.read_text()
        elif scope in scopes:
            store = scopes[scope].store(root)
            public, _ = store.public_values(store.manifest())
            files[f"layout-settings/{scope}/config.json"] = json.dumps(public, indent=2)
    if private:
        legacy = root / "legacy-dashboard.json"
        if legacy.is_file() and not legacy.is_symlink():
            files["legacy-dashboard.json"] = legacy.read_text()
        for name, path in _token_files(root):
            files[name] = path.read_text()
    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "app_version": __version__,
        "created": datetime.now(UTC).isoformat(timespec="seconds"), "private": private,
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, text in files.items():
            archive.writestr(name, text)
    return stream.getvalue()


def _merge(existing, values):
    """Layout values from a backup over the current ones. A list row keeps the private fields the backup left out."""
    merged = dict(existing)
    for key, value in values.items():
        old = merged.get(key)
        if isinstance(value, list) and isinstance(old, list):
            rows = {row.get("id"): row for row in old if isinstance(row, dict)}
            value = [{**rows.get(row.get("id"), {}), **row} if isinstance(row, dict) else row for row in value]
        merged[key] = value
    return merged


def _allowed(name):
    parts = name.split("/")
    if name in ("manifest.json", "settings.json", "legacy-dashboard.json"):
        return True
    if len(parts) == 3 and parts[0] == "layout-settings" and re.fullmatch(SCOPE, parts[1]) and parts[2] == "config.json":
        return True
    if len(parts) == 2 and parts[0] == "tokens" and TOKEN_NAME.fullmatch(parts[1]):
        return True
    return (len(parts) == 4 and parts[0] == "displays" and DISPLAY_ID.fullmatch(parts[1]) and parts[2] == "tokens"
            and TOKEN_NAME.fullmatch(parts[3]))


def read(data: bytes):
    """The files in a backup: {name: text}. Raises BackupError for anything that is not an Art Frames backup."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise BackupError("This file is not an Art Frames backup. Choose the .zip file that Export made.") from error
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise BackupError("This backup has too many files.")
        files = {}
        for entry in entries:
            if entry.is_dir():
                continue
            if not _allowed(entry.filename):
                raise BackupError(f"This backup has a file Art Frames does not know: {entry.filename[:80]}")
            if entry.file_size > MAX_FILE:
                raise BackupError("A file in this backup is too large.")
            try:
                files[entry.filename] = archive.read(entry).decode()
            except (UnicodeDecodeError, zipfile.BadZipFile, RuntimeError) as error:
                raise BackupError("This backup is damaged. Export a new one.") from error
    try:
        manifest = json.loads(files.get("manifest.json", ""))
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("This file is not an Art Frames backup. Choose the .zip file that Export made.")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("A newer Art Frames made this backup. Update Art Frames, then import it.")
    if "settings.json" not in files:
        raise BackupError("This backup has no settings.")
    return files


def restore(root: Path, configs, data: bytes, *, now=None):
    """Check a backup, then apply it. Returns {"settings", "apps", "pairings"} counts. Changes nothing on error."""
    files = read(data)
    try:
        raw = json.loads(files["settings.json"])
        if not isinstance(raw, dict):
            raise ValueError
    except ValueError as error:
        raise BackupError("The settings in this backup are not valid for this version of Art Frames.") from error
    app_values = {}
    for name, text in files.items():
        if name.startswith("layout-settings/"):
            try:
                values = json.loads(text)
            except ValueError as error:
                raise BackupError("A layout's settings in this backup are damaged.") from error
            if not isinstance(values, dict):
                raise BackupError("A layout's settings in this backup are damaged.")
            app_values[name.split("/")[1]] = values
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    with configs._lock:
        current = json.loads(configs.load().model_dump_json())
        _keep_private(raw, current)
        try:
            cfg = Config.model_validate(raw)
        except ValueError as error:
            if "wake needs both" in str(error):
                raise BackupError("A schedule in this backup wakes a TV, but the backup has no MAC address for it. "
                                  "Export again with private values, or add the MAC address first.") from error
            raise BackupError("The settings in this backup are not valid for this version of Art Frames.") from error
        kept = root / f"settings.before-import-{stamp}.json"
        written = False
        try:
            if configs.overrides.exists():
                private_write(kept, configs.overrides.read_bytes())
                written = True
        except OSError as error:
            raise BackupError("Art Frames could not write to its data folder. Nothing changed.") from error
        try:
            pairings = _write_files(root, files, app_values)
            configs.save(cfg)  # last: a failure before this point leaves the old settings in place
        except OSError as error:
            if written:
                raise BackupError(f"Art Frames could not finish the import. Your settings before the import are in "
                                  f"{kept.name} in the data folder.") from error
            raise BackupError("Art Frames could not finish the import. Some layout settings or TV pairings may have "
                              "changed. The TV settings did not change.") from error
    return {"settings": 1, "apps": len(app_values), "pairings": pairings}


def _write_files(root, files, app_values):
    """Write the layout settings, older calendar settings and TV pairings of a backup. Returns the pairing count."""
    for scope, values in app_values.items():
        path = root / "layout-settings" / scope / "config.json"
        if path.is_symlink():
            continue
        try:
            existing = json.loads(path.read_bytes()) if path.exists() else {}
        except ValueError:
            existing = {}
        merged = _merge(existing if isinstance(existing, dict) else {}, values)
        private_write(path, json.dumps(merged, indent=2).encode())
    pairings = 0
    for name, text in files.items():
        if name == "legacy-dashboard.json" or "/tokens/" in name or name.startswith("tokens/"):
            path = root / name
            if path.is_symlink():
                continue
            private_write(path, text.encode())
            pairings += "tokens/" in name
    return pairings
