"""Several installed apps: the installer, the shared Compose generator and the deploy settings.

Everything runs against disposable local directories, never a NAS or a real TV.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from test_remote import DOCKER_FILES_CHECK, config, managed_layout, prepare_fixture, remote

SCRIPTS = Path(__file__).parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("installer_apps", SCRIPTS / "install-layout.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

HARDENING = (
    "restart: unless-stopped",
    "driver: json-file",
    'options: {max-size: "10m", max-file: "3"}',
    "init: true",
    'user: "10001:10001"',
    "read_only: true",
    "cap_drop: [ALL]",
    "security_opt: [no-new-privileges:true]",
    "- /tmp:rw,noexec,nosuid,size=32m",
)


def package(path, app_id, *, network="none", memory=256):
    path.mkdir(parents=True)
    (path / "layout.json").write_text(
        json.dumps({"id": app_id, "name": app_id.title(), "network": network, "memory_mb": memory})
    )
    (path / "Dockerfile").write_text("FROM scratch\n")
    (path / "app.txt").write_text(f"{app_id} source")
    return path


def host(tmp_path):
    root = tmp_path / "host"
    root.mkdir(parents=True)
    (root / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
    return root


def origin(app_id):
    return installer.Origin("repo", "layouts", "main", "a" * 40, "github:example/layouts", app_path="layouts/" + app_id)


def scope_of(folder):
    return json.loads((folder / ".dayframe-installed.json").read_text())["settings_scope"]


def env_value(root, name):
    lines = (root / ".env").read_text().splitlines()
    return next((line.split("=", 1)[1] for line in reversed(lines) if line.startswith(name + "=")), None)


@pytest.fixture
def apps(tmp_path):
    return {app_id: (package(tmp_path / "packages" / app_id, app_id, network="bridge" if app_id == "news" else "none",
                             memory=384 if app_id == "photos" else 256), origin(app_id))
            for app_id in ("morning", "news", "photos")}


def test_three_apps_install_with_own_containers_sockets_and_scopes(tmp_path, apps):
    single = host(tmp_path / "single")
    installer.install_first(single, *apps["morning"])
    root = host(tmp_path)
    installer.install_all(root, [apps["morning"], apps["news"], apps["photos"]])

    # The primary app stays in sites/installed with the same settings scope as a single install.
    primary = root / "sites/installed"
    assert (primary / "app.txt").read_text() == "morning source"
    assert scope_of(primary) == scope_of(single / "sites/installed")
    assert env_value(root, "DAYFRAME_LAYOUT_ID") == "morning"
    assert env_value(root, "DAYFRAME_LAYOUT_CONFIG_ID") == scope_of(primary)

    scopes = {}
    for app_id in ("news", "photos"):
        folder = root / "sites/apps" / app_id
        assert (folder / "app.txt").read_text() == f"{app_id} source"
        assert (folder / ".dayframe-layout-managed").read_text() == "dayframe-layout-source-v1\n"
        scopes[app_id] = scope_of(folder)
        assert scopes[app_id].startswith(app_id + "-")
        for kind in ("layout-settings", "layout-state"):
            assert (root / "data" / kind / scopes[app_id]).stat().st_mode & 0o777 == 0o700
    assert env_value(root, "DAYFRAME_APPS") == f"news={scopes['news']} photos={scopes['photos']}"

    override = (root / "sites/compose.apps.yaml").read_text()
    head, _, rest = override.partition("\n  app-news:\n")
    news, _, photos = rest.partition("\n  app-photos:\n")
    photos = photos.split("\nvolumes:")[0]
    for block, app_id, network, memory in ((news, "news", "bridge", "256m"), (photos, "photos", "none", "384m")):
        for line in HARDENING:
            assert line in block, (app_id, line)
        assert f'context: "${{DAYFRAME_APPS_CONTEXT:-./sites/apps}}/{app_id}"' in block
        assert f'image: "dayframe-app-{app_id}:local"' in block
        assert f'network_mode: "{network}"' in block and f'mem_limit: "{memory}"' in block
        assert f'- "app-{app_id}-ipc:/run/dayframe-site"' in block
        assert f'- "./data/layout-settings/{scopes[app_id]}:/site-config:ro"' in block
        assert f'- "./data/layout-state/{scopes[app_id]}:/site-state"' in block
        assert f'- "app-{app_id}-ipc:/run/dayframe-apps/{app_id}:ro"' in head
        assert f'- "app-{app_id}"' in head
        assert f"\n  app-{app_id}-ipc:" in override.split("\nvolumes:")[1]
    assert "extends" not in override and "&" not in override and "privileged" not in override


def test_removed_extra_app_is_pruned_and_one_app_removes_the_override(tmp_path, apps):
    root = host(tmp_path)
    installer.install_all(root, [apps["morning"], apps["news"], apps["photos"]])
    secret = root / "data/layout-settings" / scope_of(root / "sites/apps/photos") / "config.json"
    secret.write_text('{"album":"KEEP"}')
    installer.install_all(root, [apps["morning"], apps["news"]])
    assert not (root / "sites/apps/photos").exists()
    assert "photos" not in (root / "sites/compose.apps.yaml").read_text()
    assert env_value(root, "DAYFRAME_APPS").startswith("news=") and " " not in env_value(root, "DAYFRAME_APPS")
    assert "KEEP" in secret.read_text()  # app settings outlive a removed app, like today
    installer.install_all(root, [apps["morning"]])
    assert not (root / "sites/compose.apps.yaml").exists()
    assert env_value(root, "DAYFRAME_APPS") == ""
    assert (root / "sites/installed/app.txt").exists()


def test_single_app_install_is_unchanged_by_the_app_list(tmp_path, apps):
    old = host(tmp_path / "old")
    installer.install_first(old, *apps["morning"])
    new = host(tmp_path / "new")
    installer.install_all(new, [apps["morning"]])
    assert (new / ".env").read_bytes() == (old / ".env").read_bytes()
    assert not (new / "sites/apps").exists() and not (new / "sites/compose.apps.yaml").exists()


def test_duplicate_or_unmanaged_apps_are_rejected(tmp_path, apps):
    root = host(tmp_path)
    with pytest.raises(ValueError):
        installer.install_all(root, [apps["morning"], apps["news"], apps["news"]])
    with pytest.raises(ValueError):
        installer.install_all(root, [apps["news"], apps["news"]])
    (root / "sites/apps/stray").mkdir(parents=True)
    (root / "sites/apps/stray/file").write_text("not ours")
    with pytest.raises(ValueError):
        installer.install_all(root, [apps["morning"], apps["news"]])
    assert (root / "sites/apps/stray/file").exists()


def write_extra(base, app_id, profile):
    folder = base / "sites/apps" / app_id
    folder.mkdir(parents=True)
    (base / "sites/apps/.dayframe-apps-managed").write_text("dayframe-apps-source-v1\n")
    (folder / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
    (folder / ".install-profile").write_text(profile + "\n")


@pytest.mark.parametrize(
    "app_id,profile",
    [
        ("news", "news bridge 2000 index.html news-0123 0"),
        ("news", "news host 256 index.html news-0123 0"),
        ("news", "other none 256 index.html other-0123 0"),
        ("news", "news none 256 ../x.html news-0123 0"),
        ("news", "news none 256 index.html Bad_Scope 0"),
    ],
)
def test_generator_rejects_invalid_profiles(tmp_path, app_id, profile):
    write_extra(tmp_path, app_id, profile)
    result = subprocess.run(["sh", str(SCRIPTS / "app-compose.sh"), str(tmp_path)], capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "sites/compose.apps.yaml").exists()


def test_prepare_writes_override_environment_and_owns_extra_scopes(tmp_path):
    base, prepare, _, log, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    write_extra(base, "news", "news bridge 256 index.html news-0123abcd 0")
    write_extra(base, "photos", "photos none 512 index.html photos-4567abcd 1")
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    environment = (base / ".env").read_text()
    assert "DAYFRAME_APPS=news=news-0123abcd photos=photos-4567abcd\n" in environment
    assert environment.count("DAYFRAME_APPS=") == 1
    assert 'mem_limit: "512m"' in (base / "sites/compose.apps.yaml").read_text()
    for scope in ("news-0123abcd", "photos-4567abcd"):
        for kind in ("layout-settings", "layout-state"):
            path = base / "data" / kind / scope
            assert path.stat().st_mode & 0o777 == 0o700
            assert str(path) in log.read_text()
    # Running again changes nothing; removing the apps removes the override and clears the value.
    before = (base / ".env").read_bytes()
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert (base / ".env").read_bytes() == before
    shutil.rmtree(base / "sites/apps/news")
    shutil.rmtree(base / "sites/apps/photos")
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert not (base / "sites/compose.apps.yaml").exists()
    assert "DAYFRAME_APPS=\n" in (base / ".env").read_text()


def test_prepare_without_extra_apps_leaves_environment_alone(tmp_path):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert "DAYFRAME_APPS" not in (base / ".env").read_text()
    assert not (base / "sites/compose.apps.yaml").exists()




def test_prepare_in_browser_mode_never_lists_extra_apps(tmp_path):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    write_extra(base, "news", "news bridge 256 index.html news-0123abcd 0")
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert "DAYFRAME_APPS=news=news-0123abcd" in (base / ".env").read_text()
    # A browser profile cannot mount extra app sockets: no override and no DAYFRAME_APPS line.
    subprocess.run(["sh", str(prepare), "browser"], env=env, check=True, capture_output=True)
    assert "DAYFRAME_APPS" not in (base / ".env").read_text()
    assert not (base / "sites/compose.apps.yaml").exists()
    subprocess.run(["sh", str(prepare), "browser"], env=env, check=True, capture_output=True)
    assert "DAYFRAME_APPS" not in (base / ".env").read_text()
    assert subprocess.run(["sh", str(prepare)], env=env, capture_output=True).returncode != 0


# --- Deploy settings -------------------------------------------------------------------


@pytest.fixture
def committed(monkeypatch):
    monkeypatch.setattr(remote, "source_committed", lambda path, label: None)
    monkeypatch.setattr(remote, "require_clean_checkout", lambda root: None)


def plan_of(args):
    return json.loads(args[args.index("--plan") + 1])


def test_an_old_layouts_folder_name_finds_the_renamed_folder(tmp_path, monkeypatch, committed, capsys):
    root = tmp_path / "art-frames"
    root.mkdir()
    monkeypatch.setattr(remote, "ROOT", root)
    with pytest.raises(ValueError, match="../dayframe-layouts does not exist"):
        remote.app_update_args(config(app={"local_repo": "../dayframe-layouts"}))
    (tmp_path / "art-frames-layouts").mkdir()
    args = remote.app_update_args(config(app={"local_repo": "../dayframe-layouts"}))
    assert plan_of(args) == {"layouts_repo": {"local_repo": str((tmp_path / "art-frames-layouts").resolve())}}
    assert "Using ../art-frames-layouts (local_repo still says ../dayframe-layouts)." in capsys.readouterr().out
    # Only that exact old name has a fallback.
    with pytest.raises(ValueError, match="../layouts does not exist"):
        remote.app_update_args(config(app={"local_repo": "../layouts"}))
    (tmp_path / "dayframe-layouts").mkdir()
    args = remote.app_update_args(config(app={"local_repo": "../dayframe-layouts"}))
    assert plan_of(args) == {"layouts_repo": {"local_repo": str((tmp_path / "dayframe-layouts").resolve())}}


def test_app_table_names_the_layouts_repo_and_sends_one_plan(tmp_path, monkeypatch, committed):
    monkeypatch.setattr(remote, "ROOT", tmp_path)
    (tmp_path.parent / "layouts").mkdir(exist_ok=True)
    args = remote.app_update_args(config(app={"layout": "morning", "local_repo": "../layouts"}))
    assert args[1:5] == [str(tmp_path / "scripts/install-layout.py"), "--remote", "--transfer-only", "--plan"]
    assert plan_of(args) == {"layouts_repo": {
        "layout": "morning", "local_repo": str((tmp_path / "../layouts").resolve())}}
    # A local_repo installs its committed HEAD, so a ref or repo there is a mistake.
    for extra in ({"ref": "main"}, {"repo": "owner/apps"}):
        with pytest.raises(ValueError, match="Leave out ref and repo"):
            remote.app_update_args(config(app={"local_repo": "../layouts", **extra}))
    with pytest.raises(ValueError, match="The layouts repo folder ../missing does not exist"):
        remote.app_update_args(config(app={"local_repo": "../missing"}))
    assert plan_of(remote.app_update_args(config(app={"repo": "owner/apps", "skip": ["news"]}))) == {
        "layouts_repo": {"repo": "owner/apps", "skip": ["news"]}}
    assert plan_of(remote.app_update_args(config(app={"app": "."}))) == {"layouts_repo": {"app": "."}}
    assert remote.app_update_args(config()) is None


def test_sources_resolve_local_folders_and_keep_git_refs(tmp_path, monkeypatch, committed):
    monkeypatch.setattr(remote, "ROOT", tmp_path / "dayframe")
    (tmp_path / "my-apps").mkdir()
    (tmp_path / "layouts").mkdir()
    plan = plan_of(remote.app_update_args(config(
        app={"local_repo": "../layouts"},
        sources=[{"path": "../my-apps", "skip": ["clock"]},
                 {"git": "https://git.example.invalid/team/apps.git", "ref": "v2"}],
    )))
    assert plan["sources"] == [
        {"path": str((tmp_path / "my-apps").resolve()), "skip": ["clock"]},
        {"git": "https://git.example.invalid/team/apps.git", "ref": "v2"},
    ]


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"apps": [{"layout": "news"}]}, "[[apps]] is no longer used"),
        ({"app": {"layouts": ["morning", "news"]}}, "layouts = [...] is no longer used"),
        ({"app": {"local_repo": "../l", "skip": "news"}}, "skip"),
        ({"app": {"local_repo": "../l", "skip": ["news", "news"]}}, "skip"),
        ({"app": {"local_repo": "../l", "skip": ["-oOops"]}}, "skip"),
        ({"app": {"app": ".", "skip": ["news"]}}, "without a catalog"),
        ({"app": {"local_repo": "../l", "password": "no"}}, "[app]"),
        ({"sources": [{"git": "https://git.example.invalid/a.git", "ref": "main"}]}, "Add an [app] table"),
        ({"app": {"local_repo": "../l"}, "sources": {"path": "../x"}}, "[[sources]]"),
        ({"app": {"local_repo": "../l"}, "sources": [{"path": "../x", "git": "https://h/a.git"}]}, "path"),
        ({"app": {"local_repo": "../l"}, "sources": [{"git": "https://git.example.invalid/a.git"}]}, "needs a ref"),
        ({"app": {"local_repo": "../l"}, "sources": [{"git": "file:///etc", "ref": "main"}]}, "https://"),
        ({"app": {"local_repo": "../l"}, "sources": [{"git": "--upload-pack=x", "ref": "main"}]}, "https://"),
        ({"app": {"local_repo": "../l"}, "sources": [{"path": "../missing-folder"}]}, "does not exist"),
        ({"app": {"local_repo": "../l"}, "sources": [{"path": "../x", "token": "no"}]}, "[[sources]]"),
    ],
)
def test_invalid_app_settings_fail_before_connection(changes, message, monkeypatch, committed):
    monkeypatch.setattr(remote, "ensure_connection", lambda *args: pytest.fail("Must validate before SSH"))
    with pytest.raises(ValueError) as error:
        remote.update(config(compose="compose.static.yaml", **changes), ["test-ssh"])
    assert message in str(error.value)


# --- Shared Compose helper --------------------------------------------------------------


def helper(tmp_path, command):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(SCRIPTS / "compose-files.sh", tmp_path / "scripts/compose-files.sh")
    return subprocess.run(["sh", "-c", ". scripts/compose-files.sh && " + command], cwd=tmp_path,
                          capture_output=True, text=True)


def compose_args(tmp_path, compose, *, override):
    (tmp_path / "sites").mkdir(exist_ok=True)
    if override:
        (tmp_path / "sites/compose.apps.yaml").write_text("services: {}\n")
    return helper(tmp_path, "printf '%s ' " + remote.compose_files(compose)).stdout.split()


def test_compose_commands_add_the_override_only_when_it_exists(tmp_path):
    assert compose_args(tmp_path, "compose.static.yaml", override=False) == ["-f", "compose.static.yaml"]
    assert compose_args(tmp_path, "compose.yaml", override=True) == ["-f", "compose.yaml"]
    assert compose_args(tmp_path, "compose.static.host.yaml", override=True) == [
        "-f", "compose.static.host.yaml", "-f", "sites/compose.apps.yaml"]
    command = remote.logs_command(config(compose="compose.static.yaml"), "app-news")
    assert remote.compose_files("compose.static.yaml") in command and "service=app-news;" in command
    assert command.endswith(' logs --tail 200 "$service"')
    for bad in ("app-", "app-News", "app-news;id", "other"):
        with pytest.raises(ValueError):
            remote.logs_command(config(compose="compose.static.yaml"), bad)


def run_logs(tmp_path, service, first="morning"):
    """Run the logs command in a copy of the NAS folders, with sudo and docker that only print."""
    base = tmp_path / "dayframe"
    source = base / "source"
    (source / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS / "compose-files.sh", source / "scripts/compose-files.sh")
    (base / "sites/installed").mkdir(parents=True)
    (base / "sites/installed/.install-profile").write_text(f"{first} bridge 256 index.html {first}-0123 0\n")
    (base / "sites/compose.apps.yaml").write_text("services: {}\n")
    (source / "sites").symlink_to(Path("..") / "sites")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, content in {"sudo": 'shift; echo "$@"', "docker": "exit 0"}.items():
        (commands / name).write_text("#!/bin/sh\n" + content + "\n")
        (commands / name).chmod(0o700)
    command = remote.logs_command(config(path=str(base), compose="compose.static.yaml"), service, since="8h")
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True,
                          env=dict(os.environ, PATH=str(commands) + ":" + os.environ["PATH"]))


def test_logs_of_the_first_layout_use_the_website_service(tmp_path):
    result = run_logs(tmp_path / "first", "app-morning")
    override = "-f compose.static.yaml -f sites/compose.apps.yaml"
    assert result.stdout == f"compose -p dayframe {override} logs --since 8h website\n"
    assert result.stderr == "morning is the first layout. It runs as the website service.\n"
    # An extra layout keeps its own service, with the override for extra layouts.
    result = run_logs(tmp_path / "extra", "app-news")
    assert result.stdout == f"compose -p dayframe {override} logs --since 8h app-news\n"
    assert result.stderr == ""


def test_compose_helper_lists_extra_app_ids_only_for_static_profiles(tmp_path):
    (tmp_path / "sites").mkdir()
    (tmp_path / "sites/compose.apps.yaml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("DAYFRAME_APPS=old=old-1\nDAYFRAME_APPS=news=news-1 photos=photos-2\n")
    assert helper(tmp_path, "extra_app_ids compose.static.yaml").stdout.split() == ["news", "photos"]
    assert helper(tmp_path, "extra_app_ids compose.yaml").stdout == ""
    assert helper(tmp_path, "compose_files compose.evil.yaml").returncode != 0
    (tmp_path / ".env").write_text("DAYFRAME_APPS=News;id=x\n")
    assert helper(tmp_path, "extra_app_ids compose.static.yaml").returncode != 0
    for script in ("update-remote.sh", "rollback-remote.sh", "wait-healthy.sh"):
        text = (SCRIPTS / script).read_text()
        assert "compose-files.sh" in text and "DAYFRAME_APPS=" not in text, script


def admin_fixture(tmp_path, docker):
    """The prepared NAS layout with the real admin scripts, stub checks and a fake docker."""
    source = managed_layout(tmp_path)
    scripts = source / "scripts"
    for name in ("update-remote.sh", "wait-healthy.sh", "rollback-remote.sh", "compose-files.sh",
                 "previous-state.sh", "build-check.sh"):
        shutil.copy(SCRIPTS / name, scripts / name)
    for name in ("prepare-remote.sh", "check-host.sh", "check-containers.sh", "rename-check.sh"):
        (scripts / name).write_text("true\n")
    (source / "sites/compose.apps.yaml").write_text("services: {}\n")
    for app_id in ("news", "photos"):
        (source / "sites/apps" / app_id).mkdir(parents=True)
        (source / "sites/apps" / app_id / ".install-profile").write_text(f"{app_id} none 256 index.html {app_id}-1 0\n")
    (source / ".env").write_text("DAYFRAME_UID=10001\nDAYFRAME_APPS=news=news-0123 photos=photos-4567\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "id").write_text("#!/bin/sh\necho 0\n")
    (commands / "sleep").write_text("#!/bin/sh\n")
    (commands / "chown").write_text('#!/bin/sh\nprintf "chown %s\\n" "$*" >> "$UPDATE_LOG"\n')
    head, _, rest = docker.partition("\n")
    (commands / "docker").write_text(head + "\n" + DOCKER_FILES_CHECK + rest)
    for executable in commands.iterdir():
        executable.chmod(0o700)
    log = tmp_path / "operations"
    env = {**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"], "UPDATE_LOG": str(log),
           "RESTORED": str(tmp_path / "restored")}
    return scripts, env, log


def test_update_builds_starts_and_keeps_previous_images_of_extra_apps(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, """#!/bin/sh
printf "%s\\n" "$*" >> "$UPDATE_LOG"
case "$*" in
    *" ps -q "*) echo container-id ;;
    "inspect --format"*) echo sha256:running ;;
