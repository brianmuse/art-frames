import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from test_remote import prepare_fixture

from art_frames import apps
from art_frames.config import Config, ConfigStore

spec = importlib.util.spec_from_file_location(
    "installer", Path(__file__).parents[1] / "scripts/install-layout.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def origin(source_id, commit=None):
    return installer.Origin("repo", "repo", "main", commit, source_id, app_path="app")


def package(path):
    path.mkdir(parents=True)
    (path / "layout.json").write_text(
        json.dumps({"id": "example", "name": "Example", "network": "bridge", "artwork": True, "settings": []})
    )
    (path / "Dockerfile").write_text("FROM scratch\n")
    (path / "app.txt").write_text("committed application source")
    return path


def test_root_install_assigns_private_app_state_to_runtime_user(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    (root / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
    changed = []
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.os, "chown", lambda path, uid, gid: changed.append((path, uid, gid)))
    installer.install_first(root, package(tmp_path / "package"), origin("example/repo"))
    scope = json.loads((root / "sites/installed/.dayframe-installed.json").read_text())["settings_scope"]
    state = root / "data/layout-state" / scope
    assert (state, 10001, 10001) in changed
    assert (state.parent, 10001, 10001) in changed
    assert state.stat().st_mode & 0o777 == 0o700


def test_install_selects_app_preserves_core_and_existing_site_secrets(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    (root / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\nDAYFRAME_PORT=8765\n")
    store = ConfigStore(root / "data")
    store.save(Config(tv={"ip": "192.168.1.50"}))
    source = package(tmp_path / "package")
    installer.install_first(root, source, origin("example/repo", "a" * 40))
    scope = json.loads((root / "sites/installed/.dayframe-installed.json").read_text())["settings_scope"]
    private = root / "data/layout-settings" / scope / "config.json"
    private.write_text('{"private_feed":"KEEP-PRIVATE"}')
    installer.install_first(root, source, origin("example/repo", "b" * 40))
    assert store.load().tv.ip == "192.168.1.50"
    assert store.load().site.providers == ["art"]
    assert "KEEP-PRIVATE" in private.read_text()
    assert "DAYFRAME_PORT=8765" in (root / ".env").read_text()
    assert "DAYFRAME_SITE_NETWORK=bridge" in (root / ".env").read_text()
    assert (root / "sites/installed/.dayframe-installed.json").is_file()
    assert "KEEP-PRIVATE" not in "".join(path.read_text() for path in (root / "sites/installed").iterdir())


def test_managed_runtime_symlinks_survive_install_and_prepare_owns_active_app(tmp_path):
    base, prepare, _, log, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    source = package(tmp_path / "package")
    installer.install_first(base / "source", source, origin("example/repo"))
    for name in ("sites", "data", ".env", "config.local.toml"):
        assert (base / "source" / name).is_symlink()
    assert (base / "sites/installed/app.txt").exists()
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    scope = json.loads((base / "sites/installed/.dayframe-installed.json").read_text())["settings_scope"]
    assert str(base / "data/layout-settings" / scope) in log.read_text()
    assert str(base / "data/settings.json") in log.read_text()


@pytest.mark.parametrize("prepare", [True, False])
def test_remote_transfer_is_separate_and_does_not_overwrite_local_runtime(tmp_path, monkeypatch, prepare):
    from scripts import remote

    root = tmp_path / "operator"
    root.mkdir()
    base = tmp_path.resolve() / "dayframe"
    from scripts.remote import sync_guard

    subprocess.run(["sh", "-c", sync_guard(str(base))], check=True)
    (base / "source/scripts").mkdir(parents=True)
    (base / "source/scripts/install-layout.py").touch()
    (base / ".dayframe-managed").write_text("dayframe-managed-source-v1")
    (root / ".deploy.local.toml").write_text(
        f'host="192.0.2.10"\nuser="example"\npath="{base}"\ncompose="compose.static.yaml"\n'
    )
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr(remote, "ssh_options", lambda cfg: ["test-ssh"])
    actual_run = subprocess.run
    calls = []

    def offline(args, **kwargs):
        calls.append(args)
        if args[0] == "test-ssh":
            if "sudo sh " in args[-1]:
                return None
            return actual_run(["sh", "-c", args[-1]], **kwargs)
        if args[0] == "rsync":
            position = args.index("-e")
            args = args[:position] + args[position + 2 :]
            args[-1] = args[-1].split(":", 1)[1]
        return actual_run(args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", offline)
    installer.install_remote(
        [(package(tmp_path / "package"), origin("example/repo", "c" * 40))],
        prepare=prepare,
    )
    # The transfer stages the app; an administrator script moves it into sites/installed.
    assert (base / "sites/.incoming/installed/app.txt").exists()
    assert not (base / "sites/installed").exists()
    profile = (base / "sites/.incoming/installed/.install-profile").read_text().split()
    assert profile[:4] == ["example", "bridge", "256", "index.html"]
    assert profile[4].startswith("example-") and profile[5] == "1"
    assert not (root / "data").exists() and not (root / ".env").exists()
    assert any(args[-1].endswith("/source/scripts/place-apps.sh static") for args in calls) is prepare


def test_archive_escapes_and_committed_private_files_are_rejected(tmp_path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo("../private")
        archive.addfile(info, io.BytesIO())
    with pytest.raises(ValueError):
        installer.unpack(raw.getvalue(), tmp_path / "out")
    source = package(tmp_path / "package")
    (source / ".env").write_text("SECRET=never-install")
    with pytest.raises(ValueError):
        installer.install_first(tmp_path / "host", source, origin("example/repo"))
    assert not (tmp_path / "host/sites/installed").exists()


def test_catalog_cli_installs_committed_app_and_excludes_ignored_secrets(tmp_path, monkeypatch):
    repo = tmp_path / "repository"
    source = package(repo / "layouts/example")
    (repo / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default": "example",
                "layouts": [{"id": "example", "path": "layouts/example"}],
            }
        )
    )
    (repo / ".gitignore").write_text(".secrets/\n")
    (source / ".secrets").mkdir()
    (source / ".secrets/config.json").write_text('{"token":"SYNTHETIC-DO-NOT-EXPORT"}')
    for args in (
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "add", "."],
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(args, check=True)
    root = tmp_path / "host"
    root.mkdir()
    (root / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--local-repo", str(repo)])
    installer.main()
    assert (root / "sites/installed/app.txt").exists()
    assert not (root / "sites/installed/.secrets").exists()


@pytest.mark.parametrize("memory", ["000", "127", "1025"])
def test_prepare_rejects_invalid_app_memory_before_environment_changes(tmp_path, memory):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    app = base / "sites/installed"
    app.mkdir()
    (app / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
    (app / ".install-profile").write_text(f"example bridge {memory} index.html example-0123456789abcdef 1\n")
    original = (base / ".env").read_bytes()
    result = subprocess.run(["sh", str(prepare), "static"], env=env, capture_output=True)
    assert result.returncode != 0
    assert (base / ".env").read_bytes() == original


def test_remote_profile_grants_and_revokes_artwork_context(tmp_path, monkeypatch):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    app = base / "sites/installed"
    app.mkdir()
    (app / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
    (app / "layout.json").write_text(json.dumps({"id": "example", "name": "Example"}))
    monkeypatch.setenv("DAYFRAME_DEFAULT_SITE_SOCKET", "website.sock")  # a static profile
    for granted in ("1", "0"):
        (app / ".install-profile").write_text(
            f"example bridge 256 index.html example-0123456789abcdef {granted}\n"
        )
        subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
        for line in (base / ".env").read_text().splitlines():
            if line.startswith("DAYFRAME_LAYOUT_"):
                name, value = line.split("=", 1)
                monkeypatch.setenv(name, value)
        monkeypatch.setenv("DAYFRAME_LAYOUT_MANIFEST", str(app / "layout.json"))  # the container path, on this host
        [first] = apps.installed()
        assert apps.configure(Config(), first).site.providers == (["art"] if granted == "1" else [])


def test_different_source_with_same_app_id_cannot_inherit_saved_secrets(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    (root / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
    source = package(tmp_path / "package")
    installer.install_first(root, source, origin("first/repo"))
    first = json.loads((root / "sites/installed/.dayframe-installed.json").read_text())["settings_scope"]
    secret = root / "data/layout-settings" / first / "config.json"
    secret.write_text('{"private_feed":"DO-NOT-SHARE"}')
    installer.install_first(root, source, origin("another/repo"))
    second = json.loads((root / "sites/installed/.dayframe-installed.json").read_text())["settings_scope"]
    assert second != first
    assert not (root / "data/layout-settings" / second / "config.json").exists()
    assert "DO-NOT-SHARE" in secret.read_text()


@pytest.mark.parametrize("providers", [[], ["art"]])
def test_removed_dashboard_entry_migrates_without_planner_provider(tmp_path, providers):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"site": {"entry": "dashboard/index.html", "providers": providers}}))
    assert ConfigStore(tmp_path).load().site.entry == "index.html"
    raw = json.loads(path.read_text())
    raw["site"]["path"] = "custom-build"
    path.write_text(json.dumps(raw))
    assert ConfigStore(tmp_path).load().site.entry == "dashboard/index.html"
