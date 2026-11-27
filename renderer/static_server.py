"""Networkless static renderer supervisor; each job has a killable child and deadline."""

import asyncio
import fcntl
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from art_frames import ipc
from art_frames.ipc import receive_async, send_async

jobs = asyncio.Semaphore(1)
log = logging.getLogger("art_frames.renderer")
SUMMARY = b"dayframe-render-error: "
STDERR_LIMIT = 8192


def describe(error):
    # Own ValueError messages are fixed text; others may quote page markup, URLs or CSS.
    return ipc.describe(error, __file__)


async def tail(stream, limit=STDERR_LIMIT):
    """Drain child stderr so it never blocks; keep only the last few KB."""
    kept = bytearray()
    while chunk := await stream.read(65536):
        kept = (kept + chunk)[-limit:]
    return bytes(kept)


def child_summary(stderr):
    lines = [line for line in stderr.splitlines() if line.startswith(SUMMARY)]
    return lines[-1][len(SUMMARY):].decode(errors="replace")[:240] if lines else ""


async def handle(reader, writer):
    process = errors = folder = None
    async with jobs:
        try:
            async with asyncio.timeout(35):
                try:
                    job = await receive_async(reader)
                except asyncio.IncompleteReadError as error:
                    if not error.partial:
                        return  # health probe: connect and close
                    raise
                # Each job gets its own temporary folder, removed after the job. The child is
                # always stopped with SIGKILL, so it never removes its own files (WeasyPrint
                # copies each web font there). Left behind, they filled /tmp in about a day.
                folder = tempfile.mkdtemp(prefix="render-")
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "renderer.static_capture",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    env={**os.environ, "TMPDIR": folder},
                )
                errors = asyncio.create_task(tail(process.stderr))
                await send_async(process.stdin, job)
                for _ in range(256):
                    message = await receive_async(process.stdout)
                    await send_async(writer, message)
                    if message.get("type") != "fetch":
                        if message.get("type") == "error":
                            log.warning("Render failed: %s", message.get("reason", "unknown"))
                        break
                    await send_async(process.stdin, await receive_async(reader))
                else:
                    raise ValueError("Too many website resources")
        except Exception as error:
            log.warning("Render job failed: %s", describe(error))
            try:
                await asyncio.wait_for(send_async(writer, {"type": "error", "reason": "static"}), 1)
            except Exception:
                pass
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                if errors is not None:
                    try:
                        summary = child_summary(await asyncio.wait_for(errors, 2))
                    except Exception:
                        summary = ""
                    if summary or process.returncode not in (0, -signal.SIGKILL):
                        log.warning("Render process: %s (exit %s)", summary or "no summary", process.returncode)
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
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
