"""Health, failure logging and deployment helpers; no NAS, TV or real renderer."""

import base64
import importlib.util
import io
import logging
import re
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from art_frames import site_service
from art_frames.web import create_app, failure

PASSWORD = "a-long-test-password-not-a-real-secret"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"dayframe:{PASSWORD}".encode()).decode()}

spec = importlib.util.spec_from_file_location("remote", Path(__file__).parents[1] / "scripts/remote.py")
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("DAYFRAME_RENDERER_SOCKET", raising=False)
    return create_app(tmp_path, password=PASSWORD, testing=True)


@pytest.fixture
def short_dir():
    # macOS limits Unix socket paths to about 100 bytes; pytest's tmp_path can exceed that.
    with tempfile.TemporaryDirectory(prefix="df-", dir="/tmp") as directory:
        yield Path(directory)


def test_health_is_ok_without_a_configured_renderer(app):
    response = app.test_client().get("/health")
    assert response.status_code == 200 and response.json == {"status": "ok"}


def test_health_fails_without_details_when_renderer_socket_is_missing(app, monkeypatch, short_dir, caplog):
    monkeypatch.setenv("DAYFRAME_RENDERER_SOCKET", str(short_dir / "render.sock"))
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.json == {"status": "unavailable"}
    assert "renderer unavailable" in caplog.text and str(short_dir) not in caplog.text


def test_health_connects_to_a_listening_renderer(app, monkeypatch, short_dir):
    path = short_dir / "render.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen()
        monkeypatch.setenv("DAYFRAME_RENDERER_SOCKET", str(path))
        assert app.test_client().get("/health").status_code == 200


def test_health_fails_when_the_scheduler_thread_died(app):
    displays = app.extensions["displays"]
    displays.thread = threading.Thread(target=lambda: None)
    displays.thread.start()
    displays.thread.join()
    assert app.test_client().get("/health").status_code == 503


def test_demo_health_ignores_the_renderer(tmp_path, monkeypatch, short_dir):
    monkeypatch.setenv("DAYFRAME_RENDERER_SOCKET", str(short_dir / "missing.sock"))
    demo = create_app(tmp_path, demo=True, testing=True)
    assert demo.test_client().get("/health").status_code == 200


def test_failure_logs_own_messages_and_only_class_names_for_others(short_dir):
    with pytest.raises(ValueError) as own:
        site_service.request(str(short_dir / "missing.sock"), "/index.html")
    assert failure(own.value) == "ValueError: The default website service is unavailable; check its container"
    with pytest.raises(ValueError) as foreign:
        raise ValueError("https://calendar.example.invalid/private-token.ics")
    assert failure(foreign.value) == "ValueError"
    assert failure(RuntimeError("SYNTHETIC-PRIVATE-TV-TOKEN")) == "RuntimeError"


def test_failed_operation_logs_its_stage_and_cause(app, caplog):
    client = app.test_client()
    client.get("/", headers=AUTH)
    with client.session_transaction() as session:
        token = session["csrf"]
    with caplog.at_level(logging.WARNING):
        client.post("/upload", headers=AUTH, data={"csrf": token})
    assert "upload failed: ValueError: Choose an image" in caplog.text


def config(**changes):
    return {"host": "192.0.2.10", "user": "example", "path": "/volume1/docker/dayframe", **changes}


def test_logs_command_is_bounded_and_validates_the_service():
    command = remote.logs_command(config(compose="compose.static.host.yaml"), "renderer", follow=True)
    # The shared helper adds the extra-app override on a static profile when the host has one.
    assert command.endswith(
        "compose -p dayframe $(. scripts/compose-files.sh && compose_files compose.static.host.yaml) "
        "logs --tail 200 --follow renderer"
    )
    assert remote.logs_command(config()).endswith("logs --tail 200")
    with pytest.raises(ValueError):
        remote.logs_command(config(), "renderer; id")
    # --since shows every line from then, in place of the last 200.
    assert remote.logs_command(config(), "dayframe", since="24h").endswith("logs --since 24h dayframe")
    for bad in ("24", "0h", "1d", "24h; id", "-1h"):
        with pytest.raises(ValueError):
            remote.logs_command(config(), since=bad)


