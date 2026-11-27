from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise

import pytest
from PIL import Image


def add_pieces(library, image_bytes):
    return [library.add(image_bytes, title=f"Piece {i}") for i in range(3)]


def test_rotation_has_no_repeats_until_collection_cycles(library, image_bytes):
    ids = add_pieces(library, image_bytes)
    chosen = [library.choose(f"2026-10-{day}")["id"] for day in range(20, 26)]
    assert set(chosen[:3]) == set(ids)
    assert set(chosen[3:]) == set(ids)
    assert all(left != right for left, right in pairwise(chosen))


def test_daily_selection_survives_restart_and_concurrent_reads(library, image_bytes):
    from art_frames.library import Library

    add_pieces(library, image_bytes)
    with ThreadPoolExecutor(max_workers=4) as pool:
        picked = list(pool.map(lambda _: library.choose("2026-10-25")["id"], range(8)))
    assert len(set(picked)) == 1
    assert Library(library.root).choose("2026-10-25")["id"] == picked[0]


def test_deselected_art_is_not_rotated_and_empty_library_is_ok(library, image_bytes):
    assert library.choose("2026-10-25") is None
    piece = library.add(image_bytes, title="Only piece")
    assert library.choose("2026-10-25")["id"] == piece
    library.update(piece, selected=False, fit="contain", x=0.5, y=0.5)
    assert library.choose("2026-10-25") is None


def test_import_deduplication_and_normalized_image(library, image_bytes):
    one = library.add(image_bytes, title="Test", source="met", source_id="123")
    two = library.add(image_bytes, title="Test again", source="met", source_id="123")
    assert one == two
    assert len(library.all()) == 1
    with Image.open(library.path_for(one)) as image:
        assert image.size == (3840, 2160)
        assert image.format == "JPEG"
        assert not image.getexif()


@pytest.mark.parametrize("value", ["../settings", "../../etc/passwd", "not-a-uuid"])
def test_artwork_paths_cannot_escape_library(library, value):
    with pytest.raises(ValueError):
        library.path_for(value)


def test_fit_validation_preserves_original(library, image_bytes):
    piece = library.add(image_bytes, title="Test")
    original = library.path_for(piece, "original").read_bytes()
    with pytest.raises(ValueError):
        library.update(piece, selected=True, fit="crop", x=-1, y=0.5)
    library.update(piece, selected=True, fit="crop", x=0.2, y=0.8)
    assert library.path_for(piece, "original").read_bytes() == original


def test_existing_catalog_adds_optional_mats_without_changing_images_or_rotation(library, image_bytes):
    from art_frames.library import Library
    piece = library.add(image_bytes, title="Existing artwork")
    library.choose("2026-10-26")
    display = library.path_for(piece).read_bytes()
    with library.connection() as db:
        db.execute("ALTER TABLE artwork DROP COLUMN mat_width")
        db.execute("ALTER TABLE artwork DROP COLUMN mat_color")
    reopened = Library(library.root)
    assert reopened.get(piece)["mat_width"] == 0
    assert reopened.get(piece)["mat_color"] == "#e9e2d7"
    assert reopened.path_for(piece).read_bytes() == display
    assert reopened.choose("2026-10-26")["id"] == piece


