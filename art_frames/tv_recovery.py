"""Find a paired TV again after its DHCP address changes.

The search is read-only and rate limited. A TV in deep standby is unreachable every
night, so a search starts only when one of these happens:

1. Art Frames sent a wake and the TV did not answer at its saved address.
2. The TV status has been unknown for 24 hours.
3. A person selects "Find this TV" in Settings.

Searches 1 and 2 run at most every 30 minutes per TV, and only one search runs at a
time across all TVs. While the outage search keeps finding nothing, its interval doubles
after each search, up to 6 hours; it resets when the TV answers. A search runs in a background thread, so a slow sweep never
delays a tick. A later tick applies the result under the scheduler lock, and only when
exactly one device answered with the stored MAC and the kernel neighbour table confirms
it. An unconfirmed match is stored as a proposal that a person must accept in Settings.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

from . import tv_discovery
from .config import wake_time
from .television import WakeError

log = logging.getLogger(__name__)

STATE_KEY = "ip_recovery"
SEARCH_AFTER_SECONDS = 24 * 60 * 60
SEARCH_EVERY_SECONDS = 30 * 60
OUTAGE_EVERY_MAX_SECONDS = 6 * 60 * 60  # the outage search backs off to this interval
PROPOSAL_SECONDS = 60 * 60  # a proposal older than this is stale
# WakeError codes that mean the TV did not answer, so it may have a new address.
# "not-confirmed" means the TV answered at its saved address; "network" means this host
# could not send the packet; "unpaired" and "missing-mac" are settings problems.
NO_ANSWER = ("state-unknown",)
QUIET_BEFORE_WAKE_SECONDS = 5 * 60

# Only one address search runs at a time across all TVs: each sweep uses twelve threads.
SEARCH_SLOT = threading.Lock()

MESSAGES = {
    "updated": "Found this TV at a new address and updated it.",
    "reachable": "The TV answered at its saved address.",
    "not-found": "No TV with this MAC address answered.",
    "ambiguous": "More than one device answered with this MAC address. The address was not changed.",
    "conflict": "The TV answered at an address that another TV uses. The address was not changed.",
    "changed": "The TV settings changed during the search. The address was not changed.",
    "failed": "The search could not complete.",
    "proposed": "Art Frames found a TV that reports this TV's MAC address at {ip}. Use this address?",
    "dismissed": "You kept the saved address.",
}
# Results that stay on the page after the TV answers at its saved address.
KEPT_RESULTS = ("updated", "reachable")


class _Changed(Exception):
    pass


def near_wake(cfg, now):
    """Return True when a scheduled wake starts within the next few minutes."""
    local = datetime.fromtimestamp(now, ZoneInfo(cfg.timezone))
    for day in (local.date(), local.date() + timedelta(days=1)):
        at = wake_time(cfg, day)
        wake = at and datetime.combine(day, clock_time.fromisoformat(at), tzinfo=local.tzinfo)
        if wake and 0 <= (wake - local).total_seconds() <= QUIET_BEFORE_WAKE_SECONDS:
            return True
    return False


def _record(library, cfg):
    """Return (stored value, record for this TV). A record for another identity is ignored."""
    saved = library.state(STATE_KEY)
    if not isinstance(saved, dict) or saved.get("device") != cfg.tv.identity:
        return saved, {"device": cfg.tv.identity}
    return saved, dict(saved)


def summary(library, cfg):
    """The last search result for this TV, for the Settings page, or None."""
    _, state = _record(library, cfg)
    result = state.get("result")
    proposal = state.get("proposal") if result == "proposed" else None
    if result not in MESSAGES or (result == "proposed" and not proposal):
        return None
    return {"result": result, "text": MESSAGES[result].format(ip=proposal), "at": state.get("at"),
            "proposal": proposal}


def clear_proposal(library, cfg):
    """Drop a proposal after a settings change."""
    saved, state = _record(library, cfg)
    if state.pop("proposal", None) and state.get("result") == "proposed":
        state.pop("result", None)
        state.pop("at", None)
    if isinstance(saved, dict) and state != saved:
        library.set_state(STATE_KEY, state)


class AddressRecovery:
    def __init__(self, configs, library, *, find=None, probe=None, networks=None, clock=time.time):
        self.configs, self.library, self.clock = configs, library, clock
        self.find = find or tv_discovery.find_mac
        self.probe = probe or tv_discovery._probe
        self.networks = networks or (lambda ip: [tv_discovery.suggested_network(), tv_discovery.suggested_network(ip)])
        self.lock = threading.Lock()  # guards self.finished
        self.state_lock = threading.RLock()  # the scheduler and web threads both change the record
        self.thread = None
        self.finished = None  # (searched ip, searched mac, result, new ip, finished at)

    def searching(self):
        return self.thread is not None and self.thread.is_alive()

    def wait(self, timeout=None):
        thread = self.thread
        if thread is not None:
            thread.join(timeout)

    def _save(self, saved, state):
        if state != saved:
            self.library.set_state(STATE_KEY, state)

    def _result(self, cfg, result, at, proposal=None):
        """Store a result. Call with state_lock held."""
        saved, state = _record(self.library, cfg)
        state.pop("proposal", None)
        if result == "updated":
            log.info("TV %s: found at a new address; updated", cfg.tv_name)
            state = {"device": cfg.tv.identity, "searched": state.get("searched")}
        if proposal:
            state["proposal"] = proposal
        state.update(result=result, at=at)
        self._save(saved, state)

    def apply(self, cfg):
        """Apply a finished search. Call under the scheduler lock. Return True if the address changed."""
        with self.lock:
            finished, self.finished = self.finished, None
        if finished is None:
            return False
        ip, mac, result, new_ip, at = finished
        if result in ("found", "proposed") and (cfg.tv.ip != ip or cfg.tv.mac != mac or not mac):
            result = "changed"
        elif result == "found":
            result = self._try_move(cfg, new_ip)
        with self.state_lock:
            self._result(cfg, result, at, new_ip if result == "proposed" else None)
        return result == "updated"

    def _try_move(self, cfg, new_ip):
        try:
            return self._move(cfg.tv, new_ip)
        except Exception as error:
            log.warning("TV %s: address update failed: %s", cfg.tv_name, type(error).__name__)
            return "failed"

    def observe(self, cfg, status):
        """Record one status read and start a background search when one is due."""
        tv, now = cfg.tv, self.clock()
        with self.state_lock:
            saved, state = _record(self.library, cfg)
            if state.get("ip") != tv.ip:
                state.pop("proposal", None)  # the settings changed since the proposal
                state.pop("since", None)
                state.pop("every", None)
            if status.get("known") or status.get("answered"):
                # The TV answered at its saved address, so an old search result no longer applies.
                for key in ("since", "every", "requested", "proposal"):
                    state.pop(key, None)
                if state.get("result") not in KEPT_RESULTS:
                    state.pop("result", None)
                    state.pop("at", None)
                self._save(saved, state)
                return False
            if not status.get("paired") or not tv.ip or not tv.mac:
                for key in ("since", "every", "requested"):
                    state.pop(key, None)
                self._save(saved, state)
                return False
            if "since" not in state:
                state.update(ip=tv.ip, since=now)
            requested = state.get("requested")
            # A failed wake keeps the fixed limit; the outage search backs off (see _back_off).
            every = SEARCH_EVERY_SECONDS if requested else state.get("every", SEARCH_EVERY_SECONDS)
            last = state.get("searched")
            limited = last is not None and 0 <= now - last < every
            outage = now - state["since"] >= SEARCH_AFTER_SECONDS and not near_wake(cfg, now)
            if limited or not (requested or outage) or self.searching():
                self._save(saved, state)
                return False
            return self._start(cfg, saved, state, now, back_off=not requested)

    @staticmethod
    def _back_off(state):
        """Set the wait after this outage search: double it while searches find nothing, up to 6 hours."""
        if state.get("result") == "not-found" and "every" in state:
            state["every"] = min(2 * state["every"], OUTAGE_EVERY_MAX_SECONDS)
        else:
            state["every"] = SEARCH_EVERY_SECONDS

    def wake_failed(self, cfg, error=None):
        """Art Frames sent a wake and the TV did not answer at its saved address: search on the next status read.

        error: the wake's exception. A WakeError that does not mean "the TV did not answer"
        (see NO_ANSWER) starts no search.
        """
        if not cfg.tv.ip or not cfg.tv.mac:
            return
        if isinstance(error, WakeError) and str(error) not in NO_ANSWER:
            return
        with self.state_lock:
            saved, state = _record(self.library, cfg)
            state["requested"] = "wake"
            self._save(saved, state)

    def request(self, cfg):
        """Start a search now for "Find this TV". Returns started, searching, busy or incomplete."""
        if not cfg.tv.ip or not cfg.tv.mac:
            return "incomplete"
        with self.state_lock:
            if self.searching():
                return "searching"
            saved, state = _record(self.library, cfg)
            if state.get("ip") != cfg.tv.ip:
                state.pop("since", None)
                state["ip"] = cfg.tv.ip
            return "started" if self._start(cfg, saved, state, self.clock()) else "busy"

    def _open_proposal(self, cfg, ip):
        """The stored proposal when it is for ip, this TV's settings and less than an hour old."""
        _, state = _record(self.library, cfg)
        proposal, at = state.get("proposal"), state.get("at")
        if not proposal or proposal != ip or state.get("ip") != cfg.tv.ip or not cfg.tv.mac:
            return None
        if state.get("result") != "proposed" or at is None or not 0 <= self.clock() - at < PROPOSAL_SECONDS:
            return None
        return state

    def accept(self, cfg, ip):
        """Use a proposed address. Returns the result, or "stale" when no such proposal is open.

        The address is probed again first, and the device there must still report this TV's MAC.
        """
        with self.state_lock:
            state = self._open_proposal(cfg, ip)
        if state is None:
            return "stale"
        device = self.probe(ip)  # outside the lock: a probe can take a second
        if not device or str(device.get("mac", "")).upper() != cfg.tv.mac.upper():
            return "stale"
        with self.state_lock:
            if self._open_proposal(cfg, ip) != state:
                return "stale"  # the record changed during the probe
            result = self._try_move(cfg, ip)
            self._result(cfg, result, self.clock())
            return result

    def clear_proposal(self, cfg):
        """Drop a proposal after a settings change, under the state lock."""
        with self.state_lock:
            clear_proposal(self.library, cfg)

    def dismiss(self, cfg):
        """Keep the saved address and drop the proposal."""
        with self.state_lock:
            _, state = _record(self.library, cfg)
            if not state.get("proposal"):
                return False
            self._result(cfg, "dismissed", self.clock())
            return True

    def _start(self, cfg, saved, state, now, back_off=False):
        """Start the search thread. Call with state_lock held. Returns False if another TV is searching.

        back_off: set the outage-search wait (see _back_off). It changes only when a search starts.
        """
        if not SEARCH_SLOT.acquire(blocking=False):
            self._save(saved, state)  # another TV is searching; this one tries on a later status read
            return False
        try:
            # Persist the attempt before it starts, so a crash or failure cannot cause a search loop.
            state["searched"] = now
            state.pop("requested", None)
            if back_off:
                self._back_off(state)
            self._save(saved, state)
            self.thread = threading.Thread(
                target=self._run, args=(cfg.model_copy(deep=True),), name="art-frames-tv-search", daemon=True
            )
            self.thread.start()
        except BaseException:
            SEARCH_SLOT.release()
            raise
        return True

    def _run(self, cfg):
        try:
            result, new_ip = self._search(cfg)
        except Exception as error:
            log.warning("TV %s: address search failed: %s", cfg.tv_name, type(error).__name__)
            result, new_ip = "failed", None
        finally:
            SEARCH_SLOT.release()
        with self.lock:
            self.finished = (cfg.tv.ip, cfg.tv.mac, result, new_ip, self.clock())

    def _search(self, cfg):
        tv = cfg.tv
        device = self.probe(tv.ip)
        if device and device["mac"] in ("", tv.mac):
            return "reachable", None
        networks = list(dict.fromkeys(value for value in self.networks(tv.ip) if value))
        matches = {}
        for ip, confirmed in self.find(tv.mac, networks):
            matches[ip] = matches.get(ip, False) or confirmed
        if not matches:
            return "not-found", None
        if len(matches) > 1:
            return "ambiguous", None
        ((ip, confirmed),) = matches.items()
        if ip == tv.ip:
            return "reachable", None
        # Only the neighbour table confirms a MAC; a device-info reply alone can be spoofed.
        return ("found" if confirmed else "proposed"), ip

    def _move(self, old, new_ip):
        # Recovery needs a stored MAC, so the identity, pairing token and state stay the same.
        def change(raw):
            if raw["tv"]["ip"] != old.ip or raw["tv"]["mac"].upper() != old.mac:
                raise _Changed
            raw["tv"]["ip"] = new_ip

        try:
            self.configs.mutate(change)
        except _Changed:
            return "changed"
        except ValueError:
            return "conflict"
        return "updated"
