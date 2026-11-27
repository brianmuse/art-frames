"""scripts/rename-check.sh: checks, a plan and one backup. It never moves or deletes runtime files."""

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from test_remote import managed_layout

SCRIPTS = Path(__file__).parents[1] / "scripts"
DOCKER = """#!/bin/sh
printf "%s\\n" "$*" >> "$DOCKER_LOG"
case "$*" in
    "ps -q --filter label=com.docker.compose.project=dayframe"*) echo controller-1 ;;
    "ps -q --filter label=com.docker.compose.project=art-frames"*) test -n "${OTHER_PROJECT:-}" && echo other ;;
    "ps --filter label=com.docker.compose.project=dayframe --format {{.Label"*) printf 'website\\ndayframe\\nrenderer\\ndayframe\\n' ;;
    "image inspect"*) exit 0 ;;
esac
exit 0
"""


def nas(tmp_path):
    base = tmp_path / "dayframe"
    source = managed_layout(base)
    for name in ("rename-check.sh", "previous-state.sh", "compose-files.sh"):
        shutil.copy(SCRIPTS / name, source / "scripts" / name)
    (base / ".dayframe-managed").write_text("dayframe-managed-source-v1\n")
    (base / ".env").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
    (base / "config.local.toml").write_text("")
    (base / ".secrets").mkdir()
    (base / "data/tokens").mkdir(parents=True)
    (base / "data/tokens/living-room-tv.txt").write_text("SYNTHETIC-TV-TOKEN")
    (base / "data/settings.json").write_text("{}")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "id").write_text("#!/bin/sh\necho 0\n")
    (bin_dir / "docker").write_text(DOCKER)
    for executable in bin_dir.iterdir():
        executable.chmod(0o700)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "DOCKER_LOG": str(tmp_path / "docker.log")}
    return base, env


def run(base, env, *args):
    return subprocess.run(["sh", str(base / "source/scripts/rename-check.sh"), *args, "compose.static.host.yaml"],
                          env=env, capture_output=True, text=True)


def backups(base):
    return sorted(base.glob(".rename-backup-*/"))


