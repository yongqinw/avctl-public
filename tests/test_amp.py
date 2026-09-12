"""Issue #21: hold-repeat volume must climb monotonically.

The fake replaces `_send` only. The narration (`_ingest`), the waiting
(`_await`) and the command methods all run for real, so the transaction lock
is exercised exactly where the phone's hold-repeat exercises it. The fake
narrates each set back after a small delay -- the very window in which two
unserialized step_volume calls both read the same VOL and collapse into one
step.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from devices.amp import McIntoshMAC7200


def fake_amp(initial: dict[str, int]) -> McIntoshMAC7200:
    amp = McIntoshMAC7200("fake-port")
    amp._state.update(initial)

    def _send(self, command: str) -> int:
        with self._cond:
            seq = self._error_seq
        frame = command.strip("()")

        def narrate() -> None:
            time.sleep(0.03)   # wire + amp latency: the race window
            if frame == "QRY":
                # Like the real dump: identity plus a re-report of every
                # known token -- query()'s freshness check (#109) waits for
                # the re-reports, not the identity line.
                self._ingest("MAC7200")
                with self._cond:
                    state = dict(self._state)
                if state.get("PWR") != 1:
                    self._ingest(f"PWR {state.get('PWR', 0)}")
                else:
                    for token, value in state.items():
                        self._ingest(f"{token} {value}")
            else:
                self._ingest(frame)
        threading.Thread(target=narrate, daemon=True).start()
        return seq

    amp._send = types.MethodType(_send, amp)
    return amp


def test_hold_repeat_climbs_monotonically():
    amp = fake_amp({"PWR": 1, "VOL": 50})
    threads = [threading.Thread(target=amp.step_volume, args=(2,))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Five serialized +2 steps from 50. Unserialized, several read 50 at
    # once and the total collapses -- the "volume climbs then snaps back"
    # the phone showed.
    assert amp.state()["VOL"] == 60


def test_mute_toggles_pair_off():
    amp = fake_amp({"PWR": 1, "MUT": 0})
    threads = [threading.Thread(target=amp.toggle_mute) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert amp.state()["MUT"] == 0


def test_step_volume_clamps():
    amp = fake_amp({"PWR": 1, "VOL": 99})
    assert amp.step_volume(4) == 100
