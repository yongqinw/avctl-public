"""LG OLED77G6 control over webOS SSAP.

Unlike the D900, the TV is authoritative about its own state: SSAP supports
queries and subscriptions, so nothing here has to be guessed or persisted.

Three things measured on the actual set (2026-08-02) shape this module, and
all three are easy to get wrong:

1. The input API is asymmetric. `set_input` takes "HDMI_1"; reading it back
   yields the app id "com.webos.app.hdmi1". Comparing the two directly always
   reports a mismatch, so every read is normalised back to the "HDMI_1" form.

2. Reachability is not power state. Quick Start+ is on, which keeps the
   network stack alive in standby -- port 3000 keeps accepting connections
   with the panel dark. Anything that infers "off" from a refused connection
   is wrong. `get_power_state` is the only honest signal.

3. Powering on needs Wake-on-LAN. The set accepts SSAP in standby but will
   not light the panel from it, so power_on() sends a magic packet and then
   waits for the power state to actually report Active.

This is a 2016 set on webOS 3.0, which has no luna settings bridge --
com.webos.settingsservice/setSystemSettings answers "404 no such service or
method". SIMPLINK/CEC therefore cannot be disabled from here; it is a menu
setting, recorded under manual_prerequisites in config.yaml.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

from aiowebostv import WebOsClient as _UpstreamWebOsClient
from aiowebostv import endpoints as webos_endpoints
from aiowebostv.exceptions import WebOsTvPairError, WebOsTvResponseTypeError

from wakeonlan import wake

from devices.config import pad_mac
from devices.tv import (NotConnectedError, PairingRequired, PowerOffTimeout,
                        PowerOnTimeout, SOUND_OUTPUT_EXTERNAL, SOUND_OUTPUT_TV,
                        STATE_ACTIVE, Tv)

# Reading an input back yields an app id. Everything else -- the home screen,
# Netflix, live TV -- is an app too, and is not an input at all.
APP_ID_PREFIX = "com.webos.app."

# How long the panel is given to come up after a magic packet before we call
# it a failure. Measured wake was a couple of seconds, but a set that has been
# cold for a while is slower, and failing early would be worse than waiting.
POWER_ON_TIMEOUT = 45.0
POWER_OFF_TIMEOUT = 12.0
POWER_POLL_INTERVAL = 2.0
DEFAULT_CLIENT_KEY_FILE = Path("~/.avctl/tv-client-key").expanduser()


def _permission_denied(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return "401" in message and "insufficient permissions" in message


def load_client_key(path: str | os.PathLike[str]) -> str | None:
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def store_client_key(path: str | os.PathLike[str], key: str) -> None:
    """Persist a pairing credential atomically, owner-readable only."""
    destination = Path(path).expanduser()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        destination.parent.chmod(0o700)
    handle, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=".tv-client-key-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(key.strip() + "\n")
        os.replace(temporary, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(handle)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class AvctlWebOsClient(_UpstreamWebOsClient):
    """webOS client compatible with LG's certificate blacklist.

    Current LG firmware rejects the legacy ``com.lge.test`` certificate
    embedded in aiowebostv 0.8.0. The set accepts an application-specific
    manifest without that obsolete signature. Its unsigned permission set
    also lacks optional metadata permissions, so startup skips that eager
    snapshot; LGTelevision reads its required state directly.
    """

    def registration_msg(self) -> dict[str, Any]:
        message = super().registration_msg()
        payload = message.get("payload", {})
        if not self.client_key:
            payload.pop("client-key", None)
        manifest = payload.get("manifest", {})
        manifest["appVersion"] = "1.0"
        manifest.pop("signatures", None)
        # aiowebostv releases disagree on whether an unsigned registration
        # template already contains this mapping. Keep the identity attached
        # to the manifest even when the dependency omitted it entirely.
        signed = manifest.setdefault("signed", {})
        signed.pop("vendorId", None)
        signed.update({
            "created": "20260825",
            "appId": "io.avctl.remote",
            "localizedAppNames": {"": "avctl"},
            "localizedVendorNames": {"": "avctl"},
            "serial": "avctl-webos-2026",
        })
        return message

    async def _get_states_and_subscribe_state_updates(self) -> None:
        # get_software_info() needs a permission LG no longer grants to
        # unsigned LAN remotes. This driver does not consume the cached data.
        return None

    async def power_off(self) -> None:
        """Send shutdown without consulting aiowebostv's disabled cache.

        The upstream implementation returns early when ``tv_state.is_on`` is
        false.  avctl deliberately does not populate that cached state because
        doing so also requests metadata this TV rejects for unsigned remotes.
        The caller has already read the authoritative power endpoint, so send
        the SSAP command directly instead of letting the empty cache swallow
        it.
        """
        await self.command("request", webos_endpoints.POWER_OFF)

# Standby on this set reports "Active Standby" -- which *contains* the
# package's STATE_ACTIVE ("Active") as a prefix (measured 2026-08-02). So
# every comparison in here is exact equality: `startswith` or `in` would
# report a standby TV as on, and a scene built on either would skip the
# power-on step and then set inputs into the dark.

def normalise_input(value: str | None) -> str | None:
    """Turn whatever the TV reports into the id `set_input` would accept.

    "com.webos.app.hdmi1" -> "HDMI_1". Non-input apps are returned unchanged,
    so a caller can still tell that the TV is sitting on the home screen
    rather than on any input at all.
    """
    if not value:
        return None
    if not value.startswith(APP_ID_PREFIX):
        return value
    name = value[len(APP_ID_PREFIX):]
    if name.startswith("hdmi") and name[4:].isdigit():
        return f"HDMI_{name[4:]}"
    return value


class LGTelevision(Tv):
    """One TV. Construct, `await connect()`, issue commands, `await disconnect()`.

    Also usable as an async context manager, which is the safer form -- an
    un-disconnected client leaves a websocket and its heartbeat task running.
    """

    def __init__(
        self,
        host: str,
        client_key: str,
        mac: str,
        inputs: dict[str, str] | None = None,
        broadcast: str = "255.255.255.255",
        client_key_file: str | os.PathLike[str] = DEFAULT_CLIENT_KEY_FILE,
    ):
        self.host = host
        self.client_key = client_key
        self.client_key_file = Path(client_key_file).expanduser()
        # WoL builds a packet from the raw bytes, so the MAC must be fully
        # padded (see pad_mac). The unpadded arp form produces a packet the
        # TV ignores -- which looks exactly like "wake is broken".
        self.mac = pad_mac(mac)
        # Friendly names from config -- {"bluray": "HDMI_1", ...} -- so scenes
        # can say "bluray" and stay readable when something gets re-cabled.
        self.inputs = dict(inputs or {})
        self.broadcast = broadcast
        self._client: AvctlWebOsClient | None = None

    @classmethod
    def from_config(cls, config: dict, broadcast: str = "255.255.255.255"):
        """Build from the merged `tv` configuration. host, mac
        and the input wiring are category facts; the SSAP client_key is
        this driver's own credential and lives in its sub-block."""
        from devices.config import driver_block
        tv = driver_block(config, "tv", "LGTelevision")
        missing = [k for k in ("host", "mac") if not tv.get(k)]
        if missing:
            raise ValueError(f"tv config is missing: {', '.join(missing)}")
        key_file = Path(tv.get("client_key_file")
                        or DEFAULT_CLIENT_KEY_FILE).expanduser()
        return cls(
            host=tv["host"],
            client_key=load_client_key(key_file) or tv.get("client_key") or "",
            mac=tv["mac"],
            inputs=tv.get("inputs"),
            broadcast=broadcast,
            client_key_file=key_file,
        )

    # -- connection ------------------------------------------------------

    async def connect(self) -> None:
        # Close whatever is already attached before building anew (#111):
        # silently overwriting a client leaves its websocket and heartbeat
        # task running forever -- the exact leak the class docstring warns
        # about. tvlink checks `connected` first, but a half-dead client
        # (websocket dropped, tasks alive) reads as not connected and used
        # to be orphaned here.
        if self._client is not None:
            try:
                await self.disconnect()
            except Exception:  # noqa: BLE001 - already being replaced
                pass
        async def open_client(key: str | None) -> AvctlWebOsClient:
            candidate = AvctlWebOsClient(self.host, key)
            try:
                await candidate.connect()
            except BaseException:
                with contextlib.suppress(Exception):
                    await candidate.disconnect()
                raise
            return candidate

        async def request_pairing(cause: BaseException) -> AvctlWebOsClient:
            # A keyless registration is the webOS pairing protocol: it puts
            # an Accept prompt on the panel and returns a fresh client key.
            try:
                return await open_client(None)
            except (WebOsTvPairError, WebOsTvResponseTypeError,
                    asyncio.TimeoutError) as repair_error:
                raise PairingRequired(
                    "TV pairing required; accept the on-screen prompt"
                ) from repair_error

        try:
            client = await open_client(self.client_key or None)
        except WebOsTvResponseTypeError as exc:
            if not self.client_key:
                raise PairingRequired(
                    "TV pairing required; accept the on-screen prompt"
                ) from exc
            if not _permission_denied(exc):
                raise
            # The TV forgot/revoked this client. A no-key registration causes
            # the normal on-screen Accept prompt and returns a replacement.
            client = await request_pairing(exc)
        except (WebOsTvPairError, asyncio.TimeoutError) as exc:
            if self.client_key:
                raise
            raise PairingRequired(
                "TV pairing required; accept the on-screen prompt"
            ) from exc

        replacement = str(client.client_key or "").strip()
        if not replacement:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise PairingRequired(
                "TV pairing required; no replacement credential was issued")
        try:
            if (replacement != self.client_key
                    or load_client_key(self.client_key_file) != replacement):
                store_client_key(self.client_key_file, replacement)
        except OSError as exc:
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise PairingRequired(
                "TV paired, but its credential could not be saved") from exc
        self.client_key = replacement
        self._client = client

    async def disconnect(self) -> None:
        # Swap before closing: if the close itself fails, the dead client must
        # not stay attached, or every later call retries a corpse.
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()

    async def __aenter__(self) -> "LGTelevision":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    @property
    def client(self) -> AvctlWebOsClient:
        if self._client is None:
            raise NotConnectedError("call connect() first")
        return self._client

    @property
    def connected(self) -> bool:
        """A live websocket exists. Says nothing about panel power -- with
        Quick Start+ on, a connection made while the TV was up keeps working
        after it drops to standby."""
        return self._client is not None and self._client.is_connected()

    # -- power -----------------------------------------------------------

    async def power_state(self) -> str:
        """Raw webOS power state, e.g. "Active" or "Suspend"."""
        result = await self.client.get_power_state()
        return result.get("state", "Unknown")

    async def is_on(self) -> bool:
        """True when the panel is actually lit.

        Deliberately not implemented as "can I open a socket" -- with Quick
        Start+ enabled the socket answers in standby, so that test reports
        every standby TV as on.
        """
        return await self.power_state() == STATE_ACTIVE

    async def power_off(self, timeout: float = POWER_OFF_TIMEOUT) -> None:
        await self.client.power_off()
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < timeout:
            await asyncio.sleep(POWER_POLL_INTERVAL)
            if not await self.is_on():
                return
        raise PowerOffTimeout(
            f"{self.host} still reports {STATE_ACTIVE} {timeout:.0f}s after "
            "the shutdown command"
        )

    async def power_on(self, timeout: float = POWER_ON_TIMEOUT) -> float:
        """Wake the panel and wait until it reports Active.

        Returns how long the wake took, in seconds. No-op if already on.
        Raises PowerOnTimeout rather than returning quietly, so a scene does
        not go on to set an input on a TV that never woke.
        """
        if await self.is_on():
            return 0.0

        wake(self.mac, host=self.broadcast)

        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < timeout:
            await asyncio.sleep(POWER_POLL_INTERVAL)
            if await self.is_on():
                return loop.time() - started
        raise PowerOnTimeout(
            f"{self.host} did not report {STATE_ACTIVE} within {timeout:.0f}s "
            f"of a magic packet to {self.mac}"
        )

    # -- inputs ----------------------------------------------------------

    async def current_input(self) -> str | None:
        """Current input as an id `select_input` would accept, or the app id
        if the TV is on something that is not an input at all."""
        return normalise_input(await self.client.get_input())

    def resolve_input(self, name: str) -> str:
        """Map a friendly name from config ("bluray") to an id ("HDMI_1").

        Ids pass through unchanged, so callers may use either.
        """
        if name in self.inputs:
            return self.inputs[name]
        if name.startswith("HDMI_"):
            return name
        known = ", ".join(sorted(self.inputs)) or "none configured"
        raise ValueError(f"unknown input {name!r} -- configured names: {known}")

    async def select_input(self, name: str) -> bool:
        """Switch to `name`. Returns True if a switch was actually issued.

        Reads first and skips the call when already there: re-issuing a switch
        makes the panel blink through the input for no reason, which is
        visible and looks like a fault.
        """
        target = self.resolve_input(name)
        if await self.current_input() == target:
            return False
        await self.client.set_input(target)
        return True

    # -- buttons ---------------------------------------------------------

    async def press(self, button: str) -> None:
        """One remote button over the pointer-input socket: "UP", "DOWN",
        "LEFT", "RIGHT", "ENTER", "BACK", "HOME", "EXIT", "INFO".

        Verified working on this webOS 3.0 set 2026-08-02 -- the separate
        input websocket is granted fine even though the settings service
        is not.
        """
        await self.client.button(button)

    # -- picture ---------------------------------------------------------

    async def read_picture_settings(self, keys: list[str]) -> dict:
        """Read whatever picture settings this firmware exposes.

        webOS 3.0 only answers per-key (an empty key list errors with "keys
        are mandatory"), and unknown keys error rather than skip -- so each
        key is fetched alone and failures are simply absent from the result.
        Used to snapshot the owner's calibration before anything alters it.
        """
        found: dict = {}
        for key in keys:
            try:
                result = await self.client.request(
                    "settings/getSystemSettings",
                    {"category": "picture", "keys": [key]},
                )
            except Exception:  # noqa: BLE001 - absent key, not a failure
                continue
            settings = result.get("settings", {})
            if key in settings:
                found[key] = settings[key]
        return found

    # -- sound -----------------------------------------------------------

    async def sound_output(self) -> str | None:
        result = await self.client.get_sound_output()
        if isinstance(result, dict):
            return result.get("soundOutput")
        return result

    async def enforce_external_audio(
        self, output: str = SOUND_OUTPUT_EXTERNAL
    ) -> bool:
        """Push audio back off the TV speakers. Returns True if it had drifted.

        This is the `tv_speakers: never` invariant. It is not a scene step:
        the LG remote can move audio back to the TV speakers at any moment, so
        it has to be re-checked rather than set once.
        """
        if await self.sound_output() != SOUND_OUTPUT_TV:
            return False
        await self.client.change_sound_output(output)
        return True