def test_logs_reuses_the_connection_and_one_sudo(monkeypatch):
    calls = []
    monkeypatch.setattr(remote, "ensure_connection", lambda cfg, ssh: calls.append("connect"))
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: calls.append(args))
    remote.logs(config(), ["test-ssh"], "dayframe")
    assert calls[0] == "connect"
    assert "BatchMode=yes" in calls[1] and "-t" in calls[1] and calls[1][-1].count("sudo ") == 1


def test_rollback_runs_the_admin_script_then_reopens_the_tunnel(monkeypatch):
    calls = []
    monkeypatch.setattr(remote, "ensure_connection", lambda cfg, ssh: calls.append("connect"))
    monkeypatch.setattr(remote, "open_tunnel", lambda cfg, ssh: calls.append("tunnel"))
    monkeypatch.setattr(remote, "run", lambda args, **kwargs: calls.append(args[-1]))
    remote.rollback(config(compose="compose.static.host.yaml"), ["test-ssh"])
    assert calls[0] == "connect" and calls[-1] == "tunnel"
    assert calls[1] == "sudo sh /volume1/docker/dayframe/source/scripts/rollback-remote.sh compose.static.host.yaml"


@pytest.mark.parametrize("code,text", [(3, "rolled back"), (1, "npm run logs")])
def test_failed_update_points_to_logs_and_reports_rollback(monkeypatch, code, text):
    def run(args, **kwargs):
        if args[-1].startswith("sudo "):
            raise subprocess.CalledProcessError(code, args)

    monkeypatch.setattr(remote, "require_clean_checkout", lambda root: None)
    monkeypatch.setattr(remote, "sync", lambda *args: None)
    monkeypatch.setattr(remote, "open_tunnel", lambda *args: None)
    monkeypatch.setattr(remote, "run", run)
    with pytest.raises(ValueError, match=text) as error:
        remote.update(config(), ["test-ssh"])
    assert "npm run logs" in str(error.value)


def test_stale_control_socket_is_replaced_before_the_single_login(monkeypatch, short_dir):
    stale = short_dir / "ssh-test"
    stale.write_text("")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "check" in args:
            raise subprocess.CalledProcessError(255, args)
        assert not stale.exists()

    monkeypatch.setattr(remote, "run", run)
    remote.ensure_connection(config(), ["ssh", "-S", str(stale)])
    assert sum("-M" in args for args in calls) == 1


def test_only_logs_accepts_a_service(monkeypatch, tmp_path):
    (tmp_path / ".deploy.local.toml").write_text('host = "192.0.2.10"\nuser = "example"\npath = "/srv/apps/dayframe"\n')
    monkeypatch.setattr(remote, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["remote.py", "update", "renderer"])
    with pytest.raises(SystemExit):
        remote.main()


def test_renderer_logs_only_its_tagged_child_summary():
    from renderer import static_server

    stderr = b"WARNING: Ignored `color: secret-page-text` at 1:1\ndayframe-render-error: ValueError: Incomplete render frame\n"
    assert static_server.child_summary(stderr) == "ValueError: Incomplete render frame"
    assert static_server.child_summary(b"secret-page-text\n") == ""
    assert static_server.describe(KeyError("secret-page-text")) == "KeyError"
    from renderer.pipe import read_pipe
    with pytest.raises(ValueError) as own:
        read_pipe(io.BytesIO(b""))
    assert static_server.describe(own.value) == "ValueError: Incomplete render frame"
    with pytest.raises(ValueError) as library:
        Path("secret-page-text").relative_to("other")  # raised inside a library, not Art Frames
    assert static_server.describe(library.value) == "ValueError"
    with pytest.raises(ValueError) as builtin:
        int("secret-page-text")
    assert static_server.describe(builtin.value) == "ValueError"


def test_renderer_names_the_os_error_code_without_its_message():
    import errno

    from renderer import static_server

    error = OSError(errno.ENOSPC, "No space left on device", "/tmp/secret-page-text")
    assert static_server.describe(error) == "OSError ENOSPC"
    assert static_server.describe(OSError("secret-page-text")) == "OSError"


