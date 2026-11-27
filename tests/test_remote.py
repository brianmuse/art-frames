"""Deployment scope guards run against disposable local directories, never a NAS."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("remote", Path(__file__).parents[1] / "scripts/remote.py")
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)



@pytest.fixture(autouse=True)
def no_home_network_probe(monkeypatch):
    # Tests never contact a real NAS.
    monkeypatch.setattr(remote, "home_network_url", lambda config: None)


def config(path="/volume1/docker/dayframe", **changes):
    return {"host": "192.0.2.10", "user": "example", "path": path, **changes}


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/volume1/docker",
        "/volume1/../dayframe",
        "/tmp/./dayframe",
        "/tmp/x;echo/dayframe",
        "/tmp//dayframe",
    ],
)
def test_refuses_unsafe_deploy_path(path):
    with pytest.raises(ValueError):
        remote.target(config(path))


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "-oProxyCommand=touch"},
        {"user": "a;id"},
        {"port": True},
        {"compose": "anything.yaml;id"},
        {"rsync_path": "rsync;id"},
        {"rsync_path": "/usr/bin/rsync --daemon"},
    ],
)
def test_target_cannot_inject_ssh_or_shell_options(changes):
    with pytest.raises(ValueError):
        remote.target(config(**changes))


def guard(path, *, initialize=True):
    return subprocess.run(
        ["sh"], input=remote.sync_guard(str(path), initialize=initialize), text=True, capture_output=True
    )


def test_existing_private_directory_cannot_be_claimed(tmp_path):
    base = tmp_path.resolve() / "dayframe"
    base.mkdir()
    private = base / "private.txt"
    private.write_text("keep")
    assert guard(base).returncode != 0
    assert private.read_text() == "keep"
    assert not (base / "source").exists()


def test_guard_refuses_source_symlink_and_bad_marker(tmp_path):
    base = tmp_path.resolve() / "dayframe"
    assert guard(base).returncode == 0
    (base / "source").rmdir()
    other = tmp_path / "private"
    other.mkdir()
    (base / "source").symlink_to(other, target_is_directory=True)
    assert guard(base).returncode != 0
    (base / "source").unlink()
    (base / "source").mkdir()
    (base / ".dayframe-managed").write_text("not ours")
    assert guard(base).returncode != 0


def test_guard_does_not_touch_runtime_files(tmp_path):
    base = tmp_path.resolve() / "dayframe"
    assert guard(base).returncode == 0
    (base / "data").mkdir()
    private = base / "data" / "token"
    private.write_text("keep")
    assert guard(base).returncode == 0
    assert private.read_text() == "keep"


def test_dry_run_guard_does_not_initialize_source(tmp_path):
    base = tmp_path.resolve() / "dayframe"
    assert guard(base, initialize=False).returncode != 0
    assert not (base / "source").exists()
    assert not (base / ".dayframe-managed").exists()
    assert not base.exists()


def test_sync_deletes_only_managed_source_and_preserves_runtime(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("print('new source')")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(
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
            "test",
        ],
        check=True,
    )
    base = tmp_path.resolve() / "dayframe"
    assert guard(base).returncode == 0
    (base / "source" / "old.py").write_text("obsolete")
    (base / "data").mkdir()
    (base / "data" / "token").write_text("private token")
    (base / "source" / "data").symlink_to("../data", target_is_directory=True)
    (repo / "ignored-password").write_text("never send untracked files")
    calls = []

    def offline_run(args, **kwargs):
        calls.append(args)
        if args[0] == "test-ssh":
            return subprocess.run(["sh", "-c", args[-1]], check=True, **kwargs)
        if args[0] == "rsync":
            # Exercise real rsync locally; no SSH service or actual NAS is involved.
            position = args.index("-e")
            args = args[:position] + args[position + 2 :]
            args[-1] = str(base / "source") + "/"
        return subprocess.run(args, check=True, **kwargs)

    monkeypatch.setattr(remote, "ROOT", repo)
    monkeypatch.setattr(remote, "run", offline_run)
    remote.sync(config(str(base), rsync_path="/usr/bin/rsync"), ["test-ssh"])
    assert (base / "source" / "app.py").is_file()
    assert not (base / "source" / "old.py").exists()
    assert not (base / "source" / "ignored-password").exists()
    assert (base / "source" / "data").is_symlink()
    assert (base / "data" / "token").read_text() == "private token"
    rsync = next(args for args in calls if args[0] == "rsync")
    assert "--rsync-path=/usr/bin/rsync" in rsync
    assert rsync[-1].endswith("/dayframe/source/")


def managed_layout(base):
    """The prepared NAS layout: runtime paths in base, linked from base/source as ../<name>."""
    source = base / "source"
    (source / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("data", "sites"):
        (base / name).mkdir(exist_ok=True)
    (base / ".env").touch()
    for name in ("data", "sites", ".env"):
        (source / name).symlink_to(Path("..") / name)
    for profile in ("compose.yaml", "compose.host.yaml", "compose.static.yaml", "compose.static.host.yaml"):
        (source / profile).write_text("services: {}\n")
    return source


# A fake docker fails, as Compose does, when a -f file is missing.
DOCKER_FILES_CHECK = """prev=""
for arg in "$@"; do
    if test "$prev" = -f && ! test -f "$arg"; then echo "missing compose file $arg" >&2; exit 14; fi
    prev=$arg
