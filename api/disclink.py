"""The sync seam for the Blu-ray player, read by the state snapshot.

Far simpler than tvlink: the player speaks stateless HTTP, so there is no
persistent socket and no background loop -- just a reused session behind a
module singleton. Status (REVIEW) and playback time (PST) need no auth and
are all that is wired today; control commands require a 32-char player key
that is not derivable from the on-screen credentials (confirmed 2026-08-03,
254 derivations tried) and will come via an IR blaster instead.
"""

from __future__ import annotations

import ipaddress
import threading
from typing import Any

from devices import config as device_config
from devices.bluray import BlurayError, PanasonicUB820

_LOCK = threading.Lock()
_PLAYER: PanasonicUB820 | None = None


def _player() -> PanasonicUB820 | None:
    global _PLAYER
    with _LOCK:
        if _PLAYER is None:
            config = device_config.load_config()
            subnet = config.get("network", {}).get("av_subnet")
            broadcast = "255.255.255.255"
            try:
                broadcast = str(ipaddress.ip_network(str(subnet)).broadcast_address)
            except ValueError:
                pass
            try:
                _PLAYER = PanasonicUB820.from_config(config, broadcast=broadcast)
            except (ValueError, KeyError):
                return None  # bluray.host still unset -- nothing to talk to
        return _PLAYER


def safe_state() -> dict[str, Any]:
    """Player facts for the snapshot. Degrades to unknowns, never raises."""
    player = _player()
    if player is None:
        return {"online": None, "power": None, "playing": None,
                "detail": "no host configured", "can_control": False}
    try:
        state = player.status()          # None when off/unreachable
    except BlurayError:
        return {"online": False, "power": None, "playing": None,
                "detail": "unreachable", "can_control": player.can_control}

    if state is None or state == "off":
        return {"online": True, "power": False, "playing": False,
                "detail": "off", "can_control": player.can_control}
    return {
        "online": True,
        "power": True,
        "playing": state == "playing",
        "detail": state,
        "can_control": player.can_control,
    }
