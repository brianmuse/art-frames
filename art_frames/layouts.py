"""Site-defined configuration. Secret values never enter page snapshots or templates."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, model_validator
from werkzeug.datastructures import MultiDict

from .config import APP_ID, Model, clock, private_write

# The household refresh choices, in minutes (Household settings → Refresh).
REFRESH_CHOICES = (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440)


class LegacyItem(Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    values: dict[str, str | bool] = Field(default_factory=dict)
    copy_from: dict[str, str] = Field(default_factory=dict)


class Setting(Model):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    type: str = Field(pattern=r"^(text|secret|choice|time|checkbox|color|list)$")
    description: str = Field("", max_length=500)
    default: str | bool = ""
    choices: list[str] = Field(default_factory=list, max_length=30)
    fields: list[Setting] = Field(default_factory=list, max_length=8)
    max_items: int = Field(8, ge=1, le=12)
    item_label: str = Field("Item", min_length=1, max_length=60)
    legacy_items: list[LegacyItem] = Field(default_factory=list, max_length=12)
    advanced: bool = False  # the layout page shows it under the card's Advanced row

    @model_validator(mode="after")
    def valid_default(self):
        if self.type == "list":
            if not self.fields or self.default or any(item.type == "list" for item in self.fields):
                raise ValueError("A list needs scalar fields and no default")
            if len({item.key for item in self.fields}) != len(self.fields) or any(
                item.key == "id" or item.key.startswith("clear_") for item in self.fields
            ):
                raise ValueError("List field keys must be unique and avoid reserved keys")
            keys = {item.key for item in self.fields}
            if len(self.legacy_items) > self.max_items or len({row.id for row in self.legacy_items}) != len(
                self.legacy_items
            ):
                raise ValueError("Invalid legacy list items")
            for row in self.legacy_items:
                if not set(row.values).issubset(keys) or not set(row.copy_from).issubset(keys):
                    raise ValueError("Legacy items must use declared fields")
                if any(field.type != "secret" for field in self.fields if field.key in row.copy_from):
                    raise ValueError("Legacy private values can only migrate into secret fields")
                for item in self.fields:
                    if item.key in row.values:
                        if item.type == "secret" and row.values[item.key]:
                            raise ValueError("Secrets cannot have manifest defaults")
                        setting_value(item, row.values[item.key])
            return self
        if self.fields or self.legacy_items:
            raise ValueError("Only lists can declare fields or legacy items")
        if self.type != "checkbox" and not isinstance(self.default, str):
            raise ValueError("This setting needs a text default")
        if self.type == "secret" and self.default:
            raise ValueError("Secrets cannot have manifest defaults")
        if self.type == "choice" and (not self.choices or self.default not in self.choices):
            raise ValueError("A choice setting needs a valid default")
        if self.type == "checkbox" and not isinstance(self.default, bool):
            raise ValueError("A checkbox setting needs a boolean default")
        if self.type == "time":
            clock(self.default)
        if self.type == "color" and not re.fullmatch(r"#[0-9a-fA-F]{6}", self.default):
            raise ValueError("Use a six-digit hexadecimal color")
        return self


def setting_value(item, value):
    if not isinstance(value, (str, bool)) or isinstance(value, str) and len(value) > 4096:
        raise ValueError("A site setting exceeds its size limit")
    if item.type == "checkbox":
        if not isinstance(value, bool):
            raise ValueError("A checkbox needs a boolean")
    elif not isinstance(value, str):
        raise ValueError("This setting needs text")
    if item.type == "choice" and value not in item.choices:
        raise ValueError("Choose an available setting value")
    if item.type == "time":
        clock(value)
    if item.type == "color" and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Use a six-digit hexadecimal color")
    return value


def list_values(item, values):
    rows = values.get(item.key)
    if rows is None:
        rows = [
            {
                "id": row.id,
                **row.values,
                **{key: values.get(source, "") for key, source in row.copy_from.items()},
            }
            for row in item.legacy_items
            if any(values.get(source) for source in row.copy_from.values())
        ]
    if not isinstance(rows, list) or len(rows) > item.max_items:
        raise ValueError("Invalid layout settings list")
    ids, result = set(), []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", row["id"])
        ):
            raise ValueError("Invalid layout settings item")
        if row["id"] in ids:
            raise ValueError("Duplicate layout settings item")
        ids.add(row["id"])
        result.append(
            {
                "id": row["id"],
                **{
                    field.key: setting_value(field, row.get(field.key, field.default))
                    for field in item.fields
                },
            }
        )
    return result


class DisplaySetting(Model):
    """A setting each TV keeps for itself, such as whether to show a calendar."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    type: Literal["boolean"]
    description: str = Field("", max_length=500)
    default: StrictBool = False
    badge: str = Field("", max_length=30)  # shown on the home screen when a TV differs from the default


