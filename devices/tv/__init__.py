"""The television, as the buttons see it.

`Tv` is the async interface api/tvlink.py schedules onto its background
loop: connection lifecycle, honest power state, inputs, remote buttons and
the speaker invariant. Drivers own their protocol entirely -- tvlink's job
is bridging sync handlers to this interface, not knowing what SSAP is.

The constants live here because callers reason in them: STATE_ACTIVE is
the one string that means "the panel is actually lit" (standby sets REPORT
things -- the LG says "Active Standby", which *contains* Active, hence
is_on() and exact comparisons rather than substring tests), and the sound
outputs name the invariant that audio never comes out of TV speakers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class NotConnectedError(RuntimeError):
    """A command was issued before connect(), so there is no socket to use."""


class PowerOnTimeout(RuntimeError):
    """The wake was sent but the panel never reported Active."""


class PowerOffTimeout(RuntimeError):
    """The shutdown was sent but the panel never reported standby."""


class PairingRequired(RuntimeError):
    """The TV rejected its credential and a replacement was not accepted."""


# Drivers must map their protocol's "panel lit" state to exactly this.
STATE_ACTIVE = "Active"

# The invariant in config.yaml: audio must never come out of the TV
# speakers, because the whole point of the rack is that it does not.
SOUND_OUTPUT_EXTERNAL = "external_optical"
SOUND_OUTPUT_TV = "tv_speaker"


class Tv(ABC):
    host: str
    mac: str

    # -- connection -------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def connected(self) -> bool:
        """A live control session exists. Says nothing about panel power."""

    # -- power ------------------------------------------------------------

    @abstractmethod
    async def power_state(self) -> str:
        """The driver's raw power state; STATE_ACTIVE when the panel is lit."""

    @abstractmethod
    async def is_on(self) -> bool:
        """True only when the panel is actually lit -- never inferred from
        reachability, which standby keeps alive on modern sets."""

    @abstractmethod
    async def power_off(self) -> None: ...

    # -- inputs and buttons ----------------------------------------------

    @abstractmethod
    async def current_input(self) -> str | None: ...

    @abstractmethod
    async def select_input(self, name: str) -> bool:
        """Returns True if a switch was actually issued."""

    @abstractmethod
    async def press(self, button: str) -> None: ...

    # -- sound ------------------------------------------------------------

    @abstractmethod
    async def sound_output(self) -> str | None: ...

    @abstractmethod
    async def enforce_external_audio(self) -> bool:
        """Push audio back off the TV speakers. Returns True if it had
        drifted. Re-checked rather than set once -- the TV's own remote can
        undo it at any moment."""


from devices.tv.lg_webos import LGTelevision  # noqa: E402

__all__ = ["LGTelevision", "NotConnectedError", "PairingRequired",
           "PowerOnTimeout",
           "SOUND_OUTPUT_EXTERNAL", "SOUND_OUTPUT_TV", "STATE_ACTIVE", "Tv"]
