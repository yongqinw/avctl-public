"""The sync seam for the MAC7200, read by the state snapshot.

Closer to disclink than tvlink: devices/amp.py is already synchronous and
keeps its own reader thread, so this module only holds the singleton, folds
failures into readouts, and maps between the amp's numeric input ids and the
`amp.input.<name>` suffixes the UI keys on.

The name map comes from the transcribed PDF and is UNVERIFIED against this
unit beyond ids 1 and 2 existing; the one id that matters -- which input the
D900's balanced pair is assigned to -- comes from config (amp.dac_input_id)
and wins over the PDF name, so the accented D900 key highlights when the amp
reports it.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from devices import config as device_config
from devices import registry
from devices.amp import AmpError

Handler = Callable[[dict[str, Any]], dict[str, Any]]

# PDF input table (configs/mac7200_protocol.yaml `inputs:`) -- names match
# the amp.input.* command suffixes in views.py.
_INPUT_NAMES = {1: "mc", 2: "mm", 3: "cd1", 4: "cd2", 5: "dvd",
                6: "aux", 7: "server", 8: "d2a", 9: "tuner"}

VOL_STEP = 2  # per press; hold-repeat in the UI does the acceleration


def max_volume() -> int:
    """The safety ceiling shared by buttons, scenes, and the AI agent."""
    value = device_config.load_config().get("amp", {}).get("max_volume", 70)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise RuntimeError("amp.max_volume must be an integer 0-100")
    return value


def _capped(level: int | float) -> tuple[int, bool]:
    requested = int(level)
    capped = max(0, min(max_volume(), requested))
    return capped, capped != requested


def _set_volume(level: int | float) -> tuple[int, bool]:
    target, limited = _capped(level)
    return _require().set_volume(target), limited


def _amp():
    try:
        return registry.device("amp")
    except (ValueError, KeyError, FileNotFoundError, OSError):
        return None  # amp.port still unset -- nothing to talk to


def _require():
    amp = _amp()
    if amp is None:
        raise RuntimeError("amp.port is not configured in config.yaml")
    return amp


def dac_input_id() -> int | None:
    """Which (INP n) the D900's balanced pair is assigned to, from config."""
    try:
        value = device_config.load_config().get("amp", {}).get("dac_input_id")
    except (FileNotFoundError, OSError):
        return None
    return value if isinstance(value, int) else None


def _input_name(input_id: int | None) -> str | None:
    if input_id is None:
        return None
    if input_id == dac_input_id():
        return "dac"
    return _INPUT_NAMES.get(input_id)


# -- what commands.py wires up ---------------------------------------------
# AmpError is a RuntimeError, so it already reaches the phone as a 502 with
# the amp's own explanation; only ValueError needs no translation either.


def power_on(args: dict[str, Any]) -> dict[str, Any]:
    changed = _require().power_on()
    return {"message": "amp on -- settles for ~10s" if changed
            else "amp already on"}


def power_off(args: dict[str, Any]) -> dict[str, Any]:
    changed = _require().power_off()
    return {"message": "amp off" if changed else "amp already in standby"}


def power_toggle(args: dict[str, Any]) -> dict[str, Any]:
    """One key for both directions -- safe because the amp's power is read
    over RS-232 before deciding, never guessed."""
    if safe_state().get("power"):
        return power_off(args)
    return power_on(args)


def vol_up(args: dict[str, Any]) -> dict[str, Any]:
    amp = _require()
    state = amp.query()
    if state.get("PWR") != 1 or "VOL" not in state:
        raise AmpError("cannot set volume while amp is in standby")
    level, limited = _set_volume(int(state["VOL"]) + VOL_STEP)
    suffix = f" (limited to {max_volume()})" if limited else ""
    return {"message": f"volume {level}{suffix}"}


def vol_down(args: dict[str, Any]) -> dict[str, Any]:
    amp = _require()
    state = amp.query()
    if state.get("PWR") != 1 or "VOL" not in state:
        raise AmpError("cannot set volume while amp is in standby")
    level, limited = _set_volume(int(state["VOL"]) - VOL_STEP)
    suffix = f" (limited to {max_volume()})" if limited else ""
    return {"message": f"volume {level}{suffix}"}


