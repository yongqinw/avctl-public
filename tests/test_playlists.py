"""The library's second shelf: playlists, speaking the album grammar.

Play whole, play from a tapped song onward, queue whole -- via ONE bulk
duplicate from the source playlist, never library-pid lookups: a
subscription playlist's cloud tracks are not library members, and the
pid path silently drops them (New Music would lose 23 of 25, measured
2026-08-08). The dispatch bookkeeping still learns the pids, so the
queued count keeps telling the truth.
"""

from __future__ import annotations

import pytest

from api import musiclink

PL = "A" * 16
T1, T2, T3 = "1" * 16, "2" * 16, "3" * 16


class FakePlaylists:
    queue_playlist = "avctl"

    def __init__(self):
        self.bulk_calls = []

    def playlists(self):
        return [{"name": "Road trip", "pid": PL, "count": 3, "art": T1}]

    def playlist_tracks(self, pid):
        if pid != PL:
            return None
        return {"name": "Road trip",
                "tracks": [{"pid": T1}, {"pid": T2}, {"pid": T3}]}

    def queue_playlist_bulk(self, playlist_pid, start_pos, replace, play):
        self.bulk_calls.append((playlist_pid, start_pos, replace, play))


@pytest.fixture
def fake(monkeypatch):
    fake = FakePlaylists()
    monkeypatch.setattr(musiclink, "_music", lambda: fake)
    return fake


def test_play_whole_playlist_is_one_bulk_duplicate(fake):
    musiclink.play_playlist({"pid": PL})
    assert fake.bulk_calls == [(PL, 1, True, True)]
    # Bookkeeping learned every pid -- cloud tracks included -- so the
    # queued count can still answer honestly.
    assert [item["pid"] for item in musiclink._QUEUE.items] == [T1, T2, T3]
    assert musiclink._QUEUE.materialized == 3


def test_play_from_a_tapped_song_onward(fake):
    musiclink.play_playlist({"pid": PL, "from": T2})
    assert fake.bulk_calls == [(PL, 2, True, True)]   # 1-based, Apple's way
    assert [item["pid"] for item in musiclink._QUEUE.items] == [T2, T3]


def test_unknown_from_pid_plays_whole(fake):
    # A stale row (track since removed): the whole playlist beats an error.
    musiclink.play_playlist({"pid": PL, "from": "F" * 16})
    assert fake.bulk_calls == [(PL, 1, True, True)]


def test_queue_whole_playlist_appends(fake):
    seed = musiclink._queue_items(["9" * 16])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(seed, 1)
    musiclink.queue_add({"playlist": PL})
    assert fake.bulk_calls == [(PL, 1, False, False)]
    assert [item["pid"] for item in musiclink._QUEUE.items] == [
        "9" * 16, T1, T2, T3,
    ]


def test_gone_playlist_is_a_value_error(fake):
    with pytest.raises(ValueError):
        musiclink.play_playlist({"pid": "B" * 16})
    assert fake.bulk_calls == []


def test_garbage_pid_is_refused(fake):
    with pytest.raises(ValueError):
        musiclink.playlist_tracks("../etc/passwd")
