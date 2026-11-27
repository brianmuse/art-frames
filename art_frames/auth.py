"""The household password, first-run setup, long sign-ins and the sign-in rate limit.

Art Frames finds the password in one of two places, in this order:

1. A password file (ART_FRAMES_PASSWORD_FILE or DAYFRAME_PASSWORD_FILE). An install with a
   password file never sees first-run setup, and auth.json is ignored.
2. <data>/auth.json: a scrypt hash. First-run setup and "Change password" write it.

With neither, Art Frames is in setup mode. It prints a setup code in the log and writes it to
<data>/setup-code. The first person who types the code creates the password.

A sign-in carries a mark: an HMAC of the sign-out generation and the password. "Sign out
everywhere" raises the generation, and a new password changes the mark, so every older sign-in
stops working. A password-file install keeps its generation in <data>/sessions.json.

ART_FRAMES_RESET_PASSWORD=1 moves auth.json to auth-before-reset.json at start, so the install
goes back to setup mode. Whoever can read the log can then set a new password: the log is the
trust anchor, as in first-run setup. A damaged auth.json puts Art Frames in "broken" mode: every
page says to use that reset, and nothing signs in.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from .config import private_write

log = logging.getLogger(__name__)

AUTH_FILE = "auth.json"
RESET_FILE = "auth-before-reset.json"
FORMAT = 1
SESSIONS_FILE = "sessions.json"
SETUP_FILE = "setup-code"
FILE_MINIMUM = 16  # a password file, as before
MINIMUM = 12  # a password someone types in setup or Change password
MAXIMUM = 1024
SETUP_SECONDS = 3600
# No 0/O, 1/I/L: people copy the code from a log by hand.
CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 10
# scrypt cost: about 32 MB and 100 ms on a small NAS for each check. Each record keeps its own
# cost, so a hash made with older values still works. Art Frames refuses a record that asks for more
# than LIMITS, so a damaged file cannot make one check use all memory.
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
LIMITS = {"n": 2**20, "r": 32, "p": 16}
KEEP_ADDRESSES = 10_000


class Unreadable(ValueError):
    """A file Art Frames may not read, for example one owned by root."""


UNREADABLE_FIX = ("Give it to the Art Frames user (chown 10001:10001 auth.json in the data folder), or start "
                  "Art Frames with ART_FRAMES_RESET_PASSWORD=1 to create a new password.")


class PasswordError(ValueError):
    """A new password Art Frames refuses. The message is a sentence for the page."""


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def normalize_code(text):
    """A typed setup code without spaces or dashes, in capitals."""
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def show_code(code):
    return code[:5] + "-" + code[5:]


def usable(record):
    """Whether an auth.json record can check a password: scrypt, with sane costs and base64 fields."""
    if not isinstance(record, dict) or record.get("scheme") != "scrypt":
        return False
    for key, limit in LIMITS.items():
        value = record.get(key)
        if type(value) is not int or not 1 <= value <= limit:
            return False
    if record["n"] < 2 or record["n"] & (record["n"] - 1):
        return False
    try:
        return all(base64.b64decode(record[key], validate=True) for key in ("salt", "hash"))
    except (KeyError, TypeError, ValueError):
        return False


class Throttle:
    """Failed tries per client: after `limit` failures in `window` seconds, that client waits."""

    def __init__(self, limit=5, window=60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.failures = {}
        self.lock = threading.Lock()

    def _trim(self, key, now):
        failures = self.failures.get(key, deque())
        while failures and now - failures[0] >= self.window:
            failures.popleft()
        if not failures:
            self.failures.pop(key, None)
        return failures

    def wait(self, key):
        """Seconds this client must wait before it may try again; 0 when it may try now."""
        with self.lock:
            now = self.clock()
            failures = self._trim(key, now)
            if len(failures) < self.limit:
                return 0
            return max(1, int(self.window - (now - failures[0]) + 0.999))

    def failed(self, key):
        with self.lock:
            now = self.clock()
            self._trim(key, now)
            failures = self.failures.pop(key, deque())
            failures.append(now)
            self.failures[key] = failures  # the newest key goes last
            # A flood of addresses must not use all memory: forget the oldest ones first.
            while len(self.failures) > KEEP_ADDRESSES:
                del self.failures[next(iter(self.failures))]

    def passed(self, key):
        with self.lock:
            self.failures.pop(key, None)


class Auth:
    def __init__(self, root: Path, secret_key: bytes, password: str | None = None, *, clock=time.time):
        self.root, self.secret_key, self.file_password, self.clock = root, secret_key, password, clock
        self.lock = threading.RLock()
        self.throttle = Throttle()
        self._cache = {}  # path -> (stat stamp, parsed JSON)
        self._verified = set()  # SHA-256 of passwords that matched the stored hash, for Basic auth
        self.code = None
        self.code_made = 0.0
        if self.mode == "setup":
            self.new_code()

    # Where the password lives.

    @property
    def mode(self):
        """"file" (a password file), "stored" (auth.json), "setup" (no password yet) or "broken"
        (auth.json is damaged: see ART_FRAMES_RESET_PASSWORD)."""
        if self.file_password is not None:
            return "file"
        try:
            return "stored" if self._stored() else "setup"
        except ValueError:
            return "broken"

    def _read(self, name):
        path = self.root / name
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise Unreadable(f"Art Frames may not read {name}") from error
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(name)
        if cached and cached[0] == stamp:
            return cached[1]
        if path.is_symlink() or stat.st_size > 10_000:
            raise ValueError(f"Refusing an unusual {name}")
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise Unreadable(f"Art Frames may not read {name}") from error
        value = json.loads(data)
        self._cache[name] = (stamp, value)
        return value

    def _stored(self):
        """The auth.json record, or None when there is none. ValueError when it is damaged."""
        value = self._read(AUTH_FILE)
        if value is None:
            return None
        if not usable(value):
            raise ValueError(f"{AUTH_FILE} is damaged")
        return value

    def problem(self):
        """Why the saved password cannot be used: a sentence for the log, or "" when it can."""
        try:
            self._stored()
        except Unreadable:
            return f"Art Frames may not read {AUTH_FILE}, so no password works. {UNREADABLE_FIX}"
        except ValueError:
            return (f"{AUTH_FILE} is damaged, so no password works. Start Art Frames with "
                    "ART_FRAMES_RESET_PASSWORD=1 to create a new one.")
        return ""

    def reset(self):
        """Go back to setup mode: keep the old auth.json as auth-before-reset.json. Settings stay.
        Returns a sentence for the log."""
        with self.lock:
            if self.mode == "file":
                return ("ART_FRAMES_RESET_PASSWORD does nothing here: the password comes from a password file. "
                        "Remove the variable.")
            path = self.root / AUTH_FILE
            try:
                path.replace(self.root / RESET_FILE)
            except FileNotFoundError:
                pass
            except OSError as error:
                return (f"ART_FRAMES_RESET_PASSWORD is set, but Art Frames could not move {AUTH_FILE} aside "
                        f"({error.strerror}). The password was not reset. Give the data folder to the Art Frames "
                        f"user (chown -R 10001:10001 on it), or delete {AUTH_FILE} yourself, then start again.")
            self._cache.pop(AUTH_FILE, None)
            self._verified.clear()
            self.new_code()
        return ("ART_FRAMES_RESET_PASSWORD is set, so the password was reset. Create a new one with the setup "
                "code. Then remove the variable: each start with it set resets the password again.")

    # Checking a password.

    def check(self, supplied):
        """Whether `supplied` is the household password. Constant time for a password file."""
        if not supplied or len(supplied) > MAXIMUM:
            return False
        mode = self.mode
        if mode == "file":
            return hmac.compare_digest(supplied.encode(), self.file_password.encode())
        if mode != "stored":
            if mode == "broken":
                log.error(self.problem())
            return False
        stored = self._stored()
        digest = hashlib.sha256(stored["hash"].encode() + b"\0" + supplied.encode()).digest()
        if digest in self._verified:
            return True
        expected = base64.b64decode(stored["hash"])
        actual = _scrypt(supplied, base64.b64decode(stored["salt"]), stored["n"], stored["r"], stored["p"])
        if hmac.compare_digest(actual, expected):
            with self.lock:
                self._verified.add(digest)
            return True
        return False

    # Sign-in marks and sign-out everywhere.

    def generation(self):
        """The sign-out generation. Older records called it "version"."""
        try:
            source = self._stored() if self.mode == "stored" else self._read(SESSIONS_FILE)
        except ValueError:
            return 1
        source = source if isinstance(source, dict) else {}
        try:
            return int(source.get("sign_out_generation", source.get("version", 1)))
        except (TypeError, ValueError):
            return 1

    def mark(self):
        """The value a signed-in session carries. It changes with the version and the password."""
        mode = self.mode
        if mode == "file":
            credential = hashlib.sha256(self.file_password.encode()).hexdigest()
        elif mode == "stored":
            credential = self._stored()["hash"]
        else:
            return None
        message = f"{mode}:{self.generation()}:{credential}".encode()
        return hmac.new(self.secret_key, message, hashlib.sha256).hexdigest()

    def valid_mark(self, value):
        mark = self.mark()
        return bool(mark and isinstance(value, str) and hmac.compare_digest(value, mark))

    def sign_out_everywhere(self):
        with self.lock:
            generation = self.generation() + 1
            if self.mode == "stored":
                record = {key: value for key, value in self._stored().items() if key != "version"}
                private_write(self.root / AUTH_FILE,
                              json.dumps({**record, "sign_out_generation": generation}, indent=2).encode())
            else:
                private_write(self.root / SESSIONS_FILE,
                              json.dumps({"format": FORMAT, "sign_out_generation": generation}).encode())
        log.info("Signed out on every device")

    # Setting a password.

    def set_password(self, new, repeat, *, current=None, setup=False):
        """Save a new password as a hash. Setup needs setup mode; a change needs the current password."""
        with self.lock:
            mode = self.mode
            if mode == "file":
                raise PasswordError("This password comes from a file. Change the file, then restart Art Frames.")
            if mode == "broken":
                raise PasswordError("The saved password is damaged. Start Art Frames with "
                                    "ART_FRAMES_RESET_PASSWORD=1 to create a new one.")
            if setup and mode != "setup":
                raise PasswordError("Someone already created the password. Sign in with it.")
            if not setup and not self.check(current or ""):
                raise PasswordError("Your current password is not right.")
            if len(new) < MINIMUM:
                raise PasswordError(f"Use at least {MINIMUM} characters.")
            if len(new) > MAXIMUM:
                raise PasswordError(f"Use at most {MAXIMUM} characters.")
            if new != repeat:
                raise PasswordError("The two passwords are not the same.")
            salt = secrets.token_bytes(16)
            record = {
                "format": FORMAT, "scheme": "scrypt", **SCRYPT,
                "salt": base64.b64encode(salt).decode(),
                "hash": base64.b64encode(_scrypt(new, salt, **SCRYPT)).decode(),
                "sign_out_generation": self.generation() + 1,
            }
            private_write(self.root / AUTH_FILE, json.dumps(record, indent=2).encode())
            self._verified.clear()
            if setup:
                self.code = None
                (self.root / SETUP_FILE).unlink(missing_ok=True)
        log.info("The password was %s", "created" if setup else "changed")

    # First-run setup.

    def new_code(self):
        """Make a new setup code, write it to <data>/setup-code and return it."""
        with self.lock:
            self.code = "".join(secrets.choice(CODE_LETTERS) for _ in range(CODE_LENGTH))
            self.code_made = self.clock()
            private_write(self.root / SETUP_FILE, (show_code(self.code) + "\n").encode())
            return self.code

    def code_expired(self):
        return self.code is None or self.clock() - self.code_made > SETUP_SECONDS

    def check_code(self, supplied):
        """Whether `supplied` is the current, unexpired setup code."""
        if self.mode != "setup" or self.code_expired():
            return False
        return hmac.compare_digest(normalize_code(supplied).encode(), self.code.encode())