esac
""")
    result = subprocess.run(["sh", str(scripts / "update-remote.sh"), "compose.static.yaml"], env=env,
                            capture_output=True, text=True)
    operations = log.read_text()
    assert result.returncode == 0, result.stderr
    files = "-f compose.static.yaml -f sites/compose.apps.yaml"
    assert f"compose -p dayframe {files} build" in operations
    assert f"compose -p dayframe {files} up -d --remove-orphans" in operations
    assert f"compose -p dayframe {files} exec -T dayframe" in operations
    for app_id in ("news", "photos"):
        assert f"compose -p dayframe {files} ps -q app-{app_id}" in operations
        assert f"tag sha256:running dayframe-app-{app_id}:previous" in operations


def test_rollback_restores_extra_app_images(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, """#!/bin/sh
printf "%s\\n" "$*" >> "$UPDATE_LOG"
case "$*" in
    "image inspect --format {{.Id}}"*) echo sha256:new ;;
    "image inspect"*) exit 0 ;;
esac
""")
    result = subprocess.run(["sh", str(scripts / "rollback-remote.sh"), "compose.static.host.yaml"], env=env,
                            capture_output=True, text=True)
    operations = log.read_text()
    assert result.returncode == 0, result.stderr
    assert "tag dayframe-app-news:previous dayframe-app-news:local" in operations
    assert "tag dayframe-app-photos:previous dayframe-app-photos:local" in operations
    # --remove-orphans, as in update-remote.sh: a container of a removed app does not linger.
    assert "compose -p dayframe -f compose.static.host.yaml -f sites/compose.apps.yaml up -d --remove-orphans" in operations




RUNNING = """#!/bin/sh
printf "%s\\n" "$*" >> "$UPDATE_LOG"
case "$*" in
    "ps -q --filter"*) echo controller-id ;;
    *" ps -q "*) echo container-id ;;
    "inspect --format"*) echo sha256:running ;;
    "image inspect --format {{.Id}}"*) echo sha256:new ;;
    "image inspect"*) exit 0 ;;