done
"""


def prepare_fixture(tmp_path):
    base = tmp_path / "dayframe"
    scripts = base / "source" / "scripts"
    scripts.mkdir(parents=True)
    (base / ".dayframe-managed").write_text(remote.MARKER)
    (base / "source" / "config.example.toml").write_text("timezone = 'UTC'")
    (base / "source" / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
    prepare = scripts / "prepare-remote.sh"
    shutil.copy(Path(__file__).parents[1] / "scripts/prepare-remote.sh", prepare)
    shutil.copy(Path(__file__).parents[1] / "scripts/app-compose.sh", scripts / "app-compose.sh")
    shutil.copy(Path(__file__).parents[1] / "scripts/app-profile.sh", scripts / "app-profile.sh")
    shutil.copy(Path(__file__).parents[1] / "scripts/keep-layout-settings.sh", scripts / "keep-layout-settings.sh")
    # Fake identity/chown only; exercise real directory, symlink, and mode operations
    # as the test user, without privileged host mutations.
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, content in {
        "id": "echo 0",
        "chown": 'printf "%s\\n" "$@" >> "$CHOWN_LOG"',
        "openssl": "echo generated-test-password",
    }.items():
        command = commands / name
        command.write_text("#!/bin/sh\n" + content + "\n")
        command.chmod(0o700)
    log = tmp_path / "chown.log"
    env = dict(os.environ, PATH=str(commands) + ":" + os.environ["PATH"], CHOWN_LOG=str(log))
    return base, prepare, commands, log, env


def test_prepare_preserves_settings_password_and_bounds_ownership(tmp_path):
    base, prepare, _, log, env = prepare_fixture(tmp_path)
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    (base / "config.local.toml").write_text("timezone = 'Europe/London'")
    (base / ".secrets" / "web_password").write_text("existing-private-password")
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert (base / "config.local.toml").read_text() == "timezone = 'Europe/London'"
    assert (base / ".secrets" / "web_password").read_text() == "existing-private-password"
    assert (base / ".secrets" / "web_password").stat().st_mode & 0o777 == 0o600
    assert (base / "sites").stat().st_mode & 0o777 == 0o755
    assert all((base / "source" / name).readlink() == Path("..") / name for name in remote.RUNTIME)
    assert "-R" not in log.read_text().splitlines()
    assert all(line.startswith(str(base) + "/") for line in log.read_text().splitlines() if "/" in line)


def test_prepare_recovers_after_failed_password_generation(tmp_path):
    base, prepare, commands, _, env = prepare_fixture(tmp_path)
    (commands / "openssl").write_text("#!/bin/sh\necho partial\nexit 1\n")
    result = subprocess.run(["sh", str(prepare), "static"], env=env, capture_output=True)
    assert result.returncode != 0
    assert not (base / ".secrets" / "web_password").exists()
    assert not list((base / ".secrets").glob(".password.*"))
    (commands / "openssl").write_text("#!/bin/sh\necho generated-test-password\n")
    subprocess.run(["sh", str(prepare), "static"], env=env, check=True, capture_output=True)
    assert (base / ".secrets" / "web_password").read_text().strip() == "generated-test-password"


def test_prepare_does_not_replace_existing_invalid_password(tmp_path):
    base, prepare, _, _, env = prepare_fixture(tmp_path)
    (base / ".secrets").mkdir()
    (base / ".secrets" / "web_password").write_text("")
    result = subprocess.run(["sh", str(prepare), "static"], env=env, capture_output=True)
    assert result.returncode != 0
    assert (base / ".secrets" / "web_password").read_text() == ""


def test_prepare_refuses_runtime_symlink_before_mutations(tmp_path):
    base = tmp_path / "dayframe"
    scripts = base / "source" / "scripts"
    scripts.mkdir(parents=True)
    (base / ".dayframe-managed").write_text(remote.MARKER)
    private = tmp_path / "private"
    private.mkdir()
    (base / "data").symlink_to(private, target_is_directory=True)
    prepare = scripts / "prepare-remote.sh"
    shutil.copy(Path(__file__).parents[1] / "scripts/prepare-remote.sh", prepare)
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "id").write_text("#!/bin/sh\necho 0\n")
    (commands / "id").chmod(0o700)
    result = subprocess.run(
        ["sh", str(prepare), "static"],
        env=dict(os.environ, PATH=str(commands) + ":" + os.environ["PATH"]),
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (base / ".secrets").exists()
    assert not list(private.iterdir())


def test_connect_keeps_host_verification_and_temporary_auth(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / ".deploy.local.toml").write_text(
        'host = "192.0.2.10"\nuser = "example"\npath = "/srv/apps/dayframe"\n'
    )
    monkeypatch.setattr(remote, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["remote.py", "connect"])
    calls = []
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: calls.append(args))
    # tmp_path can exceed the short Unix socket limit on macOS; use a short test socket.
    options = ["ssh", "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no"]
    with tempfile.TemporaryDirectory(prefix="dayframe-ssh-", dir="/tmp") as directory:
        monkeypatch.setattr(remote, "ROOT", Path(directory))
        actual = remote.ssh_options(config())
    assert all(option in actual for option in options)
    monkeypatch.setattr(remote, "ROOT", root)
    monkeypatch.setattr(remote, "ssh_options", lambda config: options)
    remote.main()
    assert len(calls) == 1
    assert "-M" in calls[0] and "-N" in calls[0]
    assert "ControlPersist=12h" in calls[0]
    assert calls[0][-1] == "example@192.0.2.10"


@pytest.mark.parametrize("host_result,capture_result", [(0, 0), (0, 1), (1, 0)])
def test_up_starts_only_after_successful_container_capture(
    tmp_path, monkeypatch, host_result, capture_result
):
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    base = tmp_path.resolve() / "dayframe"
    scripts = base / "source" / "scripts"
    scripts.mkdir(parents=True)
    (root / ".deploy.local.toml").write_text(f'host = "192.0.2.10"\nuser = "example"\npath = "{base}"\n')
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {
        "sudo": 'exec "$@"',
        "docker": 'printf "%s\\n" "$*" >> "$COMMAND_LOG"',
    }.items():
        command = commands / name
        command.write_text("#!/bin/sh\n" + body + "\n")
        command.chmod(0o700)
    (scripts / "check-containers.sh").write_text(
        'test "$1" = dayframe:local && test "$2" = dayframe-renderer:local || exit 2\necho capture >> "$COMMAND_LOG"\nexit "$CAPTURE_RESULT"\n'
    )
    (scripts / "check-host.sh").write_text('echo host >> "$COMMAND_LOG"\nexit "$HOST_RESULT"\n')
    for name in ("compose-files.sh", "build-check.sh"):
        shutil.copy(Path(__file__).parents[1] / "scripts" / name, scripts / name)
    log = tmp_path / "commands.log"
    monkeypatch.setenv("PATH", str(commands) + ":" + os.environ["PATH"])
    monkeypatch.setenv("COMMAND_LOG", str(log))
    monkeypatch.setenv("CAPTURE_RESULT", str(capture_result))
    monkeypatch.setenv("HOST_RESULT", str(host_result))
    monkeypatch.setattr(remote, "ROOT", root)
    monkeypatch.setattr(remote, "ssh_options", lambda config: ["test-ssh"])
    monkeypatch.setattr("sys.argv", ["remote.py", "up"])
    monkeypatch.setattr(
        remote, "run", lambda args, **kwargs: subprocess.run(["sh", "-c", args[-1]], check=True, **kwargs)
    )
    if host_result or capture_result:
        with pytest.raises(subprocess.CalledProcessError):
            remote.main()
    else:
        remote.main()
    lines = log.read_text().splitlines()
    if host_result:
        assert lines == ["host"]
    else:
        assert lines[:3] == ["host", "compose -p dayframe -f compose.yaml build", "capture"]
        # --remove-orphans stops the container of an app that is no longer installed.
        assert ("compose -p dayframe -f compose.yaml up -d --remove-orphans" in lines) is (capture_result == 0)


@pytest.mark.parametrize(
    "capabilities,mode,passed",
    [
        ("name=seccomp,profile=builtin\npids=true memory=true", "browser", True),
        ("pids=true memory=true", "browser", False),
        ("name=seccomp,profile=builtin\npids=false memory=true", "browser", False),
        ("name=seccomp,profile=builtin\npids=true memory=false", "browser", False),
        ("pids=false memory=true", "static", True),
        ("pids=false memory=false", "static", False),
    ],
)
def test_host_preflight_checks_real_script(tmp_path, capabilities, mode, passed):
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\ntest "$1" = info || exit 2\nprintf "%s\\n" "$TEST_CAPABILITIES"\n')
    docker.chmod(0o700)
    result = subprocess.run(
        ["sh", str(Path(__file__).parents[1] / "scripts/check-host.sh"), mode],
        env=dict(os.environ, PATH=str(tmp_path) + ":" + os.environ["PATH"], TEST_CAPABILITIES=capabilities),
        capture_output=True,
    )
    assert (result.returncode == 0) is passed


def test_update_reconnects_transfers_app_and_uses_one_admin_session(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def record(args, **kwargs):
        calls.append(args)
        if "-O" in args and "check" in args:
            raise subprocess.CalledProcessError(255, args)
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr(remote, "run", record)
    monkeypatch.setattr(remote, "sync", lambda cfg, ssh: calls.append(["SYNC"]))
    (tmp_path.parent / "my app").mkdir()
    cfg = config(compose="compose.static.host.yaml", app={"layout": "morning", "local_repo": "../my app"})
    monkeypatch.setattr(remote, "require_clean_checkout", lambda root: None)
    remote.update(cfg, ["test-ssh"])
    assert sum("-M" in args for args in calls) == 1
    transfer = next(args for args in calls if "--transfer-only" in args)
    plan = json.loads(transfer[transfer.index("--plan") + 1])
    assert plan == {"layouts_repo": {"layout": "morning", "local_repo": str((tmp_path / "../my app").resolve())}}
    admin = [args for args in calls if args[-1].startswith("sudo ")]
    assert len(admin) == 1 and "update-remote.sh" in admin[0][-1]
    assert calls.index(["SYNC"]) < calls.index(transfer) < calls.index(admin[0])
    assert calls[-1][1:3] == ["-O", "forward"]


def test_update_never_activates_after_app_transfer_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def record(args, **kwargs):
        calls.append(args)
        if "--transfer-only" in args:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr(remote, "run", record)
    monkeypatch.setattr(remote, "sync", lambda cfg, ssh: None)
    with pytest.raises(ValueError, match="Nothing was activated"):
        remote.update(config(compose="compose.static.yaml", app={"layout": "morning"}), ["test-ssh"])
    assert not any(args[-1].startswith("sudo ") for args in calls)


@pytest.mark.parametrize(
    "app",
    [
        {},
        "wrong",
        {"password": "no"},
        {"layout": "-oOops"},
        {"layout": "morning", "app": "."},
        {"layout": True},
    ],
)
def test_invalid_update_configuration_fails_before_connection(app, monkeypatch):
    monkeypatch.setattr(remote, "require_clean_checkout", lambda root: None)
    monkeypatch.setattr(remote, "ensure_connection", lambda *args: pytest.fail("Must validate before SSH"))
    with pytest.raises(ValueError):
        remote.update(config(app=app), ["test-ssh"])


def test_update_with_no_app_keeps_existing_selection_and_reuses_connection(monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(remote, "require_clean_checkout", lambda root: None)
    monkeypatch.setattr(remote, "sync", lambda *args: None)
    monkeypatch.setattr(
        remote, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(stdout=b"")
    )
    remote.update(config(), ["test-ssh"])
    assert not any("-M" in args or "--transfer-only" in args for args in calls)
    assert sum(args[-1].startswith("sudo ") for args in calls) == 1


def test_tunnel_only_replaces_its_own_exact_forward(monkeypatch):
    calls = []

    def record(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(255, args)

    monkeypatch.setattr(remote, "run", record)
    remote.open_tunnel(config(), ["test-ssh"])
    assert [args[2] for args in calls] == ["forward", "cancel", "forward"]
    assert all(args[3:] == ["-L", "127.0.0.1:8765:127.0.0.1:8765", "example@192.0.2.10"] for args in calls)


@pytest.mark.parametrize("capture_failure", [False, True])
def test_single_admin_script_checks_before_activation(tmp_path, capture_failure):
    source = managed_layout(tmp_path)
    scripts = source / "scripts"
    updater = scripts / "update-remote.sh"
    for name in ("update-remote.sh", "wait-healthy.sh", "rollback-remote.sh", "compose-files.sh",
                 "previous-state.sh", "build-check.sh"):
        shutil.copy(Path(__file__).parents[1] / "scripts" / name, scripts / name)
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "id").write_text("#!/bin/sh\necho 0\n")
    (commands / "docker").write_text(
        '#!/bin/sh\nprintf "%s\n" "$*" >> "$UPDATE_LOG"\n' + DOCKER_FILES_CHECK
        + 'if test "$6" = exec; then cat >/dev/null; fi\n'
    )
    for executable in commands.iterdir():
        executable.chmod(0o700)
    for name in ("prepare-remote.sh", "check-host.sh", "check-containers.sh", "rename-check.sh"):
        (scripts / name).write_text(
            f'printf "%s\n" {name} >> "$UPDATE_LOG"\n'
            + ("exit 1\n" if capture_failure and name == "check-containers.sh" else "")
        )
    log = tmp_path / "operations"
    env = {**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"], "UPDATE_LOG": str(log)}
    result = subprocess.run(
        ["sh", str(updater), "compose.static.host.yaml"], env=env, capture_output=True, text=True
    )
    operations = log.read_text()
    assert "prepare-remote.sh" in operations and "check-host.sh" in operations
    assert operations.index(" build") < operations.index("check-containers.sh")
    assert (" up -d" in operations) is not capture_failure
    assert "image prune" not in operations
    assert operations.count("check-host.sh") == 1
    assert result.returncode == int(capture_failure)


@pytest.mark.parametrize("previous", [False, True])
def test_failed_health_check_rolls_back_to_previous_images(tmp_path, previous):
    scripts = managed_layout(tmp_path) / "scripts"
    for name in ("update-remote.sh", "wait-healthy.sh", "rollback-remote.sh", "compose-files.sh",
                 "previous-state.sh", "build-check.sh"):
        shutil.copy(Path(__file__).parents[1] / "scripts" / name, scripts / name)
    for name in ("prepare-remote.sh", "check-host.sh", "check-containers.sh", "rename-check.sh"):
        (scripts / name).write_text("true\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "id").write_text("#!/bin/sh\necho 0\n")
    (commands / "sleep").write_text("#!/bin/sh\n")
    # Running containers exist; the new version never becomes healthy; restored images do.
    (commands / "docker").write_text(
        """#!/bin/sh
