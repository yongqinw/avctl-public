"""The sync side of the TV: what command handlers and the state snapshot call.

The routes run in worker threads and speak plain functions; the TV speaks an
async websocket that is worth keeping open between calls. This module owns the
seam: one background event loop, one persistent LGTelevision, and sync
wrappers that schedule onto it. Handlers stay `Callable[[dict], dict]` and
know nothing about asyncio.

Two measured facts (2026-08-02) drive the shape of this file:

* **Standby refuses fresh registrations.** With Quick Start+ on, port 3000
  keeps accepting TCP in standby, but a new SSAP handshake is closed with
  websocket code 1008. A connection made while the TV was up keeps working
  after it drops to standby. Hence: the connection is kept rather than made
  per call, "TCP accepts but registration refused" is *recognised* as
  standby, and power-on sends the magic packet before trying to connect,
  not after a connect that cannot succeed.

* **WoL is the only power-on.** SSAP cannot light the panel, so waking is a
  magic packet followed by polling until the set reports Active -- measured
  at ~8s, given 45 because a long-idle set is slower.
"""

from __future__ import annotations

import asyncio
import ipaddress
import threading
from typing import Any, Callable

import aiohttp
from aiohttp.client_exceptions import WSMessageTypeError
from aiowebostv.exceptions import WebOsTvError

from devices import config as device_config
from devices import registry
from devices.blaster import BlasterError, ITachIR
from devices.tv import STATE_ACTIVE, NotConnectedError, PairingRequired

from wakeonlan import wake

SSAP_PORT = 3000
WAKE_TIMEOUT = 45.0
# Extra patience after the IR fallback fires: a deep-standby set boots
# slower than one dozing under Quick Start+.
IR_WAKE_EXTRA = 25.0
# How long an LG may keep reporting Active after being told to sleep. Only
# ever waited out when this app is the one that sent it to standby.
OFF_SETTLE = 12.0

# The standby pacemaker. MEASURED 2026-08-03: after ~a day of standby with no
# network traffic at all, this set goes deaf to Wake-on-LAN (45s of
# 2s-interval magic packets, silence; the physical remote worked first
# press). But a standby TV keeps port 3000 accepting TCP, and accepting a
# connection is traffic -- so the mini, which never sleeps, touches that
# socket every couple of minutes to keep the TV's network stack out of its
# deep nap. A TCP touch does NOT light the panel (measured back on
# 2026-08-02: connects are accepted in standby all day long). Unproven over
# multi-day standby until it has run for one, but it is the only lever that
# exists without IR hardware.
KEEPALIVE_INTERVAL = 120.0

# Failures that mean "the TV did not do it", as opposed to a caller error.
# The route maps RuntimeError to 502, so everything here is folded into it.
# WSMessageTypeError is named separately: it subclasses TypeError, not
# ClientError, and unlisted it surfaced standby's ws-close-1008 refusals as
# raw 500s (seen in the deploy log 2026-08-03). NotConnectedError is our own
# device's "the link dropped between check and call" -- downstream by
# definition, and listed so a concurrent _drop shows up as a clean 502
# rather than whatever shape the raw exception happens to have.
_DOWNSTREAM = (WebOsTvError, aiohttp.ClientError, WSMessageTypeError,
               OSError, asyncio.TimeoutError, NotConnectedError,
               PairingRequired)


