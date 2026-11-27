#!/bin/sh
# Test built images with synthetic content, no TV, and no host-data mounts.
set -eu
cd "$(dirname "$0")/.."
app_image=${1:-dayframe:test}
renderer_image=${2:-dayframe-renderer:test}
mode=${3:-browser}
website_image=${4:-dayframe-website:test}
# Callers run scripts/check-host.sh before building.
check_id="dayframe-check-$(date +%s)-$$"
worker="$check_id-renderer"
volume="$check_id-ipc"
website="$check_id-website"
site_volume="$check_id-site"
cleanup() {
    status=$?
    if test "$status" -ne 0; then
        # Only the renderer's tagged one-line summaries, never page content.
        echo "Renderer errors (last 5):"
        docker logs "$worker" 2>&1 | grep -F "dayframe-render-error: " | tail -n 5 || true
        docker logs "$worker" 2>&1 | grep -F "Render" | tail -n 5 || true
        # Only the website container's state, never its output: did it stop, and why.
        docker inspect --type container -f 'Website container: {{.State.Status}}, exit code {{.State.ExitCode}}, out of memory {{.State.OOMKilled}}' \
            "$website" 2>/dev/null || true
    fi
    docker rm -f "$worker" "$website" >/dev/null 2>&1 || true
    docker volume rm "$volume" "$site_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
docker volume create "$volume" >/dev/null
if test "$mode" = static; then
    docker volume create "$site_volume" >/dev/null
    docker run -d --name "$website" --init --user 10001:10001 \
        --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
        --memory 256m --tmpfs /tmp:rw,noexec,nosuid,size=32m \
        --tmpfs /site-state:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=700 \
        --mount "type=volume,src=$site_volume,dst=/run/dayframe-site" \
        "$website_image" >/dev/null
fi
set -- -d --name "$worker" --init --user 10001:10001 \
    --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,size=256m \
    --mount "type=volume,src=$volume,dst=/run/dayframe-renderer"
if test "$mode" = browser; then
    set -- "$@" --security-opt seccomp=renderer/seccomp_profile.json --pids-limit 256 --memory 1024m --shm-size 256m
else
    set -- "$@" --memory 512m
fi
docker run "$@" "$renderer_image" >/dev/null
set -- --rm -i --init --user 10001:10001 --network none \
    --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 768m --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    --mount "type=volume,src=$volume,dst=/run/dayframe-renderer,readonly" \
    --entrypoint python
if test "$mode" = browser; then
    set -- "$@" --pids-limit 64
else
    set -- "$@" -e DAYFRAME_DEFAULT_SITE_SOCKET=/run/dayframe-site/site.sock \
        --mount "type=volume,src=$site_volume,dst=/run/dayframe-site,readonly"
fi
docker run "$@" "$app_image" - <<'PY'
import io
import base64
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from art_frames.config import Config
from art_frames.displays import DisplayLibrary
from art_frames.library import Library
from art_frames.site import capture
from art_frames.site_service import request

endpoint = Path("/run/dayframe-renderer/render.sock")
deadline = time.monotonic() + 30
while not endpoint.is_socket():
    if time.monotonic() >= deadline:
        raise RuntimeError("Container renderer socket did not become ready")
    time.sleep(0.1)
site = os.environ.get("DAYFRAME_DEFAULT_SITE_SOCKET")
while site and not Path(site).is_socket():
    if time.monotonic() >= deadline:
        raise RuntimeError("Default website socket did not become ready")
    time.sleep(0.1)
with tempfile.TemporaryDirectory(prefix="dayframe-check-") as directory:
    cfg = Config()
    cfg.site.data = {"headline": "Server-side proof <em>escaped</em>"}
    now = datetime(2030, 1, 2, 8, tzinfo=timezone.utc)
    library = Library(Path(directory))
    if site:
        _, html = request(site, "/index.html", {"now": now.isoformat(), "timezone": "UTC", "data": cfg.site.data})
        assert b"<html" in html.lower()
    image = capture(cfg, library, now)
    with Image.open(io.BytesIO(image)) as result:
        assert result.format == "JPEG", result.format
        assert result.size == (3840, 2160), result.size
        result.load()
    print("Container capture passed: bundled Astro site → 3840×2160 JPEG")
    if site:
        cfg.site.providers = ["art"]
        artwork = io.BytesIO()
        Image.new("RGB", (400, 225), (240, 70, 20)).save(artwork, "PNG")
        piece = library.add(artwork.getvalue(), title="Synthetic artwork check")
        library.update(piece, selected=True, fit="crop", x=.5, y=.5, mat_width=.1, mat_color="#e9e2d7")
        with Image.open(io.BytesIO(capture(cfg, library, now))) as result:
            assert result.size == (3840, 2160)
        print("Installed website with optional artwork context → 3840×2160 JPEG")
        Image.new("RGB", (400, 225), (20, 90, 160)).save(artwork := io.BytesIO(), "PNG")
        library.add(artwork.getvalue(), title="Second TV artwork check")
        second = DisplayLibrary(library, "a" * 32)
        first_image = capture(cfg, library, now, purpose="preview")
        second_image = capture(cfg, second, now, purpose="preview")
        assert first_image != second_image
        for image in (first_image, second_image):
            with Image.open(io.BytesIO(image)) as result:
                assert result.format == "JPEG" and result.size == (3840, 2160)
                result.load()
        print("Two TVs share the installed layout with distinct artwork previews → 3840×2160 JPEG")
        custom = Path(directory) / "sites" / "test"
        custom.mkdir(parents=True)
        os.environ["DAYFRAME_SITES_DIR"] = str(custom.parent)
        cfg.site.path = "test"
        cfg.site.entry = "index.html"
        cfg.site.providers = []
        for url in ("file:///etc/passwd", "http://169.254.169.254/private", "https://example.invalid/private"):
            (custom / "index.html").write_text(f'<html><body><img src="{url}"></body></html>')
            try:
                capture(cfg, library, now)
            except ValueError:
                pass
            else:
                raise AssertionError("Unsafe resource URL was not rejected")
        (custom / "nested.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><image href="file:///etc/passwd" width="100" height="100"/></svg>')
        for markup in ('<img src="nested.svg">', '<style>@import url("file:///etc/passwd");</style>'):
            (custom / "index.html").write_text('<html><body>' + markup + '</body></html>')
            try:
                capture(cfg, library, now)
            except ValueError:
                pass
            else:
                raise AssertionError("Unsafe nested SVG/CSS resource URL was not rejected")
        print("Filesystem and external resource URLs rejected")
        inline = base64.b64encode(artwork.getvalue()).decode()
        (custom / "index.html").write_text('<html><body><img src="data:image/png;base64,' + inline + '"></body></html>')
        with Image.open(io.BytesIO(capture(cfg, library, now))) as result:
            assert result.size == (3840, 2160)
        print("Bounded inline raster images render without external access")
PY
