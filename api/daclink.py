"""The sync seam for the DAC.

The thinnest seam of the lot: one singleton built from config, and
handlers that speak the Dac interface -- facts (current, power), verbs
(select, step, power_to) and the open-loop corrections (resync). What the
DAC actually is, and how it is driven, lives entirely under devices/dac/;
this module turns interface answers into messages for the phone.

"(tracked)" in the messages is the honesty marker for an open-loop
device: every value is our own bookkeeping, never a readback.
"""

from __future__ import annotations

from typing import Any

from devices import registry


def _dac():
    return registry.device("dac")


def input_next(args: dict[str, Any]) -> dict[str, Any]:
    landed = _dac().step()
    if landed is None:
        return {"message": "stepped the D900 input (position unknown -- "
                           "resync when you can see the panel)"}
    return {"message": f"D900 to {landed.upper()} (tracked)"}


def _select(target: str) -> dict[str, Any]:
    presses = _dac().select(target)
    if presses == 0:
        return {"message": f"D900 already on {target.upper()} (tracked)"}
    return {"message": f"D900 to {target.upper()} -- {presses} press"
                       f"{'es' if presses > 1 else ''} (tracked)"}


def select(target: str):
    """Handler factory, tvlink.press style: bind the target at table time."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        return _select(target)
    return handler


def power_toggle(args: dict[str, Any]) -> dict[str, Any]:
    """The manual key: flip the DAC and the belief about it together."""
    now = _dac().power_toggle()
    if now is None:
        return {"message": "D900 toggled, but its power was never "
                           "established -- say which it is with Resync"}
    return {"message": f"D900 {'on' if now else 'off'} (tracked)"}


def _power_to(target: bool) -> dict[str, Any]:
    """Drive power to a known state, or refuse rather than gamble -- see
    Dac.power_to for why None means "decline and ask"."""
    result = _dac().power_to(target)
    if result is False:
        return {"message": f"D900 already {'on' if target else 'off'}"}
    if result is None:
        return {"message": "D900 power unknown -- Resync it and this "
                           "becomes automatic"}
    return {"message": f"D900 {'on' if target else 'off'}"}


def power_on(args: dict[str, Any]) -> dict[str, Any]:
    return _power_to(True)


def power_off(args: dict[str, Any]) -> dict[str, Any]:
    return _power_to(False)


def power_resync(args: dict[str, Any]) -> dict[str, Any]:
    """Declare the DAC's true power, read off the front panel."""
    on = bool(args.get("on"))
    _dac().power_resync(on)
    return {"message": f"noted: the D900 is {'on' if on else 'off'}"}


def resync(args: dict[str, Any]) -> dict[str, Any]:
    """Pure bookkeeping: nothing is sent, only the belief is corrected.
    The one honest answer to an open-loop device."""
    target = str(args.get("input", "")).lower()
    _dac().resync(target)   # raises ValueError on junk -> the route answers 400
    return {"message": f"noted: the D900 is on {target.upper()}"}


def to_usb(args: dict[str, Any]) -> dict[str, Any]:
    """The safe harbour, and the music scene's DAC step.

    USB is the one input that always has signal (the mini never stops
    feeding it), so arriving there both wakes the DAC from auto-standby and
    puts it where the music comes from. Never fails a scene: with the input
    unestablished the presses cannot be counted, and saying so is better
    than throwing the rest of the scene away.
    """
    dac = _dac()
    powered = power_on({})
    if dac.current is None:
        return {"message": powered["message"] + "; input unknown -- tap "
                           "Resync with what the panel reads"}
    switched = _select("usb")
    return {"message": f"{powered['message']}, {switched['message']}"}
