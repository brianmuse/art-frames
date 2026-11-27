"""The built-in Artwork: the day's artwork for one TV, full frame, composed in this process.

It needs no website app and no renderer. The picture is the one the Artwork page previews and an
artwork layout gets as `art.image`: the library's 3840×2160 framing of the piece (fit, zoom, focal point,
mat width and color), or the same framing without the mat on a TV set to never show mats. The library
rebuilds a missing framing from the original. Without
included artwork, or with daily artwork turned off, it shows a calm placeholder with one short line.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .ipc import JPEG_QUALITY
from .library import SIZE
from .site import art_day

# Change it when the picture for the same settings changes, so each TV gets the new one.
VERSION = 1
FONT = Path(__file__).parent / "fonts" / "Inter-Regular.ttf"
PLACEHOLDER_BACKGROUND, PLACEHOLDER_TEXT = "#ece8e0", "#6f6a60"
EMPTY = "Add artwork in Art Frames to show it here."
OFF = "Turn on daily artwork in Art Frames to show it here."


def todays(cfg, library, now):
    """The day's artwork row for this TV (a DisplayLibrary picks per TV), or None: none included, or turned off."""
    if not cfg.art.enabled:
        return None
    return library.choose(art_day(cfg, now), cfg.art.order)


def revision(cfg, library, now):
    """The picture's revision: it changes only with the day's piece, its framing, the TV's mats or the art switch."""
    art = todays(cfg, library, now)
    data = {"kind": "artwork", "version": VERSION, "enabled": cfg.art.enabled, "mats": cfg.mats,
            "art": art["id"] if art else None,
            "framing": library.state("art_revision:" + art["id"], 0) if art else None}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def compose(cfg, library, now):
    """The JPEG bytes the TV shows: 3840×2160."""
    art = todays(cfg, library, now)
    if art is None:
        return placeholder(OFF if not cfg.art.enabled else EMPTY)
    try:
        _, image, _ = library.presentation(art["id"], mat=cfg.mats != "never")
    except OSError:
        # The stored original is gone too: show the calm frame, not an error on every pass.
        return placeholder(EMPTY)
    return image


def placeholder(line):
    """A calm, plain frame with one short line in the middle."""
    canvas = Image.new("RGB", SIZE, PLACEHOLDER_BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(str(FONT), 64)
    except OSError:
        font = ImageFont.load_default()
    draw.text((SIZE[0] // 2, SIZE[1] // 2), line, fill=PLACEHOLDER_TEXT, font=font, anchor="mm")
    return _jpeg(canvas)


def _jpeg(image):
    output = io.BytesIO()
    image.save(output, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return output.getvalue()