printf "%s\\n" "$*" >> "$UPDATE_LOG"
""" + DOCKER_FILES_CHECK + """case "$*" in
    *" ps -q "*) echo container-id ;;
    "inspect --format"*) echo sha256:running ;;
    "image inspect dayframe:previous") test -n "$HAS_PREVIOUS" ;;
    "image inspect --format {{.Id}}"*) echo sha256:new ;;
    "image inspect"*) exit 0 ;;
    "tag dayframe:previous dayframe:local") touch "$RESTORED" ;;
    *" exec "*) test -e "$RESTORED" ;;
esac
"""
    )
    for executable in commands.iterdir():
        executable.chmod(0o700)
    log = tmp_path / "operations"
    env = {
        **os.environ,
        "PATH": str(commands) + ":" + os.environ["PATH"],
        "UPDATE_LOG": str(log),
        "RESTORED": str(tmp_path / "restored"),
        "HAS_PREVIOUS": "1" if previous else "",
    }
    result = subprocess.run(
        ["sh", str(scripts / "update-remote.sh"), "compose.static.host.yaml"], env=env, capture_output=True, text=True
    )
    operations = log.read_text()
    assert "tag sha256:running dayframe-renderer-static:previous" in operations
    assert operations.index(":previous") < operations.index(" build")
    assert "image prune" not in operations
    assert result.returncode == (3 if previous else 1)
    assert ("Rolled back to the previous images." in result.stdout) is previous
    assert ("image rm sha256:new" in operations) is previous  # the untagged failed image


def week_rollback(tmp_path, *, backup, label="", args=("--yes",), answer="", backup_age=0, label_fails=False,
                  on_copy=None):
    """Run rollback-remote.sh with week-shaped settings and no saved copy.

    The fake docker prints `label` for the previous image's art-frames.settings label, or fails to read it.
    `on_copy` is shell code that a fake cp runs when it copies settings.pre-week.json.
    """
    source = managed_layout(tmp_path)
    for name in ("rollback-remote.sh", "wait-healthy.sh", "compose-files.sh", "previous-state.sh"):
        shutil.copy(Path(__file__).parents[1] / "scripts" / name, source / "scripts" / name)
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "id").write_text("#!/bin/sh\necho 0\n")
    (commands / "docker").write_text(
        "#!/bin/sh\n" + DOCKER_FILES_CHECK
        + 'case "$*" in *art-frames.settings*) test -z "$LABEL_FAILS" || exit 1; printf "%s\\n" "$PREVIOUS_LABEL" ;; esac\n')
    if on_copy:
        (commands / "cp").write_text(
            f'#!/bin/sh\ncase "$*" in *settings.pre-week.json*) {on_copy} ;; esac\nexec {shutil.which("cp")} "$@"\n')
    for executable in commands.iterdir():
        executable.chmod(0o700)
    data = tmp_path / "data"
    (data / "settings.json").write_text('{"app_schedule": {"default": "", "week": {}}}')
    if backup:
        kept = data / "settings.pre-week.json"
        kept.write_text('{"app_schedule": {"default": "", "windows": []}}')
        if backup_age:
            then = time.time() - backup_age
            os.utime(kept, (then, then))
    env = {**os.environ, "PATH": str(commands) + ":" + os.environ["PATH"], "PREVIOUS_LABEL": label,
           "LABEL_FAILS": "1" if label_fails else ""}
    result = subprocess.run(
        ["sh", str(source / "scripts" / "rollback-remote.sh"), *args, "compose.static.host.yaml"],
        env=env, capture_output=True, text=True, input=answer,
    )
    return data, result


@pytest.mark.parametrize("backup", [True, False])
def test_rollback_without_saved_settings_undoes_the_week_shape(tmp_path, backup):
    data, result = week_rollback(tmp_path, backup=backup)
    assert result.returncode == 0, result.stderr
    # The current settings are never lost.
    moved = list(data.glob("settings.week-*.json"))
    assert len(moved) == 1
    assert '"week"' in moved[0].read_text()
    assert f"data/{moved[0].name}" in result.stdout
    if backup:
        assert '"windows"' in (data / "settings.json").read_text()
        assert not (data / "settings.pre-week.json").exists()
        assert len(list(data.glob("settings.pre-week.restored-*.json"))) == 1
        assert "Put back the settings from before the new layout schedule." in result.stdout
    else:
        assert not (data / "settings.json").exists()
        assert "starts from the installation defaults" in result.stdout


@pytest.mark.parametrize("backup", [True, False])
def test_rollback_keeps_week_settings_when_the_previous_image_reads_them(tmp_path, backup):
    data, result = week_rollback(tmp_path, backup=backup, label="week")
    assert result.returncode == 0, result.stderr
    assert '"week"' in (data / "settings.json").read_text()
    assert not list(data.glob("settings.week-*.json"))
    assert (data / "settings.pre-week.json").exists() == backup
    assert "settings stay as they are" in result.stdout


def test_rollback_stops_when_it_cannot_read_the_previous_image_label(tmp_path):
    data, result = week_rollback(tmp_path, backup=True, label_fails=True)
    assert result.returncode == 1
    assert "Could not read the label of the previous Art Frames image. Nothing changed." in result.stderr
    assert '"week"' in (data / "settings.json").read_text()
    assert (data / "settings.pre-week.json").exists() and not list(data.glob("settings.week-*.json"))


def test_rollback_puts_the_current_settings_back_when_the_copy_fails(tmp_path):
    data, result = week_rollback(tmp_path, backup=True, on_copy="exit 1")
    assert result.returncode == 1
    assert "Could not replace the settings. They are back as they were." in result.stderr
    assert '"week"' in (data / "settings.json").read_text()
    assert (data / "settings.pre-week.json").exists() and not list(data.glob("settings.week-*.json"))


def test_a_signal_during_the_settings_move_puts_the_current_settings_back(tmp_path):
    # The signal comes while the pre-week copy goes in. The copy itself works.
    data, result = week_rollback(tmp_path, backup=True, on_copy='kill -TERM "$PPID"')
    assert result.returncode == 1
    assert "They are back as they were." in result.stderr
    assert '"week"' in (data / "settings.json").read_text()
    assert not list(data.glob("settings.week-*.json"))


def test_rollback_asks_before_an_old_pre_week_copy_replaces_the_settings(tmp_path):
    data, result = week_rollback(tmp_path, backup=True, args=(), answer="no\n", backup_age=3 * 3600)
    assert result.returncode == 1
    assert "180 minutes old" in result.stdout
    assert "Rollback stopped. Nothing changed." in result.stdout
    assert '"week"' in (data / "settings.json").read_text()
    assert (data / "settings.pre-week.json").exists()
    assert not list(data.glob("settings.week-*.json"))


def test_open_management_reconnects_then_forwards_without_deployment(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(remote.sys, "platform", "linux")
    monkeypatch.setattr(remote, "ensure_connection", lambda cfg, ssh: calls.append("connect"))
    monkeypatch.setattr(remote, "open_tunnel", lambda cfg, ssh: calls.append("tunnel"))
    monkeypatch.setattr(remote, "run", lambda *args, **kwargs: pytest.fail("Opening must not deploy or sudo"))
    remote.open_management(config(), ["test-ssh"])
    assert calls == ["connect", "tunnel"]
    assert "http://127.0.0.1:8765/" in capsys.readouterr().out


SECRET = "c2VjcmV0LXBhc3N3b3JkLWZvci10ZXN0cw=="


@pytest.mark.parametrize("saved", [True, False])
def test_open_management_copies_password_without_printing_it(monkeypatch, capsys, saved):
    calls = []
    monkeypatch.setattr(remote.sys, "platform", "darwin")
    monkeypatch.setattr(remote, "ensure_connection", lambda cfg, ssh: None)
    monkeypatch.setattr(remote, "open_tunnel", lambda cfg, ssh: None)
    monkeypatch.setattr(remote, "keychain_password", lambda: SECRET if saved else None)
    monkeypatch.setattr(remote, "save_password", lambda cfg, ssh: calls.append("sudo") or SECRET)
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: calls.append((args[0], kwargs.get("input"))))
    remote.open_management(config(), ["test-ssh"])
    assert calls == ([] if saved else ["sudo"]) + [("pbcopy", SECRET), ("open", None)]
    out = capsys.readouterr().out
    assert SECRET not in out and "Paste it in the sign-in form." in out


def test_new_password_skips_the_keychain(monkeypatch):
    monkeypatch.setattr(remote.sys, "platform", "darwin")
    monkeypatch.setattr(remote, "ensure_connection", lambda cfg, ssh: None)
    monkeypatch.setattr(remote, "open_tunnel", lambda cfg, ssh: None)
    monkeypatch.setattr(remote, "keychain_password", lambda: pytest.fail("refresh must not reuse the saved password"))
    monkeypatch.setattr(remote, "save_password", lambda cfg, ssh: SECRET)
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: None)
    remote.open_management(config(), ["test-ssh"], refresh=True)


def test_save_password_uses_sudo_once_and_keeps_secret_off_argv(monkeypatch, capsys):
    seen = []

    class Done:
        returncode = 0
        stdout = "\r\nDAYFRAME-PW:" + SECRET + "\r\n"

    monkeypatch.setattr(remote.subprocess, "run", lambda args, **kwargs: seen.append(args) or Done())
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: seen.append((args, kwargs["input"])))
    monkeypatch.setattr(remote, "keychain_service", lambda: "dayframe-management")
    assert remote.save_password(config(), ["test-ssh"]) == SECRET
    ssh_args, (keychain_args, keychain_input) = seen
    assert "sudo -p ''" in ssh_args[-1] and "/volume1/docker/dayframe/.secrets/web_password" in ssh_args[-1]
    assert keychain_args == ["security", "-i"] and SECRET in keychain_input
    assert "-s dayframe-management -a dayframe -l 'Art Frames'" in keychain_input
    assert all(SECRET not in arg for arg in ssh_args + keychain_args)
    assert SECRET not in capsys.readouterr().out


def test_save_password_fails_without_the_marker(monkeypatch):
    class Failed:
        returncode = 1
        stdout = "Sorry, try again.\r\n"

    monkeypatch.setattr(remote.subprocess, "run", lambda args, **kwargs: Failed())
    monkeypatch.setattr(remote, "run", lambda *args, **kwargs: pytest.fail("nothing is saved"))
    with pytest.raises(ValueError, match="Could not read"):
        remote.save_password(config(), ["test-ssh"])


@pytest.mark.parametrize("saved, password, service", [
    ({}, None, "art-frames-management"),
    ({"dayframe-management": "old-saved-password"}, "old-saved-password", "dayframe-management"),
    ({"art-frames-management": "new-saved-password", "dayframe-management": "old"}, "new-saved-password",
     "art-frames-management"),
])
def test_the_keychain_reads_the_new_service_then_the_old_one(monkeypatch, saved, password, service):
    class Found:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    def find(args, **kwargs):
        name = args[args.index("-s") + 1]
        assert args[args.index("-a") + 1] == "dayframe"
        return Found(0, saved[name] + "\n") if name in saved else Found(44)

    monkeypatch.setattr(remote.subprocess, "run", find)
    assert remote.keychain_password() == password
    assert remote.keychain_service() == service


def test_the_nas_path_may_end_in_dayframe_or_art_frames():
    assert remote.target(config())[1] == "/volume1/docker/dayframe"
    assert remote.target(config("/volume1/docker/art-frames"))[1] == "/volume1/docker/art-frames"
    with pytest.raises(ValueError, match="ending in /dayframe"):
        remote.target(config("/volume1/docker/frames"))