class _Link:
    """One TV, one loop, one connection. Module-level singleton below."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._tv = None   # the registry's Tv, once built
        self._connect_lock: asyncio.Lock | None = None
        self._broadcast = "255.255.255.255"
        # When we last told the TV to sleep. Loop time, so it survives
        # nothing but the process -- which is fine: the window it guards is
        # seconds long. See _power_on for why it exists.
        self._slept_at = 0.0
        self._keepalive_started = False

    # -- plumbing --------------------------------------------------------

    def _run(self, coro, timeout: float) -> dict[str, Any]:
        """Schedule onto the background loop and wait, normalising failure."""
        with self._start_lock:
            if self._loop is None:
                # Lazily started so importing this module (from scripts, or
                # from state.py in a test) does not spawn a thread.
                self._loop = asyncio.new_event_loop()
                threading.Thread(
                    target=self._loop.run_forever, name="tvlink", daemon=True
                ).start()
            if not self._keepalive_started:
                # Rides the same lazy start: the first real use of the TV
                # (the state poll at service boot) is what turns the
                # pacemaker on, and it runs for the life of the service.
                self._keepalive_started = True
                asyncio.run_coroutine_threadsafe(
                    self._keepalive_loop(), self._loop
                )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise RuntimeError(f"TV did not answer within {timeout:.0f}s")
        except _DOWNSTREAM as exc:
            raise RuntimeError(str(exc) or type(exc).__name__)

    async def _get_tv(self):
        if self._tv is None:
            config = device_config.load_config()
            subnet = config.get("network", {}).get("av_subnet")
            try:
                self._broadcast = str(
                    ipaddress.ip_network(str(subnet)).broadcast_address
                )
            except ValueError:
                pass  # keep the limited broadcast; WoL still reaches the LAN
            self._tv = registry.device("tv", broadcast=self._broadcast)
            self._connect_lock = asyncio.Lock()
        return self._tv

    async def _ensure(self):
        tv = await self._get_tv()
        async with self._connect_lock:
            if not tv.connected:
                await tv.connect()
        return tv

    async def _drop(self) -> None:
        """Forget a connection that misbehaved; the next call redials.

        Under the same lock as _ensure: a drop landing in the middle of
        another coroutine's connect tears down the websocket it is busy
        registering, and the two commands then fail each other for no
        reason either one could see.
        """
        if self._tv is None or self._connect_lock is None:
            return
        async with self._connect_lock:
            # Unconditional, not gated on `connected` (#111): a client whose
            # websocket dropped reads as not-connected while its heartbeat
            # task still runs -- exactly the client that most needs the
            # disconnect. disconnect() on nothing is a no-op.
            try:
                await self._tv.disconnect()
            except Exception:  # noqa: BLE001 - already being discarded
                pass

    async def _keepalive_loop(self) -> None:
        """Touch the TV's SSAP port forever, so standby never deepens into
        the WoL-deaf sleep. Every failure is swallowed: an unplugged or
        hard-off TV makes this a no-op loop, never a crash, and it simply
        resumes mattering when the TV is back."""
        while True:
            try:
                tv = await self._get_tv()
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(tv.host, SSAP_PORT), 3.0
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
            except Exception:  # noqa: BLE001 - the loop must outlive anything
                pass
            await asyncio.sleep(KEEPALIVE_INTERVAL)

    async def _ssap_port_accepts(self) -> bool:
        """TCP-level probe. Registration refused + port open = standby."""
        tv = await self._get_tv()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(tv.host, SSAP_PORT), 1.5
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    # -- operations ------------------------------------------------------

    async def _state(self) -> dict[str, Any]:
        """Never raises: the screen must paint something whatever the TV does."""
        try:
            tv = await self._ensure()
            power = await tv.power_state()
            on = power == STATE_ACTIVE
            # Input and sound are only read when the panel is up: a standby
            # set answers them with leftovers from before it went down.
            inp = _short(await tv.current_input()) if on else None
            sound = (await tv.sound_output()) if on else None
            return {"online": True, "power": on, "input": inp, "sound": sound,
                    "detail": "on" if on else power.lower()}
        except PairingRequired:
            await self._drop()
            return {"online": True, "power": None, "input": None,
                    "sound": None, "detail": "pairing required"}
        except Exception:  # noqa: BLE001 - every failure becomes a readout
            await self._drop()
            if await self._ssap_port_accepts():
                return {"online": True, "power": False, "input": None,
                        "sound": None, "detail": "standby"}
            return {"online": False, "power": None, "input": None,
                    "sound": None, "detail": "unreachable"}

    async def _power_on(self) -> dict[str, Any]:
        tv = await self._get_tv()
        loop = asyncio.get_running_loop()

        # A TV told to sleep a moment ago still answers "Active" while the
        # panel makes the trip. Believing that skips the wake entirely and
        # leaves the set dark while the scene claims success -- seen doing
        # exactly that on 2026-08-07, an Off scene followed straight by a
        # Music one. So inside the settle window, wait for the set to admit
        # it is going down before treating anything it says as the truth.
        elapsed = loop.time() - self._slept_at
        if elapsed < OFF_SETTLE:
            until = loop.time() + (OFF_SETTLE - elapsed)
            while loop.time() < until:
                try:
                    if not await (await self._ensure()).is_on():
                        break      # down for real; wake it below
                except Exception:  # noqa: BLE001 - mid-transition refusals
                    await self._drop()
                    break
                await asyncio.sleep(1)
            self._slept_at = 0.0

        started = loop.time()
        deadline = WAKE_TIMEOUT
        ir_fired = False
        while True:
            # Re-sent every round: it is one UDP datagram, and a lost first
            # packet otherwise turns into the full 45s failure.
            wake(tv.mac, host=self._broadcast)
            try:
                tv = await self._ensure()
                if await tv.is_on():
                    took = loop.time() - started
                    if ir_fired:
                        return {"message": f"TV awake via IR ({took:.0f}s) -- "
                                "deep standby ignored Wake-on-LAN again"}
                    return {"message": f"TV awake ({took:.0f}s)"
                            if took >= 1 else "TV already on"}
            except Exception:  # noqa: BLE001 - still waking; retry until deadline
                await self._drop()
            if loop.time() - started > deadline:
                if not ir_fired and await self._try_ir_wake():
                    # Deep standby ignores WoL on this set (measured
                    # 2026-08-03 after ~a day asleep); the iTach fires the
                    # power code the physical remote would. Safe though IR
                    # power is a toggle: the whole window above just proved
                    # the TV is not on. Deep standby also boots slower,
                    # hence the extra patience.
                    ir_fired = True
                    deadline += IR_WAKE_EXTRA
                    continue
                raise RuntimeError(
                    f"TV did not wake within {deadline:.0f}s -- "
                    + ("Wake-on-LAN and the IR blaster both tried"
                       if ir_fired else
                       "and no IR fallback is configured (blaster.host + "
                       "blaster.codes.tv_power in config.yaml)")
                )
            await asyncio.sleep(2)

    async def _try_ir_wake(self) -> bool:
        """Fire the TV power code through the iTach, if one is configured.

        Never raises -- the caller has a better error to report than
        anything that happens in here. The blaster exchange is one small
        TCP roundtrip, pushed off this loop so the keepalive and state
        polls never wait on it.
        """
        config = device_config.load_config()
        code = (config.get("blaster") or {}).get("codes", {}).get("tv_power")
        blaster = ITachIR.from_config(config)
        if blaster is None or not code or code == "TODO":
            return False
        port = (config.get("blaster") or {}).get("ports", {}).get("tv")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: blaster.send(str(code),
                                           port if isinstance(port, int) else None))
            return True
        except (BlasterError, ValueError):
            return False

    async def _power_off(self) -> dict[str, Any]:
        tv = await self._ensure()
        if not await tv.is_on():
            return {"message": "TV already in standby"}
        await tv.power_off()
        self._slept_at = asyncio.get_running_loop().time()
        return {"message": "TV off"}

    async def _set_input(self, input_id: str) -> dict[str, Any]:
        tv = await self._ensure()
        if not await tv.is_on():
            # Deliberately not an implicit power-on: an input key that also
            # wakes the rack is a scene, and scenes say so on the button.
            raise RuntimeError("TV is in standby -- wake it first")
        switched = await tv.select_input(input_id)
        return {"message": f"TV to {input_id}"
                if switched else f"already on {input_id}"}

    async def _press(self, button: str) -> dict[str, Any]:
        tv = await self._ensure()
        await tv.press(button)
        return {}

    async def _speakers_off(self) -> dict[str, Any]:
        tv = await self._ensure()
        drifted = await tv.enforce_external_audio()
        return {"message": "audio pushed back to the optical out"
                if drifted else "already external -- nothing to fix"}


_LINK = _Link()

Handler = Callable[[dict[str, Any]], dict[str, Any]]


# -- what commands.py wires up -------------------------------------------


def power_on(args: dict[str, Any]) -> dict[str, Any]:
    # Derived, not a literal: the outer deadline must outlive the coroutine's
    # own worst case -- OFF_SETTLE, the full WoL wait, AND the IR-fallback
    # extension -- so the specific message from inside (not a generic
    # timeout) is the one that reaches the phone. A hardcoded 60 drifted
    # under the ~82s IR path and cancelled the wake mid-boot (#101).
    return _LINK._run(_LINK._power_on(),
                      timeout=OFF_SETTLE + WAKE_TIMEOUT + IR_WAKE_EXTRA + 5)


def power_off(args: dict[str, Any]) -> dict[str, Any]:
    return _LINK._run(_LINK._power_off(), timeout=20)


def speakers_off(args: dict[str, Any]) -> dict[str, Any]:
    return _LINK._run(_LINK._speakers_off(), timeout=20)


def set_input(input_id: str) -> Handler:
    """Handler factory: one per HDMI key in the command table."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        return _LINK._run(_LINK._set_input(input_id), timeout=20)
    return handler


def press(button: str) -> Handler:
    """Handler factory for the nav cluster."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        return _LINK._run(_LINK._press(button), timeout=10)
    return handler


# -- what state.py reads --------------------------------------------------


def safe_state() -> dict[str, Any]:
    """TV facts for the snapshot. Degrades to unknowns, never raises."""
    try:
        return _LINK._run(_LINK._state(), timeout=12)
    except Exception as exc:  # noqa: BLE001 - the screen still has to paint
        return {"online": None, "power": None, "input": None, "sound": None,
                "detail": f"state read failed: {exc}"}


def _short(value: str | None) -> str | None:
    """App ids to the tokens the UI keys on: com.webos.app.hdmi1 -> hdmi1.

    The buttons are data-cmd="tv.input.hdmi1", and the browser highlights by
    comparing its suffix to this value -- so the shape here is part of the
    UI contract, not a cosmetic choice.
    """
    if not value:
        return None
    if value.startswith("HDMI_"):
        return "hdmi" + value[len("HDMI_"):]
    if value.startswith("com.webos.app."):
        return value[len("com.webos.app."):]
    return value
