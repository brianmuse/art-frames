"""The install file (install/docker-compose.yml) runs the layouts from the layouts repo.

Each layout image copies its layout.json next to its socket. Art Frames finds the first layout
through ART_FRAMES_LAYOUT_MANIFEST and the others through ART_FRAMES_APPS. These tests check that
the file's settings, mounts and services fit together, without Docker.
"""

import json
import re
from pathlib import Path

import pytest

from art_frames import apps

FILE = Path(__file__).parents[1] / "install" / "docker-compose.yml"
LAYOUTS = ["morning", "news", "weather"]


def text():
    return FILE.read_text()


def service(name):
    """The lines of one top-level service, up to the next service or section."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|^[a-z])", text(), re.S | re.M)
    assert match, f"No {name} service in the install file"
    return match.group(1)


def environment():
    """The art-frames service's environment, as written in the file."""
    block = service("art-frames").split("    environment:\n", 1)[1].split("    volumes:\n", 1)[0]
    values = {}
    for line in block.splitlines():
        match = re.fullmatch(r"\s+([A-Z_]+): (\"[^\"]*\"|[^#]*?)\s*(#.*)?", line)
        if match:
            values[match.group(1)] = match.group(2).strip('"')
    return values


def test_every_layout_runs_hardened_with_only_its_own_folders():
    host = service("art-frames")
    for layout in LAYOUTS:
        body = service(f"layout-{layout}")
        assert "<<: *hardening" in body
        assert f"image: ghcr.io/brianmuse/art-frames-layout-{layout}:${{ART_FRAMES_LAYOUTS_VERSION:-latest}}" in body
        mounts = re.findall(r"^      - (\S+)$", body.split("    volumes:\n", 1)[1], re.M)
        assert mounts == [f"layout-{layout}-ipc:/run/dayframe-site",
                          f"layout-{layout}-settings:/site-config:ro",
                          f"layout-{layout}-state:/site-state"]
        # Art Frames writes the settings where the layout reads them, and starts after the layout.
        assert f"- layout-{layout}-settings:/data/layout-settings/{layout}\n" in host
        assert f"layout-{layout}" in re.search(r"depends_on: \[(.*)\]", host).group(1)
    assert "website" not in re.search(r"depends_on: \[(.*)\]", host).group(1)


def test_the_host_finds_each_layout_socket_and_manifest():
    host, env = service("art-frames"), environment()
    first, others = LAYOUTS[0], LAYOUTS[1:]
    assert env["ART_FRAMES_LAYOUT_ID"] == env["ART_FRAMES_LAYOUT_CONFIG_ID"] == first
    assert env["ART_FRAMES_DEFAULT_SITE_SOCKET"] == "/run/dayframe-site/site.sock"
    assert env["ART_FRAMES_LAYOUT_MANIFEST"] == "/run/dayframe-site/layout.json"
    assert f"- layout-{first}-ipc:/run/dayframe-site:ro\n" in host
    assert env["ART_FRAMES_APPS"].split() == [f"{layout}={layout}" for layout in others]
    assert env["ART_FRAMES_APPS_SOCKET_DIR"] == env["ART_FRAMES_SITES_DIR"] + "/apps"
    for layout in others:
        assert f"- layout-{layout}-ipc:{env['ART_FRAMES_APPS_SOCKET_DIR']}/{layout}:ro\n" in host


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    """The host's view of the install file: each layout's socket folder holds its layout.json."""
    env = environment()

    def place(path):
        return tmp_path / path.lstrip("/")

    for key, value in env.items():
        monkeypatch.setenv(key, str(place(value)) if value.startswith("/run/") else value)
    folders = {LAYOUTS[0]: place("/run/dayframe-site")}
    folders.update({layout: place(env["ART_FRAMES_APPS_SOCKET_DIR"]) / layout for layout in LAYOUTS[1:]})
    for layout, folder in folders.items():
        folder.mkdir(parents=True)
        (folder / "layout.json").write_text(json.dumps({"id": layout, "name": layout.title(), "artwork": True}))
    return folders


def test_the_install_file_installs_the_three_layouts(mounted):
    found = apps.installed()
    assert [app.id for app in found] == LAYOUTS
    assert [app.scope for app in found] == LAYOUTS
    for app in found:
        assert app.socket == str(mounted[app.id] / "site.sock")
        assert app.manifest_path == mounted[app.id] / "layout.json"
