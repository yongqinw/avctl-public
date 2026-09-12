"""#141: a stop at the queue's seam gets re-lit; deliberate stops stay quiet.

Music never extends Up Next past a track that was already the last when it
started playing -- so a song queued during the final song strands. The
rescuer watches the poll beat: stopped + unplayed placed tracks past the
last needle = rebuild and play from there, exactly once per seam."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from api import musiclink
from tests.test_music_queue import FakeMusic

P = [f"{n:016X}" for n in range(1, 5)]


@pytest.fixture
def rig(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play, **_kwargs: calls.append(
            [item["pid"] if isinstance(item, dict) else item for item in tracks]
        ) or len(tracks))
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(musiclink._queue_items(P), len(P))
    yield calls


def test_stop_at_the_seam_relights_the_rest(rig):
    musiclink._watch_queue({"state": "playing", "pid": P[1]})
    musiclink._watch_queue({"state": "stopped", "pid": None})
    assert rig == [P[2:]]


def test_one_shot_per_seam(rig):
    musiclink._watch_queue({"state": "playing", "pid": P[1]})
    musiclink._watch_queue({"state": "stopped", "pid": None})
    musiclink._watch_queue({"state": "stopped", "pid": None})
    assert len(rig) == 1


def test_natural_end_stays_stopped(rig):
    musiclink._watch_queue({"state": "playing", "pid": P[-1]})
    musiclink._watch_queue({"state": "stopped", "pid": None})
    assert rig == []


def test_cleared_queue_stays_stopped(rig):
    musiclink._watch_queue({"state": "playing", "pid": P[1]})
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.clear_locked()
    musiclink._watch_queue({"state": "stopped", "pid": None})
    assert rig == []


def test_playing_again_re_arms(rig):
    musiclink._watch_queue({"state": "playing", "pid": P[0]})
    musiclink._watch_queue({"state": "stopped", "pid": None})
    musiclink._watch_queue({"state": "playing", "pid": P[2]})
    musiclink._watch_queue({"state": "stopped", "pid": None})
    assert rig == [P[1:], P[3:]]


def test_stale_rescue_cannot_replace_a_new_play_request(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    dispatch = musiclink._dispatch
    dispatch(P[:2], replace=True, play=True)
    musiclink._QUEUE.observe(fake.now_playing())
    rescue_ready = threading.Event()
    resume_rescue = threading.Event()

    def delayed_dispatch(tracks, replace, play, **kwargs):
        rescue_ready.set()
        assert resume_rescue.wait(timeout=3)
        return dispatch(tracks, replace=replace, play=play, **kwargs)

    monkeypatch.setattr(musiclink, "_dispatch", delayed_dispatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        rescue = executor.submit(musiclink._watch_queue, {"state": "stopped"})
        try:
            assert rescue_ready.wait(timeout=3)
            dispatch(P[2:], replace=True, play=True)
        finally:
            resume_rescue.set()
        rescue.result(timeout=3)

    assert fake.playing == P[2]
    assert [item["pid"] for item in musiclink._QUEUE.items] == P[2:]


def test_snapshot_before_new_play_does_not_rescue_the_new_queue(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    musiclink._dispatch(P[:2], replace=True, play=True)
    musiclink._QUEUE.observe(fake.now_playing())

    def old_snapshot_after_new_play():
        musiclink._dispatch(P[2:], replace=True, play=True)
        musiclink._QUEUE.observe({"state": "playing", "pid": P[2]})
        return {"state": "stopped"}

    monkeypatch.setattr(fake, "now_playing", old_snapshot_after_new_play)

    musiclink.safe_state()

    assert fake.playing == P[2]
    assert [item["pid"] for item in musiclink._QUEUE.items] == P[2:]
