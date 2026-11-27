"""scripts/lan-access.sh and `remote.py lan`: open the app on the home network, and close it again."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_remote import remote

home_network_url = remote.home_network_url

SCRIPT = Path(__file__).parents[1] / "scripts" / "lan-access.sh"


def run_script(base, mode, host="192.0.2.10"):
    return subprocess.run(["sh", str(SCRIPT), str(base), mode, host], capture_output=True, text=True)


def env_lines(base):
    return (base / ".env").read_text().splitlines()


@pytest.fixture
def base(tmp_path):
    folder = tmp_path / "dayframe"
    folder.mkdir()
    (folder / ".env").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\nDAYFRAME_SITE_NETWORK=none\n")
    (folder / ".env").chmod(0o600)
    return folder


def test_on_adds_the_two_lines_and_keeps_every_other_line(base):
    result = run_script(base, "on")
    assert result.returncode == 0, result.stderr
    assert env_lines(base) == [
        "DAYFRAME_UID=10001",
        "DAYFRAME_GID=10001",
        "DAYFRAME_SITE_NETWORK=none",
        "DAYFRAME_LISTEN=0.0.0.0",
        "DAYFRAME_HOSTS=*",
    ]
    assert "open http://192.0.2.10:8765" in result.stdout
    assert (base / ".env").stat().st_mode & 0o777 == 0o600
    assert [path.name for path in base.iterdir()] == [".env"]


def test_on_twice_changes_nothing_more(base):
    run_script(base, "on")
    first = env_lines(base)
    run_script(base, "on")
    assert env_lines(base) == first


def test_on_keeps_listed_host_names_and_off_puts_them_back(base):
    (base / ".env").write_text("DAYFRAME_HOSTS=frames.home.arpa\nDAYFRAME_PORT=9000\n")
    result = run_script(base, "on")
    assert env_lines(base) == ["DAYFRAME_PORT=9000", "DAYFRAME_LISTEN=0.0.0.0", "DAYFRAME_HOSTS=*,frames.home.arpa"]
    assert "open http://192.0.2.10:9000" in result.stdout
    run_script(base, "off")
    assert env_lines(base) == ["DAYFRAME_PORT=9000", "DAYFRAME_HOSTS=frames.home.arpa"]


def test_off_removes_the_lines_it_added(base):
    before = env_lines(base)
    run_script(base, "on")
    result = run_script(base, "off")
    assert result.returncode == 0, result.stderr
    assert env_lines(base) == before
    assert "npm run open" in result.stdout


def test_a_missing_env_file_or_a_bad_mode_stops(tmp_path, base):
    assert run_script(tmp_path / "missing", "on").returncode == 1
    assert run_script(base, "maybe").returncode == 2
    assert "DAYFRAME_LISTEN" not in (base / ".env").read_text()


@pytest.mark.parametrize("action, mode", [("lan", "on"), ("lan-off", "off")])
def test_remote_lan_edits_env_then_restarts_the_project(tmp_path, monkeypatch, action, mode):
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    nas = tmp_path.resolve() / "dayframe"
    scripts = nas / "source" / "scripts"
    scripts.mkdir(parents=True)
    (nas / ".env").write_text("DAYFRAME_UID=10001\n")
    (root / ".deploy.local.toml").write_text(f'host = "192.0.2.10"\nuser = "example"\npath = "{nas}"\n')
    for name in ("compose-files.sh", "build-check.sh", "lan-access.sh"):
        shutil.copy(Path(__file__).parents[1] / "scripts" / name, scripts / name)
    (scripts / "check-host.sh").write_text("exit 0\n")
    (scripts / "check-containers.sh").write_text("exit 0\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {"sudo": 'exec "$@"', "docker": 'printf "%s\\n" "$*" >> "$COMMAND_LOG"'}.items():
        (commands / name).write_text("#!/bin/sh\n" + body + "\n")
        (commands / name).chmod(0o700)
    log = tmp_path / "commands.log"
    monkeypatch.setenv("PATH", str(commands) + ":" + os.environ["PATH"])
    monkeypatch.setenv("COMMAND_LOG", str(log))
    monkeypatch.setattr(remote, "ROOT", root)
    monkeypatch.setattr(remote, "ssh_options", lambda config: ["test-ssh"])
    monkeypatch.setattr(remote, "ensure_connection", lambda config, ssh: None)
    monkeypatch.setattr("sys.argv", ["remote.py", action])
    monkeypatch.setattr(
        remote, "run", lambda args, **kwargs: subprocess.run(["sh", "-c", args[-1]], check=True, **kwargs)
    )
    remote.main()
    assert ("DAYFRAME_LISTEN=0.0.0.0" in (nas / ".env").read_text()) is (mode == "on")
    assert "compose -p dayframe -f compose.yaml up -d --remove-orphans" in log.read_text().splitlines()


def test_remote_lan_before_the_update_says_to_deploy_first(tmp_path, monkeypatch, capfd):
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    nas = tmp_path.resolve() / "dayframe"
    (nas / "source" / "scripts").mkdir(parents=True)
    (root / ".deploy.local.toml").write_text(f'host = "192.0.2.10"\nuser = "example"\npath = "{nas}"\n')
    monkeypatch.setattr(remote, "ROOT", root)
    monkeypatch.setattr(remote, "ssh_options", lambda config: ["test-ssh"])
    monkeypatch.setattr(remote, "ensure_connection", lambda config, ssh: None)
    monkeypatch.setattr("sys.argv", ["remote.py", "lan"])
    monkeypatch.setattr(
        remote, "run", lambda args, **kwargs: subprocess.run(["sh", "-c", args[-1]], check=True, **kwargs)
    )
    with pytest.raises(subprocess.CalledProcessError):
        remote.main()
    assert "Run npm run deploy first." in capfd.readouterr().err


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_the_hint_names_the_address_when_the_app_answers_there(monkeypatch, capsys):
    monkeypatch.setattr(remote, "home_network_url", home_network_url)
    asked = []
    monkeypatch.setattr(remote.urllib.request, "urlopen", lambda url, timeout: asked.append(url) or FakeResponse())
    remote.print_home_network_hint({"host": "192.0.2.10"})
    assert asked == ["http://192.0.2.10:8765/health"]
    assert "On your home network: http://192.0.2.10:8765/" in capsys.readouterr().out


def test_the_hint_points_to_npm_run_lan_when_the_app_does_not_answer(monkeypatch, capsys):
    def refuse(url, timeout):
        raise ConnectionRefusedError

    monkeypatch.setattr(remote, "home_network_url", home_network_url)
    monkeypatch.setattr(remote.urllib.request, "urlopen", refuse)
    remote.print_home_network_hint({"host": "192.0.2.10"})
    assert "run `npm run lan`" in capsys.readouterr().out
