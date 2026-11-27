"""Frozen internal names. Do not change them.

The product is Art Frames, but these names keep the old word "dayframe". The NAS,
Docker, the TVs and the installed layouts depend on them, and a rollback can pair new
code with old images. tests/test_frozen_names.py checks that they stay the same in
the Python code, the compose files and the shell scripts.
"""

# Docker. A new project or service name would start a second controller next to the
# running one and leave rollback with no :previous images.
COMPOSE_PROJECT = "dayframe"
CONTROLLER_SERVICE = "dayframe"
IMAGES = ("dayframe", "dayframe-renderer", "dayframe-renderer-static", "dayframe-website")
APP_IMAGE_PREFIX = "dayframe-app-"

# The NAS folder. remote.py accepts only a path that ends in this name.
NAS_FOLDER = "dayframe"

# Marker files and their contents. A changed marker makes the deploy scripts stop.
SOURCE_MARKER_FILE = ".dayframe-managed"
SOURCE_MARKER = "dayframe-managed-source-v1"
LAYOUT_MARKER_FILE = ".dayframe-layout-managed"
LAYOUT_MARKER = "dayframe-layout-source-v1"
APPS_MARKER_FILE = ".dayframe-apps-managed"
APPS_MARKER = "dayframe-apps-source-v1"
SOURCE_FILE = ".dayframe-source.json"
INSTALLED_FILE = ".dayframe-installed.json"
# The scopes a layout had under earlier names of its source, one per line (install-layout.py).
EARLIER_SCOPES_FILE = ".earlier-scopes"

# The Samsung TV keys its list of allowed devices on this name. A new name makes each
# TV ask to pair again.
TV_CLIENT_NAME = "Dayframe"

# The protocol between the host and the installed layouts (the layouts repository).
SITE_HOST = "dayframe.local"
SITE_ORIGIN = "http://" + SITE_HOST
SNAPSHOT_PATH = "/dayframe.json"
ART_PATH = "/dayframe-art.jpg"
READY_ATTR = "data-dayframe-ready"
READY_SELECTOR = f"[{READY_ATTR}]"
RENDERER_SOCKET = "/run/dayframe-renderer/render.sock"
SITE_SOCKET_DIR = "/run/dayframe-site"
APPS_SOCKET_DIR = "/run/dayframe-apps"

# Environment names in .env, compose and the shell scripts. Python also reads ART_FRAMES_*.
ENV_PREFIX = "DAYFRAME_"

# The Basic auth username that saved passwords use.
LOGIN_USER = "dayframe"
