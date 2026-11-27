"""The product is Art Frames, but the NAS, Docker, the TVs and the apps keep the name dayframe.

These tests fail when someone renames a frozen name. Read art_frames/names.py first:
each name there has a reason. A change needs a migration for the NAS and a rollback plan.
"""

import re
from pathlib import Path

import pytest

from art_frames import names

ROOT = Path(__file__).resolve().parents[1]


def text(name):
    return (ROOT / name).read_text()


def test_names_keep_their_values():
    assert names.COMPOSE_PROJECT == "dayframe"
    assert names.CONTROLLER_SERVICE == "dayframe"
    assert names.IMAGES == ("dayframe", "dayframe-renderer", "dayframe-renderer-static", "dayframe-website")
    assert names.APP_IMAGE_PREFIX == "dayframe-app-"
    assert names.NAS_FOLDER == "dayframe"
    assert (names.SOURCE_MARKER_FILE, names.SOURCE_MARKER) == (".dayframe-managed", "dayframe-managed-source-v1")
    assert (names.LAYOUT_MARKER_FILE, names.LAYOUT_MARKER) == (".dayframe-layout-managed", "dayframe-layout-source-v1")
    assert (names.APPS_MARKER_FILE, names.APPS_MARKER) == (".dayframe-apps-managed", "dayframe-apps-source-v1")
    assert names.SOURCE_FILE == ".dayframe-source.json"
    assert names.INSTALLED_FILE == ".dayframe-installed.json"
    assert names.TV_CLIENT_NAME == "Dayframe"
    assert names.SITE_ORIGIN == "http://dayframe.local"
    assert (names.SNAPSHOT_PATH, names.ART_PATH) == ("/dayframe.json", "/dayframe-art.jpg")
    assert names.READY_SELECTOR == "[data-dayframe-ready]"
    assert names.RENDERER_SOCKET == "/run/dayframe-renderer/render.sock"
    assert (names.SITE_SOCKET_DIR, names.APPS_SOCKET_DIR) == ("/run/dayframe-site", "/run/dayframe-apps")
    assert names.ENV_PREFIX == "DAYFRAME_"
    assert names.LOGIN_USER == "dayframe"


def test_the_tv_client_name_comes_from_names():
    source = text("art_frames/television.py")
    assert source.count("name=TV_CLIENT_NAME") == 2
    assert 'name="' not in source.split("def remote", 1)[1].split("def current_id", 1)[0]


@pytest.mark.parametrize("name", ["compose.yaml", "compose.host.yaml", "compose.static.yaml", "compose.static.host.yaml"])
def test_compose_profiles_extend_the_dayframe_service(name):
    assert re.search(r"^  dayframe:$", text(name), re.M)
    assert "service: dayframe" in text(name)


def test_compose_base_keeps_service_images_and_mounts():
    base = text("compose.base.yaml")
    assert re.search(r"^  dayframe:$", base, re.M)
    for image in ("dayframe:local", "dayframe-renderer:local", "dayframe-renderer-static:local", "dayframe-website:local"):
        assert f"image: {image}" in base
    assert "/config/dayframe.toml" in base
    assert "/run/dayframe-renderer" in base and "/run/dayframe-site" in base
    assert "${DAYFRAME_UID" in base or "DAYFRAME_UID" in base


@pytest.mark.parametrize(
    "name",
    ["scripts/update-remote.sh", "scripts/rollback-remote.sh", "scripts/build-check.sh", "scripts/wait-healthy.sh"],
)
def test_deploy_scripts_use_the_dayframe_project(name):
    assert "compose -p dayframe " in text(name)
    assert not re.search(r"compose -p (?!dayframe )", text(name))


def test_rollback_finds_the_controller_by_its_old_labels():
    state = text("scripts/previous-state.sh")
    assert "label=com.docker.compose.project=dayframe" in state
    assert "label=com.docker.compose.service=dayframe" in state
    assert "ancestor=dayframe:local" in state and "ancestor=dayframe:previous" in state


def test_update_and_rollback_keep_the_image_names():
    update = text("scripts/update-remote.sh")
    assert 'previous="dayframe:previous $renderer:previous dayframe-website:previous"' in update
    assert 'pairs="dayframe:dayframe renderer:$renderer website:dayframe-website"' in update
    assert 'images="dayframe $renderer dayframe-website"' in text("scripts/rollback-remote.sh")
    assert 'image: \\"dayframe-app-$app_id:local\\"' in text("scripts/app-compose.sh")


@pytest.mark.parametrize(
    "name",
    ["scripts/prepare-remote.sh", "scripts/enable-tunnel.sh", "scripts/remote.py", "scripts/install-layout.py"],
)
def test_scripts_check_the_source_marker(name):
    assert ".dayframe-managed" in text(name)


def test_shell_markers_match_names():
    assert "= dayframe-managed-source-v1" in text("scripts/prepare-remote.sh")
    assert "= dayframe-apps-source-v1" in text("scripts/app-compose.sh")
    assert "= dayframe-layout-source-v1" in text("scripts/app-compose.sh")
    state = text("scripts/previous-state.sh")
    assert "text=dayframe-layout-source-v1" in state and "text=dayframe-apps-source-v1" in state


def test_env_files_keep_the_dayframe_prefix():
    assert "DAYFRAME_UID=10001" in text("scripts/prepare-remote.sh")
    assert "DAYFRAME_GID=10001" in text("scripts/prepare-remote.sh")
    assert not re.search(r"\bART_FRAMES_", text("compose.base.yaml"))
    for script in (ROOT / "scripts").glob("*.sh"):
        assert not re.search(r"\bART_FRAMES_", script.read_text()), script.name


def test_nas_path_and_login_user_still_work():
    remote = text("scripts/remote.py")
    assert "/dayframe" in remote
    assert "names.LOGIN_USER" in text("art_frames/web.py")


def test_app_protocol_names_in_renderers_and_site():
    assert '"http://dayframe.local/"' in text("renderer/server.py")
    assert 'parts.netloc != "dayframe.local"' in text("renderer/static_capture.py")
    for name in ("renderer/server.py", "renderer/static_server.py"):
        assert '"/run/dayframe-renderer/render.sock"' in text(name)
    assert "/run/dayframe-site" in text("Dockerfile.website")
