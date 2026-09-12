"""Ordered plays never hand shuffle back mid-flight (#137).

The old dance -- shuffle off, play, shuffle back ON -- re-materialized a
shuffled Up Next from the playlist's play-time snapshot, so every track
appended afterwards (queue_add, the drip's own chunks) joined the playlist
but never the queue: "queued music never gets played", measured live with
shuffle:true mid-queue. These pin the generated scripts: shuffle may be
lifted, never restored. Transport stubbed; no real Music.app involved.
"""

from __future__ import annotations

import pytest

from devices.music.apple_music import AppleMusic


@pytest.fixture
def sent(monkeypatch):
    calls: list[str] = []

    def fake(self, script, lang="JavaScript", timeout=None):
        calls.append(script)
        return ""

    monkeypatch.setattr(AppleMusic, "_osascript", fake)
    return calls


def test_play_tracks_lifts_shuffle_and_leaves_it_down(sent):
    AppleMusic().play_tracks(["00DEC0DEDEADBEEF"])
    script = sent[-1]
    assert "set shuffle enabled to false" in script
    assert "set shuffle enabled to true" not in script, \
        "restoring shuffle freezes Up Next at the play-time snapshot"


def test_playlist_bulk_lifts_shuffle_and_leaves_it_down(sent):
    AppleMusic().queue_playlist_bulk("00DEC0DEDEADBEEF", 1,
                                     replace=True, play=True)
    script = sent[-1]
    assert "set shuffle enabled to false" in script
    assert "set shuffle enabled to true" not in script


def test_append_only_paths_leave_shuffle_alone(sent):
    music = AppleMusic()
    music.queue_append(["00DEC0DEDEADBEEF"])
    music.queue_playlist_bulk("00DEC0DEDEADBEEF", 1,
                              replace=False, play=False)
    for script in sent:
        assert "shuffle" not in script, script.splitlines()[0]
