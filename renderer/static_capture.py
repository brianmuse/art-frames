"""One HTML/CSS-only render. No JavaScript, browser, network, or filesystem fetcher."""

import base64
import io
import re
import resource
import sys
from urllib.parse import urlsplit

import pypdfium2
from weasyprint import CSS, HTML
from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetcherResponse

from art_frames.ipc import JPEG_QUALITY, describe, encode
from renderer.pipe import read_pipe

SUMMARY = "dayframe-render-error: "


def fetch(url):
    if url.startswith("data:"):
        if len(url) > 34_000_000:
            raise FatalURLFetchingError("Inline image is too large")
        match = re.fullmatch(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)", url)
        if not match:
            raise FatalURLFetchingError("Only inline raster images are supported")
        body = base64.b64decode(match[2], validate=True)
        if len(body) > 25_000_000:
            raise FatalURLFetchingError("Inline image is too large")
        return match[1], body
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.netloc != "dayframe.local":
        raise FatalURLFetchingError("Only synthetic local assets are allowed")
    sys.stdout.buffer.write(encode({"type": "fetch", "url": url, "method": "GET"}))
    sys.stdout.buffer.flush()
    result = read_pipe(sys.stdin.buffer)
    if result.get("status") != 200:
        raise FatalURLFetchingError("Website asset is unavailable")
    body = base64.b64decode(result["body"], validate=True)
    if len(body) > 25_000_000:
        raise FatalURLFetchingError("Website asset is too large")
    return result["content_type"], body


class LocalAssets(URLFetcher):
    failed = False

    def fetch(self, url, headers=None):
        # Never fall back to the library's file/HTTP handlers, including for SVG resources.
        try:
            content_type, body = fetch(url)
            return URLFetcherResponse(url, body=body, headers={"Content-Type": content_type})
        except BaseException:
            # WeasyPrint's fatal URL error is a BaseException and SVG drawing may catch it.
            self.failed = True
            raise


def capture(job):
    content_type, body = fetch(job["url"])
    if not content_type.startswith("text/html"):
        raise ValueError("The entry page must be HTML")
    html = body.decode("utf-8")
    assets = LocalAssets()
    page = HTML(string=html, base_url=job["url"], url_fetcher=assets, media_type="screen")
    document = page.render(
        stylesheets=[CSS(string="@page { size: 3840px 2160px !important; margin: 0 !important; }")]
    )
    if assets.failed:
        raise ValueError("A website resource could not be loaded safely")
    if len(document.pages) != 1:
        return {"type": "error", "reason": "layout"}
    pdf = document.write_pdf()
    if assets.failed:
        raise ValueError("A nested website resource could not be loaded safely")
    if len(pdf) > 32_000_000:
        raise ValueError("Rendered document is too large")
    with pypdfium2.PdfDocument(pdf) as result:
        first = result[0]
        try:
            bitmap = first.render(scale=4 / 3)
            try:
                image = bitmap.to_pil().convert("RGB")
                if image.size != (3840, 2160):
                    raise ValueError("Incorrect image dimensions")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                return {"type": "image", "body": base64.b64encode(output.getvalue()).decode()}
            finally:
                bitmap.close()
        finally:
            first.close()


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    # RLIMIT_NPROC counts every thread of this UID on the host, including the running
    # Dayframe containers, so 64 starved the renderer (GLib then stops it with SIGTRAP).
    # The container's own process limit is the real guard; this only stops a runaway.
    resource.setrlimit(resource.RLIMIT_NPROC, (4096, 4096))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32_000_000,) * 2)
    try:
        response = capture(read_pipe(sys.stdin.buffer))
    except Exception as error:
        # One tagged line for the supervisor. Library messages may quote page markup or CSS.
        sys.stderr.write(f"{SUMMARY}{describe(error, __file__)}\n")
        response = {"type": "error", "reason": "static"}
    sys.stdout.buffer.write(encode(response))
    sys.stdout.buffer.flush()
