"""What the rack is currently doing, as far as anything can tell.

Every field is `None` until it can genuinely be read, and the UI prints `--`
for those rather than inventing a plausible number. Most of this IS read: the TV over
SSAP, the amp over RS-232 QRY, Music.app over AppleScript. The D900 is the
exception -- IR out, nothing back -- so its entry is marked untrusted and
the phone renders it with a ~. The disc player stays absent: its emitter is
not aimed yet, and a readout for a device the app cannot touch is
furniture.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from devices import config as device_config
from devices import registry

from . import amplink, musiclink, settings, tvlink


@dataclass
class DeviceState:
    name: str
    transport: str  # how it is spoken to, shown on the diagnostics line
    online: bool | None = None
    power: bool | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    # False whenever the value is our own bookkeeping rather than a readback --
    # the phone renders those differently, because they can be wrong.
    trusted: bool = True
    detail: str = "no device module yet"


# The durable record of what the rack was last seen doing. Deliberately in
# ~/.avctl, which the deployer never touches -- it replaces ~/avctl-live and
# nothing else -- so a redeploy, a restart or a power cut all come back to a
# service that still knows what it knew. Nothing READ from a device is ever
# restored from here (the TV, amp and Music are asked directly, every poll);
# this exists for the values that cannot be asked at all, and as a record
# worth having when something goes strange at 2am.
RACK_STATE_FILE = Path("~/.avctl/rack_state.json").expanduser()
_PERSIST_EVERY = 60.0
_persist_lock = threading.Lock()
_persisted_at = 0.0


def _persist(snapshot: dict[str, Any]) -> None:
    """Write the snapshot to RACK_STATE_FILE, atomically and rarely.

    Atomic because a half-written file read at startup is worse than no
    file; rarely because the phone polls every 15s and none of this is
    worth that much disk. Never raises: a rack that cannot write its diary
    should still answer the remote.
    """
    global _persisted_at
    with _persist_lock:
        if time.monotonic() - _persisted_at < _PERSIST_EVERY:
            return
    payload = dict(snapshot)
    payload["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    temp = None
    try:
        RACK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=RACK_STATE_FILE.parent,
                                        prefix=".rack_state-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        os.replace(temp, RACK_STATE_FILE)
        # Advanced only on success, so a failed beat retries next poll
        # instead of going quiet for a whole window (#96).
        with _persist_lock:
            _persisted_at = time.monotonic()
    except Exception:  # noqa: BLE001 - "never raises" means never, not "never OSError"
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)


def last_known() -> dict[str, Any] | None:
    """The last persisted snapshot, or None. For diagnostics and for the
    window after a restart before anything has been polled."""
    try:
        snapshot = json.loads(RACK_STATE_FILE.read_text(encoding="utf-8"))
        return snapshot if isinstance(snapshot, dict) else None
    except (OSError, ValueError):
        return None


def _dac_tracked() -> tuple[str | None, bool | None, list[str]]:
    """The DAC's input and power as the configured driver reports them.

    Through the registry's shared instance -- the same one the buttons
    drive -- so the snapshot and the walk can never disagree about what
    was last believed. Never raises: a snapshot that cannot read the DAC
    still has a screen to paint.
    """
    try:
        dac = registry.device("dac")
        return dac.current, dac.power, list(dac.inputs)
    except Exception:  # noqa: BLE001 - the screen still has to paint
        return None, None, []


def _inferred_scene(devices: dict[str, "DeviceState"]) -> str | None:
    """"music" is a claim about the whole chain being ready -- TV on, amp
    on, DAC on AND passing USB -- so a half-woken rack says nothing instead
    of pretending. Everything down is "off"; anything else has no name
    worth inventing. (The panel's app.js draws the same inference; these
    two must stay twins.)"""
    tv, amp, dac = devices["tv"], devices["amp"], devices["dac"]
    if (tv.power is True and amp.power is True and dac.power is True
            and dac.fields.get("input") == "usb"):
        return "music"
    if tv.power is False and amp.power is False and dac.power is False:
        return "off"
    return None


def snapshot() -> dict[str, Any]:
    """One object with everything the screen shows."""
    tv_live = tvlink.safe_state()
    amp_live = amplink.safe_state()
    music_live = musiclink.safe_state()
    dac_input, dac_power, dac_cycle = _dac_tracked()

    devices = {
        "tv": DeviceState(
            name="LG OLED77G6",
            transport="SSAP websocket + Wake-on-LAN",
            online=tv_live["online"],
            power=tv_live["power"],
            # picture stays None: readable in bulk (see the owner's private
            # TV picture baseline) but too slow to fetch per poll --
            # webOS 3.0 only answers one key per request.
            fields={"input": tv_live["input"], "picture": None,
                    "speakers": tv_live["sound"]},
            detail=tv_live["detail"],
        ),
        "amp": DeviceState(
            name="McIntosh MAC7200",
            transport="RS-232 115200 8N1",
            online=amp_live["online"],
            power=amp_live["power"],
            fields={"volume": amp_live["volume"], "muted": amp_live["muted"],
                    "input": amp_live["input"]},
            detail=amp_live["detail"],
        ),
        "dac": DeviceState(
            name="Topping D900",
            transport="infrared via the iTach, port 2",
            # Tracked, not read -- like the input. It holds because the DAC
            # only auto-sleeps on a SILENT input, and this rack leaves it on
            # USB, which the mini feeds continuously. Park it elsewhere and
            # the DAC may sleep on its own and this goes stale; Resync is
            # the cure, and trusted=False is the warning.
            power=dac_power,
            fields={"input": dac_input, "cycle": dac_cycle},
            trusted=False,   # every value here is our own bookkeeping
            detail=("tracked" if dac_input
                    else "input not established yet"),
        ),
        "music": DeviceState(
            name="Music.app on the mini",
            transport="AppleScript via osascript",
            online=music_live["online"],
            # power stays None: Music has no meaningful off state worth
            # claiming -- not_running is carried in fields.state instead.
            # This list is a whitelist, so anything safe_state() learns to
            # report is dropped on the floor until it is named HERE too --
            # which is exactly how `queued` reached the phone as undefined
            # while the server had the number all along.
            fields={key: music_live[key]
                    for key in ("state", "track", "artist", "album",
                                "shuffle", "repeat", "position", "duration", "pid",
                                "volume", "muted", "queued", "queue_revision")},
            detail=music_live["detail"],
        ),
    }

    snapshot = {
        # The scene is a CLAIM about the rack, never a memory of a button --
        # the same inference the panel draws in app.js paint(), mirrored
        # here so the widget's status strip (and anything else reading the
        # snapshot) agrees with the panel to the letter.
        "scene": _inferred_scene(devices),
        "devices": {key: asdict(value) for key, value in devices.items()},
        "implemented": [],  # filled in by the route, from the command table
    }
    _persist(snapshot)
    return snapshot


# --- the background poller -------------------------------------------------
#
# One thread reads the rack on a beat; every HTTP request reads the cache.
# Before this, EVERY /api/state paid the full snapshot -- the TV probe, the
# amp QRY, the osascript -- serially, per phone, per 15s poll. The devices
# answer the same questions either way; now they answer them once.
#
# Commands poke() the poller instead of waiting out the beat, so the screen
# still repaints promptly after a button press -- and the SSE route waits on
# the same condition, which is what makes "the phone stops asking" possible.

_cache_cond = threading.Condition()
_cached: dict[str, Any] | None = None
_cached_at = 0.0        # monotonic; 0 means never
_cache_seq = 0          # bumps once per completed poll
_poke = threading.Event()
_poller_stop = threading.Event()
_poller_thread: threading.Thread | None = None
_poller_lock = threading.Lock()

# Callbacks fired (from the poller thread) after each published snapshot.
# For async consumers -- the SSE route bridges these into its event loop with
# call_soon_threadsafe, so a stream costs a coroutine, not a pinned worker.
_listeners: list[Callable[[], None]] = []


def add_listener(callback: Callable[[], None]) -> None:
    """Register a nullary callback run after every completed poll.

    Called from the poller thread: it must be cheap and must not raise
    meaningfully (exceptions are swallowed so one bad listener cannot
    stall the beat)."""
    with _cache_cond:
        _listeners.append(callback)


def remove_listener(callback: Callable[[], None]) -> None:
    with _cache_cond:
        with contextlib.suppress(ValueError):
            _listeners.remove(callback)


def start_poller() -> None:
    """Idempotent; called from the app's lifespan."""
    global _poller_thread
    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return
        _poller_stop.clear()
        _poller_thread = threading.Thread(
            target=_poll_forever, name="avctl-poller", daemon=True)
        _poller_thread.start()


