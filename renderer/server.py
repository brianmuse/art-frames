"""Networkless browser worker; only bounded local assets arrive over a Unix socket."""

import asyncio
import base64
import fcntl
import logging
import os
from pathlib import Path

from playwright.async_api import async_playwright

from art_frames import ipc
from art_frames.ipc import JPEG_QUALITY, receive_async, send_async

jobs = asyncio.Semaphore(1)
log = logging.getLogger("art_frames.renderer")


def describe(error):
    # Own ValueError messages are fixed text; browser errors may quote page URLs or selectors.
    return ipc.describe(error, __file__)


async def handle(reader, writer):
    async with jobs:
        try:
            async with asyncio.timeout(35):
                try:
                    job = await receive_async(reader)
                except asyncio.IncompleteReadError as error:
                    if not error.partial:
                        return  # health probe: connect and close
                    raise
                if not job.get("url", "").startswith("http://dayframe.local/"):
                    raise ValueError("Only local website jobs are supported")
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        chromium_sandbox=True,
                        args=[
                            "--disable-dev-shm-usage",
                            "--disable-background-networking",
                            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        ],
                    )
                    context = await browser.new_context(
                        viewport={"width": 3840, "height": 2160},
                        device_scale_factor=1,
                        service_workers="block",
                        accept_downloads=False,
                    )
                    lock = asyncio.Lock()

                    async def fulfill(route):
                        async with lock:
                            await send_async(
                                writer,
                                {"type": "fetch", "url": route.request.url, "method": route.request.method},
                            )
                            reply = await receive_async(reader)
                            await route.fulfill(
                                status=reply["status"],
                                content_type=reply["content_type"],
                                body=base64.b64decode(reply["body"], validate=True),
                            )

                    try:
                        await context.route("**/*", fulfill)
                        await context.route_web_socket("**/*", lambda channel: channel.close())
                        page = await context.new_page()
                        page.set_default_timeout(20_000)
                        response = await page.goto(job["url"], wait_until="domcontentloaded", timeout=25_000)
                        if response is None or response.status >= 400:
                            raise ValueError("Website entry could not be loaded")
                        if job.get("ready_selector"):
                            await page.locator(job["ready_selector"]).wait_for(state="attached")
                        else:
                            await page.wait_for_load_state("networkidle", timeout=20_000)
                        await page.evaluate("document.fonts.ready")
                        await page.locator("img").evaluate_all(
                            "images => Promise.all(images.map(image => image.decode().catch(() => {})))"
                        )
                        image = await page.screenshot(type="jpeg", quality=JPEG_QUALITY, animations="disabled")
                        await send_async(writer, {"type": "image", "body": base64.b64encode(image).decode()})
                    finally:
                        for close in (context.close, browser.close):
                            try:
                                await asyncio.wait_for(close(), 3)
                            except Exception:
                                pass
        except Exception as error:
            log.warning("Render job failed: %s", describe(error))
            try:
                await asyncio.wait_for(send_async(writer, {"type": "error"}), 1)
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except Exception:
                pass


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    os.umask(0o007)
    path = Path(os.environ.get("DAYFRAME_RENDERER_SOCKET", "/run/dayframe-renderer/render.sock"))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o770)
    with (path.parent / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            if not path.is_socket():
                raise ValueError("Refusing to replace a non-socket file")
            path.unlink()
        server = await asyncio.start_unix_server(handle, path=path, limit=40_000_000)
        path.chmod(0o660)
        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
