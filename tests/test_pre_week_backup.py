"""The one-time copy of settings.json that older builds can still read."""

import json

import pytest

from art_frames.config import ConfigStore

OLD = {"app_schedule": {"windows": []}}


def store_with(tmp_path, settings=None):
    if settings is not None:
        (tmp_path / "settings.json").write_text(json.dumps(settings))
    return ConfigStore(tmp_path)


def test_old_shape_is_backed_up_once_on_save(tmp_path):
    store = store_with(tmp_path, OLD)
    original = (tmp_path / "settings.json").read_bytes()
    store.save(store.load())
    assert (tmp_path / "settings.pre-week.json").read_bytes() == original
    assert "week" in json.loads((tmp_path / "settings.json").read_text())["app_schedule"]


def test_backup_is_not_overwritten_by_a_later_save(tmp_path):
    store = store_with(tmp_path, OLD)
    backup = tmp_path / "settings.pre-week.json"
    store.save(store.load())
    first = backup.read_bytes()
    # Another old-shape file must not replace the first copy.
    (tmp_path / "settings.json").write_text(json.dumps(OLD, indent=4))
    store.save(store.load())
    assert backup.read_bytes() == first


def test_no_backup_when_settings_already_have_week(tmp_path):
    store = store_with(tmp_path, OLD)
    store.save(store.load())
    (tmp_path / "settings.pre-week.json").unlink()
    store.save(store.load())
    assert not (tmp_path / "settings.pre-week.json").exists()


def test_display_with_week_counts_as_new_shape(tmp_path):
    store = store_with(tmp_path, {"displays": [{"app_schedule": {"default": "", "week": {}}}]})
    store._backup_pre_week()
    assert not (tmp_path / "settings.pre-week.json").exists()


def test_no_backup_without_settings_json(tmp_path):
    store = store_with(tmp_path)
    store.save(store.load())
    assert not (tmp_path / "settings.pre-week.json").exists()


def test_symlinked_backup_path_is_refused(tmp_path):
    store = store_with(tmp_path, OLD)
    target = tmp_path / "elsewhere.json"
    (tmp_path / "settings.pre-week.json").symlink_to(target)
    with pytest.raises(ValueError, match="symlinked"):
        store.save(store.load())
    assert not target.exists()
