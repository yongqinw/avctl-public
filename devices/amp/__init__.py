"""The amplifier, as the buttons see it.

`Amp` is the interface the api layer calls. Every method is a whole
transaction -- read, decide, send, wait for the device to say it happened
-- and implementations own whatever locking that takes; the MAC7200 driver
carries a reentrant transaction lock precisely because hold-repeat volume
issues these calls concurrently.

`query()` returns the driver's own token dict (the MAC7200's PWR/VOL/MUT/
INP ints); `state()` is the same shape without wire traffic. A future amp
maps its protocol into the same tokens rather than growing a new shape --
the snapshot and the panel read these keys.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AmpError(RuntimeError):
    """The amp did not do it: port dead, standby, or a refusal."""


class Amp(ABC):
    @abstractmethod
    def power_on(self) -> bool:
        """Returns False if it was already on."""

    @abstractmethod
    def power_off(self) -> bool:
        """Returns False if it was already off."""

    @abstractmethod
    def set_volume(self, level: int) -> int:
        """Absolute, 0-100 on the amp's own scale; returns the level."""

    @abstractmethod
    def step_volume(self, delta: int) -> int:
        """Relative nudge; returns the level landed on."""

    @abstractmethod
    def set_mute(self, muted: bool) -> bool: ...

    @abstractmethod
    def toggle_mute(self) -> bool: ...

    def set_input(self, input_id: int) -> bool:
        """Returns False if already there. Optional: an amp with one source
        leaves this alone and the input keys dim."""
        raise NotImplementedError("this amp has no input selection")

    @abstractmethod
    def query(self) -> dict[str, int]:
        """Fresh state off the wire."""

    @abstractmethod
    def state(self) -> dict[str, int]:
        """Last known state, no wire traffic. May be empty before a query."""


from devices.amp.mcintosh_mac7200 import McIntoshMAC7200  # noqa: E402
from devices.amp.roon import RoonAmp  # noqa: E402

__all__ = ["Amp", "AmpError", "McIntoshMAC7200", "RoonAmp"]