def test_a_dry_run_checks_and_prints_the_plan_without_a_backup(tmp_path):
    base, env = nas(tmp_path)
    before = sorted(path.relative_to(base) for path in base.rglob("*"))
    result = run(base, env, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STOP" not in result.stdout
    assert "OK    TV tokens: 1 in data/tokens" in result.stdout
    assert "OK    Compose project dayframe runs: dayframe renderer website" in result.stdout
    assert 'STAYS:   the NAS folder, the Docker project "dayframe"' in result.stdout
    assert "BACKUP:  skipped: --dry-run" in result.stdout
    assert "living-room-tv" not in result.stdout and "SYNTHETIC-TV-TOKEN" not in result.stdout
    assert sorted(path.relative_to(base) for path in base.rglob("*")) == before
    assert not (base / ".update-lock").exists()


def test_the_backup_runs_once_and_keeps_every_runtime_file(tmp_path):
    base, env = nas(tmp_path)
    result = run(base, env)
    assert result.returncode == 0, result.stdout + result.stderr
    [backup] = backups(base)
    assert (base / ".rename-backup-done").is_file()
    assert (backup / "env").read_text() == (base / ".env").read_text()
    with tarfile.open(backup / "runtime.tar") as archive:
        members = archive.getnames()
    assert "data/tokens/living-room-tv.txt" in members and ".dayframe-managed" in members
    assert (base / "data/tokens/living-room-tv.txt").read_text() == "SYNTHETIC-TV-TOKEN"
    assert "SYNTHETIC-TV-TOKEN" not in result.stdout
    # update-remote.sh runs it with --auto on every update: silent, and no second backup.
    again = run(base, env, "--auto")
    assert again.returncode == 0 and again.stdout == "" and len(backups(base)) == 1


def test_a_stop_changes_nothing(tmp_path):
    base, env = nas(tmp_path)
    (base / ".previous").mkdir()
    (base / ".previous/unfinished").touch()
    result = run(base, env)
    assert result.returncode == 1
    assert "STOP  An update did not finish" in result.stdout
    assert backups(base) == [] and not (base / ".rename-backup-done").exists()


def test_a_second_compose_project_stops_the_check(tmp_path):
    base, env = nas(tmp_path)
    result = run(base, {**env, "OTHER_PROJECT": "1"}, "--dry-run")
    assert result.returncode == 1 and "Compose project named art-frames" in result.stdout


def test_the_env_keys_must_keep_the_dayframe_prefix(tmp_path):
    base, env = nas(tmp_path)
    (base / ".env").write_text("ART_FRAMES_UID=10001\nART_FRAMES_GID=10001\n")
    result = run(base, env, "--dry-run")
    assert result.returncode == 1 and "STOP  .env needs the lines DAYFRAME_UID=10001" in result.stdout


def test_every_update_runs_the_check_before_it_changes_anything():
    update = (SCRIPTS / "update-remote.sh").read_text()
    call = 'DAYFRAME_LOCK_HELD=1 sh scripts/rename-check.sh --auto "$compose"'
    assert call in update
    assert update.index("\ntake_lock\n") < update.index(call) < update.index("staged=$(check_incoming)")


def test_a_new_install_has_nothing_to_back_up(tmp_path):
    base, env = nas(tmp_path)
    (base / "data/settings.json").unlink()
    result = run(base, env, "--auto")
    assert result.returncode == 0 and result.stdout == ""
    assert (base / ".rename-backup-done").is_file() and backups(base) == []


def fake_tar(tmp_path, status):
    """A GNU-like tar: real tar does the work, then the fake exits with `status`."""
    real = shutil.which("tar")
    (tmp_path / "bin/tar").write_text(f"""#!/bin/sh
case "$1" in --version) echo 'tar (GNU tar) 1.34'; exit 0 ;; esac
for arg in "$@"; do
    shift
    case "$arg" in --warning=*) ;; *) set -- "$@" "$arg" ;; esac
done
{real} "$@" || exit 2
exit {status}
""")
    (tmp_path / "bin/tar").chmod(0o700)


def test_a_file_that_changed_while_tar_read_it_is_only_a_warning(tmp_path):
    base, env = nas(tmp_path)
    fake_tar(tmp_path, 1)
    result = run(base, env)
    assert result.returncode == 0, result.stdout + result.stderr
    [backup] = backups(base)
    assert (backup / "runtime.tar").is_file() and (base / ".rename-backup-done").is_file()


def test_a_failed_backup_leaves_no_partial_folder(tmp_path):
    base, env = nas(tmp_path)
    fake_tar(tmp_path, 2)
    result = run(base, env)
    assert result.returncode == 1 and "The backup failed" in result.stderr
    assert backups(base) == [] and not (base / ".rename-backup-done").exists()
    assert not (base / ".update-lock").exists()


def test_in_an_update_a_failed_backup_warns_and_the_update_goes_on(tmp_path):
    base, env = nas(tmp_path)
    fake_tar(tmp_path, 2)
    result = run(base, env, "--auto")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN  The rename backup failed" in result.stderr
    # No marker: the next update tries the backup again.
    assert backups(base) == [] and not (base / ".rename-backup-done").exists()


def test_the_backup_skips_sqlite_wal_and_shm_files(tmp_path):
    base, env = nas(tmp_path)
    for name in ("photos.db", "photos.db-wal", "photos.db-shm"):
        (base / "data" / name).write_text("x")
    assert run(base, env).returncode == 0
    [backup] = backups(base)
    with tarfile.open(backup / "runtime.tar") as archive:
        members = archive.getnames()
    assert "data/photos.db" in members
    assert "data/photos.db-wal" not in members and "data/photos.db-shm" not in members


def test_a_free_space_stop_says_how_to_skip_the_backup(tmp_path):
    base, env = nas(tmp_path)
    (tmp_path / "bin/df").write_text("#!/bin/sh\necho header\necho fs 1 1 0 100% /\n")
    (tmp_path / "bin/df").chmod(0o700)
    result = run(base, env, "--dry-run")
    assert result.returncode == 1 and "STOP  Not enough free space" in result.stdout
    assert f"date -u > {base}/.rename-backup-done" in result.stdout