esac
"""


@pytest.mark.parametrize("broken", ["", "photos"])
def test_update_checks_each_extra_app_image_before_activation(tmp_path, broken):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    (scripts / "check-containers.sh").write_text(
        'printf "check %s\\n" "$4" >> "$UPDATE_LOG"\n'
        'test "$4" != "dayframe-app-$BROKEN:local"\n'
    )
    result = subprocess.run(["sh", str(scripts / "update-remote.sh"), "compose.static.yaml"],
                            env={**env, "BROKEN": broken}, capture_output=True, text=True)
    operations = log.read_text()
    checks = [line.split()[1] for line in operations.splitlines() if line.startswith("check ")]
    if broken:
        # A broken app image stops the update before anything is activated.
        assert result.returncode != 0
        assert checks == ["dayframe-website:local", "dayframe-app-news:local", "dayframe-app-photos:local"]
        assert " up -d" not in operations
    else:
        assert result.returncode == 0, result.stderr
        assert checks == ["dayframe-website:local", "dayframe-app-news:local", "dayframe-app-photos:local"]
        assert operations.index("check dayframe-app-photos:local") < operations.index(" up -d")


def run_script(scripts, name, env, *args, stdin=None):
    return subprocess.run(["sh", str(scripts / name), *args], env=env, capture_output=True, text=True, input=stdin)


def test_update_saves_settings_and_rollback_restores_them_once(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    data = scripts.parent / "data"
    (data / "settings.json").write_text('{"version": "before"}')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    saved = tmp_path / ".previous"
    assert (saved / "settings.json").read_text() == '{"version": "before"}'
    assert (saved / "settings.json").stat().st_mode & 0o777 == 0o600
    assert saved.stat().st_mode & 0o777 == 0o700
    operations = log.read_text()
    # The controller stops before the copy and the new version starts after it.
    assert operations.index("stop controller-id") < operations.index(" up -d")
    assert operations.index(" build") < operations.index("stop controller-id")
    # The new version saves settings that the previous image's strict schema rejects.
    (data / "settings.json").write_text('{"version": "after", "app_schedule": {}}')
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert (data / "settings.json").read_text() == '{"version": "before"}'
    assert "Settings returned to their state before the update." in result.stdout
    assert "chown -h 10001:10001" in log.read_text()
    assert not (saved / "settings.json").exists() and len(list(saved.glob("settings.restored-*.json"))) == 1
    assert not list(data.glob(".settings-*"))
    # A second rollback keeps the settings saved since the first one.
    (data / "settings.json").write_text('{"version": "changed after rollback"}')
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert "settings stay as they are" in result.stdout
    assert (data / "settings.json").read_text() == '{"version": "changed after rollback"}'


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_update_refuses_a_linked_settings_file_and_changes_nothing(tmp_path, kind):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    data = scripts.parent / "data"
    secret = tmp_path / "host-secret"
    secret.write_text("HOST-SECRET")
    if kind == "symlink":
        (data / "settings.json").symlink_to(secret)
    else:
        os.link(secret, data / "settings.json")
    before = (scripts.parent / ".env").read_text()
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode != 0
    assert "not a plain file" in result.stderr
    assert "Nothing changed on the TVs. The previous version is still running." in result.stderr
    assert not (tmp_path / ".previous/settings.json").exists()
    assert "HOST-SECRET" not in "".join(p.read_text() for p in (tmp_path / ".previous").iterdir() if p.is_file())
    operations = log.read_text()
    assert " up -d" not in operations and "start controller-id" in operations
    assert (scripts.parent / ".env").read_text() == before


def test_rollback_refuses_a_linked_settings_file(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    data = scripts.parent / "data"
    (data / "settings.json").write_text('{"version": "before"}')
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0
    target = tmp_path / "host-file"
    target.write_text("KEEP")
    (data / "settings.json").unlink()
    (data / "settings.json").symlink_to(target)
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode != 0 and "not a plain file" in result.stderr
    assert target.read_text() == "KEEP" and (data / "settings.json").is_symlink()


def test_failure_before_activation_restores_files_and_images(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    source = scripts.parent
    before_env = (source / ".env").read_text()
    before_override = (source / "sites/compose.apps.yaml").read_text()
    # prepare-remote.sh writes the new app set: news stays, photos goes, clock is new.
    (scripts / "prepare-remote.sh").write_text(
        'printf "DAYFRAME_UID=10001\\nDAYFRAME_APPS=news=news-0123 clock=clock-1\\n" > .env\n'
        'printf "services: {new: {}}\\n" > sites/compose.apps.yaml\n'
    )
    (scripts / "check-containers.sh").write_text('test "$4" != dayframe-app-clock:local\n')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "Nothing changed on the TVs. The previous version is still running." in result.stderr
    assert (source / ".env").read_text() == before_env
    assert (source / "sites/compose.apps.yaml").read_text() == before_override
    operations = log.read_text()
    assert " up -d" not in operations and "stop controller-id" not in operations
    for name in ("dayframe", "dayframe-website", "dayframe-app-news", "dayframe-app-photos"):
        assert f"tag {name}:previous {name}:local" in operations
    assert "image rm dayframe-app-clock:local" in operations


def test_rollback_refuses_when_an_old_app_is_gone(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0
    shutil.rmtree(scripts.parent / "sites/apps/photos")
    log.write_text("")
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "photos" in result.stderr and "redeploy the previous commit" in result.stderr
    assert " tag " not in log.read_text() and " up -d" not in log.read_text()


def test_rollback_asks_before_restoring_settings_older_than_an_hour(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    data = scripts.parent / "data"
    (data / "settings.json").write_text('{"version": "before"}')
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0
    (tmp_path / ".previous/saved-at").write_text("1000\n")
    (data / "settings.json").write_text('{"version": "after"}')
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml", stdin="no\n")
    assert result.returncode == 1 and "Rollback stopped. Nothing changed." in result.stdout
    assert "minutes old" in result.stdout
    assert (data / "settings.json").read_text() == '{"version": "after"}'
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml", stdin="yes\n")
    assert result.returncode == 0, result.stderr
    assert (data / "settings.json").read_text() == '{"version": "before"}'


def write_app(folder, app_id, text):
    folder.mkdir(parents=True)
    (folder / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
    (folder / ".install-profile").write_text(f"{app_id} none 256 index.html {app_id}-1 0\n")
    (folder / "app.txt").write_text(text)


def stage(base, first, extras):
    """What install-layout.py --remote leaves on the NAS: the new apps in sites/.incoming."""
    incoming = base / "sites/.incoming"
    write_app(incoming / "installed", first, f"new {first}")
    (incoming / "apps").mkdir()
    (incoming / "apps/.dayframe-apps-managed").write_text("dayframe-apps-source-v1\n")
    for app_id in extras:
        write_app(incoming / "apps" / app_id, app_id, f"new {app_id}")
    (incoming / ".complete").write_text("installed\napps\n")


def test_update_that_removes_the_last_extra_app_succeeds(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    # prepare-remote.sh finds no extra apps: it removes the override and DAYFRAME_APPS.
    (scripts / "prepare-remote.sh").write_text('printf "DAYFRAME_UID=10001\\n" > .env\nrm -f sites/compose.apps.yaml\n')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    operations = log.read_text()
    assert "stop controller-id" in operations
    assert "compose -p dayframe -f compose.static.yaml up -d --remove-orphans" in operations
    # The next update works too.
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0


def test_staged_apps_move_in_at_update_and_back_on_failure(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    (scripts / "check-containers.sh").write_text('test "$4" != dayframe-website:local\n')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "Nothing changed on the TVs. The previous version is still running." in result.stderr
    # The running apps are back in place; the old extra apps never left.
    assert (tmp_path / "sites/installed/app.txt").read_text() == "old morning"
    assert (tmp_path / "sites/apps/news/.install-profile").exists()
    assert not (tmp_path / "sites/apps/clock").exists()


def test_automatic_rollback_after_a_first_app_change_restores_the_old_apps(tmp_path):
    docker = RUNNING.replace("esac", '    *" exec "*) test -e "$RESTORED" ;;\n    "tag dayframe:previous dayframe:local") touch "$RESTORED" ;;\nesac')
    scripts, env, log = admin_fixture(tmp_path, docker)
    (tmp_path / ".env").write_text("DAYFRAME_UID=10001\nDAYFRAME_LAYOUT_ID=morning\nDAYFRAME_APPS=news=news-0123 photos=photos-4567\n")
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["morning"])
    data = tmp_path / "data"
    (data / "settings.json").write_text('{"version": "before"}')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 3, result.stderr
    assert "Rolled back to the previous images." in result.stdout
    assert (tmp_path / "sites/installed/app.txt").read_text() == "old morning"
    assert sorted(path.name for path in (tmp_path / "sites/apps").iterdir()) == ["news", "photos"]
    assert "DAYFRAME_LAYOUT_ID=morning" in (tmp_path / ".env").read_text()
    assert not (tmp_path / ".update-lock").exists()


def test_manual_rollback_restores_the_app_folders_of_the_update(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", [])
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0
    assert (tmp_path / "sites/installed/app.txt").read_text() == "new news"
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sites/installed/app.txt").read_text() == "old morning"
    assert (tmp_path / "sites/apps/photos/.install-profile").exists()


def test_failed_file_restore_never_leaves_an_empty_env(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    (scripts / "prepare-remote.sh").write_text('printf "DAYFRAME_UID=10001\\nWRITTEN=by-prepare\\n" > .env\n')
    (scripts / "check-containers.sh").write_text("exit 1\n")
    # cp fails when it writes a restore temp file, as with a full disk.
    real_cp = shutil.which("cp")
    (tmp_path / "bin/cp").write_text(
        f'#!/bin/sh\ncase "$*" in *.restore.*) exit 1 ;; esac\nexec {real_cp} "$@"\n')
    (tmp_path / "bin/cp").chmod(0o700)
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "Could not put back .env" in result.stderr
    assert (tmp_path / ".env").read_text() == "DAYFRAME_UID=10001\nWRITTEN=by-prepare\n"
    assert not list(tmp_path.glob(".restore.*")) and not list((tmp_path / "sites").glob(".restore.*"))


def test_lost_connection_after_activation_still_finishes_the_update(tmp_path):
    docker = RUNNING.replace("esac", '    *" up -d "*) kill -HUP "$PPID" ;;\nesac')
    scripts, env, log = admin_fixture(tmp_path, docker)
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert " exec -T dayframe" in log.read_text()  # the health check still ran


def test_a_second_update_or_rollback_waits_for_the_first(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    (tmp_path / ".update-lock").mkdir()
    for script in ("update-remote.sh", "rollback-remote.sh"):
        result = run_script(scripts, script, env, "compose.static.yaml")
        assert result.returncode == 1 and "Another update is running." in result.stderr
    assert (tmp_path / ".update-lock").is_dir()
    assert " up -d" not in (log.read_text() if log.exists() else "")
    (tmp_path / ".update-lock").rmdir()
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0
    assert not (tmp_path / ".update-lock").exists()


def test_failed_first_deploy_says_nothing_was_started(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$UPDATE_LOG"\n')
    (scripts / "check-containers.sh").write_text("exit 1\n")
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "Nothing was started." in result.stderr and "still running" not in result.stderr


def fake_mv(tmp_path, body):
    """A mv that runs <body> first. $1 and $2 are its arguments."""
    real_mv = shutil.which("mv")
    (tmp_path / "bin/mv").write_text(f'#!/bin/sh\n{body}\nexec {real_mv} "$@"\n')
    (tmp_path / "bin/mv").chmod(0o700)


def sites_state(base):
    """("old" or "new" for sites/installed, the app IDs in sites/apps)."""
    first = (base / "sites/installed/app.txt").read_text().split()[0]
    return first, sorted(path.name for path in (base / "sites/apps").iterdir() if not path.name.startswith("."))


SWAP_MOVES = [
    "*/sites/installed\\ */.previous/sites/installed",
    "*/.incoming/installed\\ */sites/installed",
    "*/sites/apps\\ */.previous/sites/apps",
    "*/.incoming/apps\\ */sites/apps",
]


@pytest.mark.parametrize("move", SWAP_MOVES)
def test_failed_swap_leaves_every_old_app_folder_in_place(tmp_path, move):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    fake_mv(tmp_path, f'case "$1 $2" in {move}) exit 1 ;; esac')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert "Nothing changed on the TVs. The previous version is still running." in result.stderr
    assert "stop controller-id" not in log.read_text()
    assert not (tmp_path / ".previous/sites").exists()


@pytest.mark.parametrize("signal", ["HUP", "INT", "TERM"])
def test_signal_during_the_swap_does_not_stop_it_halfway(tmp_path, signal):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    fake_mv(tmp_path, f'case "$1 $2" in {SWAP_MOVES[1]}) kill -{signal} "$PPID" ;; esac')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert sites_state(tmp_path) == ("new", ["clock"])


def test_apps_build_from_incoming_and_move_in_just_before_the_controller_stops(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    (scripts / "prepare-remote.sh").write_text(
        'printf "prepare %s %s\\n" "${DAYFRAME_FIRST_DIR#$PWD/../}" "${DAYFRAME_APPS_DIR#$PWD/../}" >> "$UPDATE_LOG"\n')
    (scripts / "check-containers.sh").write_text(
        'printf "build %s %s %s\\n" "$(cat sites/installed/app.txt)" "$DAYFRAME_SITE_BUILD_CONTEXT" '
        '"$DAYFRAME_APPS_CONTEXT" >> "$UPDATE_LOG"\n')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    operations = log.read_text()
    assert f"prepare {tmp_path}/sites/.incoming/installed {tmp_path}/sites/.incoming/apps" in operations.replace(
        "/source/../", "/")
    # The running controller still saw the old apps during the build.
    assert "build old morning ./sites/.incoming/installed ./sites/.incoming/apps" in operations
    assert sites_state(tmp_path) == ("new", ["clock"])
    assert not (tmp_path / "sites/.incoming").exists()


def test_symlinked_incoming_folder_is_refused_before_anything_moves(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    shutil.rmtree(tmp_path / "sites/.incoming/apps")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "sites/.incoming/apps").symlink_to(tmp_path / "elsewhere")
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "sites/.incoming is not a valid layout transfer. Run the deploy again." in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert not log.exists() or " build" not in log.read_text()


def test_half_transfer_is_removed_and_the_installed_apps_stay(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    (tmp_path / "sites/.incoming/.complete").unlink()
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert "Removed sites/.incoming: its transfer did not finish." in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert not (tmp_path / "sites/.incoming").exists()


def test_browser_profile_update_removes_a_stale_transfer(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    result = run_script(scripts, "update-remote.sh", env, "compose.yaml")
    assert result.returncode == 0, result.stderr
    assert "Removed the layouts in sites/.incoming: a browser Compose profile runs no layout." in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert not (tmp_path / "sites/.incoming").exists()


def test_controller_must_be_one_dayframe_container(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING.replace("echo controller-id", "printf 'one\\ntwo\\n'"))
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "More than one Art Frames controller is running." in result.stderr
    operations = log.read_text()
    assert "--filter ancestor=dayframe:local --filter ancestor=dayframe:previous" in operations
    assert " build" not in operations and "stop " not in operations


def test_controller_that_does_not_start_again_is_reported(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING.replace("esac", '    "start "*) exit 1 ;;\nesac'))
    (tmp_path / "elsewhere.json").write_text("{}")
    (tmp_path / "data/settings.json").symlink_to(tmp_path / "elsewhere.json")
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "The previous version could not be started again. Run: sudo docker start controller-id" in result.stderr
    assert "still running" not in result.stderr


def place_fixture(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    shutil.copy(SCRIPTS / "place-apps.sh", scripts / "place-apps.sh")
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    return scripts, env


def test_place_apps_swaps_and_removes_the_old_folders_after(tmp_path):
    scripts, env = place_fixture(tmp_path)
    result = run_script(scripts, "place-apps.sh", env, "static")
    assert result.returncode == 0, result.stderr
    assert sites_state(tmp_path) == ("new", ["clock"])
    assert not (tmp_path / ".replaced-sites").exists() and not (tmp_path / "sites/.incoming").exists()
    assert not (tmp_path / ".update-lock").exists()


@pytest.mark.parametrize("move", [m.replace(".previous/sites", ".replaced-sites") for m in SWAP_MOVES])
def test_place_apps_failure_keeps_the_installed_apps(tmp_path, move):
    scripts, env = place_fixture(tmp_path)
    fake_mv(tmp_path, f'case "$1 $2" in {move}) exit 1 ;; esac')
    result = run_script(scripts, "place-apps.sh", env, "static")
    assert result.returncode == 1
    assert "Could not move the new layouts into place. The installed layouts stay as they are." in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert not (tmp_path / ".replaced-sites").exists()


def test_place_apps_puts_back_a_swap_that_stopped_halfway(tmp_path):
    scripts, env = place_fixture(tmp_path)
    replaced = tmp_path / ".replaced-sites"
    replaced.mkdir()
    (tmp_path / "sites/installed").rename(replaced / "installed")
    (replaced / "steps").write_text("out installed\nin installed\n")
    shutil.rmtree(tmp_path / "sites/.incoming")
    result = run_script(scripts, "place-apps.sh", env, "static")
    assert result.returncode == 0, result.stderr
    assert "Put back the layout folders of an earlier run that stopped halfway." in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])


def killed_mid_swap(tmp_path):
    """An update killed (SIGKILL) right after the old first app moved out. Its lock stays."""
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    real_mv = shutil.which("mv")
    (tmp_path / "bin/mv").write_text(
        f'#!/bin/sh\n{real_mv} "$@"; s=$?\n'
        f'case "$1 $2" in {SWAP_MOVES[0]}) kill -9 "$PPID" ;; esac\nexit $s\n')
    (tmp_path / "bin/mv").chmod(0o700)
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode != 0
    assert not (tmp_path / "sites/installed").exists()
    (tmp_path / "bin/mv").unlink()
    (tmp_path / ".update-lock").rmdir()  # as the lock message says
    return scripts, env


def failed_restore(tmp_path):
    """An update that failed after the swap, and whose move back of the first app failed too."""
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    (tmp_path / "elsewhere.json").write_text("{}")
    (tmp_path / "data/settings.json").symlink_to(tmp_path / "elsewhere.json")
    fake_mv(tmp_path, 'case "$1 $2" in */.previous/sites/installed\\ */sites/installed) exit 1 ;; esac')
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "Could not put back the layout folders. The steps are in" in result.stderr
    assert "still running" not in result.stderr or "not all put back" in result.stderr
    (tmp_path / "bin/mv").unlink()
    (tmp_path / "data/settings.json").unlink()
    return scripts, env


@pytest.mark.parametrize("broken", [killed_mid_swap, failed_restore])
def test_next_update_puts_back_an_unfinished_swap_first(tmp_path, broken):
    scripts, env = broken(tmp_path)
    shutil.rmtree(tmp_path / "sites/.incoming", ignore_errors=True)
    stage(tmp_path, "news", ["clock"])
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert ("Put back the layout folders, .env and the layout override of an update that stopped "
            "before it started the new version.") in result.stderr
    assert sites_state(tmp_path) == ("new", ["clock"])
    assert "done" in (tmp_path / ".previous/sites/steps").read_text().split()
    # The old apps were saved again, so a rollback still finds them.
    assert run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml").returncode == 0
    assert sites_state(tmp_path) == ("old", ["news", "photos"])


@pytest.mark.parametrize("broken", [killed_mid_swap, failed_restore])
def test_rollback_puts_back_an_unfinished_swap_first(tmp_path, broken):
    scripts, env = broken(tmp_path)
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert ("Put back the layout folders, .env and the layout override of an update that stopped "
            "before it started the new version.") in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])


def test_update_refuses_when_an_unfinished_swap_cant_be_put_back(tmp_path):
    scripts, env = killed_mid_swap(tmp_path)
    fake_mv(tmp_path, 'case "$1 $2" in */.previous/sites/installed\\ */sites/installed) exit 1 ;; esac')
    for script in ("update-remote.sh", "rollback-remote.sh"):
        result = run_script(scripts, script, env, "compose.static.yaml")
        assert result.returncode == 1
        assert f"Could not put back the layout folders. The steps are in {tmp_path}/.previous/sites/steps." in result.stderr
        assert (tmp_path / ".previous/sites/installed/app.txt").read_text() == "old morning"


def test_save_state_never_deletes_an_unfinished_swap(tmp_path):
    saved = tmp_path / ".previous/sites"
    saved.mkdir(parents=True)
    (saved / "steps").write_text("out installed\n")
    script = f'source_dir={tmp_path}/source base={tmp_path}; . {SCRIPTS}/previous-state.sh; save_state morning ""'
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == 1 and "holds a swap that did not finish" in result.stderr
    assert (saved / "steps").exists()


def test_rollback_says_when_the_app_folders_cant_be_put_back(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    stage(tmp_path, "news", ["clock"])
    assert run_script(scripts, "update-remote.sh", env, "compose.static.yaml").returncode == 0
    fake_mv(tmp_path, 'case "$1 $2" in */.previous/sites/installed\\ */sites/installed) exit 1 ;; esac')
    result = run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 1
    assert "The steps are in" in result.stderr and "Run the rollback again." in result.stderr
    (tmp_path / "bin/mv").unlink()
    assert run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml").returncode == 0
    assert sites_state(tmp_path) == ("old", ["news", "photos"])


def test_place_apps_prepares_from_incoming_before_the_swap(tmp_path):
    scripts, env = place_fixture(tmp_path)
    (scripts / "prepare-remote.sh").write_text(
        'printf "%s %s\\n" "${DAYFRAME_FIRST_DIR##*/sites/}" "${DAYFRAME_APPS_DIR##*/sites/}" >> "$UPDATE_LOG"\n'
        'test -z "$FAIL"\n')
    result = run_script(scripts, "place-apps.sh", {**env, "FAIL": "1"}, "static")
    assert result.returncode == 1
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert (tmp_path / "sites/.incoming/.complete").exists()
    result = run_script(scripts, "place-apps.sh", {**env, "FAIL": ""}, "static")
    assert result.returncode == 0, result.stderr
    assert sites_state(tmp_path) == ("new", ["clock"])
    assert (tmp_path / "operations").read_text().splitlines()[-1] == ".incoming/installed .incoming/apps"


STOPPED_UPDATE = ("Put back the layout folders, .env and the layout override of an update that stopped "
                  "before it started the new version.")


def test_place_apps_first_puts_back_a_stopped_update(tmp_path):
    scripts, env = killed_mid_swap(tmp_path)
    shutil.copy(SCRIPTS / "place-apps.sh", scripts / "place-apps.sh")
    shutil.rmtree(tmp_path / "sites/.incoming", ignore_errors=True)
    stage(tmp_path, "photos", ["z"])
    result = run_script(scripts, "place-apps.sh", env, "static")
    assert result.returncode == 0, result.stderr
    assert STOPPED_UPDATE in result.stderr
    assert sites_state(tmp_path) == ("new", ["z"])
    assert not (tmp_path / ".previous/unfinished").exists() and not (tmp_path / ".previous/sites").exists()


def test_saved_app_folders_without_steps_are_left_alone(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "cur morning")
    saved = tmp_path / ".previous/sites"
    write_app(saved / "installed", "old", "old old")
    (tmp_path / ".previous/app-set").write_text("first old\nextra \n")
    for script in ("rollback-remote.sh", "update-remote.sh"):
        result = run_script(scripts, script, env, "compose.static.yaml")
        assert result.returncode == 1
        assert f"{saved} holds layout folders but no steps file" in result.stderr
        assert (saved / "installed/app.txt").read_text() == "old old"
    assert (tmp_path / "sites/installed/app.txt").read_text() == "cur morning"


def test_empty_saved_folder_is_removed_without_a_message(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    (tmp_path / ".previous/sites").mkdir(parents=True)
    stage(tmp_path, "news", ["clock"])
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    assert "Put back" not in result.stderr
    assert sites_state(tmp_path) == ("new", ["clock"])


# A prepare-remote.sh that writes the first app's ID to .env, as the real one does.
WRITES_ENV = ('id=$(cut -d" " -f1 "${DAYFRAME_FIRST_DIR:-$PWD/../sites/installed}/.install-profile"); '
              'grep -v ^DAYFRAME_LAYOUT_ID= ../.env > ../.env.t; echo "DAYFRAME_LAYOUT_ID=$id" >> ../.env.t; '
              'mv ../.env.t ../.env\n')


@pytest.mark.parametrize("killed", ["swap", "build"])
def test_stopped_update_puts_back_env_so_later_runs_save_the_right_one(tmp_path, killed):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    (scripts / "prepare-remote.sh").write_text(WRITES_ENV)
    write_app(tmp_path / "sites/installed", "morning", "old morning")
    (tmp_path / ".env").write_text((tmp_path / ".env").read_text() + "DAYFRAME_LAYOUT_ID=morning\n")
    stage(tmp_path, "news", ["clock"])
    if killed == "swap":
        real_mv = shutil.which("mv")
        (tmp_path / "bin/mv").write_text(
            f'#!/bin/sh\n{real_mv} "$@"; s=$?\ncase "$1 $2" in {SWAP_MOVES[0]}) kill -9 "$PPID" ;; esac\nexit $s\n')
        (tmp_path / "bin/mv").chmod(0o700)
    else:
        # SIGKILL the update itself, the parent of build-check.sh, during the image check.
        (scripts / "check-containers.sh").write_text('kill -9 "$(ps -o ppid= -p "$PPID")"\nsleep 5\n')
    run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert env_value(tmp_path, "DAYFRAME_LAYOUT_ID") == "news"  # written for the apps that never started
    (tmp_path / "bin/mv").unlink(missing_ok=True)
    (scripts / "check-containers.sh").write_text("true\n")
    (tmp_path / ".update-lock").rmdir()
    shutil.rmtree(tmp_path / "sites/.incoming", ignore_errors=True)
    stage(tmp_path, "photos", ["clock"])
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    if killed == "swap":
        assert STOPPED_UPDATE in result.stderr
    else:
        # No folder moved before the build, so the message names only the files.
        assert STOPPED_UPDATE not in result.stderr
        assert ("Put back .env and the layout override of an update that stopped "
                "before it started the new version.") in result.stderr
    assert "DAYFRAME_LAYOUT_ID=morning" in (tmp_path / ".previous/env").read_text()
    assert run_script(scripts, "rollback-remote.sh", env, "compose.static.yaml").returncode == 0
    assert env_value(tmp_path, "DAYFRAME_LAYOUT_ID") == "morning"
    assert sites_state(tmp_path) == ("old", ["news", "photos"])


def test_place_apps_prepares_the_installed_apps_after_putting_them_back(tmp_path):
    scripts, env = place_fixture(tmp_path)
    (scripts / "prepare-remote.sh").write_text('printf "prepare %s\\n" "${DAYFRAME_FIRST_DIR:-installed}" >> "$UPDATE_LOG"\n')
    fake_mv(tmp_path, 'case "$1 $2" in */.incoming/apps\\ */sites/apps) exit 1 ;; '
                      '*/.replaced-sites/installed\\ */sites/installed) exit 1 ;; esac')
    assert run_script(scripts, "place-apps.sh", env, "static").returncode == 1
    (tmp_path / "bin/mv").unlink()
    (tmp_path / "operations").write_text("")
    result = run_script(scripts, "place-apps.sh", env, "static")
    assert "Put back the layout folders of an earlier run that stopped halfway." in result.stderr
    assert sites_state(tmp_path) == ("old", ["news", "photos"])
    assert (tmp_path / "operations").read_text().splitlines()[0] == "prepare installed"


def test_automatic_rollback_does_not_ask(tmp_path):
    update = (SCRIPTS / "update-remote.sh").read_text()
    assert 'rollback-remote.sh --yes "$compose"' in update


def test_healthy_update_removes_app_tags_nothing_uses(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING.replace(
        'esac', '    "image ls"*) printf "%s\\\\n" dayframe-app-gone:local dayframe-app-news:local '
        'dayframe-app-news:previous dayframe-app-old:previous ;;\nesac'))
    result = run_script(scripts, "update-remote.sh", env, "compose.static.yaml")
    assert result.returncode == 0, result.stderr
    operations = log.read_text()
    assert "image rm dayframe-app-gone:local" in operations
    assert "image rm dayframe-app-old:previous" in operations
    assert "image rm dayframe-app-news" not in operations


def test_rollback_without_saved_settings_says_settings_stay(tmp_path):
    scripts, env, log = admin_fixture(tmp_path, RUNNING)
    result = subprocess.run(["sh", str(scripts / "rollback-remote.sh"), "compose.static.yaml"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "settings stay as they are" in result.stdout
    assert " stop dayframe" not in log.read_text()


# --- Whole catalogs and extra sources ----------------------------------------------------


def catalog(path, app_ids, *, default=None, paths=None):
    for app_id in app_ids:
        package(path / "layouts" / app_id, app_id)
    (path / "catalog.json").write_text(json.dumps({
        "schema_version": 1, "default": default or app_ids[0],
        "layouts": [{"id": app_id, "name": app_id.title() + " app", "path": (paths or {}).get(app_id, "layouts/" + app_id)}
                    for app_id in app_ids],
    }))
    return path


def commit_all(path):
    for args in (["init", "-q"], ["add", "."],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def git_repository(path, app_ids, **options):
    catalog(path, app_ids, **options)
    commit_all(path)
    return path


def collect(tmp_path, plan):
    directory = tempfile.mkdtemp(dir=tmp_path, prefix="work-")
    messages = []
    items = installer.collect(plan, directory, report=lambda text, **_: messages.append(text))
    return items, messages


def ids(items):
    return [installer.read_manifest(package).id for package, _ in items]


def source_file(root, app_id=None):
    folder = root / "sites/installed" if app_id is None else root / "sites/apps" / app_id
    return json.loads((folder / ".dayframe-source.json").read_text())


def test_whole_catalog_installs_with_the_default_first(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news", "photos"], default="news")
    items, messages = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})
    assert ids(items) == ["news", "morning", "photos"]
    root = host(tmp_path)
    installer.install_all(root, items)
    assert env_value(root, "DAYFRAME_LAYOUT_ID") == "news"
    assert env_value(root, "DAYFRAME_APPS").split() == [
        f"morning={scope_of(root / 'sites/apps/morning')}", f"photos={scope_of(root / 'sites/apps/photos')}"]
    commit = subprocess.check_output(["git", "-C", str(layouts), "rev-parse", "HEAD"], text=True).strip()
    assert messages == [f"layouts (repo): {commit}. Layouts: news, morning, photos"]


def test_old_layout_setting_picks_the_first_app_and_the_rest_still_install(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news", "photos"])
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "layout": "photos"}})
    assert ids(items) == ["photos", "morning", "news"]
    with pytest.raises(installer.InstallError, match="skip also lists"):
        collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "layout": "photos", "skip": ["photos"]}})


def test_scope_of_first_app_is_unchanged_by_the_whole_catalog(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    single = host(tmp_path / "single")
    installer.install_first(single, *collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "app": "layouts/morning"}})[0][0])
    whole = host(tmp_path / "whole")
    installer.install_all(whole, collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})[0])
    assert scope_of(whole / "sites/installed") == scope_of(single / "sites/installed")


def test_skip_leaves_apps_out_and_a_new_catalog_app_appears_on_the_next_install(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    root = host(tmp_path)
    installer.install_all(root, collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})[0])
    assert (root / "sites/apps/news").is_dir()
    items, messages = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "skip": ["news", "clock"]}})
    installer.install_all(root, items)
    assert not (root / "sites/apps/news").exists()
    assert "Note: skip lists clock, which is not in layouts. Nothing to skip." in messages
    # Skipping the default makes the next catalog app the first app.
    assert ids(collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "skip": ["morning"]}})[0]) == ["news"]
    # A new app committed to the repo installs with no settings change.
    package(layouts / "layouts/photos", "photos")
    data = json.loads((layouts / "catalog.json").read_text())
    data["layouts"].append({"id": "photos", "name": "Photos", "path": "layouts/photos"})
    (layouts / "catalog.json").write_text(json.dumps(data))
    subprocess.run(["git", "-C", str(layouts), "add", "."], check=True)
    subprocess.run(["git", "-C", str(layouts), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                    "commit", "-qm", "photos"], check=True)
    installer.install_all(root, collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})[0])
    assert sorted(path.name for path in (root / "sites/apps").iterdir() if not path.name.startswith(".")) == [
        "news", "photos"]


def test_folder_source_adds_its_apps_and_records_where_each_came_from(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    folder = catalog(tmp_path / "my-apps", ["clock", "chores"])
    plan = {"layouts_repo": {"local_repo": str(layouts)},
            "sources": [{"path": str(folder), "skip": ["chores"]}]}
    items, messages = collect(tmp_path, plan)
    assert ids(items) == ["morning", "news", "clock"]
    assert messages[1] == "my-apps (path): no commit (plain folder). Layouts: clock"
    root = host(tmp_path)
    installer.install_all(root, items)
    commit = subprocess.check_output(["git", "-C", str(layouts), "rev-parse", "HEAD"], text=True).strip()
    first = source_file(root)
    assert first.pop("installed_at").endswith("+00:00")
    assert first == {"schema_version": 1, "id": "morning", "label": "layouts", "kind": "repo",
                     "ref": first["ref"], "commit": commit, "catalog_name": "Morning app", "path": "layouts/morning"}
    clock = source_file(root, "clock")
    del clock["installed_at"]
    assert clock == {"schema_version": 1, "id": "clock", "label": "my-apps", "kind": "path",
                     "ref": None, "commit": None, "catalog_name": "Clock app", "path": "layouts/clock"}
    # The private file holds only what the runtime needs.
    assert json.loads((root / "sites/apps/clock/.dayframe-installed.json").read_text()) == {
        "id": "clock", "settings_scope": scope_of(root / "sites/apps/clock")}
    assert "clock=" in env_value(root, "DAYFRAME_APPS")


def test_git_folder_source_installs_only_committed_content(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    repo = tmp_path / "monorepo"
    catalog(repo / "dayframe", ["clock"])
    commit = commit_all(repo)
    (repo / "dayframe/layouts/clock/uncommitted.txt").write_text("not committed")
    items, messages = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)},
                                         "sources": [{"path": str(repo / "dayframe")}]})
    package, origin = items[1]
    assert not (package / "uncommitted.txt").exists()
    assert origin.commit == commit and origin.app_path == "dayframe/layouts/clock"
    assert messages[1] == f"dayframe (path): {commit}. Layouts: clock"


def test_same_app_id_in_two_sources_stops_and_names_both(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    folder = catalog(tmp_path / "my-apps", ["news"])
    plan = {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(folder)}]}
    with pytest.raises(installer.InstallError) as error:
        collect(tmp_path, plan)
    assert str(error.value) == (
        'Two sources give the layout ID "news": layouts and my-apps. Add skip = ["news"] to one of them.')
    plan["sources"][0]["skip"] = ["news"]
    assert ids(collect(tmp_path, plan)[0]) == ["morning", "news"]


@pytest.mark.parametrize("problem", ["escape", "symlink", "id", "schema", "missing", "duplicate"])
def test_source_catalogs_are_checked_like_the_layouts_repo(tmp_path, problem):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    folder = catalog(tmp_path / "my-apps", ["clock"])
    data = json.loads((folder / "catalog.json").read_text())
    if problem == "escape":
        package(tmp_path / "outside", "clock")
        data["layouts"][0]["path"] = "../outside"
    elif problem == "symlink":
        package(tmp_path / "outside", "clock")
        (folder / "layouts/linked").symlink_to(tmp_path / "outside")
        data["layouts"][0]["path"] = "layouts/linked"
    elif problem == "id":
        data["layouts"][0]["id"] = "watch"
    elif problem == "schema":
        data["schema_version"] = 2
    elif problem == "missing":
        (folder / "catalog.json").unlink()
    elif problem == "duplicate":
        data["layouts"].append(dict(data["layouts"][0]))
    if problem != "missing":
        (folder / "catalog.json").write_text(json.dumps(data))
    with pytest.raises(installer.InstallError, match="my-apps"):
        collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(folder)}]})


def test_symlinked_file_in_a_folder_source_is_refused(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    folder = catalog(tmp_path / "my-apps", ["clock"])
    (tmp_path / "secret.txt").write_text("private")
    (folder / "layouts/clock/notes.txt").symlink_to(tmp_path / "secret.txt")
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(folder)}]})
    with pytest.raises(ValueError, match="notes.txt, which is a link, private or reserved"):
        installer.install_all(host(tmp_path), items)


def test_git_source_fetches_one_pinned_commit_without_submodules_or_credentials(tmp_path, monkeypatch):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    remote_repo = git_repository(tmp_path / "team-apps", ["clock"])
    commit = subprocess.check_output(["git", "-C", str(remote_repo), "rev-parse", "HEAD"], text=True).strip()
    url = "https://builder:s3cret-token@git.example.invalid/team/apps.git"
    calls = []
    real_run = subprocess.run

    def offline(args, **kwargs):
        calls.append(list(args))
        if "fetch" in args:
            # Stand in for the network: the same fetch, from a local copy of the remote.
            args = [*args[:1], "-c", "protocol.file.allow=always", *args[1:]]
            args[args.index(url)] = str(remote_repo)
        return real_run(args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", offline)
    plan = {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"git": url, "ref": "HEAD"}]}
    items, messages = collect(tmp_path, plan)
    fetch = next(args for args in calls if "fetch" in args)
    for option in ("--depth", "--no-tags", "--no-recurse-submodules", "protocol.allow=never"):
        assert option in fetch
    assert messages[1] == f"https://git.example.invalid/team/apps.git (git): {commit}. Layouts: clock"
    root = host(tmp_path)
    installer.install_all(root, items)
    record = source_file(root, "clock")
    del record["installed_at"]
    assert record == {"schema_version": 1, "id": "clock", "label": "https://git.example.invalid/team/apps.git",
                      "kind": "git", "ref": "HEAD", "commit": commit, "catalog_name": "Clock app",
                      "path": "layouts/clock"}
    for path in (root / "sites/apps/clock").iterdir():
        if path.is_file():
            assert "s3cret" not in path.read_text(errors="ignore") and "builder" not in path.read_text(errors="ignore")
    # A pinned commit that the ref does not resolve to is refused.
    plan["sources"][0]["ref"] = "0" * 40
    with pytest.raises((installer.InstallError, subprocess.CalledProcessError)):
        collect(tmp_path, plan)


@pytest.mark.parametrize("url,label", [
    ("https://user:token@git.example.invalid:8443/team/apps.git", "https://git.example.invalid:8443/team/apps.git"),
    ("ssh://git@git.example.invalid/team/apps.git", "ssh://git.example.invalid/team/apps.git"),
    ("git@github.com:owner/apps.git", "github.com:owner/apps.git"),
])
def test_git_urls_lose_their_credentials(url, label):
    assert installer.safe_url(url) == label


@pytest.mark.parametrize("url", ["file:///etc", "http://git.example.invalid/a.git", "-oProxyCommand=x",
                                 "https://git.example.invalid/a.git?x=1", "ext::sh -c id"])
def test_unsafe_git_urls_are_refused(url):
    with pytest.raises(installer.InstallError):
        installer.safe_url(url)


def test_more_than_the_app_limit_asks_for_skip(tmp_path):
    names = [f"app{letter}" for letter in "abcdefghi"]
    layouts = git_repository(tmp_path / "layouts", names)
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})
    assert len(items) == installer.MAX_APPS + 1
    with pytest.raises(installer.InstallError, match="skip"):
        installer.install_all(host(tmp_path), items)
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "skip": ["appi"]}})
    assert len(installer.install_all(host(tmp_path / "ok"), items)) == installer.MAX_APPS


def remote_host(tmp_path, monkeypatch, compose):
    """A local stand-in for the NAS, with SSH and rsync run locally."""
    operator = tmp_path / "operator"
    operator.mkdir()
    base = tmp_path.resolve() / "dayframe"
    subprocess.run(["sh", "-c", remote.sync_guard(str(base))], check=True)
    (base / "source/scripts").mkdir(parents=True)
    (base / "source/scripts/install-layout.py").touch()
    (operator / ".deploy.local.toml").write_text(
        f'host="192.0.2.10"\nuser="example"\npath="{base}"\ncompose="{compose}"\n')
    monkeypatch.setattr(installer, "ROOT", operator)
    import scripts.remote as imported
    monkeypatch.setattr(imported, "ssh_options", lambda cfg: ["test-ssh"])
    calls, real_run = [], subprocess.run

    def offline(args, **kwargs):
        calls.append(args)
        if args[0] == "test-ssh":
            return None if "sudo sh " in args[-1] else real_run(["sh", "-c", args[-1]], **kwargs)
        if args[0] == "rsync":
            position = args.index("-e")
            args = args[:position] + args[position + 2:]
            args[-1] = args[-1].split(":", 1)[1]
        return real_run(args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", offline)
    return base, calls


def test_browser_profile_installs_no_apps_and_says_how_to_run_them(tmp_path, monkeypatch, capsys, apps):
    base, calls = remote_host(tmp_path, monkeypatch, "compose.yaml")
    installer.install_remote([apps["morning"], apps["news"], apps["photos"]])
    out = capsys.readouterr().out
    assert out.strip() == installer.BROWSER_NOTE
    assert 'Set compose = "compose.static.yaml" in .deploy.local.toml' in out
    assert not (base / "sites/installed").exists() and not (base / "sites/apps").exists()
    assert not [args for args in calls if args[0] in ("test-ssh", "rsync")]


def test_update_with_a_browser_profile_skips_the_installer(tmp_path, monkeypatch, capsys, committed):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr(remote, "sync", lambda *args: None)
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(stdout=b""))
    remote.update(config(compose="compose.yaml", app={"repo": "owner/apps"}), ["test-ssh"])
    assert not any("--transfer-only" in args for args in calls)
    assert sum(args[-1].startswith("sudo ") for args in calls) == 1
    assert installer.BROWSER_NOTE in capsys.readouterr().out


def test_prepare_in_browser_mode_removes_every_app_setting(tmp_path):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    app = base / "sites/installed"
    app.mkdir()
    (app / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
    (app / ".install-profile").write_text("morning bridge 256 index.html morning-0123 1\n")
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert "DAYFRAME_LAYOUT_ID=morning" in (base / ".env").read_text()
    subprocess.run(["sh", str(prepare), "browser"], env=env, check=True, capture_output=True)
    environment = (base / ".env").read_text()
    for key in ("DAYFRAME_LAYOUT_", "DAYFRAME_SITE_", "DAYFRAME_APPS"):
        assert key not in environment
    assert "DAYFRAME_UID=10001" in environment
    assert (app / ".install-profile").exists()


def test_static_profile_names_new_apps_and_a_new_first_app(tmp_path, monkeypatch, capsys, apps):
    base, calls = remote_host(tmp_path, monkeypatch, "compose.static.yaml")
    installer.install_remote([apps["morning"], apps["news"]])
    out = capsys.readouterr().out
    assert "New layout: morning from layouts (network: none)" in out
    assert "New layout: news from layouts (network: bridge)" in out
    assert "first layout changes" not in out
    # The transfer only stages the apps. The running apps in sites/ stay as they are.
    assert (base / "sites/.incoming/apps/news/app.txt").read_text() == "news source"
    assert not (base / "sites/installed").exists() and not (base / "sites/apps").exists()
    assert any(args[-1].endswith("place-apps.sh static") for args in calls)
    place(base)
    installer.install_remote([apps["news"], apps["morning"], apps["photos"]])
    out = capsys.readouterr().out
    assert "The first layout changes from morning to news. TVs without a chosen layout will switch to news." in out
    assert "New layout: photos from layouts (network: none)" in out
    assert "New layout: news" not in out and "New layout: morning" not in out


def place(base):
    """What update-remote.sh does: move the staged apps into sites/."""
    for name in ("installed", "apps"):
        shutil.rmtree(base / "sites" / name, ignore_errors=True)
        if (base / "sites/.incoming" / name).exists():
            (base / "sites/.incoming" / name).rename(base / "sites" / name)


def test_remote_probe_failure_does_not_call_every_app_new(capsys, apps):
    messages = []
    installer.report_changes(None, [apps["morning"]], report=lambda text, **_: messages.append(text))
    assert messages == ["Could not read the layouts on the NAS now, so new layouts are not named."]


def test_local_install_names_new_apps_and_a_new_first_app(tmp_path, capsys):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    root = host(tmp_path)
    installer.install_all(root, collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})[0])
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts), "layout": "news"}})
    messages = []
    installer.report_changes(installer.installed_apps(root / "sites"), items,
                             report=lambda text, **_: messages.append(text))
    assert messages == ["The first layout changes from morning to news. TVs without a chosen layout will switch to news."]


def plant_hooks(repo, marker):
    """Repository settings that run a command whenever Git reads the work tree."""
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {marker}-fsmonitor; false"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "filter.planted.smudge", f"touch {marker}-smudge; cat"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "filter.planted.clean", f"touch {marker}-clean; cat"],
                   check=True)
    (repo / ".git/info").mkdir(exist_ok=True)
    (repo / ".git/info/attributes").write_text("* filter=planted\n")


def test_local_sources_never_run_git_in_the_source_repository(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    apps = git_repository(tmp_path / "my-apps", ["clock"])
    marker = tmp_path / "ran"
    for repo in (layouts, apps):
        plant_hooks(repo, marker)
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(apps)}]})
    assert ids(items) == ["morning", "clock"]
    remote.source_committed(apps, "my-apps")
    (apps / "layouts/clock/app.txt").write_text("changed")
    with pytest.raises(installer.InstallError, match="my-apps has uncommitted changes .layouts/clock/app.txt"):
        remote.source_committed(apps, "my-apps")
    assert not list(tmp_path.glob("ran-*"))


def test_full_commit_ref_that_resolves_to_another_commit_is_refused(tmp_path, monkeypatch):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    remote_repo = git_repository(tmp_path / "team-apps", ["clock"])
    url = "https://git.example.invalid/team/apps.git"
    pinned = "0123456789abcdef0123456789abcdef01234567"
    real_run = subprocess.run

    def offline(args, **kwargs):
        if "fetch" in args:
            # The fetch succeeds, but the server sends its HEAD, not the pinned commit.
            args = [*args[:1], "-c", "protocol.file.allow=always", *args[1:]]
            args[args.index(url)] = str(remote_repo)
            args[args.index(pinned)] = "HEAD"
        return real_run(args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", offline)
    with pytest.raises(installer.InstallError, match="did not resolve to that commit"):
        collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"git": url, "ref": pinned}]})


@pytest.mark.parametrize("name", [".dayframe-source.json", ".dayframe-installed.json", ".install-profile"])
def test_package_with_a_file_reserved_for_dayframe_is_refused(tmp_path, name):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    folder = catalog(tmp_path / "my-apps", ["clock"])
    (folder / "layouts/clock" / name).write_text('{"id": "forged"}')
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(folder)}]})
    with pytest.raises(installer.InstallError) as error:
        installer.install_all(host(tmp_path), items)
    assert str(error.value).startswith(f"my-apps: layout clock (layouts/clock) contains {name},")


def test_package_without_a_dockerfile_names_the_app_and_source(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    folder = catalog(tmp_path / "my-apps", ["clock"])
    (folder / "layouts/clock/Dockerfile").unlink()
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(folder)}]})
    with pytest.raises(installer.InstallError, match=r"my-apps: layout clock \(layouts/clock\) has no Dockerfile"):
        installer.install_all(host(tmp_path), items)


def test_invalid_manifest_says_to_update_dayframe_or_skip(tmp_path):
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    folder = catalog(tmp_path / "my-apps", ["clock"])
    (folder / "layouts/clock/layout.json").write_text(json.dumps({"id": "clock", "name": "C", "future": True}))
    with pytest.raises(installer.InstallError) as error:
        collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}, "sources": [{"path": str(folder)}]})
    assert str(error.value) == (
        "my-apps: layouts/clock/layout.json isn't valid for this version of Art Frames. "
        'Update Art Frames first, or add skip = ["clock"].')


def test_github_source_record_links_to_the_app_folder(tmp_path):
    repo = tmp_path / "monorepo"
    catalog(repo / "dayframe", ["clock"])
    commit = commit_all(repo)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Example/Apps.git"], check=True)
    layouts = git_repository(tmp_path / "layouts", ["morning"])
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(repo / "dayframe")},
                                  "sources": [{"path": str(layouts)}]})
    root = host(tmp_path)
    installer.install_all(root, items)
    record = source_file(root)
    assert record["repository"] == "Example/Apps"
    assert record["url"] == f"https://github.com/Example/Apps/tree/{commit}/dayframe/layouts/clock"
    assert record["path"] == "layouts/clock"
    assert "repository" not in source_file(root, "morning") and "url" not in source_file(root, "morning")


def test_scope_uses_the_source_identity_and_app_path():
    origin = installer.Origin("repo", "layouts", "main", "a" * 40, "github:example/layouts", app_path="layouts/news")
    digest = installer.hashlib.sha256(b"github:example/layouts:layouts/news").hexdigest()[:32]
    assert origin.settings_scope("news") == "news-" + digest
    assert origin.settings_scope("x" * 64) == "x" * 40 + "-" + digest


def test_check_validates_every_app_and_installs_nothing(tmp_path, monkeypatch, capsys):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    root = host(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--local-repo", str(layouts), "--check"])
    installer.main()
    assert "All 2 layouts are valid. Nothing was installed." in capsys.readouterr().out
    assert not (root / "sites").exists() and not (root / ".env").exists()


def test_first_only_installs_just_the_first_app(tmp_path, monkeypatch):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    root = host(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--local-repo", str(layouts), "--first-only"])
    installer.main()
    assert (root / "sites/installed/app.txt").read_text() == "morning source"
    assert not (root / "sites/apps").exists()


@pytest.mark.parametrize("source,message", [
    ({"path": "../my-apps", "ref": "main"}, "Leave out ref"),
    ({"git": "https://git.example.invalid/a.git", "ref": "a..b"}, "plain Git branch"),
    ({"git": "https://git.example.invalid/a.git", "ref": "-x"}, "plain Git branch"),
])
def test_source_refs_are_checked_before_connection(tmp_path, monkeypatch, committed, source, message):
    monkeypatch.setattr(remote, "ROOT", tmp_path / "dayframe")
    (tmp_path / "my-apps").mkdir()
    monkeypatch.setattr(remote, "ensure_connection", lambda *args: pytest.fail("Must validate before SSH"))
    with pytest.raises(ValueError, match=message):
        remote.update(config(compose="compose.static.yaml", app={"repo": "owner/apps"}, sources=[source]),
                      ["test-ssh"])


def test_scp_style_git_urls_are_accepted(tmp_path, monkeypatch, committed):
    monkeypatch.setattr(remote, "ROOT", tmp_path)
    plan = plan_of(remote.app_update_args(config(
        app={"repo": "owner/apps"}, sources=[{"git": "git@git.example.invalid:team/apps.git", "ref": "v1"}])))
    assert plan["sources"] == [{"git": "git@git.example.invalid:team/apps.git", "ref": "v1"}]


def test_compose_mode_comes_from_the_shared_helper():
    assert remote.compose_mode("compose.static.host.yaml") == "static"
    assert remote.compose_mode("compose.host.yaml") == "browser"
    for script in ("update-remote.sh", "rollback-remote.sh", "build-check.sh"):
        text = (SCRIPTS / script).read_text()
        assert "compose_mode" in text and "compose.static.yaml|" not in text, script
    assert "build-check.sh" in (SCRIPTS / "update-remote.sh").read_text()
    for script in ("prepare-remote.sh", "app-compose.sh"):
        assert "read_profile" in (SCRIPTS / script).read_text()
        assert "read -r app_id" not in (SCRIPTS / script).read_text()


def test_cli_installs_the_whole_catalog_and_extra_sources(tmp_path, monkeypatch):
    layouts = git_repository(tmp_path / "layouts", ["morning", "news"])
    photos = git_repository(tmp_path / "photos", ["photos"])
    root = host(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--local-repo", str(layouts)])
    installer.main()
    assert (root / "sites/installed/app.txt").read_text() == "morning source"
    assert (root / "sites/apps/news/app.txt").read_text() == "news source"
    plan = {"layouts_repo": {"local_repo": str(layouts), "skip": ["news"]}, "sources": [{"path": str(photos)}]}
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--plan", json.dumps(plan)])
    installer.main()
    assert (root / "sites/apps/photos/app.txt").read_text() == "photos source"
    assert not (root / "sites/apps/news").exists()
    assert env_value(root, "DAYFRAME_APPS").startswith("photos=")


def commit_with_origin(path, url):
    commit_all(path)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)
    return path


def test_settings_scopes_match_earlier_releases(tmp_path, monkeypatch):
    """A changed scope would hide each app's saved settings, so these values are fixed."""
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text('[url "git@github.com:"]\n\tinsteadOf = gh:\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    mono = tmp_path / "mono"
    catalog(mono / "dayframe", ["clock"])
    commit_with_origin(mono, "https://github.com/Example/Apps.git")
    alias = commit_with_origin(catalog(tmp_path / "alias", ["morning"]), "gh:Example/Alias.git")
    layouts = commit_with_origin(catalog(tmp_path / "layouts", ["morning"]), "https://github.com/Example/Layouts.git")
    team = git_repository(tmp_path / "team", ["chores"])
    plain = catalog(tmp_path / "plain", ["news"])
    url = "https://git.example.invalid/team/apps.git"
    real_run = subprocess.run

    def offline(args, **kwargs):
        if "fetch" in args and url in args:
            args = [*args[:1], "-c", "protocol.file.allow=always", *args[1:]]
            args[args.index(url)] = str(team)
        return real_run(args, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", offline)

    def scopes(plan):
        items, _ = collect(tmp_path, plan)
        return [origin.settings_scope(installer.read_manifest(package).id) for package, origin in items]

    assert scopes({"layouts_repo": {"local_repo": str(mono / "dayframe")}}) == [
        "clock-4b2f02d0b16d90cb203b469b1c3b13f8"]
    assert scopes({"layouts_repo": {"local_repo": str(alias)}}) == ["morning-f205cf6dfd1595a3d1e20375bf1c04ff"]
    digest = installer.hashlib.sha256(str(plain.resolve()).encode()).hexdigest()
    plain_scope = "news-" + installer.hashlib.sha256(f"local:{digest}:layouts/news".encode()).hexdigest()[:32]
    assert scopes({"layouts_repo": {"local_repo": str(layouts)},
                   "sources": [{"path": str(mono / "dayframe")}, {"git": url, "ref": "HEAD"},
                               {"path": str(plain)}]})[1:] == [
        "clock-3a65c2e249aaba4f340a83dda1874753", "chores-11a540390e9e52250d2b775392c037e7", plain_scope]
    github = installer.Origin("repo", "layouts", "main", "a" * 40, "github:example/layouts", app_path="layouts/morning")
    assert github.settings_scope("morning") == "morning-5e5434bbd3f17e5f9b7e31b74ab05155"


def test_filters_from_the_user_git_config_never_run(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[filter "planted"]\n\tsmudge = touch {marker}-smudge; cat\n\tclean = touch {marker}-clean; cat\n'
                         f'[core]\n\tfsmonitor = touch {marker}-fsmonitor; false\n')
    layouts = catalog(tmp_path / "layouts", ["morning"])
    (layouts / ".gitattributes").write_text("* filter=planted\n")
    commit_all(layouts)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    items, _ = collect(tmp_path, {"layouts_repo": {"local_repo": str(layouts)}})
    assert ids(items) == ["morning"]
    remote.source_committed(layouts, "layouts")
    assert not list(tmp_path.glob("ran-*"))


def test_line_ending_conversion_is_not_an_uncommitted_change(tmp_path):
    repo = catalog(tmp_path / "my-apps", ["clock"])
    (repo / ".gitattributes").write_text("* text eol=crlf\n")
    commit_all(repo)
    # A checkout on this repository writes CRLF line endings; the commit holds LF.
    app = repo / "layouts/clock/Dockerfile"
    app.write_bytes(app.read_bytes().replace(b"\n", b"\r\n"))
    remote.source_committed(repo, "my-apps")
    app.write_bytes(b"FROM busybox\r\n")
    with pytest.raises(installer.InstallError, match=r"my-apps has uncommitted changes \(layouts/clock/Dockerfile\)"):
        remote.source_committed(repo, "my-apps")


def test_file_with_a_git_filter_is_named_and_not_checked(tmp_path):
    repo = catalog(tmp_path / "my-apps", ["clock"])
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 4\n"
    (repo / "layouts/clock/photo.bin").write_bytes(pointer)
    commit_all(repo)
    remote.source_committed(repo, "my-apps")
    # Git LFS replaces the pointer with the real file in the work tree.
    (repo / "layouts/clock/photo.bin").write_bytes(b"\x89PNG")
    with pytest.raises(installer.InstallError) as error:
        remote.source_committed(repo, "my-apps")
    assert str(error.value) == ("my-apps: layouts/clock/photo.bin uses a Git filter (lfs); Art Frames can't check it. "
                                "Remove the filter attribute, or skip this source")


@pytest.mark.parametrize("problem,message", [
    ("dockerfile", r"layouts: layout news \(layouts/news\) has no Dockerfile"),
    ("env", r"layouts: layout news \(layouts/news\) contains .env, which is a link, private or reserved"),
])
def test_check_applies_the_package_rules(tmp_path, monkeypatch, problem, message):
    layouts = catalog(tmp_path / "layouts", ["morning", "news"])
    if problem == "dockerfile":
        (layouts / "layouts/news/Dockerfile").unlink()
    else:
        (layouts / "layouts/news/.env").write_text("TOKEN=committed\n")
    commit_all(layouts)
    root = host(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--local-repo", str(layouts), "--check"])
    with pytest.raises(installer.InstallError, match=message):
        installer.main()
    assert not (root / "sites").exists()


@pytest.mark.parametrize("argv,message", [
    (["--first-only", "--remote"], "--first-only is for a local run"),
    (["--path", "my-apps"], "--path works only with --check"),
    (["--check", "--path", "my-apps", "--local-repo", "layouts"], "--path checks one source folder"),
])
def test_conflicting_options_stop_with_a_message(monkeypatch, capsys, argv, message):
    monkeypatch.setattr("sys.argv", ["install-layout.py", *argv])
    with pytest.raises(SystemExit):
        installer.main()
    assert message in capsys.readouterr().err


def test_check_path_checks_one_source_folder_with_the_source_rules(tmp_path, monkeypatch, capsys):
    folder = catalog(tmp_path / "my-apps", ["clock", "chores"])
    root = host(tmp_path)
    monkeypatch.setattr(installer, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["install-layout.py", "--check", "--path", str(folder), "--skip", "chores"])
    installer.main()
    out = capsys.readouterr().out
    assert "my-apps (path): no commit (plain folder). Layouts: clock" in out
    assert "All 1 layouts are valid. Nothing was installed." in out
    (folder / "layouts/clock/.env.local").write_text("x")
    with pytest.raises(installer.InstallError, match="my-apps: layout clock"):
        installer.main()
    assert not (root / "sites").exists()


def test_transfer_ends_with_the_complete_file_and_reuses_unchanged_files(tmp_path, monkeypatch, apps):
    base, calls = remote_host(tmp_path, monkeypatch, "compose.static.yaml")
    installer.install_remote([apps["morning"], apps["news"]])
    assert (base / "sites/.incoming/.complete").read_text() == "installed\napps\n"
    assert [args for args in calls if args[0] == "rsync"][-1][-3] == "--link-dest=../../apps"
    place(base)
    live = base / "sites/apps/news"
    (apps["news"][0] / "app.txt").write_text("changed news")
    installer.install_remote([apps["morning"], apps["news"]])
    staged = base / "sites/.incoming/apps/news"
    # A changed file is a new file; the running app's copy stays as it was.
    assert (staged / "app.txt").read_text() == "changed news"
    assert (live / "app.txt").read_text() == "news source"
    assert (staged / "app.txt").stat().st_ino != (live / "app.txt").stat().st_ino
    # An unchanged file is linked, not sent again.
    assert (staged / "Dockerfile").stat().st_ino == (live / "Dockerfile").stat().st_ino


def test_single_app_transfer_lists_only_the_first_app(tmp_path, monkeypatch, apps):
    base, _ = remote_host(tmp_path, monkeypatch, "compose.static.yaml")
    installer.install_remote([apps["morning"]])
    assert (base / "sites/.incoming/.complete").read_text() == "installed\n"


def test_transfer_waits_for_a_running_update(tmp_path, monkeypatch, capfd, apps):
    base, calls = remote_host(tmp_path, monkeypatch, "compose.static.yaml")
    (base / ".update-lock").mkdir()
    with pytest.raises(subprocess.CalledProcessError):
        installer.install_remote([apps["morning"]])
    assert "Another update is running." in capfd.readouterr().err
    assert not [args for args in calls if args[0] == "rsync"]
    assert not (base / "sites/.incoming").exists()


@pytest.mark.parametrize("settings", [{"compose": "compose.yaml", "app": {"repo": "owner/apps"}},
                                      {"compose": "compose.static.yaml"}])
def test_update_without_apps_removes_a_stale_transfer(tmp_path, monkeypatch, committed, settings):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr(remote, "sync", lambda *args: None)
    monkeypatch.setattr(remote, "ensure_connection", lambda *args: None)
    monkeypatch.setattr(remote, "open_tunnel", lambda *args: None)
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(stdout=b""))
    base = tmp_path.resolve() / "dayframe"
    (base / "sites/.incoming/installed").mkdir(parents=True)
    remote.update(config(str(base), **settings), ["test-ssh"])
    clear = [args[-1] for args in calls if ".incoming" in args[-1]]
    assert clear == [remote.clear_incoming_command(str(base))]
    subprocess.run(["sh", "-c", clear[0]], check=True)
    assert not (base / "sites/.incoming").exists() and (base / "sites").is_dir()


def test_prepare_reads_the_transferred_apps_and_writes_final_paths(tmp_path):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    incoming = base / "sites/.incoming"
    for folder, app_id in ((incoming / "installed", "news"), (incoming / "apps/clock", "clock")):
        folder.mkdir(parents=True)
        (folder / ".dayframe-layout-managed").write_text("dayframe-layout-source-v1\n")
        (folder / ".install-profile").write_text(f"{app_id} none 256 index.html {app_id}-0123 0\n")
    (incoming / "apps/.dayframe-apps-managed").write_text("dayframe-apps-source-v1\n")
    staged = {**env, "DAYFRAME_FIRST_DIR": str(incoming / "installed"), "DAYFRAME_APPS_DIR": str(incoming / "apps")}
    subprocess.run(["sh", str(prepare), "static"], env=staged, check=True, capture_output=True)
    assert env_value(base, "DAYFRAME_LAYOUT_ID") == "news"
    assert env_value(base, "DAYFRAME_SITE_BUILD_CONTEXT") == "./sites/installed"
    assert env_value(base, "DAYFRAME_APPS") == "clock=clock-0123"
    assert 'context: "${DAYFRAME_APPS_CONTEXT:-./sites/apps}/clock"' in (base / "sites/compose.apps.yaml").read_text()
    outside = {**env, "DAYFRAME_FIRST_DIR": str(tmp_path)}
    result = subprocess.run(["sh", str(prepare), "static"], env=outside, capture_output=True, text=True)
    assert result.returncode != 0 and "must be in sites/ or sites/.incoming" in result.stderr


def test_git_settings_from_the_environment_never_run(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    layouts = catalog(tmp_path / "layouts", ["morning"])
    (layouts / ".gitattributes").write_text("* filter=planted\n")
    commit_all(layouts)
    (layouts / "layouts/morning/app.txt").write_text("changed, so Git hashes it with its attributes")
    settings = {"filter.planted.clean": f"touch {marker}-clean; cat",
                "filter.planted.smudge": f"touch {marker}-smudge; cat",
                "filter.planted.required": "true"}
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(settings)))
    for index, (key, value) in enumerate(settings.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{index}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{index}", value)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.fsmonitor'='touch {marker}-fsmonitor; false'")
    with pytest.raises(installer.InstallError, match="uses a Git filter"):
        remote.source_committed(layouts, "layouts")
    assert not list(tmp_path.glob("ran-*"))
