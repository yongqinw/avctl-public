"""The song view's tap (#139): play from here, shuffle decided server-side.

Everything stubbed at the seams -- recent_songs and _dispatch -- so these
test the ORDERING decision, which is the whole feature: newest-onward when
shuffle is off, tapped-first-then-pre-shuffled when it is on (Music's own
shuffle would freeze Up Next, #137)."""

from __future__ import annotations

import pytest

from api import musiclink

SONGS = [{"pid": f"{n:016X}", "name": f"song{n}", "added": 1000 - n}
         for n in range(1, 6)]


@pytest.fixture
def rig(monkeypatch):
    captured = {}
    monkeypatch.setattr(musiclink, "recent_songs",
                        lambda force=False: SONGS)
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: captured.update(
            pids=[track["pid"] for track in tracks],
            replace=replace, play=play) or len(tracks))
    return captured


def test_tap_plays_newest_onward(rig):
    out = musiclink.play_recent({"pid": SONGS[2]["pid"]})
    assert rig["pids"] == [s["pid"] for s in SONGS[2:]]
    assert rig["replace"] and rig["play"]
    assert "song3" in out["message"]


def test_tap_with_shuffle_pre_shuffles_the_rest(rig, monkeypatch):
    musiclink._QUEUE.set_shuffle(True)
    # Deterministic 'shuffle': reverse -- enough to prove the order came
    # from us, not from list position.
    monkeypatch.setattr(musiclink.random, "shuffle",
                        lambda lst: lst.reverse())
    musiclink.play_recent({"pid": SONGS[2]["pid"]})
    rest = [s["pid"] for s in SONGS if s is not SONGS[2]]
    assert rig["pids"] == [SONGS[2]["pid"]] + list(reversed(rest))


def test_unknown_pid_says_refresh(rig):
    with pytest.raises(ValueError, match="refresh"):
        musiclink.play_recent({"pid": "F" * 16})
