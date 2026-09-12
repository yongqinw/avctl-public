"""The authoritative queue's projection pump and replacement boundaries.

The fake Music.app records the queue playlist as a plain list, so every
assertion is about what would actually be in the playlist -- the thing the
user hears. Pump ticks are floored at 1s in production code, so the pump
tests spend real seconds; their deadlines are generous for CI.
"""

from __future__ import annotations

import threading
import time

import pytest

from api import musiclink

# 16-hex "persistent IDs", enough of them for a small library.
PIDS = [f"{n:016X}" for n in range(1, 40)]


class FakeMusic:
    queue_playlist = "avctl"

    def __init__(self):
        self.playlist: list[str] = []
        self.playing: str | None = None
        self.lock = threading.Lock()

    def play_tracks(self, pids, start=0):
        with self.lock:
            self.playlist = list(pids)
            self.playing = pids[0] if pids else None

    def queue_append(self, pids):
        with self.lock:
            self.playlist.extend(pids)

    def now_playing(self):
        with self.lock:
            return {"pid": self.playing,
                    "state": "playing" if self.playing else "stopped",
                    "queued": len(self.playlist)}

    def quiesce(self):
        with self.lock:
            dropped = len(self.playlist)
            self.playlist = []
            self.playing = None
            return dropped


TUNABLES = {"dispatch_budget_ms": 200, "place_ms": 100,   # lead = 2
            "drip_tick": 1, "queue_window": 2, "drip_chunk": 2,
            "drip_max_idle_ticks": 3, "autoplay_depth": 0,
            "order_cache_ttl": 30}


@pytest.fixture
def music(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_tunable",
                        lambda name, default: TUNABLES.get(name, default))
    yield fake


def chase_needle(fake, until, deadline=20.0):
    """Keep 'playing' the newest placed track until the playlist satisfies
    `until` -- the pump only tops up when the needle is near the end."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        with fake.lock:
            if fake.playlist:
                fake.playing = fake.playlist[-1]
            if until(list(fake.playlist)):
                return True
        time.sleep(0.1)
    return False


def pumps_alive():
    return [t for t in threading.enumerate()
            if t.name == "avctl-queue-pump" and t.is_alive()]


def test_queue_add_keeps_logical_tail_order(music):
    """An add never jumps ahead of an older unmaterialized tail."""
    musiclink._dispatch(PIDS[:10], replace=True, play=True)
    assert len(music.playlist) == 2          # the lead
    added = PIDS[20]
    musiclink.queue_add({"pid": added})
    assert added not in music.playlist        # it belongs after the old tail
    assert [item["pid"] for item in musiclink._QUEUE.items] == PIDS[:10] + [added]
    # The original ten AND the add reach Music in the logical order.
    assert chase_needle(music, lambda pl: len(pl) == 11), \
        f"pump died after the add; playlist stalled at {len(music.playlist)}"
    assert music.playlist == PIDS[:10] + [added]


def test_play_song_replaces_the_pump_work(music):
    """#53: a wholesale replacement must bump the generation."""
    musiclink._dispatch(PIDS[:10], replace=True, play=True)
    chosen = PIDS[30]
    musiclink.play_song({"pid": chosen, "name": "the song"})
    assert music.playlist == [chosen]
    music.playing = chosen
    time.sleep(2.5)                          # two pump ticks
    assert music.playlist == [chosen], \
        "a live pump refilled the queue the user just replaced"


def test_quiesce_voids_the_pump(music):
    """Everything-off must not leave a pump refilling a dark room."""
    musiclink._dispatch(PIDS[:10], replace=True, play=True)
    answer = musiclink.quiesce({})
    assert "queue cleared" in answer["message"]
    assert music.playlist == []
    time.sleep(2.5)
    assert music.playlist == [], \
        "the pump appended tracks into a queue the Off scene had emptied"


def test_replace_without_play_is_refused(music):
    """#55.4: the latent branch that would append instead of replacing."""
    with pytest.raises(ValueError, match="replace without playing"):
        musiclink._dispatch(PIDS[:3], replace=True, play=False)


def test_needle_uses_the_last_occurrence():
    """#55.3: a duplicated pid must not park the needle too early."""
    placed = ["A", "B", "A", "C"]
    assert musiclink._needle_ahead(placed, "A") == 1   # not 3
    assert musiclink._needle_ahead(placed, "C") == 0
    assert musiclink._needle_ahead(placed, "Z") == 4   # foreign: all unplayed
    assert musiclink._needle_ahead(placed, None) == 4


def test_zombie_pump_bails_after_idle_ticks(music):
    """A needle that is never ours must not spin the pump forever.

    No queue_window fiddling needed anymore: since #99 the idle counter is
    keyed on the needle being foreign, not on the window arithmetic.
    """
    musiclink._dispatch(PIDS[:10], replace=True, play=True)
    music.playing = PIDS[35]              # started in Music.app directly
    assert pumps_alive()
    deadline = time.monotonic() + 15      # 3 idle ticks at 1s, plus slack
    while pumps_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not pumps_alive(), "the pump never bailed on a foreign needle"
    placed_before = len(music.playlist)
    time.sleep(1.5)
    assert len(music.playlist) == placed_before


def test_topped_up_pump_is_not_idle(music):
    """#99: the healthy window-full steady state must not count as idle.

    With the old counter, a fully topped-up queue read as 'nobody is
    playing our queue' and max_idle expired mid-album, stranding the whole
    pending tail while queue_remaining kept promising it.
    """
    musiclink._dispatch(PIDS[:20], replace=True, play=True)
    music.playing = music.playlist[0]     # our queue, needle at the top
    # Wait well past max_idle (3 ticks at 1s): the pump must still be alive,
    # holding the tail for when the needle draws close again.
    time.sleep(5)
    assert pumps_alive(), "pump bailed during the healthy window-full state"
    # And when the needle does advance, the whole tail still arrives.
    assert chase_needle(music, lambda pl: len(pl) == 20)
    # Drain: the emptied pump exits at its next tick; do not let it linger.
    deadline = time.monotonic() + 10
    while pumps_alive() and time.monotonic() < deadline:
        time.sleep(0.1)


def test_second_add_does_not_start_a_second_pump(music):
    """One controller owns one live pump while revisions change."""
    musiclink._dispatch(PIDS[:10], replace=True, play=True)
    musiclink.queue_add({"pid": PIDS[20]})
    musiclink.queue_add({"pid": PIDS[21]})
    assert len(pumps_alive()) == 1
    assert chase_needle(music, lambda pl: len(pl) == 12)
    # Nothing doubled: the pump placed each track exactly once.
    assert sorted(music.playlist) == sorted(set(music.playlist))


def test_work_arriving_during_pump_exit_gets_a_successor(music):
    """An append in the worker's return/finally gap must not be stranded.

    Model the exact lock-protected state at that boundary. The retiring pump
    accepted `old`; queue_add then revised its live ownership marker to `new`
    because it believed that pump would handle the appended tail.
    """
    items = musiclink._queue_items(PIDS[:2])
    with musiclink._QUEUE.lock:
        old = musiclink._QUEUE.replace_locked(items[:1], materialized=1)
        musiclink._QUEUE.pump_revision = old
        new = musiclink._QUEUE.append_locked(items[1:], materialized=0)
        musiclink._QUEUE.pump_revision = new

        assert musiclink._finish_pump_locked(
            musiclink._QUEUE, accepted_revision=old) is True
        assert musiclink._QUEUE.pump_revision is None