# A render child that leaves a web-font copy in its temporary folder, answers, then waits
# to be stopped: the supervisor always stops it with SIGKILL, so it never cleans up itself.
LEAKY_CHILD = """
import os, sys, tempfile
from art_frames.ipc import encode
folder = tempfile.mkdtemp(prefix="weasyprint-")
open(os.path.join(folder, "font.ttf"), "wb").write(b"x" * 400_000)
sys.stdout.buffer.write(encode({"type": "image", "body": ""}))
sys.stdout.buffer.flush()
import time; time.sleep(30)
"""


def test_renderer_removes_each_jobs_temporary_files(monkeypatch, tmp_path):
    import asyncio
    import os
    import sys

    from art_frames.ipc import encode, receive_async
    from renderer import static_server

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))  # a child that is not given its own folder uses this one
    real_exec = asyncio.create_subprocess_exec
    seen = []

    async def leaky_child(*args, env=None, **kwargs):
        seen.append((env or os.environ)["TMPDIR"])
        return await real_exec(sys.executable, "-c", LEAKY_CHILD, env=env, **kwargs)
    monkeypatch.setattr(static_server.asyncio, "create_subprocess_exec", leaky_child)

    async def render():
        reader = asyncio.StreamReader()
        reader.feed_data(encode({"url": "http://dayframe.local/index.html"}))
        sent = asyncio.StreamReader()

        class Writer:
            def write(self, data):
                sent.feed_data(data)

            async def drain(self):
                pass

            def close(self):
                sent.feed_eof()

            async def wait_closed(self):
                pass

        await static_server.handle(reader, Writer())
        return await receive_async(sent)

    for _ in range(3):
        assert asyncio.run(render())["type"] == "image"
    # Nothing is left behind, and each job had its own folder inside the temporary folder.
    assert list(tmp_path.iterdir()) == []
    assert len(set(seen)) == 3 and all(Path(folder).parent == tmp_path for folder in seen)


def test_renderer_ignores_health_probes_quietly(caplog):
    import asyncio

    from renderer import static_server

    async def probe():
        reader = asyncio.StreamReader()
        reader.feed_eof()

        class Writer:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        await static_server.handle(reader, Writer())

    with caplog.at_level(logging.INFO):
        asyncio.run(probe())
    assert "Render" not in caplog.text


@pytest.mark.parametrize("user, status", [("dayframe", 200), ("art-frames", 200), ("someone", 401), ("Art Frames", 401)])
def test_sign_in_accepts_the_old_and_the_new_username(app, user, status):
    header = {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()}
    response = app.test_client().get("/", headers=header)
    assert (response.status_code == 401) == (status == 401)


def test_the_sign_in_prompt_names_art_frames(app):
    response = app.test_client().get("/")
    assert response.status_code == 401
    # No Basic challenge, so a browser shows the sign-in form, not its own box. Scripts still send Basic auth.
    assert "WWW-Authenticate" not in response.headers
    page = response.get_data(as_text=True)
    # The form says where the password is, and has its stylesheet.
    assert "<h1>Sign in to Art Frames</h1>" in page and 'name="password"' in page
    assert "Art Frames password file" in page and 'href="/static/style.css' in page
    # It leaks no settings: no TV name, address or page context, and no session token.
    assert "Frame 1" not in page and "csrf" not in page
    assert app.test_client().get("/static/style.css").status_code == 200
    assert app.test_client().get("/household").status_code == 401


def test_pages_link_the_art_frames_icons_and_they_load(app):
    client = app.test_client()
    page = client.get("/", headers=AUTH, follow_redirects=True).get_data(as_text=True)
    assert "<title>" in page and " · Art Frames</title>" in page
    links = re.findall(r'<link rel="(icon|apple-touch-icon|manifest)" href="([^"]+)"', page)
    assert [rel for rel, _ in links] == ["icon", "icon", "apple-touch-icon", "manifest"]
    for _, href in links:
        assert client.get(href, headers=AUTH).status_code == 200, href
    manifest = client.get("/static/manifest.json", headers=AUTH).get_json()
    assert manifest["name"] == "Art Frames"
    for icon in manifest["icons"]:
        assert client.get("/static/" + icon["src"], headers=AUTH).status_code == 200, icon["src"]
