"""Topping D900 input control.

The D900 has no network or serial interface, and its remote exposes a single
button that advances the input by one. So there is no way to command an input
directly, and no way to read the current one back -- the open-loop case
TrackedDac exists for: input modelled as persisted local state, advanced by
the right number of presses.

USB is the resting state worth returning to. It always carries signal from
the mini, and the D900 only auto-sleeps on a silent input, so a DAC left on
USB stays awake and stays put indefinitely -- which makes the tracked value
correct for as long as nobody touches the physical remote. Every scene ends
there for that reason.

This is exact only while nothing else touches the DAC. Reaching for the
physical remote desyncs it, as does any IR press the DAC misses -- and with
eight inputs, a switch can take up to seven presses, so the odds of dropping
one are not negligible. Hence resync(), and hence the note in config.yaml
about adding a passive IR receiver to observe the remote directly.
"""

from __future__ import annotations

import os
from typing import Any

from devices import config as device_config
from devices.blaster import ITachIR
from devices.dac.tracked import TrackedDac


class ToppingD900(TrackedDac):
    # Order the single input button steps through, wrapping at the end.
    CYCLE = ["usb", "opt1", "opt2", "coax1", "coax2", "aes", "i2s",
             "bluetooth"]

    # The DAC needs a beat between presses or it drops them, and a dropped
    # press is the whole reason resync exists -- it desyncs the count
    # silently and the user has to notice and correct it. So this is tuned
    # for reliability, not speed: switches are rare, mistrust is expensive.
    #
    # 0.3s was too fast, and the evidence is indirect but conclusive: a
    # 3-press walk to USB at 0.3s was followed minutes later by the DAC
    # auto-sleeping -- which it cannot do on USB while the mini is feeding
    # it signal (owner, 2026-08-07). It therefore never arrived. At 2.0s a
    # full 8-press cycle landed every press. 0.8s sits between, and even
    # the worst case -- seven presses from OPT1 back to USB -- costs under
    # six seconds.
    PRESS_INTERVAL = 0.8

    def __init__(self, blaster, code_next: str,
                 state_file: str | os.PathLike, code_power: str = ""):
        super().__init__(state_file)
        self.blaster = blaster
        self.code_next = code_next
        self.code_power = code_power

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ToppingD900":
        """Wired from config: the D900's own emitter only. Firing all three
        connectors would bounce Topping codes off the TV and the player for
        no reason -- blaster.ports.dac is a measured fact (port 2,
        2026-08-07). A missing blaster still yields a working instance:
        resync is pure bookkeeping and must not need hardware in hand."""
        mine = device_config.driver_block(config, "dac", "ToppingD900")
        codes = mine.get("codes") or {}
        blaster = ITachIR.from_config(config)
        port = (config.get("blaster") or {}).get("ports", {}).get("dac")
        return cls(
            blaster=(blaster.on_port(port if isinstance(port, int) else None)
                     if blaster else None),
            code_next=str(codes.get("input_next") or ""),
            code_power=str(codes.get("power") or ""),
            state_file=mine.get("state_file") or "~/.avctl/dac_input",
        )

    # -- the wire ---------------------------------------------------------

    def _press_next(self) -> None:
        if self.blaster is None:
            raise RuntimeError(
                "no blaster configured -- blaster.host in config.yaml")
        if not self.code_next or self.code_next == "TODO":
            raise RuntimeError("the D900 input code is not learned yet -- "
                               "scripts/learn_ir.py against the physical "
                               "remote")
        self.blaster.send(self.code_next)

    def _press_power(self) -> None:
        if self.blaster is None:
            raise RuntimeError(
                "no blaster configured -- blaster.host in config.yaml")
        if not self.code_power or self.code_power == "TODO":
            raise RuntimeError("no D900 power code learned yet")
        self.blaster.send(self.code_power)
