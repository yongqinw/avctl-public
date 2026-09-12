"""The D900 walk: the races behind issues #18, #19 and #20.

The fake blaster stands where the iTach does. It records every send with a
live overlap counter, so the assertion is the property itself -- two walks
never interleave presses -- rather than a coincidence of timing.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from devices.dac import CYCLE, ToppingD900, UnknownInputError


class FakeBlaster:
    """Counts concurrent sends; a positive overlap means two walks raced."""

    def __init__(self, delay: float = 0.01, on_send=None):
        self.delay = delay
        self.on_send = on_send
        self.sent: list[str] = []
        self._active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def send(self, code: str) -> None:
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.sent.append(code)
        if self.on_send:
            self.on_send()
        time.sleep(self.delay)
        with self._guard:
            self._active -= 1


@pytest.fixture
def dac(tmp_path, monkeypatch):
    monkeypatch.setattr(ToppingD900, "PRESS_INTERVAL", 0.0)
    device = ToppingD900(
        blaster=FakeBlaster(),
        code_next="gc:next",
        code_power="gc:power",
        state_file=tmp_path / "dac_input",
    )
    device.resync("usb")
    device.power_resync(True)
    return device


def test_walk_reaches_target(dac):
    assert dac.select("coax1") == 3
    assert dac.current == "coax1"
    assert dac.blaster.sent == ["gc:next"] * 3


def test_concurrent_walks_never_interleave(dac):
    """Issue #18: two selects at once must serialize, or the press count
    stops describing what the DAC actually received."""
    threads = [threading.Thread(target=dac.select, args=(target,))
               for target in ("opt2", "aes")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert dac.blaster.max_active == 1
    # Serialized, the second walk starts from where the first landed, so the
    # final belief is the second target -- one of the two, never a third.
    assert dac.current in ("opt2", "aes")


def test_walk_survives_truncated_state_file(dac):
    """Issue #19: a state file that goes bad mid-walk (power cut, editor,
    anything) must not crash the walk with CYCLE.index(None)."""
    def clobber():
        dac.state_file.write_text("")   # simulates the torn write
    dac.blaster.on_send = clobber
    assert dac.select("coax1") == 3
    # The walk's own bookkeeping repaired the file on the way through.
    assert dac.current == "coax1"


def test_power_toggle_pairs_cancel(dac):
    """Issue #18 again, power flavor: two toggles must land back where they
    started, not double-read 'on' and both write 'off'."""
    dac.blaster.delay = 0.05
    threads = [threading.Thread(target=dac.power_toggle) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert dac.power is True
    assert dac.blaster.max_active == 1


def test_resync_failure_leaves_old_belief(dac, monkeypatch):
    """Issue #20: the write is atomic -- a failed replace must leave the old
    value intact and no temp litter behind."""
    def refuse(src, dst):
        raise OSError("disk said no")
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        dac.resync("aes")
    monkeypatch.undo()
    assert dac.current == "usb"
    leftovers = [p for p in dac.state_file.parent.iterdir()
                 if p.name.startswith(".")]
    assert leftovers == []


def test_unknown_state_still_refuses_to_guess(tmp_path):
    device = ToppingD900(blaster=FakeBlaster(), code_next="gc:next",
                         state_file=tmp_path / "never_seeded")
    with pytest.raises(UnknownInputError):
        device.select("usb")


def test_state_file_readers_never_see_a_torn_value(dac):
    """A reader polling the state file while walks rewrite it must only ever
    see a complete input name -- that is what os.replace buys."""
    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            value = dac.state_file.read_text().strip()
            if value not in CYCLE:
                bad.append(value)

    t = threading.Thread(target=reader)
    t.start()
    for target in ("coax2", "usb", "i2s", "usb"):
        dac.select(target)
    stop.set()
    t.join()
    assert bad == []
