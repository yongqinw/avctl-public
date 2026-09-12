"""The open-loop mechanism: tracked state, advanced by counted presses.

For DACs that are driven blind -- IR out, nothing back -- the input is
modelled as persisted local state and moved by the right number of
presses of a single cycle button. This class owns everything that must be
true for ANY such device; a driver subclasses it and provides only the
wire (one method to press "next input", one to press "power") plus the
facts of its model: which inputs the cycle steps through, and how fast
presses may follow each other.

What the base guarantees, because these were all real bugs once:

* One walk, toggle or resync at a time, under a reentrant per-device
  lock -- a walk is a read-modify-write held open across seconds of real
  IR, and two interleaved walks desync the count silently.
* State files written atomically (mkstemp + replace). They live outside
  any release directory and promise power-cut survival; a half-written
  word breaks that promise exactly when it was needed.
* The walk computes its positions from where it STARTED, never by
  re-reading its own belief mid-walk -- a torn file must not crash a
  sequence of real IR halfway through.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from abc import abstractmethod
from pathlib import Path

from devices.dac import Dac, UnknownInputError


class TrackedDac(Dac):
    # The inputs the single button steps through, wrapping at the end, and
    # the beat the device needs between presses. Facts of the model, so
    # the driver declares them.
    CYCLE: list[str] = []
    PRESS_INTERVAL: float = 0.0

    def __init__(self, state_file: str | os.PathLike):
        self.state_file = Path(state_file).expanduser()
        # Power is tracked the same way the input is: optimistically, in a
        # sibling file, because an IR power button is a toggle and an
        # open-loop DAC reports nothing back.
        self.power_file = self.state_file.with_name(
            self.state_file.name + "_power")
        self.lock = threading.RLock()
        # When the last IR press left, monotonic. -inf: the first press of
        # the process never waits.
        self._last_press = float("-inf")

    # -- the wire: all a driver must provide ------------------------------

    @abstractmethod
    def _press_next(self) -> None:
        """Fire one press of the input-cycle button, or raise RuntimeError
        with a human answer when the hardware path is not configured."""

    @abstractmethod
    def _press_power(self) -> None:
        """Fire one press of the power toggle; same contract."""

    # -- pacing ------------------------------------------------------------

    def _pace(self) -> None:
        """Sleep out the remainder of PRESS_INTERVAL since the last press.

        Enforced before EVERY press, not merely between the presses of one
        walk (#108): two back-to-back step() calls, or a select() starting
        right after a power toggle, used to fire IR faster than the device
        registers presses -- and a dropped press desyncs the tracked input
        silently, the exact bug this class exists to prevent.
        """
        if self.PRESS_INTERVAL <= 0:
            return
        wait = self.PRESS_INTERVAL - (time.monotonic() - self._last_press)
        if wait > 0:
            time.sleep(wait)

    def _fire_next(self) -> None:
        self._pace()
        self._press_next()
        self._last_press = time.monotonic()

    def _fire_power(self) -> None:
        self._pace()
        self._press_power()
        self._last_press = time.monotonic()

    # -- facts ------------------------------------------------------------

    @property
    def inputs(self) -> list[str]:
        return self.CYCLE

    @property
    def current(self) -> str | None:
        """Last input we believe the DAC is on, or None if never established."""
        try:
            value = self.state_file.read_text().strip()
        except FileNotFoundError:
            return None
        return value if value in self.CYCLE else None

    @property
    def power(self) -> bool | None:
        """Last power state we believe, or None if never established."""
        try:
            value = self.power_file.read_text().strip()
        except FileNotFoundError:
            return None
        return {"on": True, "off": False}.get(value)

    # -- corrections ------------------------------------------------------

    def _write_state(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent,
                                        prefix="." + path.name + "-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(value)
                fh.flush()
                # The promise is power-cut survival; without the fsync the
                # rename is atomic but the bytes may still be in the page
                # cache, and a cut leaves a zero-length belief (#108).
                os.fsync(fh.fileno())
            os.replace(temp, path)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def resync(self, actual: str) -> None:
        """Declare the DAC's true input. Needed after using the physical
        remote, and once at first run to seed the state."""
        if actual not in self.CYCLE:
            raise ValueError(f"{actual!r} is not one of {self.CYCLE}")
        with self.lock:
            self._write_state(self.state_file, actual)

    def power_resync(self, actual: bool) -> None:
        with self.lock:
            self._write_state(self.power_file, "on" if actual else "off")

    # -- the walk ---------------------------------------------------------

    def presses_to(self, target: str) -> int:
        current = self.current
        if current is None:
            raise UnknownInputError(
                "input state unknown -- call resync() with what the front "
                "panel actually reads"
            )
        return ((self.CYCLE.index(target) - self.CYCLE.index(current))
                % len(self.CYCLE))

    def step(self) -> str | None:
        """One press of the cycle button, under the lock; returns where we
        now believe the DAC is, or None when there was no belief to advance."""
        with self.lock:
            self._fire_next()
            current = self.current
            if current is None:
                return None
            landed = self.CYCLE[(self.CYCLE.index(current) + 1)
                                % len(self.CYCLE)]
            self.resync(landed)
            return landed

    def select(self, target: str) -> int:
        """Walk the cycle to `target`. Returns the number of presses sent.

        State is written after each press, so an interrupted walk leaves
        behind our best guess rather than a stale one from before the walk
        started.
        """
        if target not in self.CYCLE:
            raise ValueError(f"{target!r} is not one of {self.CYCLE}")
        with self.lock:
            count = self.presses_to(target)
            start = self.CYCLE.index(self.current)
            for press in range(count):
                # _fire_next paces itself off the last press's timestamp,
                # so the walk needs no intra-loop sleep of its own.
                self._fire_next()
                self.resync(self.CYCLE[(start + press + 1) % len(self.CYCLE)])
            return count

    # -- power ------------------------------------------------------------

    def power_toggle(self) -> bool | None:
        """Fire the power toggle and flip the tracked state.

        Returns the state we now believe -- or None when there was no
        prior belief, in which case a toggle produces... the other unknown
        state. The caller says so honestly rather than guessing.
        """
        with self.lock:
            self._fire_power()
            before = self.power
            if before is None:
                return None
            after = not before
            self._write_state(self.power_file, "on" if after else "off")
            return after

    def power_to(self, target: bool) -> bool | None:
        """Read-then-toggle under the lock, or a second press in the window
        double-fires the toggle and lands the opposite state."""
        with self.lock:
            current = self.power
            if current is target:
                return False
            if current is None:
                return None
            self.power_toggle()
            return True