def stop_poller() -> None:
    with _poller_lock:
        _poller_stop.set()
        _poke.set()     # wake it so it can notice
    # Wake anyone blocked in wait_for_change so shutdown does not have to
    # out-wait their timeouts (the feeder joins with a 5s budget).
    with _cache_cond:
        _cache_cond.notify_all()


def poke() -> None:
    """A command just ran; read the rack now rather than at the next beat."""
    _poke.set()


def _poll_forever() -> None:
    global _cached, _cached_at, _cache_seq
    while not _poller_stop.is_set():
        try:
            snap = snapshot()
        except Exception:  # noqa: BLE001 - a bad poll is a skipped beat, not a dead poller
            snap = None
        if snap is not None:
            with _cache_cond:
                _cached = snap
                _cached_at = time.monotonic()
                _cache_seq += 1
                _cache_cond.notify_all()
                watchers = list(_listeners)
            for callback in watchers:
                with contextlib.suppress(Exception):
                    callback()
        _poke.wait(settings.POLL_INTERVAL)
        _poke.clear()


def latest() -> tuple[dict[str, Any] | None, int, float]:
    """The cached snapshot, its sequence number, and its age in seconds."""
    with _cache_cond:
        if _cached is None:
            return None, 0, 0.0
        return _cached, _cache_seq, time.monotonic() - _cached_at


def wait_for_change(seen_seq: int, timeout: float
                    ) -> tuple[dict[str, Any] | None, int]:
    """Block until a poll newer than `seen_seq` lands, or the timeout does.

    Returns the latest snapshot either way; the caller compares sequence
    numbers to tell an update from a heartbeat.
    """
    with _cache_cond:
        # `_cached is None` matters as much as the seq compare: before the
        # first poll lands, seq is 0 while callers start at -1, and skipping
        # the wait turned this into a busy spin for every consumer (#92).
        if _cached is None or _cache_seq == seen_seq:
            _cache_cond.wait(timeout)
        return _cached, _cache_seq