def display_values(manifest, stored):
    """One TV's values: declared keys only, with defaults for keys it has not saved
    or whose saved value has the wrong type."""
    return {
        item.key: stored[item.key] if type(stored.get(item.key)) is type(item.default) else item.default
        for item in manifest.display_settings
    }


class PresetOption(Model):
    """One choice of a preset: the setting values it sets together."""

    label: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=200)
    values: dict[str, str | bool] = Field(min_length=1, max_length=8)
    # The household refresh this choice needs at most, such as 5 for a photo every 5 minutes. One of REFRESH_CHOICES.
    refresh_minutes: int | None = None

    @model_validator(mode="after")
    def check_refresh(self):
        if self.refresh_minutes is not None and self.refresh_minutes not in REFRESH_CHOICES:
            raise ValueError(f"refresh_minutes must be one of {', '.join(map(str, REFRESH_CHOICES))}")
        return self


class Preset(Model):
    """A few settings set by one choice, shown at the top of the layout's Settings card. Custom shows the fields."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    fields: list[str] = Field(min_length=1, max_length=8)
    options: list[PresetOption] = Field(min_length=1, max_length=8)
    style: Literal["select", "cards"] = "select"


def preset_choice(preset, values):
    """The index of the option that matches these values, or None for Custom."""
    for index, option in enumerate(preset.options):
        if all(values.get(key) == value for key, value in option.values.items()):
            return index
    return None


def apply_presets(manifest, form):
    """The posted form with each chosen preset option's values in place of the typed ones."""
    form = MultiDict(form)
    for preset in manifest.presets:
        chosen = form.get(f"preset.{preset.key}", "custom")
        if not re.fullmatch(r"[0-9]{1,2}", chosen) or int(chosen) >= len(preset.options):  # ASCII digits only
            continue
        settings = {item.key: item for item in manifest.settings}
        for key, value in preset.options[int(chosen)].values.items():
            if settings[key].type == "checkbox":
                form.pop(key, None)
                if value:
                    form[key] = "on"
            else:
                form[key] = value
    return form


