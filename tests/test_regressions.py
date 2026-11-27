import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def test_growing_collection_does_not_repeat_new_piece(library, image_bytes):
    library.add(image_bytes, title="Original")
    for day in range(1, 10):
        library.choose(f"2026-10-{day:02}")
    library.add(image_bytes, title="New")
    chosen = [library.choose(f"2026-10-{day:02}")["id"] for day in range(10, 16)]
    assert all(left != right for left, right in zip(chosen, chosen[1:], strict=False))


def test_concurrent_config_mutations_preserve_site_data(configs):
    def add(index):
        def change(raw):
            time.sleep(0.01)
            raw["site"]["data"][str(index)] = index

        configs.mutate(change)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(8)))
    assert len(configs.load().site.data) == 8


def test_startup_error_never_prints_private_feed_url(tmp_path):
    secret = "SYNTHETIC-PRIVATE-FEED-TOKEN"
    config = tmp_path / "bad.toml"
    config.write_text(f'[site]\nrefresh_minutes = "{secret}"\n')
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            str(root / ".venv/bin/python"),
            "-m",
            "art_frames",
            "--demo",
            "--config",
            str(config),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "DAYFRAME_CONFIG"},
    )
    assert result.returncode == 2
    assert "site.refresh_minutes" in result.stderr
    assert secret not in result.stderr + result.stdout
