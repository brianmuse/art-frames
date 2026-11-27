"""Content-agnostic static-site assets and opt-in data for bundled examples."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import secrets
import socket
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image

from . import apps
from .config import private_write
from .env import env
from .ipc import JPEG_QUALITY, encode, receive
from .layouts import display_values
from .names import ART_PATH, RENDERER_SOCKET, SITE_HOST, SITE_ORIGIN, SNAPSHOT_PATH
from .site_service import WebsitePending
from .site_service import request as service_request

log = logging.getLogger(__name__)

ORIGIN = SITE_ORIGIN
BUNDLED = Path(__file__).parent / "site"
MAX_ASSET = 25_000_000
TREE_SECONDS = 60  # one scan serves every TV in a scheduler pass
_trees = {}


def art_day(cfg, now):
    return (
        now.date() if now.strftime("%H:%M") >= cfg.art.change_at else now.date() - timedelta(days=1)
    ).isoformat()


def site_root(cfg):
    if cfg.site.path:
        mount = Path(env("SITES_DIR", "sites")).resolve()
        result = (mount / cfg.site.path).resolve()
        if not result.is_relative_to(mount):
            raise ValueError("Website build folder must remain inside the sites mount")
        return result
    local_build = Path(__file__).parents[1] / "website" / "dist"
    return (BUNDLED if BUNDLED.exists() else local_build).resolve()


def asset_path(root: Path, relative: str):
    decoded = unquote(relative).lstrip("/")
    if any(part.startswith(".") for part in Path(decoded).parts):
        raise ValueError("Hidden site files cannot be rendered")
    path = (root / decoded).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Site asset is outside the configured build output")
    if path.stat().st_size > MAX_ASSET:
        raise ValueError("Site asset exceeds the size limit")
    return path


def tree_digest(root):
    """Site file metadata, rescanned at most every TREE_SECONDS per build root."""
    cached = _trees.get(root)
    if cached and time.monotonic() - cached[0] < TREE_SECONDS:
        return cached[1]
    digest = hashlib.sha256()
    # Metadata only. Never read or follow a symlink outside the opted-in site folder.
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            continue
        stat = path.stat()
        digest.update(f"{path.relative_to(root)}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    _trees[root] = (time.monotonic(), digest.hexdigest())
    return _trees[root][1]


def display_settings(cfg, library, app=None):
    """This TV's values for an installed layout's display settings, or None without a layout (an apps.App)."""
    if cfg.site.path or app is None:
        return None
    return display_values(app.manifest, cfg.app_settings.get(app.scope, {}))


def revision(cfg, library, now, app=None):
    if app is not None and app.builtin:
        from . import artwork  # artwork.py imports this module

        return artwork.revision(cfg, library, now)
    # The config dump includes this TV's mats and stored layout values; the resolved
    # values below also cover a changed manifest default.
    # Leave out the other TVs so a change on one TV does not re-render the rest,
    # the layout schedule (the layout itself is part of the revision) and the sensor, which never changes the picture.
    digest = hashlib.sha256(cfg.model_dump_json(exclude={"displays", "app_schedule", "sensor"}).encode())
    digest.update(json.dumps(display_settings(cfg, library, app), sort_keys=True).encode())
    digest.update(tree_digest(site_root(cfg)).encode())
    if "art" in cfg.site.providers and cfg.art.enabled:
        art = library.choose(art_day(cfg, now), cfg.art.order)
        if art:
            digest.update(art["id"].encode())
            digest.update(str(library.state("art_revision:" + art["id"], 0)).encode())
    digest.update(str(int(now.timestamp()) // (cfg.site.refresh_minutes * 60)).encode())
    if app is not None:
        digest.update(("app:" + app.id).encode())
        settings = library.root / "layout-settings" / app.scope / "config.json"
        if settings.exists():
            digest.update(str(settings.stat().st_mtime_ns).encode())
    return digest.hexdigest()


def display_id(library, display, app):
    """A stable, opaque ID for one TV as one layout sees it.

    It is keyed per installation and differs per layout scope, so two layouts cannot match their
    IDs to find out they run on the same TV.
    """
    path = library.root / "display-id.key"
    if not path.exists():
        # Write the key in full beside it, then link it into place: the link fails when another
        # writer was first, so every writer reads the same, complete key.
        draft = path.with_name(f".display-id-{secrets.token_hex(8)}")
        private_write(draft, secrets.token_bytes(32))
        try:
            os.link(draft, path)
        except FileExistsError:
            pass
        except OSError:
            # No hard links on this filesystem (exFAT, some SMB, FUSE or Docker Desktop mounts):
            # move the draft into place, unless another writer was first.
            if not path.exists():
                os.replace(draft, path)
        finally:
            draft.unlink(missing_ok=True)
    return hmac.new(path.read_bytes(), f"{display}:{app.scope}".encode(), hashlib.sha256).hexdigest()[:16]


def snapshot(cfg, library, now, *, demo=False, app=None, purpose="scheduled", display="main"):
    """The page's context. `purpose` says why the layout renders: "scheduled", "prepare" (before a
    scheduled wake), "rotation" (a layout that is not on screen) or "preview" (a management
    preview; the layout must not move its own state forward). `display` is the TV's display config
    ID; layouts see only display_id() of it."""
    result = {
        "version": 1,
        "now": now.isoformat(),
        "timezone": cfg.timezone,
        "display": {"width": 3840, "height": 2160},
        "data": cfg.site.data,
        "render_purpose": purpose,
        "refresh_minutes": cfg.site.refresh_minutes,
    }
    if app is not None:
        result["display"]["id"] = display_id(library, display, app)
    values = display_settings(cfg, library, app)
    if values is not None:
        result["display_settings"] = values
    extra = {}
    if "art" in cfg.site.providers and cfg.art.enabled:
        art = library.choose(art_day(cfg, now), cfg.art.order)
        if art:
            art, image, viewport = library.presentation(art["id"], mat=cfg.mats != "never")
            result["art"] = {key: art[key] for key in ("title", "artist", "year", "rights", "source_url")}
            result["art"]["image"] = ART_PATH
            if viewport:
                result["art"]["viewport"] = viewport
            extra[ART_PATH] = ("image/jpeg", image)
    extra[SNAPSHOT_PATH] = ("application/json", json.dumps(result).encode())
    return extra


def frame_capture(image, extras):
    """Keep website pixels inside the artwork opening; the host owns the mat."""
    context = json.loads(extras[SNAPSHOT_PATH][1])
    viewport = context.get("art", {}).get("viewport")
    if not viewport:
        return image
    x, y, width, height = (viewport[key] for key in ("x", "y", "width", "height"))
    with Image.open(io.BytesIO(extras[ART_PATH][1])) as artwork, Image.open(io.BytesIO(image)) as rendered:
        if artwork.size != (3840, 2160) or rendered.size != artwork.size:
            raise ValueError("Framed website must render at 3840×2160")
        canvas = artwork.convert("RGB")
        canvas.paste(rendered.crop((x, y, x+width, y+height)), (x, y))
        output = io.BytesIO()
        canvas.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return output.getvalue()


def resource(root, url, extras, service=None):
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.netloc != SITE_HOST:
        raise ValueError("Rendered sites may load only their local assets and explicit Art Frames data")
    if parts.path in extras:
        return extras[parts.path]
    if service:
        return service_request(service, parts.path + ("?" + parts.query if parts.query else ""))
    path = asset_path(root, parts.path)
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.read_bytes()


def capture(cfg, library, now, *, demo=False, app=None, purpose="scheduled", display="main"):
    """The browser has no NAS mounts or outbound network. Supply local assets over IPC.

    `app` (an apps.App) picks the installed layout's socket. Without an installed layout, the
    starter website's socket serves the default site, if it runs.
    """
    root = site_root(cfg)
    if cfg.site.path:
        service = None
    else:
        service = app.socket if app is not None else apps.starter_socket()
    if not service:
        asset_path(root, cfg.site.entry)
    extras = snapshot(cfg, library, now, demo=demo, app=app, purpose=purpose, display=display)
    if service:
        context = json.loads(extras[SNAPSHOT_PATH][1])
        if context.get("art"):
            context["art"]["image_data"] = base64.b64encode(extras[ART_PATH][1]).decode("ascii")
        extras["/" + cfg.site.entry] = service_request(service, "/" + cfg.site.entry, context)
    endpoint = env("RENDERER_SOCKET", RENDERER_SOCKET)
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    channel.settimeout(35)
    budget, deadline = 64_000_000, time.monotonic() + 35
    try:
        channel.connect(endpoint)
        channel.sendall(
            encode({"url": ORIGIN + "/" + cfg.site.entry, "ready_selector": cfg.site.ready_selector})
        )
        for _ in range(256):
            if time.monotonic() > deadline:
                raise ValueError("Website rendering exceeded its time limit")
            message = receive(channel, deadline)
            if message.get("type") == "fetch":
                try:
                    if message.get("method") != "GET":
                        raise ValueError("Only read-only site requests are supported")
                    content_type, body = resource(root, message["url"], extras, service)
                    budget -= len(body)
                    if budget < 0:
                        raise ValueError("Website exceeded the render asset budget")
                    reply = {
                        "status": 200,
                        "content_type": content_type,
                        "body": base64.b64encode(body).decode(),
                    }
                except WebsitePending:
                    raise
                except (ValueError, OSError):
                    reply = {"status": 403, "content_type": "text/plain", "body": ""}
                channel.sendall(encode(reply))
            elif message.get("type") == "image":
                image = base64.b64decode(message["body"], validate=True)
                if len(image) > MAX_ASSET:
                    raise ValueError("Rendered image exceeds the size limit")
                return frame_capture(image, extras)
            else:
                if message.get("reason") == "layout":
                    raise ValueError("This page must fit on one 3840×2160 screen for HTML/CSS rendering")
                if message.get("reason") == "static":
                    raise ValueError(
                        "HTML/CSS rendering failed; check local assets and the supported site format"
                    )
                raise ValueError("Website could not be rendered; check its entry and readiness selector")
        raise ValueError("Website made too many asset requests")
    finally:
        channel.close()
