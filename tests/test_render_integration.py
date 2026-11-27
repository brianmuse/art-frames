"""Opt-in real browser check. Uses generated page data, a private socket, and no TV."""

import asyncio
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from art_frames.config import Config
from art_frames.site import capture


@pytest.mark.skipif(os.environ.get("DAYFRAME_TEST_RENDER") != "1", reason="Opt-in real browser UI check")
def test_tv_discovery_selects_without_saving_or_pairing(tmp_path):
    import json
    from urllib.parse import parse_qs, urlsplit

    from playwright.async_api import async_playwright

    from art_frames.web import create_app

    async def check():
        app = create_app(tmp_path, demo=True, testing=True)
        client = app.test_client()
        requests = []
        ready = asyncio.Event()
        name = '<img src=x onerror="alert(1)">'
        responses = [
            (
                200,
                {
                    "devices": [
                        {
                            "ip": "192.168.1.2",
                            "name": name,
                            "model": "QN65LS03D",
                            "frame": True,
                            "mac": "AA:BB:CC:DD:EE:FF",
                        }
                    ]
                },
            ),
            (
                200,
                {
                    "devices": [
                        {"ip": "192.168.1.3", "name": "Second TV", "model": "", "frame": False, "mac": ""}
                    ]
                },
            ),
            (429, {"error": "Please wait 30 seconds between TV searches."}),
            (200, {"devices": []}),
        ]

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(chromium_sandbox=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})

            async def serve(route):
                path = urlsplit(route.request.url).path
                if path == "/tvs/discover":
                    requests.append(parse_qs(route.request.post_data))
                    await ready.wait()
                    status, body = responses.pop(0)
                    await route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
                else:
                    assert route.request.method == "GET", "Discovery unexpectedly saved settings or paired"
                    response = client.get(path)
                    await route.fulfill(
                        status=response.status_code, headers=dict(response.headers), body=response.get_data()
                    )

            await page.route("**/*", serve)
            # Load the TV settings page itself. The older /settings URL answers with a redirect, and
            # Playwright does not route the request that follows a fulfilled redirect, so the browser
            # would try the real network.
            await page.goto("http://127.0.0.1/tv/main/settings")
            # The MAC, the wake broadcast and the search network sit in folded rows of the Address card.
            await page.get_by_text("MAC and wake", exact=True).click()
            await page.get_by_text("Search a different network", exact=True).click()
            await page.locator('[name="broadcast"]').fill("192.168.1.255")
            await page.locator('[name="tv_mac"]').fill("11:22:33:44:55:66")
            await page.locator('[name="discovery_network"]').fill("192.168.1.0/24")
            find = page.get_by_role("button", name="Find TVs", exact=True)
            await page.locator('[name="discovery_network"]').press("Enter")
            assert await find.is_disabled()
            ready.set()
            choices = page.locator("[data-tv-results] button")
            await choices.first.wait_for()
            assert name in await choices.first.text_content()
            assert await page.locator("[data-tv-results] img").count() == 0
            await choices.first.click()
            assert await page.locator('[name="tv_ip"]').input_value() == "192.168.1.2"
            assert await page.locator('[name="tv_mac"]').input_value() == "AA:BB:CC:DD:EE:FF"
            # Choosing a TV fills only its address and MAC. Other unsaved fields keep their values.
            assert await page.locator('[name="broadcast"]').input_value() == "192.168.1.255"
            await find.click()
            await page.get_by_role("button", name="Second TV", exact=False).click()
            assert await page.locator('[name="tv_mac"]').input_value() == ""
            await find.click()
            await page.get_by_text("Please wait 30 seconds between TV searches.", exact=True).wait_for()
            assert not await find.is_disabled()
            await find.click()
            await page.get_by_text("No Samsung TVs found.", exact=False).wait_for()
            assert all(item["csrf"] and item["network"] == ["192.168.1.0/24"] for item in requests)
            assert not app.extensions["configs"].load().tv.ip
            await page.screenshot(path=tmp_path / "tv-discovery-ui.png", full_page=True)
            await browser.close()

    asyncio.run(check())


@pytest.mark.skipif(os.environ.get("DAYFRAME_TEST_RENDER") != "1", reason="Opt-in real Chromium capture")
def test_astro_site_produces_4k_image(tmp_path, library, monkeypatch):
    # Unix socket names have a short platform limit; pytest's macOS paths can exceed it.
    endpoint = Path(tempfile.mkdtemp(prefix="dayframe-render-", dir="/tmp")) / "render.sock"
    monkeypatch.setenv("DAYFRAME_RENDERER_SOCKET", str(endpoint))
    root = Path(__file__).parents[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "renderer.server"],
        cwd=root,
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        while not endpoint.exists():
            if process.poll() is not None:
                pytest.fail("Local renderer could not start: " + process.stderr.read().decode())
            if time.monotonic() > deadline:
                pytest.fail("Local renderer socket was not ready")
            time.sleep(0.05)
        image = capture(Config(), library, datetime.now(ZoneInfo("UTC")))
        with Image.open(io.BytesIO(image)) as result:
            assert result.size == (3840, 2160)
            assert result.format == "JPEG"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stderr.close()
        shutil.rmtree(endpoint.parent)
