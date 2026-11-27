"""A layout keeps its saved settings when its GitHub repository is renamed.

The settings scope comes from the layout's source, so a renamed repository gives each layout a
new, empty scope. Everything runs in temporary folders, never on a NAS or the real data folder.
"""

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
from test_app_install import host, installer, package, scope_of, write_extra
from test_remote import prepare_fixture

from art_frames import apps
from art_frames.config import ConfigStore

SCRIPTS = Path(__file__).parents[1] / "scripts"
OLD = "morning-" + "a" * 32
NEW = "morning-" + "b" * 32
MESSAGE = "Layout {}: restored its saved settings from an earlier install.\n"


def layout(base, app_id, scope, earlier):
    """A layout folder as install-layout.py leaves it: a profile and the earlier scopes."""
    folder = base / "sites/apps" / app_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".install-profile").write_text(f"{app_id} none 256 index.html {scope} 0\n")
    (folder / ".earlier-scopes").write_text("".join(name + "\n" for name in earlier))
    return folder


def keep(data, *folders):
    return subprocess.run(["sh", str(SCRIPTS / "keep-layout-settings.sh"), str(data), *map(str, folders)],
                          capture_output=True, text=True, check=True)


def saved(data, scope, values, *, state=None, mtime=None):
    folder = data / "layout-settings" / scope
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps(values))
    (folder / "config.json").chmod(0o600)
    if mtime is not None:
        os.utime(folder / "config.json", (mtime, mtime))
    if state is not None:
        (data / "layout-state" / scope).mkdir(parents=True)
        (data / "layout-state" / scope / "cache.json").write_text(state)
    return folder


def config(data, scope):
    return json.loads((data / "layout-settings" / scope / "config.json").read_text())


