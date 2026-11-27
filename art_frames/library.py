"""SQLite catalog, daily selections, and normalized images owned by Art Frames."""

from __future__ import annotations

import io
import json
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageOps

from .config import private_write

Image.MAX_IMAGE_PIXELS = 40_000_000
SIZE = (3840, 2160)


def _art_geometry(size, fit, zoom, border):
    inner = (SIZE[0] - 2 * border, SIZE[1] - 2 * border)
    scale = (max if fit == "crop" else min)(inner[0] / size[0], inner[1] / size[1]) * zoom
    fitted = (max(1, min(inner[0], round(size[0] * scale))), max(1, min(inner[1], round(size[1] * scale))))
    return scale, fitted


def _mat_edge(display, bounds, border, color):
    """A shallow cut edge lit from above-left, sized in output pixels."""
    left, top, right, bottom = bounds
    depth = min(6, border)
    rgb = ImageColor.getrgb(color)
    shade = tuple(round(channel * .88) for channel in rgb)
    light = tuple(round(channel + (255 - channel) * .38) for channel in rgb)
    draw = ImageDraw.Draw(display)
    draw.polygon([(left-depth, top-depth), (right+depth-1, top-depth), (right-1, top), (left, top)], fill=shade)
    draw.polygon([(left-depth, top-depth), (left, top), (left, bottom-1), (left-depth, bottom+depth-1)], fill=shade)
    draw.polygon([(left-depth, bottom+depth-1), (left, bottom-1), (right-1, bottom-1), (right+depth-1, bottom+depth-1)], fill=light)
    draw.polygon([(right+depth-1, top-depth), (right-1, top), (right-1, bottom-1), (right+depth-1, bottom+depth-1)], fill=light)
    # Feather a small contact shadow into the painting, rather than a heavy frame.
    draw = ImageDraw.Draw(display, "RGBA")
    shadow = min(10, border, (right-left)//2, (bottom-top)//2)
    for offset in range(shadow):
        alpha = round(35 * (1-offset/shadow)**2)
        draw.line((left+offset, top+offset, right-1, top+offset), fill=(0, 0, 0, alpha))
        draw.line((left+offset, top+offset+1, left+offset, bottom-1), fill=(0, 0, 0, round(alpha*.65)))


class Library:
    def __init__(self, root: Path):
        self.root = root
        self.presentation_lock = threading.RLock()
        self.images = root / "artwork"
        self.images.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "catalog.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS artwork (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '', rights TEXT NOT NULL DEFAULT '',
                    selected INTEGER NOT NULL DEFAULT 1, fit TEXT NOT NULL DEFAULT 'contain',
                    focal_x REAL NOT NULL DEFAULT 0.5, focal_y REAL NOT NULL DEFAULT 0.5,
                    zoom REAL NOT NULL DEFAULT 1.0,
                    mat_width REAL NOT NULL DEFAULT 0.0,
                    mat_color TEXT NOT NULL DEFAULT '#e9e2d7',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source, source_id)
                );
                CREATE TABLE IF NOT EXISTS art_days (
                    day TEXT PRIMARY KEY, artwork_id TEXT NOT NULL REFERENCES artwork(id)
                );
                CREATE TABLE IF NOT EXISTS display_art_days (
                    display_id TEXT NOT NULL, day TEXT NOT NULL,
                    artwork_id TEXT NOT NULL REFERENCES artwork(id),
                    PRIMARY KEY(display_id, day)
                );
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            version = db.execute("SELECT version FROM schema_version").fetchone()[0]
            if version != 1:
                raise RuntimeError("This catalog needs a compatible Art Frames version")
            db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(artwork)")}
            if "zoom" not in columns:
                db.execute("ALTER TABLE artwork ADD COLUMN zoom REAL NOT NULL DEFAULT 1.0")
            if "mat_width" not in columns:
                db.execute("ALTER TABLE artwork ADD COLUMN mat_width REAL NOT NULL DEFAULT 0.0")
            if "mat_color" not in columns:
                db.execute("ALTER TABLE artwork ADD COLUMN mat_color TEXT NOT NULL DEFAULT '#e9e2d7'")
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db

    def all(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM artwork ORDER BY created_at DESC, id")]

    def get(self, art_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM artwork WHERE id=?", (art_id,)).fetchone()
            if row is None:
                raise ValueError("Artwork was not found")
            return dict(row)

    def path_for(self, art_id: str, variant="display") -> Path:
        # Never interpolate arbitrary browser paths into filesystem operations.
        normalized = str(uuid.UUID(art_id))
        if normalized != art_id or variant not in {"original", "display", "thumb", "plain"}:
            raise ValueError("Invalid artwork path")
        return self.images / f"{art_id}-{variant}.jpg"

    def add(
        self,
        data: bytes,
        *,
        title: str,
        artist="",
        year="",
        source="upload",
        source_id: str | None = None,
        source_url="",
        rights="User supplied",
    ) -> str:
        if not title.strip() or len(title) > 300 or len(artist) > 300:
            raise ValueError("Give the artwork a title up to 300 characters")
        source_id = source_id or str(uuid.uuid4())
        with self.connection() as db:
            existing = db.execute(
                "SELECT id FROM artwork WHERE source=? AND source_id=?", (source, source_id)
            ).fetchone()
            if existing:
                return existing[0]
        with Image.open(io.BytesIO(data)) as raw:
            if raw.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Upload a JPEG, PNG, or WebP image")
            if raw.width * raw.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Image exceeds the 40 megapixel limit")
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.thumbnail((7680, 7680), Image.Resampling.LANCZOS)
        art_id = str(uuid.uuid4())
        output = io.BytesIO()
        image.save(output, "JPEG", quality=95)  # drops EXIF and other embedded metadata
        private_write(self.path_for(art_id, "original"), output.getvalue())
        self._render(art_id, image, "contain", 0.5, 0.5)
        try:
            with self.connection() as db:
                db.execute(
                    """INSERT INTO artwork(id,title,artist,year,source,source_id,source_url,rights)
                              VALUES (?,?,?,?,?,?,?,?)""",
                    (art_id, title.strip(), artist, year, source, source_id, source_url, rights),
                )
        except Exception:
            for variant in ("original", "display", "thumb", "plain"):
                self.path_for(art_id, variant).unlink(missing_ok=True)
            raise
        return art_id

    def _render(self, art_id, image, fit, x, y, zoom=1.0, mat_width=0.0, mat_color="#e9e2d7"):
        display = self._compose(image, fit, x, y, zoom, mat_width, mat_color)
        for variant, size in (("display", SIZE), ("thumb", (800, 450))):
            output = io.BytesIO()
            display.resize(size, Image.Resampling.LANCZOS).save(output, "JPEG", quality=90)
            private_write(self.path_for(art_id, variant), output.getvalue())

    @staticmethod
    def _compose(image, fit, x, y, zoom, mat_width, mat_color):
        border = round(SIZE[1] * mat_width)
        inner = (SIZE[0] - 2 * border, SIZE[1] - 2 * border)
        scale, fitted_size = _art_geometry(image.size, fit, zoom, border)
        width, height = min(image.width, inner[0] / scale), min(image.height, inner[1] / scale)
        left, top = (image.width - width) * x, (image.height - height) * y
        fitted = image.resize(
            fitted_size,
            Image.Resampling.LANCZOS,
            box=(left, top, min(image.width, left + width), min(image.height, top + height)),
        )
        display = Image.new("RGB", SIZE, mat_color if border else "#e9e2d7")
        art_left, art_top = (SIZE[0] - fitted.width) // 2, (SIZE[1] - fitted.height) // 2
        display.paste(fitted, (art_left, art_top))
        if border:
            _mat_edge(display, (art_left, art_top, art_left+fitted.width, art_top+fitted.height), border, mat_color)
        return display

    def _plain(self, art):
        """The painting as framed, without its mat, for TVs that never show mats.

        Rendered from the original on first use and cached until the framing changes.
        """
        path = self.path_for(art["id"], "plain")
        if not path.exists():
            with Image.open(self.path_for(art["id"], "original")) as image:
                display = self._compose(image, art["fit"], art["focal_x"], art["focal_y"], art["zoom"], 0.0, art["mat_color"])
            output = io.BytesIO()
            display.save(output, "JPEG", quality=90)
            private_write(path, output.getvalue())
        return path.read_bytes()

    def update(self, art_id, *, selected: bool, fit: str, x: float, y: float, zoom: float | None = None,
               mat_width: float | None = None, mat_color: str | None = None):
        with self.presentation_lock:
            return self._update(art_id, selected=selected, fit=fit, x=x, y=y, zoom=zoom, mat_width=mat_width, mat_color=mat_color)

    def _update(self, art_id, *, selected, fit, x, y, zoom, mat_width, mat_color):
        current = self.get(art_id)
        zoom = current["zoom"] if zoom is None else zoom
        mat_width = current["mat_width"] if mat_width is None else mat_width
        mat_color = current["mat_color"] if mat_color is None else mat_color
        if fit not in {"contain", "crop"} or not 0 <= x <= 1 or not 0 <= y <= 1 or not 1 <= zoom <= 1.5:
            raise ValueError("Invalid fitting options")
        if not 0 <= mat_width <= 0.1 or not isinstance(mat_color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", mat_color):
            raise ValueError("Choose a mat from 0–10% and a six-digit hex color")
        mat_color = mat_color.lower()
        with Image.open(self.path_for(art_id, "original")) as image:
            self._render(art_id, image, fit, x, y, zoom, mat_width, mat_color)
        self.path_for(art_id, "plain").unlink(missing_ok=True)  # stale framing
        with self.connection() as db:
            db.execute(
                "UPDATE artwork SET selected=?,fit=?,focal_x=?,focal_y=?,zoom=?,mat_width=?,mat_color=? WHERE id=?",
                (selected, fit, x, y, zoom, mat_width, mat_color, art_id),
            )
            key = "art_revision:" + art_id
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            revision = json.loads(row[0]) + 1 if row else 1
            db.execute(
                "INSERT INTO state(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(revision)),
            )

    def presentation(self, art_id, *, mat=True):
        # Autosave and capture must agree on both the image and its opening.
        with self.presentation_lock:
            art = self.get(art_id)
            viewport = None
            border = round(SIZE[1] * art["mat_width"])
            if border and not mat:
                return art, self._plain(art), None
            if border:
                with Image.open(self.path_for(art_id, "original")) as original:
                    _, (width, height) = _art_geometry(original.size, art["fit"], art["zoom"], border)
                viewport = {"x": (SIZE[0]-width)//2, "y": (SIZE[1]-height)//2, "width": width, "height": height}
            return art, self._display(art), viewport

    def _display(self, art):
        """The framed painting at 3840×2160. A missing or wrong-size file is rebuilt from the original once.

        Call it under presentation_lock. Without the original it raises OSError.
        """
        path = self.path_for(art["id"])
        try:
            data = path.read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                if image.size == SIZE:
                    return data
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
        with Image.open(self.path_for(art["id"], "original")) as original:
            self._render(art["id"], original.convert("RGB"), art["fit"], art["focal_x"], art["focal_y"], art["zoom"],
                         art["mat_width"], art["mat_color"])
        return path.read_bytes()

    def choose(self, day: str, order="shuffle", *, display_id="main"):
        primary = display_id == "main"
        table = "art_days" if primary else "display_art_days"
        scope = "" if primary else " AND d.display_id=?"
        args = (day,) if primary else (day, display_id)
        query = f"SELECT a.* FROM {table} d JOIN artwork a ON a.id=d.artwork_id WHERE d.day=? AND a.selected=1{scope}"
        with self.connection() as db:
            # Most calls only read; take the write lock only when a choice must be made.
            current = db.execute(query, args).fetchone()
            if current:
                return dict(current)
            if not db.execute("SELECT 1 FROM artwork WHERE selected=1 LIMIT 1").fetchone():
                return None
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(query, args).fetchone()
            if current:
                return dict(current)
            candidates = db.execute(
                "SELECT * FROM artwork WHERE selected=1 ORDER BY created_at,id"
            ).fetchall()
            if not candidates:
                return None
            previous = db.execute(
                f"SELECT d.artwork_id FROM {table} d WHERE d.day<?{scope} ORDER BY d.day DESC LIMIT 1", args
            ).fetchone()
            assigned = {row[0] for row in db.execute(
                "SELECT artwork_id FROM art_days WHERE day=? UNION SELECT artwork_id FROM display_art_days WHERE day=? AND display_id!=?",
                (day, day, display_id),
            )}
            available = [row for row in candidates if row["id"] not in assigned] or candidates
            stored_cycle = db.execute("SELECT value FROM state WHERE key='art_cycle'").fetchone()
            cycle = json.loads(stored_cycle[0]) if stored_cycle else []
            selected = {row["id"] for row in candidates}
            cycle = [art_id for art_id in cycle if art_id in selected]
            pool = [row for row in available if row["id"] not in cycle]
            if not pool:
                cycle = []
                pool = list(available)
            if previous and len(pool) > 1:
                pool = [row for row in pool if row["id"] != previous[0]]
            picked = secrets.choice(pool) if order == "shuffle" else pool[0]
            cycle.append(picked["id"])
            db.execute(
                "INSERT INTO state(key,value) VALUES ('art_cycle',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(cycle),),
            )
            if primary:
                db.execute(
                    "INSERT INTO art_days(day,artwork_id) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET artwork_id=excluded.artwork_id",
                    (day, picked["id"]),
                )
            else:
                db.execute(
                    "INSERT INTO display_art_days(display_id,day,artwork_id) VALUES (?,?,?) ON CONFLICT(display_id,day) DO UPDATE SET artwork_id=excluded.artwork_id",
                    (display_id, day, picked["id"]),
                )
            return dict(picked)

    def state(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        encoded = json.dumps(value)
        with self.connection() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            if row and row[0] == encoded:
                return  # the scheduler repeats unchanged status every 30 s
            db.execute(
                "INSERT INTO state(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def delete_state(self, key):
        with self.connection() as db:
            if db.execute("SELECT 1 FROM state WHERE key=?", (key,)).fetchone():
                db.execute("DELETE FROM state WHERE key=?", (key,))
