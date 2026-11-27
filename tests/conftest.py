import io

import pytest
from PIL import Image

from art_frames.config import ConfigStore
from art_frames.library import Library


@pytest.fixture(autouse=True)
def no_action_wait(monkeypatch):
    """Tests that hold the TV lock expect "busy" at once, not after the button wait."""
    monkeypatch.setattr("art_frames.controller.ACTION_WAIT", 0)
    # Tests run queued commands in the request thread, so a held lock must not block them for minutes.
    monkeypatch.setattr("art_frames.jobs.JOB_WAIT", 0)


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path)


@pytest.fixture
def configs(tmp_path):
    return ConfigStore(tmp_path)


@pytest.fixture
def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (600, 400), "#668877").save(stream, "PNG")
    return stream.getvalue()