def vol_set(args: dict[str, Any]) -> dict[str, Any]:
    level = args.get("level")
    if not isinstance(level, (int, float)) or isinstance(level, bool):
        raise ValueError("args.level must be a number 0-100")
    actual, limited = _set_volume(level)
    suffix = f" (limited to {max_volume()})" if limited else ""
    return {"message": f"volume {actual}{suffix}"}


def _preset_level(name: str) -> int:
    presets = device_config.load_config().get("amp", {}).get("presets", {})
    level = presets.get(name)
    if not isinstance(level, int):
        raise RuntimeError(f"amp.presets.{name} is not set in config.yaml")
    return level


def vol_preset(name: str) -> Handler:
    """Handler factory for the level presets; the numbers live in config."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        level, limited = _set_volume(_preset_level(name))
        suffix = f" (limited to {max_volume()})" if limited else ""
        return {"message": f"{name} level: volume {level}{suffix}"}
    return handler


def on_at_music_level(args: dict[str, Any]) -> dict[str, Any]:
    """The music scene's amp step: power on, then the music preset level.

    A fresh power-on gets the ~10s settle before the volume set -- mid-boot
    the amp can still answer QRY with (PWR 0) (measured 2026-08-03), which
    set_volume would misread as standby. Already-on skips straight to the
    level; the retry covers a boot that ran long.
    """
    amp = _require()
    level, limited = _capped(_preset_level("music"))
    changed = amp.power_on()
    if changed:
        time.sleep(10)
    try:
        amp.set_volume(level)
    except AmpError:
        if not changed:
            raise
        time.sleep(4)
        amp.set_volume(level)
    suffix = f" (limited to {max_volume()})" if limited else ""
    return {"message": (f"amp on at volume {level}" if changed
                         else f"amp already on, volume {level}") + suffix}


def mute(args: dict[str, Any]) -> dict[str, Any]:
    return {"message": "amp muted" if _require().toggle_mute()
            else "amp unmuted"}


def set_mute(muted: bool) -> Handler:
    """Build an idempotent mute handler for intent-bearing callers."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        actual = _require().set_mute(muted)
        return {"message": "amp muted" if actual else "amp unmuted"}
    return handler


def set_input(input_id: int | None = None) -> Handler:
    """Handler factory: one per input key. None means "the D900's input,
    whatever config currently says it is"."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        target = input_id if input_id is not None else dac_input_id()
        if target is None:
            raise RuntimeError(
                "amp.dac_input_id is not confirmed in config.yaml -- select "
                "the balanced input on the front panel and read the id back "
                "with the query key"
            )
        name = (_input_name(target) or str(target)).upper()
        switched = _require().set_input(target)
        return {"message": f"amp to {name}" if switched
                else f"amp already on {name}"}
    return handler


def query(args: dict[str, Any]) -> dict[str, Any]:
    """The manual re-read; also how dac_input_id gets confirmed by hand."""
    state = _require().query()
    if state.get("PWR") != 1:
        return {"message": "amp is in standby"}
    name = _input_name(state.get("INP"))
    return {"message": f"amp on -- input {state.get('INP')}"
                       f" ({name or 'unnamed'}), volume {state.get('VOL')}"
                       f"{', muted' if state.get('MUT') == 1 else ''}"}


# -- what state.py reads ---------------------------------------------------


def safe_state() -> dict[str, Any]:
    """Amp facts for the snapshot. Degrades to unknowns, never raises."""
    unknown = {"online": None, "power": None, "volume": None, "muted": None,
               "input": None}
    amp = _amp()
    if amp is None:
        return {**unknown, "detail": "no port configured"}
    try:
        state = amp.query()
    except (AmpError, ValueError) as exc:
        return {**unknown, "online": False, "detail": str(exc)}
    if state.get("PWR") != 1:
        return {**unknown, "online": True, "power": False, "detail": "standby"}
    return {
        "online": True,
        "power": True,
        "volume": state.get("VOL"),
        "muted": state.get("MUT") == 1,
        "input": _input_name(state.get("INP")),
        "detail": "on",
    }