def test_renamed_source_recovers_settings_and_state_and_keeps_the_old_folders(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Home"}, state="cached")
    # The earlier deploy already made the new, empty folders.
    (data / "layout-settings" / NEW).mkdir()
    (data / "layout-state" / NEW).mkdir()
    morning = layout(tmp_path, "morning", NEW, [OLD])
    result = keep(data, morning)
    assert config(data, NEW) == {"location": "Home"}
    assert (data / "layout-state" / NEW / "cache.json").read_text() == "cached"
    assert (data / "layout-settings" / NEW / "config.json").stat().st_mode & 0o777 == 0o600
    assert result.stdout == MESSAGE.format("morning")
    # Only a copy: the earlier folders stay as they were.
    assert config(data, OLD) == {"location": "Home"}
    assert (data / "layout-state" / OLD / "cache.json").read_text() == "cached"
    # A second run does nothing.
    assert keep(data, morning).stdout == ""


def test_missing_new_folders_are_made_private(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Home"}, state="cached")
    keep(data, layout(tmp_path, "morning", NEW, [OLD]))
    for kind in ("layout-settings", "layout-state"):
        assert (data / kind / NEW).stat().st_mode & 0o777 == 0o700
    assert config(data, NEW) == {"location": "Home"}


def test_saved_settings_in_the_current_scope_are_never_replaced(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Old"}, state="old state")
    saved(data, NEW, {"location": "New"})
    assert keep(data, layout(tmp_path, "morning", NEW, [OLD])).stdout == ""
    assert config(data, NEW) == {"location": "New"}
    assert not (data / "layout-state" / NEW).exists()


def test_state_already_in_the_current_scope_stays(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Home"}, state="old state")
    (data / "layout-state" / NEW).mkdir()
    (data / "layout-state" / NEW / "cache.json").write_text("new state")
    keep(data, layout(tmp_path, "morning", NEW, [OLD]))
    assert config(data, NEW) == {"location": "Home"}
    assert (data / "layout-state" / NEW / "cache.json").read_text() == "new state"


def test_the_newest_earlier_settings_win(tmp_path):
    data = tmp_path / "data"
    earlier = ["morning-" + digit * 32 for digit in "123"]
    for name, place, when in zip(earlier, ("Oldest", "Newest", "Middle"), (1_000_000, 3_000_000, 2_000_000),
                                 strict=True):
        saved(data, name, {"location": place}, mtime=when)
    keep(data, layout(tmp_path, "morning", NEW, earlier))
    assert config(data, NEW) == {"location": "Newest"}


def test_only_listed_scopes_of_the_same_layout_are_used(tmp_path):
    data = tmp_path / "data"
    # Another source's folder for the same layout ID: not an earlier name of this source.
    saved(data, "morning-" + "c" * 32, {"location": "Other source"})
    saved(data, "news-" + "a" * 32, {"feed": "News"}, state="news state")
    saved(data, "morning-" + "a" * 31, {"location": "Short hash"})
    morning = layout(tmp_path, "morning", NEW, ["news-" + "a" * 32, "morning-" + "a" * 31, "../" + OLD, NEW])
    assert keep(data, morning).stdout == ""
    assert not (data / "layout-settings" / NEW).exists()
    # The news layout recovers only its own folder.
    news = "news-" + "b" * 32
    keep(data, morning, layout(tmp_path, "news", news, ["news-" + "a" * 32]))
    assert config(data, news) == {"feed": "News"}
    assert config(data, "news-" + "a" * 32) == {"feed": "News"}
    assert sorted(path.name for path in (data / "layout-settings").iterdir()) == sorted(
        ["morning-" + "c" * 32, "news-" + "a" * 32, news, "morning-" + "a" * 31]
    )


def test_a_symlinked_candidate_is_refused(tmp_path):
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.json").write_text(json.dumps({"location": "Outside"}))
    (data / "layout-settings").mkdir(parents=True)
    (data / "layout-settings" / OLD).symlink_to(outside, target_is_directory=True)
    # A link inside a candidate is refused too, and so is a linked config.json.
    inner = "morning-" + "c" * 32
    (saved(data, inner, {"location": "Home"}) / "secret").symlink_to(outside / "config.json")
    linked = "morning-" + "d" * 32
    (data / "layout-settings" / linked).mkdir()
    (data / "layout-settings" / linked / "config.json").symlink_to(outside / "config.json")
    assert keep(data, layout(tmp_path, "morning", NEW, [OLD, inner, linked])).stdout == ""
    assert not (data / "layout-settings" / NEW).exists()


def test_a_symlinked_state_folder_is_not_copied(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Home"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text("not layout state")
    (data / "layout-state").mkdir()
    (data / "layout-state" / OLD).symlink_to(outside, target_is_directory=True)
    keep(data, layout(tmp_path, "morning", NEW, [OLD]))
    assert config(data, NEW) == {"location": "Home"}
    assert not (data / "layout-state" / NEW).exists()


def test_a_bad_profile_or_scope_is_ignored(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Home"})
    bad = layout(tmp_path, "morning", NEW, [OLD])
    (bad / ".install-profile").write_text(f"Morning none 256 index.html {NEW} 0\n")
    assert keep(data, bad).stdout == ""
    other = layout(tmp_path / "other", "morning", "starter", [OLD])
    assert keep(data, other).stdout == ""
    assert not (data / "layout-settings" / NEW).exists()


def test_earlier_scopes_come_only_from_a_renamed_github_repository():
    def origin(source_id):
        return installer.Origin("repo", "layouts", "main", "a" * 40, source_id, app_path="layouts/morning")

    digest = hashlib.sha256(b"github:brianmuse/dayframe-layouts:layouts/morning").hexdigest()[:32]
    assert origin("github:brianmuse/art-frames-layouts").earlier_scopes("morning") == ["morning-" + digest]
    for other in ("github:brianmuse/dayframe-layouts", "github:someone/layouts", "local:abc", "git:https://x/y"):
        assert origin(other).earlier_scopes("morning") == []


def test_local_install_recovers_settings_after_the_repository_rename(tmp_path, capsys):
    root = host(tmp_path)
    morning = package(tmp_path / "packages/morning", "morning")
    news = package(tmp_path / "packages/news", "news")

    def items(repository):
        def origin(app_id):
            return installer.Origin("repo", repository, "main", "a" * 40, "github:" + repository,
                                    app_path="layouts/" + app_id, repository=repository)
        return [(morning, origin("morning")), (news, origin("news"))]

    def scopes():
        return {"morning": scope_of(root / "sites/installed"), "news": scope_of(root / "sites/apps/news")}

    installer.install_all(root, items("brianmuse/dayframe-layouts"))
    old = scopes()
    for app_id, scope in old.items():
        (root / "data/layout-settings" / scope / "config.json").write_text(json.dumps({"layout": app_id}))
        (root / "data/layout-state" / scope / "state.json").write_text(app_id)
    capsys.readouterr()
    installer.install_all(root, items("brianmuse/art-frames-layouts"))
    new = scopes()
    assert new != old
    for app_id, scope in new.items():
        assert config(root / "data", scope) == {"layout": app_id}
        assert (root / "data/layout-state" / scope / "state.json").read_text() == app_id
        assert config(root / "data", old[app_id]) == {"layout": app_id}
    out = capsys.readouterr().out
    assert MESSAGE.format("morning") in out and MESSAGE.format("news") in out
    installer.install_all(root, items("brianmuse/art-frames-layouts"))
    assert "restored" not in capsys.readouterr().out


def test_prepare_on_the_nas_recovers_the_first_and_extra_layouts(tmp_path):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    data = base / "data"
    old_news, new_news = "news-" + "a" * 32, "news-" + "b" * 32
    installed = base / "sites/installed"
    installed.mkdir(parents=True, exist_ok=True)
    (installed / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
    (installed / ".install-profile").write_text(f"morning bridge 256 index.html {NEW} 0\n")
    (installed / ".earlier-scopes").write_text(OLD + "\n")
    write_extra(base, "news", f"news bridge 256 index.html {new_news} 0")
    (base / "sites/apps/news/.earlier-scopes").write_text(old_news + "\n")
    saved(data, OLD, {"location": "Home"}, state="morning state")
    saved(data, old_news, {"feed": "News"})
    result = subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True, text=True)
    assert config(data, NEW) == {"location": "Home"}
    assert (data / "layout-state" / NEW / "cache.json").read_text() == "morning state"
    assert config(data, new_news) == {"feed": "News"}
    assert MESSAGE.format("morning") in result.stdout and MESSAGE.format("news") in result.stdout
    again = subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True, text=True)
    assert "restored" not in again.stdout


def test_a_link_planted_in_the_destination_is_never_written_through(tmp_path):
    data = tmp_path / "data"
    saved(data, OLD, {"location": "Home"}, state="cached")
    outside = tmp_path / "outside"
    outside.mkdir()
    # The running layout container can write to its own state folder. Here it plants a link
    # just before each move, the worst moment for the copy.
    (data / "layout-state" / NEW).mkdir()
    commands = tmp_path / "bin"
    commands.mkdir()
    real_mv = subprocess.run(["sh", "-c", "command -v mv"], capture_output=True, text=True).stdout.strip()
    (commands / "mv").write_text(
        "#!/bin/sh\n"
        'for last; do :; done\n'
        'case "$last" in */) ln -s "$OUTSIDE" "$last$(basename "$1")" 2>/dev/null || true ;; esac\n'
        f'exec {real_mv} "$@"\n'
    )
    (commands / "mv").chmod(0o700)
    env = dict(os.environ, PATH=str(commands) + ":" + os.environ["PATH"], OUTSIDE=str(outside))
    subprocess.run(["sh", str(SCRIPTS / "keep-layout-settings.sh"), str(data),
                    str(layout(tmp_path, "morning", NEW, [OLD]))], env=env, capture_output=True, check=True)
    assert list(outside.iterdir()) == []
    assert not (data / "layout-state" / NEW / "cache.json").is_symlink()
    assert config(data, NEW) == {"location": "Home"}
    assert (data / "layout-state" / NEW / "cache.json").read_text() == "cached"
    # A link that is already there stays, and the copy leaves that name alone.
    other = tmp_path / "second"
    data = other / "data"
    saved(data, OLD, {"location": "Home"})
    (data / "layout-settings" / OLD / "notes.txt").write_text("from the earlier install")
    (data / "layout-settings" / NEW).mkdir()
    (data / "layout-settings" / NEW / "notes.txt").symlink_to(outside / "notes.txt")
    keep(data, layout(other, "morning", NEW, [OLD]))
    assert not (outside / "notes.txt").exists()
    assert (data / "layout-settings" / NEW / "notes.txt").is_symlink()
    assert config(data, NEW) == {"location": "Home"}


def test_one_failed_layout_does_not_stop_the_others_and_leaves_no_half_copy(tmp_path):
    data = tmp_path / "data"
    folder = saved(data, OLD, {"location": "Home"})
    (folder / "nested").mkdir()
    (folder / "nested" / "a.txt").write_text("a")
    (folder / "nested" / "b.txt").write_text("b")
    (folder / "nested" / "b.txt").chmod(0)
    news = "news-" + "b" * 32
    saved(data, "news-" + "a" * 32, {"feed": "News"})
    morning = layout(tmp_path, "morning", NEW, [OLD])
    result = subprocess.run(["sh", str(SCRIPTS / "keep-layout-settings.sh"), str(data), str(morning),
                             str(layout(tmp_path, "news", news, ["news-" + "a" * 32]))],
                            capture_output=True, text=True, check=True)
    assert "Layout morning: could not restore its saved settings from an earlier install." in result.stderr
    assert result.stdout == MESSAGE.format("news")
    assert config(data, news) == {"feed": "News"}
    assert not (data / "layout-settings" / NEW).exists()
    assert not [path for path in (data / "layout-settings").iterdir() if path.name.startswith(".keep.")]
    # After the cause is fixed, the next run copies the whole folder.
    (folder / "nested" / "b.txt").chmod(0o600)
    assert keep(data, morning).stdout == MESSAGE.format("morning")
    assert (data / "layout-settings" / NEW / "nested" / "b.txt").read_text() == "b"


def test_a_folder_that_find_cannot_read_is_not_used(tmp_path):
    data = tmp_path / "data"
    folder = saved(data, OLD, {"location": "Home"})
    (folder / "private").mkdir()
    (folder / "private" / "link").symlink_to(tmp_path)
    (folder / "private").chmod(0)
    try:
        assert keep(data, layout(tmp_path, "morning", NEW, [OLD])).stdout == ""
    finally:
        (folder / "private").chmod(0o700)
    assert not (data / "layout-settings" / NEW).exists()


def test_a_package_may_not_ship_earlier_scopes(tmp_path):
    folder = package(tmp_path / "morning", "morning")
    (folder / ".earlier-scopes").write_text(OLD + "\n")
    origin = installer.Origin("repo", "layouts", "main", "a" * 40, "github:brianmuse/art-frames-layouts",
                              app_path="layouts/morning")
    with pytest.raises(installer.InstallError):
        installer.check_package(folder, origin, installer.read_manifest(folder))


def test_a_rename_maps_only_to_the_same_owners_earlier_repository():
    origin = installer.Origin("repo", "layouts", "main", "a" * 40, "github:someone/art-frames-layouts",
                              app_path="layouts/morning")
    digest = hashlib.sha256(b"github:someone/dayframe-layouts:layouts/morning").hexdigest()[:32]
    assert origin.earlier_scopes("morning") == ["morning-" + digest]


# --- Each TV's options for a layout (settings.json) --------------------------------------


def host_app(tmp_path, app_id, scope, earlier):
    folder = tmp_path / "sites" / app_id
    folder.mkdir(parents=True)
    (folder / "layout.json").write_text("{}")
    (folder / ".earlier-scopes").write_text("".join(name + "\n" for name in earlier))
    return apps.App(app_id, scope, folder / "layout.json", None, None)


def add_tv(raw, name, values):
    raw["displays"].append({"id": hashlib.md5(name.encode()).hexdigest(), "name": name, "app_settings": values})


def test_each_tv_gets_its_layout_options_back(tmp_path, caplog):
    configs = ConfigStore(tmp_path)
    old_news, new_news = "news-" + "a" * 32, "news-" + "b" * 32

    def setup(raw):
        raw["app_settings"] = {OLD: {"clock": False}, old_news: {"show_art": True}}
        add_tv(raw, "Kitchen", {old_news: {"show_art": False}, new_news: {"show_art": True}})
        add_tv(raw, "Office", {"other-" + "a" * 32: {"x": True}})
    configs.mutate(setup)
    available = [host_app(tmp_path, "morning", NEW, [OLD]), host_app(tmp_path, "news", new_news, [old_news])]
    caplog.set_level(logging.INFO, logger="art_frames.apps")
    assert apps.keep_tv_options(configs, available) == 2
    cfg = configs.load()
    assert cfg.app_settings[NEW] == {"clock": False} and cfg.app_settings[new_news] == {"show_art": True}
    assert cfg.app_settings[OLD] == {"clock": False}
    kitchen, office = cfg.displays
    # Saved options are never replaced, and other layouts' values stay as they are.
    assert kitchen.app_settings[new_news] == {"show_art": True}
    assert office.app_settings == {"other-" + "a" * 32: {"x": True}}
    assert caplog.messages == [
        "Layout morning: restored the options of TV Frame 1 from an earlier install.",
        "Layout news: restored the options of TV Frame 1 from an earlier install.",
    ]
    before = (tmp_path / "settings.json").read_bytes()
    assert apps.keep_tv_options(configs, available) == 0
    assert (tmp_path / "settings.json").read_bytes() == before


def test_tv_options_use_only_listed_scopes_of_the_same_layout(tmp_path):
    configs = ConfigStore(tmp_path)
    configs.mutate(lambda raw: raw.update(app_settings={"morning-" + "c" * 32: {"clock": False}}))
    (tmp_path / "sites").mkdir()
    linked = host_app(tmp_path / "linked", "morning", NEW, [])
    (linked.manifest_path.parent / ".earlier-scopes").unlink()
    (linked.manifest_path.parent / ".earlier-scopes").symlink_to(tmp_path / "missing")
    for available in ([host_app(tmp_path / "a", "morning", NEW, ["news-" + "c" * 32, "morning-" + "d" * 32])],
                      [linked]):
        assert apps.keep_tv_options(configs, available) == 0
    assert NEW not in configs.load().app_settings
