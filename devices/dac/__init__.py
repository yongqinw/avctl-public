"""The DAC, as the buttons see it.

`Dac` is a pure interface: the questions and verbs the api layer needs,
and nothing about HOW any device answers them. The Topping D900 answers
with tracked state files and a counted IR walk -- that whole mechanism
lives in `TrackedDac` (devices/dac/tracked.py), an intermediate for
open-loop devices, NOT here: a DAC with a serial port and real readback
would implement this interface directly and carry none of it.

The open-loop verbs (step, resync, power_toggle) default to "this DAC
does not work that way" rather than being required -- a closed-loop
driver leaves them alone and the corresponding buttons dim, the same
honesty the command table applies everywhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class UnknownInputError(RuntimeError):
    """State was never established, so a relative walk would be a guess."""


class Dac(ABC):
    # -- facts ------------------------------------------------------------

    @property
    @abstractmethod
    def inputs(self) -> list[str]:
        """The selectable inputs, in the device's own order."""

    @property
    @abstractmethod
    def current(self) -> str | None:
        """The input in use, or None when it cannot be known."""

    @property
    @abstractmethod
    def power(self) -> bool | None:
        """Power state, or None when it cannot be known."""

    # -- verbs ------------------------------------------------------------

    @abstractmethod
    def select(self, target: str) -> int:
        """Land on `target`. Returns how many device actions it took --
        0 means it was already there."""

    @abstractmethod
    def power_to(self, target: bool) -> bool | None:
        """Drive power to a known state. True: done. False: already there.
        None: refused because the outcome would be a guess."""

    # -- open-loop verbs: meaningful only for devices that are driven
    # blind. Defaults say so instead of demanding implementations.

    def step(self) -> str | None:
        """One press of an input-cycle button, where one exists."""
        raise NotImplementedError("this DAC has no input-cycle button")

    def power_toggle(self) -> bool | None:
        """Fire a power toggle, where power is a toggle."""
        raise NotImplementedError("this DAC's power is not a blind toggle")

    def resync(self, actual: str) -> None:
        """Declare the true input, read off the front panel."""
        raise NotImplementedError("this DAC reports its own input -- "
                                  "there is nothing to correct")

    def power_resync(self, actual: bool) -> None:
        """Declare the true power state."""
        raise NotImplementedError("this DAC reports its own power -- "
                                  "there is nothing to correct")


from devices.dac.tracked import TrackedDac  # noqa: E402
from devices.dac.topping_d900 import ToppingD900  # noqa: E402
from devices.dac.roon import RoonDac  # noqa: E402

# What state.py and the resync sheet render. Compat alias for the one
# configured driver until the registry (3.0 step 2) hands them the class.
CYCLE = ToppingD900.CYCLE

__all__ = [
    "CYCLE", "Dac", "RoonDac", "ToppingD900", "TrackedDac",
    "UnknownInputError",
]
