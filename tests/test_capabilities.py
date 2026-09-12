"""Issue #31, the 3.0 shape: the table's reach derives from the drivers.

A driver that leaves an optional interface verb alone must produce a
dimmed key and an honest 501 -- not a missing row, and not a 500 when
pressed. The fake below is a legal closed-loop DAC: it answers the
required questions and has no cycle button, no toggle, nothing to resync.
"""

from __future__ import annotations

import pytest

from api import commands
from devices import registry
from devices.dac import Dac


class ClosedLoopDac(Dac):
    """Reads its own state back; none of the open-loop verbs exist."""

    @property
    def inputs(self):
        return ["usb", "opt1"]

    @property
    def current(self):
        return "usb"

    @property
    def power(self):
        return True

    def select(self, target):
        return 1

    def power_to(self, target):
        return True


@pytest.fixture
def closed_loop_dac(monkeypatch):
    monkeypatch.setitem(registry._classes, "dac", ClosedLoopDac)
    commands.rebuild()
    yield
    monkeypatch.delitem(registry._classes, "dac", raising=False)
    commands.rebuild()


def test_real_drivers_light_everything_they_light_today():
    ids = commands.implemented()
    for command_id in ("dac.input.next", "dac.resync", "dac.power",
                      "amp.input.dac", "music.vol.up", "music.mute"):
        assert command_id in ids


def test_open_loop_verbs_dim_for_a_closed_loop_dac(closed_loop_dac):
    ids = commands.implemented()
    # The verbs this driver genuinely has stay lit...
    assert "dac.input.usb" in ids
    # ...and the open-loop ones dim instead of lying.
    for command_id in ("dac.input.next", "dac.power", "dac.resync",
                      "dac.power.resync"):
        assert command_id not in ids
        assert commands.get(command_id).handler is None   # honest 501, kept row