def test_capture_waits_for_autosave_image_and_opening_to_match(library, image_bytes, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    piece = library.add(image_bytes, title="Concurrent framing")
    written, release, started = threading.Event(), threading.Event(), threading.Event()
    render = library._render
    def slow_render(*args):
        render(*args)
        written.set()
        assert release.wait(5)
    monkeypatch.setattr(library, "_render", slow_render)
    def read_presentation():
        started.set()
        return library.presentation(piece)
    with ThreadPoolExecutor(max_workers=2) as pool:
        save = pool.submit(library.update, piece, selected=True, fit="crop", x=.5, y=.5, mat_width=.04, mat_color="#e9e2d7")
        assert written.wait(5)
        capture = pool.submit(read_presentation)
        try:
            assert started.wait(5)
            with pytest.raises(TimeoutError):
                capture.result(timeout=.1)
        finally:
            release.set()
        save.result()
        art, image, viewport = capture.result()
    assert art["mat_width"] == .04 and viewport["x"] == round(2160*.04)
    assert image == library.path_for(piece).read_bytes()


@pytest.mark.parametrize("fit", ["contain", "crop"])
@pytest.mark.parametrize("mat_width", [.05, .1])
def test_mat_color_and_width_survive_zoom_without_covering_border(library, fit, mat_width):
    import io
    source = io.BytesIO()
    Image.new("RGB", (1600, 900), "blue").save(source, "PNG")
    piece = library.add(source.getvalue(), title="Blue painting")
    original = library.path_for(piece, "original").read_bytes()
    library.update(piece, selected=True, fit=fit, x=.2, y=.8, zoom=1.3, mat_width=mat_width, mat_color="#D8B48C")
    with Image.open(library.path_for(piece)) as display:
        assert display.size == (3840, 2160)
        for pixel in [(50,50),(50,1080),(3790,1080),(1920,50),(1920,2110)]:
            assert max(abs(a-b) for a,b in zip(display.getpixel(pixel),(216,180,140), strict=True)) < 5
        assert display.getpixel((1920,1080))[2] > 240
    assert library.path_for(piece, "original").read_bytes() == original
    assert library.get(piece)["mat_color"] == "#d8b48c"
    library.update(piece, selected=True, fit=fit, x=.5, y=.5)
    assert library.get(piece)["mat_width"] == mat_width


@pytest.mark.parametrize("width,color", [(-.01,"#ffffff"),(.101,"#ffffff"),(float('nan'),"#ffffff"),(.02,"#fff"),(.02,"red"),(.02,"#xyzxyz")])
def test_invalid_mat_preserves_existing_artwork(library, image_bytes, width, color):
    piece = library.add(image_bytes, title="Unchanged")
    before = library.path_for(piece).read_bytes()
    with pytest.raises(ValueError):
        library.update(piece, selected=True, fit="crop", x=.5, y=.5, mat_width=width, mat_color=color)
    assert library.path_for(piece).read_bytes() == before
    assert library.get(piece)["mat_width"] == 0


def test_existing_catalog_adds_zoom_without_changing_artwork_or_selection(library, image_bytes):
    from art_frames.library import Library

    art_id = library.add(image_bytes, title="Existing")
    library.choose("2026-10-26")
    display = library.path_for(art_id).read_bytes()
    with library.connection() as db:
        db.execute("ALTER TABLE artwork DROP COLUMN zoom")
    reopened = Library(library.root)
    assert reopened.get(art_id)["zoom"] == 1.0
    assert reopened.choose("2026-10-26")["id"] == art_id
    assert reopened.path_for(art_id).read_bytes() == display
    assert Library(library.root).get(art_id)["zoom"] == 1.0


def test_zoom_trims_edges_keeps_focal_point_and_preserves_original(library):
    import io

    from PIL import ImageDraw

    image = Image.new("RGB", (1024, 576), "blue")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1023, 575), outline="red", width=50)
    source = io.BytesIO()
    image.save(source, "PNG")
    art_id = library.add(source.getvalue(), title="Border test")
    original = library.path_for(art_id, "original").read_bytes()
    library.update(art_id, selected=True, fit="crop", x=0.5, y=0.5, zoom=1.3)
    with Image.open(library.path_for(art_id)) as display:
        red, _, blue = display.getpixel((20, 20))
        assert blue > 200 and red < 30
        assert display.size == (3840, 2160)
    library.update(art_id, selected=True, fit="crop", x=0, y=0.5, zoom=1.3)
    with Image.open(library.path_for(art_id)) as display:
        red, _, blue = display.getpixel((20, 20))
        assert red > 200 and blue < 30
    assert library.path_for(art_id, "original").read_bytes() == original
    assert library.get(art_id)["zoom"] == 1.3
    # Older callers that only toggle selection keep the saved zoom.
    library.update(art_id, selected=False, fit="crop", x=0, y=0.5)
    assert library.get(art_id)["zoom"] == 1.3


@pytest.mark.parametrize("zoom", [0.99, 1.51, float("nan"), float("inf")])
def test_invalid_zoom_cannot_modify_saved_images(library, image_bytes, zoom):
    art_id = library.add(image_bytes, title="Unchanged")
    before = library.path_for(art_id).read_bytes()
    with pytest.raises(ValueError):
        library.update(art_id, selected=True, fit="crop", x=0.5, y=0.5, zoom=zoom)
    assert library.path_for(art_id).read_bytes() == before
    assert library.get(art_id)["zoom"] == 1
