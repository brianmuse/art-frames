"""Opt-in browser workflow with a simulated TV and no external network calls."""

import hashlib
import io
import os
import threading

import pytest
from PIL import Image

from art_frames.config import Config, private_write
from art_frames.web import create_app


@pytest.mark.skipif(os.environ.get("DAYFRAME_TEST_RENDER") != "1", reason="Opt-in browser UI check")
def test_on_tv_and_preview_stay_distinct_through_refresh_send_and_stale_read(tmp_path):
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import WSGIRequestHandler, make_server

    def jpeg(color):
        output = io.BytesIO()
        Image.new("RGB", (1600, 900), color).save(output, "JPEG")
        return output.getvalue()

    old, preview, external = jpeg("red"), jpeg("blue"), jpeg("green")
    app = create_app(tmp_path, demo=True, testing=True)
    configs, controller = app.extensions["configs"], app.extensions["controller"]
    configs.save(Config(tv={"ip": "192.168.1.50"}, tv_name="Living Room"))
    class TV:
        current = "old"
        image = old
        def status(self): return {"paired": True, "known": True, "awake": True, "playing": False}
        def publish(self, image, owned, record, keep=()):
            record("new")
            self.current, self.image = "new", image
            return "new", ["new"], True
        def read_artwork(self, *args): return self.current, self.image, "thumbnail"
    tv = TV()
    controller.tv_factory = lambda cfg: tv
    controller.capture = lambda *a, **kw: preview
    # The saved preview starts as the old picture. "Make a preview" renders the new one.
    private_write(tmp_path / "screen-preview.jpg", old)
    class Quiet(WSGIRequestHandler):
        def log_request(self, *args, **kwargs): pass
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(chromium_sandbox=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            base = f"http://127.0.0.1:{server.server_port}"
            on_tv = page.locator("[data-tv-artwork-stage] img")
            dialog = page.locator("#preview-dialog")

            def open_preview():
                page.locator(".more-menu summary").click()
                page.get_by_role("link", name="Preview first…", exact=True).click()
                assert dialog.is_visible()

            # Home (/) lists every TV. The TV's Now page shows the picture Art Frames sent to it.
            page.goto(f"{base}/tv/main")
            assert on_tv.count() == 0
            open_preview()
            dialog.get_by_role("button", name="Show on Living Room", exact=True).click()
            page.get_by_text("The TV confirmed this artwork is selected.", exact=True).wait_for()
            assert (tmp_path / "tv-upload.jpg").read_bytes() == old
            original_src = on_tv.get_attribute("src")
            assert hashlib.sha256(old).hexdigest() in original_src

            # A new preview stays in the dialog. The picture on the TV does not change until it is sent.
            open_preview()
            dialog.get_by_role("button", name="Make a preview", exact=True).click()
            dialog.get_by_text("Preview updated.", exact=True).wait_for()
            assert on_tv.get_attribute("src") == original_src
            expected = hashlib.sha256(preview).hexdigest()
            assert dialog.locator("[data-preview-revision]").input_value() == expected
            dialog.get_by_role("button", name="Show on Living Room", exact=True).click()
            page.get_by_text("The TV confirmed this artwork is selected.", exact=True).wait_for()
            assert expected in on_tv.get_attribute("src")
            assert (tmp_path / "tv-upload.jpg").read_bytes() == preview

            # Someone changes the picture with the remote. Check TV reads it, and the page shows both pictures.
            tv.current, tv.image = "external", external
            page.locator(".more-menu summary").click()
            page.get_by_role("button", name="Check TV", exact=True).click()
            page.get_by_text("TV artwork checked.", exact=True).wait_for()
            assert (tmp_path / "tv-artwork.jpg").read_bytes() == external
            assert expected in on_tv.get_attribute("src")
            assert page.get_by_alt_text("What Living Room showed at the last check", exact=True).is_visible()
            page.screenshot(path=tmp_path / "on-tv.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=tmp_path / "on-tv-mobile.png", full_page=True)

            # A stale read of the sent picture shows a reload link, not a broken image.
            page.route("**/screen/sent.jpg?*", lambda route: route.fulfill(status=409))
            page.reload()
            page.get_by_text("This picture changed or could not load.", exact=True).wait_for()
            assert page.get_by_role("link", name="Reload", exact=True).is_visible()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