class Manifest(Model):
    schema_version: int = Field(1, ge=1, le=1)
    id: str = Field(pattern=f"^{APP_ID}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    entry: str = "index.html"
    network: str = Field("none", pattern=r"^(none|bridge)$")
    memory_mb: int = Field(256, ge=128, le=1024)
    artwork: bool = False
    settings: list[Setting] = Field(default_factory=list, max_length=30)
    display_settings: list[DisplaySetting] = Field(default_factory=list, max_length=10)
    presets: list[Preset] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def valid_paths_and_keys(self):
        if len({item.key for item in self.settings}) != len(self.settings):
            raise ValueError("Setting keys must be unique")
        settings = {item.key: item for item in self.settings}
        if len({item.key for item in self.presets}) != len(self.presets):
            raise ValueError("Preset keys must be unique")
        for preset in self.presets:
            if any(key not in settings or settings[key].type in ("list", "secret") for key in preset.fields):
                raise ValueError("A preset sets only the layout's own text, choice, time, color or checkbox settings")
            for option in preset.options:
                for key, value in option.values.items():
                    if key not in preset.fields:
                        raise ValueError("A preset option sets only its preset's fields")
                    setting_value(settings[key], value)  # raises on a value the setting refuses
        keys = {item.key for item in self.display_settings}
        if len(keys) != len(self.display_settings) or keys & {item.key for item in self.settings}:
            raise ValueError("Display setting keys must be unique and differ from layout setting keys")
        if any(item.key in {"csrf", "manifest"} or item.key.startswith("clear_") for item in self.settings):
            raise ValueError("Setting key conflicts with host form metadata")
        if (
            not re.fullmatch(r"[A-Za-z0-9_/-]+\.html", self.entry)
            or ".." in self.entry.split("/")
            or self.entry.startswith("/")
        ):
            raise ValueError("Use a relative HTML entry page")
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def load_manifest(path: Path, expected_id=None):
    """A layout's manifest. `expected_id` is the ID the installer recorded for it."""
    if path.stat().st_size > 50_000:
        raise ValueError("Site manifest is too large")
    manifest = Manifest.model_validate_json(path.read_bytes())
    if expected_id and manifest.id != expected_id:
        raise ValueError("Installed site identity does not match its manifest")
    return manifest


class LayoutStore:
    """One installed layout's manifest and private settings. `app` is an apps.App."""

    def __init__(self, root: Path, app):
        self.root = root / "layout-settings"
        self.app = app
        self.lock = threading.RLock()

    def manifest(self):
        return load_manifest(self.app.manifest_path, self.app.id)

    def path(self, manifest):
        return self.root / self.app.scope / "config.json"

    def values(self, manifest):
        path = self.path(manifest)
        if not path.exists():
            return {}
        if path.is_symlink() or path.stat().st_size > 150_000:
            raise ValueError("Invalid site configuration file")
        values = json.loads(path.read_bytes())
        if not isinstance(values, dict):
            raise ValueError("Invalid site configuration")
        return values

    def fingerprint(self, manifest):
        identity = manifest.fingerprint + ":" + self.path(manifest).parent.name
        return hashlib.sha256(identity.encode()).hexdigest()

    def public_values(self, manifest):
        values = self.values(manifest)
        public, configured = {}, set()
        for item in manifest.settings:
            if item.type == "list":
                public[item.key] = []
                for row in list_values(item, values):
                    visible = {"id": row["id"]}
                    for field in item.fields:
                        if field.type == "secret":
                            if row[field.key]:
                                configured.add(f"list.{item.key}.{row['id']}.{field.key}")
                        else:
                            visible[field.key] = row[field.key]
                    public[item.key].append(visible)
            elif item.type == "secret":
                if values.get(item.key):
                    configured.add(item.key)
            else:
                public[item.key] = values.get(item.key, item.default)
        return public, configured

    def set_values(self, changes):
        """Set a few declared settings, such as a starter plan's, and keep the rest. Returns the old values."""
        with self.lock:
            manifest = self.manifest()
            known = {item.key: item for item in manifest.settings if item.type != "list"}
            values = self.values(manifest)
            before = {key: values.get(key, known[key].default) for key in changes if key in known}
            for key, value in changes.items():
                if key in known:
                    values[key] = setting_value(known[key], value)
            private_write(self.path(manifest), json.dumps(values, indent=2).encode())
            return before

    def get_values(self, keys):
        """The stored values of a few declared settings (their defaults when unset): {key: value}."""
        with self.lock:
            manifest = self.manifest()
            known = {item.key: item for item in manifest.settings if item.type != "list"}
            values = self.values(manifest)
            return {key: values.get(key, known[key].default) for key in keys if key in known}

    def update(self, form):
        with self.lock:
            manifest = self.manifest()
            if manifest is None or form.get("manifest") != self.fingerprint(manifest):
                raise ValueError("Site changed; reload its settings before saving")
            values = self.values(manifest)
            updated = {}
            for item in manifest.settings:
                if item.type == "list":
                    prior = {row["id"]: row for row in list_values(item, values)}
                    ids = form.getlist(f"list.{item.key}.rows")
                    if len(ids) > item.max_items or len(set(ids)) != len(ids):
                        raise ValueError("Too many or duplicate layout settings items")
                    rows = []
                    for row_id in ids:
                        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", row_id):
                            raise ValueError("Invalid layout settings item")
                        row = {"id": row_id}
                        for field in item.fields:
                            name = f"list.{item.key}.{row_id}.{field.key}"
                            value = form.get(name, field.default)
                            if field.type == "secret":
                                value = value or prior.get(row_id, {}).get(field.key, "")
                                if "clear_" + name in form:
                                    value = ""
                            elif field.type == "checkbox":
                                value = name in form
                            row[field.key] = setting_value(field, value)
                        rows.append(row)
                    updated[item.key] = rows
                    continue
                if item.type == "secret":
                    value = values.get(item.key, "") if not form.get(item.key) else form[item.key]
                    if "clear_" + item.key in form:
                        value = ""
                elif item.type == "checkbox":
                    value = item.key in form
                else:
                    value = form.get(item.key, item.default)
                updated[item.key] = setting_value(item, value)
            encoded = json.dumps(updated, indent=2).encode()
            if len(encoded) > 150_000:
                raise ValueError("Layout settings exceed their size limit")
            private_write(self.path(manifest), encoded)
