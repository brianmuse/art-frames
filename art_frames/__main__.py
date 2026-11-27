"""Entrypoint: one process, one scheduler, private files, no root requirement."""

from __future__ import annotations

import argparse
import atexit
import ipaddress
import logging
import os
import sys
import warnings
from pathlib import Path

from pydantic import ValidationError
from urllib3.exceptions import InsecureRequestWarning
from waitress import serve

from . import auth, reach
from .env import env
from .web import create_app, seed_demo


def writable(folder):
    """None when Art Frames can write to the data folder, else a sentence that names the fix."""
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = folder / ".write-check"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as error:
        return (f"Art Frames cannot write to its data folder {folder} ({error.strerror}). It runs as user "
                f"{os.getuid()}. Give that user the folder, for example: sudo chown -R {os.getuid()}:{os.getgid()} "
                "<the folder on the host>. A named Docker volume, as in install/docker-compose.yml, needs no change.")
    return None


def main():
    parser = argparse.ArgumentParser(description="Your website and artwork, on your screen")
    parser.add_argument("--demo", action="store_true", help="Sample data; no real TV commands")
    parser.add_argument("--data-dir", type=Path, default=Path(env("DATA_DIR", "data")))
    parser.add_argument("--config", type=Path, default=env("CONFIG"))
    parser.add_argument("--host", default=env("BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("PORT", "8765")))
    args = parser.parse_args()
    address = ipaddress.ip_address(args.host)
    if not address.is_loopback and (args.demo or not address.is_private):
        parser.error("Demo must bind to loopback; production must bind to loopback or a private LAN IP")
    os.umask(0o077)
    password_path = env("PASSWORD_FILE")
    password = Path(password_path).read_text().strip() if password_path else None
    if not address.is_loopback:
        if env("HOSTS") is None:
            # Any home network name: a private IP address, name.local or a name without dots (reach.lan_host).
            os.environ["DAYFRAME_HOSTS"] = "*"
    level = env("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        parser.error("ART_FRAMES_LOG_LEVEL (or DAYFRAME_LOG_LEVEL) must be DEBUG, INFO, WARNING or ERROR")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %z",
    )
    # The TV SDK logs newly issued pairing tokens at INFO; keep it quiet at every level.
    # urllib3 and websocket-client log request URLs and frames at DEBUG.
    for name in ("samsungtvws", "urllib3", "websocket"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # The TV serves a self-signed certificate, so the SDK skips verification on every
    # status check. Art Frames makes no other unverified HTTPS requests.
    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
    problem = writable(args.data_dir)
    if problem:
        parser.exit(2, problem + "\n")
    if env("SECURE_COOKIES") == "1" and not address.is_loopback:
        logging.getLogger(__name__).warning(
            "ART_FRAMES_SECURE_COOKIES=1 is for an HTTPS reverse proxy. Set ART_FRAMES_BIND=127.0.0.1 with it: "
            "now plain HTTP on %s also answers, and browsers there can't keep a sign-in", args.host)
    where = reach.Reach(args.host, args.port)
    try:
        app = create_app(args.data_dir, config_path=args.config, password=password, demo=args.demo, reach=where)
    except ValidationError as error:
        details = "; ".join(
            ".".join(map(str, item["loc"])) + ": " + item["msg"]
            for item in error.errors(include_input=False, include_url=False)
        )
        parser.exit(2, "Invalid configuration: " + details + "\n")
    if args.demo:
        seed_demo(app.extensions["library"])
    displays = app.extensions["displays"]
    displays.start()
    atexit.register(displays.stop)
    sensors = app.extensions["sensors"]
    sensors.start()
    atexit.register(sensors.stop)
    if not args.demo:
        advertiser = reach.advertise(where, env("MDNS"), env("MDNS_NAME"))
        if advertiser:
            atexit.register(advertiser.stop)
        updates = app.extensions["updates"]
        updates.start()
        atexit.register(updates.stop)
    signin = app.extensions["auth"]
    code = auth.show_code(signin.code) if not args.demo and signin.mode == "setup" else None
    print(where.banner(code, qr_code=sys.stdout.isatty()), flush=True)
    serve(app, host=args.host, port=args.port, threads=4, max_request_body_size=25_000_000)


if __name__ == "__main__":
    main()
