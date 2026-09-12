"""Panasonic DP-UB820 control over its HTTP CGI.

Unlike the TV, this is stateless HTTP -- there is no socket worth keeping, so
each command is a fresh request and there is no connection lifecycle to manage.

Measured on the actual player (2026-08-03), and each fact shaped the code:

* The control interface only exists when **Voice Control** is enabled in the
  player's network menu. With it off the player pings but every port is
  closed -- there is simply no HTTP server running.

* Every request needs `User-Agent: MEI-LAN-REMOTE-CALL`. Without it the CGI
  returns an empty HTML shell instead of a response, which looks like a
  working endpoint doing nothing.

* Two tiers of command:
  - **Status** (REVIEW) and **playback time** (PST) work unauthenticated.
  - **Control** (power, transport, nav, eject) requires a per-request
    signature. On stock firmware an unauthenticated control command returns
    `52,"認証に失敗しました"` -- "authentication failed".

* The signature is `SHA-256(player_key + nonce)` uppercased, where the nonce
  is fetched per command from get_nonce.cgi and the 32-char player_key is
  obtained by pairing (it is not the device id or password shown on screen).
  cAUTH_FORM is the key's first 2 chars if it contains a '2', else the first
  3 -- a quirk carried from the openHAB binding, matched here for safety.

* Power-on is Wake-on-LAN then RC_POWERON, because the HTTP server is not
  guaranteed up from deep standby. Needs "Remote Start" enabled on the player.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

import requests

from wakeonlan import wake

from devices.config import pad_mac

NONCE_PATH = "/cgi-bin/get_nonce.cgi"
CTRL_PATH = "/WAN/dvdr/dvdr_ctrl.cgi"
USER_AGENT = "MEI-LAN-REMOTE-CALL"
AUTH_FAILED_CODE = "52"

# Alias -> the player's internal RC code. Deliberately excludes the streaming
# app launch keys (Netflix etc.): this rack drives a projector chain, not a
# smart-TV surface, and those buttons would only ever be dead weight.
COMMANDS = {
    "power_on": "RC_POWERON",
    "power_off": "RC_POWEROFF",
    "play": "RC_PLAYBACK",
    "stop": "RC_STOP",
    "pause": "RC_PAUSE",
    "skip_fwd": "RC_SKIPFWD",
    "skip_rev": "RC_SKIPREV",
    "scan_fwd": "RC_CUE",          # fast forward
    "scan_rev": "RC_REV",          # rewind
    "eject": "RC_OP_CL",           # open/close tray (toggle)
    "up": "RC_UP",
    "down": "RC_DOWN",
    "left": "RC_LEFT",
    "right": "RC_RIGHT",
    "ok": "RC_SELECT",
    "return": "RC_RETURN",
    "home": "RC_MLTNAVI",
    "option": "RC_MENU",
    "top_menu": "RC_TITLE",
    "popup_menu": "RC_PUPMENU",
    "info": "RC_DSPSEL",
    "audio": "RC_AUDIOSEL",
    "subtitle": "RC_TITLEONOFF",
    "skip_back_10": "RC_MNBACK",
    "skip_fwd_60": "RC_MNSKIP",
}

# REVIEW status field (index 5) -> human state.
STATUS = {
    "00": "stopped",
    "01": "tray_open",
    "02": "reverse",
    "05": "cue",
    "06": "slow_forward",
    "07": "off",
    "08": "playing",
    "09": "paused",
    "86": "slow_backward",
}

# Control commands that do not need auth. REVIEW/PST are read-only status.
_UNAUTHED = {"REVIEW", "PST"}


class BlurayError(RuntimeError):
    """The player was reachable but rejected or failed the command."""


class AuthRequiredError(BlurayError):
    """A control command needs a player key and none is configured (or it is
    wrong). Status still works, so this is raised only for control."""


class PanasonicUB820:
    def __init__(
        self,
        host: str,
        mac: str | None = None,
        player_key: str | None = None,
        broadcast: str = "255.255.255.255",
        timeout: float = 4.0,
    ):
        self.host = host
        self.mac = pad_mac(mac) if mac else None
        if player_key is not None and len(player_key) != 32:
            raise ValueError(
                f"player_key must be 32 chars, got {len(player_key)}"
            )
        self.player_key = player_key
        self.broadcast = broadcast
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        # One authenticated command at a time (#111): the nonce fetch and
        # the hash that answers it are a two-request transaction, and two
        # threads interleaving them fail each other's auth for no visible
        # reason. (It also keeps the shared Session single-threaded, which
        # requests never promises to tolerate.)
        self._txn_lock = threading.Lock()

    @classmethod
    def from_config(cls, config: dict, broadcast: str = "255.255.255.255"):
        bluray = config["bluray"]
        host = bluray.get("host")
        if not host or host == "TODO":
            raise ValueError("bluray.host is not set in the owner config")
        key = bluray.get("player_key")
        # A blank/placeholder key means "not paired yet" -- treat as absent so
        # status still works and only control raises.
        if not key or key == "TODO" or len(str(key)) != 32:
            key = None
        return cls(host=host, mac=bluray.get("mac"), player_key=key,
                   broadcast=broadcast)

    @property
    def can_control(self) -> bool:
        """True when authenticated control is possible (key present)."""
        return self.player_key is not None

    # -- raw protocol ----------------------------------------------------

    def _post(self, path: str, data: dict[str, str]) -> str:
        try:
            resp = self._session.post(
                f"http://{self.host}{path}", data=data, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise BlurayError(f"{self.host} unreachable: {exc}") from exc
        # The CGI answers 200 even for auth failures; the body carries the
        # real status, so decode it as the player does (Shift-JIS).
        return resp.content.decode("shift_jis", errors="replace")

    def _nonce(self) -> str:
        nonce = self._post(NONCE_PATH, {"SID": "AVCTL"}).strip()
        if not nonce:
            raise BlurayError("player returned an empty nonce")
        return nonce

    def _command(self, code: str) -> str:
        data = {f"cCMD_{code}.x": "100", f"cCMD_{code}.y": "100"}

        with self._txn_lock:
            if code not in _UNAUTHED:
                if self.player_key is None:
                    raise AuthRequiredError(
                        f"{code} needs a player key -- pair the player and "
                        "set bluray.player_key in config.yaml"
                    )
                nonce = self._nonce()
                key = self.player_key
                data["cAUTH_FORM"] = key[:2] if "2" in key else key[:3]
                data["cAUTH_VALUE"] = hashlib.sha256(
                    (key + nonce).encode()).hexdigest().upper()

            body = self._post(CTRL_PATH, data)
        if body.split(",", 1)[0] == AUTH_FAILED_CODE:
            raise AuthRequiredError(
                f"{code} rejected: player key missing or wrong "
                "(player said authentication failed)"
            )
        return body

    # -- status (no auth needed) -----------------------------------------

    def status(self) -> str | None:
        """Current transport state, e.g. 'playing' / 'stopped' / 'tray_open'.

        None if the player is off or the field is unrecognised.
        """
        parts = self._command("REVIEW").split(",")
        if len(parts) <= 5:
            return None
        return STATUS.get(parts[5])

    def play_position(self) -> int | None:
        """Elapsed playback time in seconds, or None if not playing.

        PST skips auth, so this is the cheap poll -- one request, no nonce.
        """
        parts = self._command("PST").split(",")
        if len(parts) < 4:
            return None
        try:
            return int(parts[3])
        except ValueError:
            return None

    def is_on(self) -> bool:
        return self.status() not in (None, "off")

    # -- control (auth needed) -------------------------------------------

    def send(self, alias: str) -> str:
        """Send a command by alias (see COMMANDS)."""
        code = COMMANDS.get(alias)
        if code is None:
            raise ValueError(f"unknown command {alias!r}")
        return self._command(code)

    def power_on(self) -> None:
        """Wake (WoL) then power on. WoL needs 'Remote Start' on the player."""
        if self.mac:
            wake(self.mac, host=self.broadcast)
        # RC_POWERON needs auth; if unpaired, WoL alone may still wake it from
        # network standby, so swallow the auth error rather than fail outright.
        try:
            self.send("power_on")
        except AuthRequiredError:
            if not self.mac:
                raise
