"""DAC compatibility driver for optional Roon output source controls."""

from __future__ import annotations

from typing import Any

from devices.config import driver_block
from devices.dac import Dac
from devices.roon.contracts import RoonController
from devices.roon.factory import controller_from_config


class RoonDac(Dac):
    def __init__(self, controller: RoonController, output_id: str):
        self.controller = controller
        self.output_id = output_id

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RoonDac":
        mine = driver_block(config, "dac", "RoonDac")
        roon = config.get("roon") or {}
        output_id = str(mine.get("output_id") or roon.get("output_id") or "")
        if not output_id:
            raise ValueError("dac.RoonDac.output_id is not configured")
        return cls(controller_from_config(config), output_id)

    @property
    def inputs(self) -> list[str]:
        return list(self.controller.output(self.output_id).inputs)

    @property
    def current(self) -> str | None:
        return self.controller.output(self.output_id).selected_input

    @property
    def power(self) -> bool | None:
        standby = self.controller.output(self.output_id).standby
        return None if standby is None else not standby

    def select(self, target: str) -> int:
        return int(self.controller.select_input(self.output_id, target))

    def step(self) -> str | None:
        inputs = self.inputs
        current = self.current
        if not inputs or current not in inputs:
            return None
        target = inputs[(inputs.index(current) + 1) % len(inputs)]
        self.select(target)
        return target

    def power_to(self, target: bool) -> bool | None:
        return self.controller.set_standby(self.output_id, not target)

    def power_toggle(self) -> bool | None:
        current = self.power
        if current is None:
            return None
        self.power_to(not current)
        return not current
