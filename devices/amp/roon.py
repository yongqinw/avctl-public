"""Amp compatibility driver for a capability-bearing Roon output."""

from __future__ import annotations

from typing import Any

from devices.amp import Amp, AmpError
from devices.config import driver_block
from devices.roon.contracts import RoonController
from devices.roon.factory import controller_from_config


class RoonAmp(Amp):
    def __init__(self, controller: RoonController, output_id: str):
        self.controller = controller
        self.output_id = output_id

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RoonAmp":
        mine = driver_block(config, "amp", "RoonAmp")
        roon = config.get("roon") or {}
        output_id = str(mine.get("output_id") or roon.get("output_id") or "")
        if not output_id:
            raise ValueError("amp.RoonAmp.output_id is not configured")
        return cls(controller_from_config(config), output_id)

    def power_on(self) -> bool:
        return self.controller.set_standby(self.output_id, False)

    def power_off(self) -> bool:
        return self.controller.set_standby(self.output_id, True)

    def set_volume(self, level: int) -> int:
        return round(self.controller.set_volume(self.output_id, level))

    def step_volume(self, delta: int) -> int:
        return round(self.controller.step_volume(self.output_id, delta))

    def set_mute(self, muted: bool) -> bool:
        return self.controller.set_muted(self.output_id, muted)

    def toggle_mute(self) -> bool:
        muted = self.controller.output(self.output_id).volume.muted
        if muted is None:
            raise AmpError("Roon output does not report mute state")
        return self.set_mute(not muted)

    def set_input(self, input_id: int) -> bool:
        output = self.controller.output(self.output_id)
        try:
            name = output.inputs[int(input_id) - 1]
        except (IndexError, TypeError, ValueError):
            raise ValueError(
                f"input {input_id} not in 1-{len(output.inputs)}") from None
        return self.controller.select_input(self.output_id, name)

    def query(self) -> dict[str, int]:
        return self.state()

    def state(self) -> dict[str, int]:
        output = self.controller.output(self.output_id)
        answer: dict[str, int] = {}
        if output.standby is not None:
            answer["PWR"] = 0 if output.standby else 1
        if output.volume.value is not None:
            answer["VOL"] = round(output.volume.value)
        if output.volume.muted is not None:
            answer["MUT"] = int(output.volume.muted)
        if output.selected_input in output.inputs:
            answer["INP"] = output.inputs.index(output.selected_input) + 1
        return answer
