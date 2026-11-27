"""Exercise SSH policy validation against a disposable config, never host settings."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def policy(tmp_path):
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).is_file() or not shutil.which("ssh-keygen"):
        pytest.skip("OpenSSH server tools unavailable")
    base = tmp_path / "dayframe"
    scripts = base / "source" / "scripts"
    scripts.mkdir(parents=True)
    (base / ".dayframe-managed").write_text("dayframe-managed-source-v1")
    config = tmp_path / "sshd_config"
    config.write_text("AllowTcpForwarding no\nMatch User anonymous\n    AllowTcpForwarding no\n")
    config.chmod(0o644)
    key = tmp_path / "hostkey"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pidfile = tmp_path / "sshd.pid"
    pidfile.write_text("12345")
    comm = tmp_path / "sshd.comm"
    comm.write_text("sshd")
    commands = tmp_path / "bin"
    commands.mkdir()
    for name, body in {
        "id": 'if test "$#" = 1; then echo 0; else echo 1026; fi',
        "sshd": f'exec "{sshd}" -h "{key}" "$@"',
        "dayframe_test_reload": 'echo reload >> "$RELOAD_LOG"; exit "${RELOAD_FAIL:-0}"',
    }.items():
        command = commands / name
        command.write_text("#!/bin/sh\n" + body + "\n")
        command.chmod(0o700)
    # Redirect only fixed system paths and the reload signal in this disposable copy.
    source = (Path(__file__).parents[1] / "scripts/enable-tunnel.sh").read_text()
    source = source.replace("config=/etc/ssh/sshd_config", f'config="{config}"')
    source = source.replace("/etc/ssh/.dayframe.XXXXXX", str(tmp_path / ".dayframe.XXXXXX"))
    source = source.replace("/run/sshd.pid", str(pidfile))
    source = source.replace("/proc/$pid/comm", str(comm))
    source = source.replace('kill -HUP "$pid"', 'dayframe_test_reload "$pid"')
    script = scripts / "enable-tunnel.sh"
    script.write_text(source)
    log = tmp_path / "reload.log"
    env = dict(os.environ, PATH=str(commands) + ":" + os.environ["PATH"], RELOAD_LOG=str(log))
    return base, config, script, env, log


def apply(policy, **extra_env):
    _, _, script, env, _ = policy
    return subprocess.run(
        ["sh", str(script), "example"], env=dict(env, **extra_env), text=True, capture_output=True
    )


def test_scoped_policy_validates_backs_up_and_is_idempotent(policy):
    base, config, _, env, log = policy
    original = config.read_bytes()
    result = apply(policy)
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "reload\n"
    backups = list((base / "ssh-backups").iterdir())
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert (base / "ssh-backups").stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == 0o644
    for user, forwarding in [("example", "local"), ("anonymous", "no"), ("other", "no")]:
        effective = subprocess.check_output(
            ["sshd", "-T", "-f", str(config), "-C", f"user={user},host=localhost,addr=127.0.0.1"],
            env=env,
            text=True,
        )
        # Newer OpenSSH prints keywords in mixed case (AllowTcpForwarding); compare them in lower case.
        effective = "".join(f"{key.lower()} {value}\n" for key, _, value in
                            (line.partition(" ") for line in effective.splitlines()))
        assert f"allowtcpforwarding {forwarding}\n" in effective
        if user == "example":
            assert "permitopen 127.0.0.1:8765\n" in effective
    assert apply(policy).returncode == 0
    assert log.read_text() == "reload\nreload\n"
    assert len(list((base / "ssh-backups").iterdir())) == 1


@pytest.mark.parametrize(
    "content",
    [
        "AllowTcpForwarding yes\n",
        "AllowTcpForwarding no\nMatch User example\n    AllowTcpForwarding no\n",
        "AllowTcpForwarding no\n# Dayframe tunnel for example\n",
        "AllowTcpForwarding no\n# Art Frames tunnel for example\n",
        "AllowTcpForwarding no\nMatch Address 192.0.2.10\n    PermitOpen any\n",
        "AllowTcpForwarding no\nMatch=Address 192.0.2.10\n    PermitOpen any\n",
        'AllowTcpForwarding no\n"Match" Address 192.0.2.10\n    PermitOpen any\n',
        "AllowTcpForwarding no\nMatch User !root\n    PermitOpen any\n",
        "AllowTcpForwarding no\nInclude /etc/ssh/sshd_config.d/*.conf\n",
        "AllowTcpForwarding no\nInclude=/etc/ssh/sshd_config.d/*.conf\n",
        'AllowTcpForwarding no\n"Include" /etc/ssh/sshd_config.d/*.conf\n',
        "AllowTcpForwarding no\nDisableForwarding yes\n",
        "DisableForwarding yes\nMatch User example\n    AllowTcpForwarding local\n    PermitOpen 127.0.0.1:8765\n",
        "InvalidDirective yes\n",
    ],
)
def test_refuses_conflicting_or_invalid_policy_without_writing(policy, content):
    base, config, _, _, log = policy
    config.write_text(content)
    assert apply(policy).returncode != 0
    assert config.read_text() == content
    assert not log.exists()
    assert not (base / "ssh-backups").exists()
    assert not list(config.parent.glob(".dayframe.*"))


def test_failed_reload_restores_original_bytes_and_mode(policy):
    _, config, _, _, log = policy
    original = config.read_bytes()
    result = apply(policy, RELOAD_FAIL="1")
    assert result.returncode != 0
    assert "original configuration restored" in result.stderr
    assert log.read_text() == "reload\n"
    assert config.read_bytes() == original
    assert config.stat().st_mode & 0o777 == 0o644


def test_retry_reloads_after_interruption_between_install_and_signal(policy):
    _, config, _, env, log = policy
    commands = Path(env["PATH"].split(":")[0])
    move = commands / "mv"
    move.write_text(f'#!/bin/sh\n"{shutil.which("mv")}" "$@"\nkill -HUP "$PPID"\n')
    move.chmod(0o700)
    assert apply(policy).returncode != 0
    assert "Match User example" in config.read_text()
    assert not log.exists()
    move.unlink()
    assert apply(policy).returncode == 0
    assert log.read_text() == "reload\n"
