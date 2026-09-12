"""The queued number comes from the authoritative logical queue.

Music's playlist count is deliberately ignored: it keeps played tracks and
misses the pump's pending tail. These are the truths the controller holds.
"""

from __future__ import annotations

import pytest

from api import musiclink


@pytest.fixture
def dispatch_state(monkeypatch):
    def set_state(placed, pending):
        items = musiclink._queue_items(list(placed) + list(pending))
        with musiclink._QUEUE.lock:
            musiclink._QUEUE.replace_locked(items, len(placed))
    return set_state


def now(state="playing", pid=None, queued=None):
    return {"state": state, "pid": pid, "queued": queued}


def test_mid_queue_counts_ahead_plus_pending(dispatch_state):
    # Ten placed, needle on the third, forty still waiting in the pump:
    # the phone should say 7 + 40, not the playlist's 10.
    placed = [f"{i:016X}" for i in range(10)]
    dispatch_state(placed, [f"{i + 100:016X}" for i in range(40)])
    assert musiclink.queue_remaining(
        now(pid=placed[2], queued=10)) == 7 + 40


def test_played_out_queue_reads_zero(dispatch_state):
    # All three songs played, needle on the last: Music still says 3
    # (the playlist keeps its dead), the phone must say 0.
    placed = ["A" * 16, "B" * 16, "C" * 16]
    dispatch_state(placed, [])
    assert musiclink.queue_remaining(now(pid=placed[-1], queued=3)) == 0


def test_foreign_needle_does_not_replace_avctls_truth(dispatch_state):
    # Something started inside Music.app does not rewrite avctl's queue.
    dispatch_state(["A" * 16], [])
    assert musiclink.queue_remaining(now(pid="F" * 16, queued=5)) == 1


def test_stopped_reports_the_logical_queue(dispatch_state):
    # Before a cursor exists, avctl will play its queue from the top.
    dispatch_state(["A" * 16, "B" * 16], [])
    assert musiclink.queue_remaining(
        now(state="stopped", pid=None, queued=2)) == 2


def test_duplicate_track_uses_the_last_occurrence(dispatch_state):
    # The same song queued twice: the needle is the LATER copy, or the
    # count inflates -- the _needle_ahead rule, now visible on the phone.
    placed = ["A" * 16, "B" * 16, "A" * 16, "C" * 16]
    dispatch_state(placed, [])
    assert musiclink.queue_remaining(now(pid="A" * 16, queued=4)) == 1


class FakeLibrary:
    def __init__(self, tracks):
        self.tracks = tracks

    def search_library(self, query):
        return self.tracks


class ScriptedLibrary:
    """Answers per-query, like a library tagged in one script only."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.asked = []

    def search_library(self, query):
        self.asked.append(query)
        return self.by_query.get(query, [])


def test_simplified_query_finds_traditional_tags(monkeypatch):
    # The user types 周杰伦; the library is tagged 周杰倫. The variant pass
    # must ask both scripts and merge without duplicating shared hits.
    tagged = [{"album": "范特西", "artist": "周杰倫", "pid": "A" * 16}]
    fake = ScriptedLibrary({"周杰倫": tagged})
    monkeypatch.setattr(musiclink, "_music", lambda: fake)
    albums = musiclink.local_album_search("周杰伦")
    assert [a["artist"] for a in albums] == ["周杰倫"]
    assert "周杰倫" in fake.asked          # the converted variant was tried


def test_compilation_groups_as_one_album(monkeypatch):
    # A soundtrack: one album, Various Artists on the album, a different
    # singer on every track. One tile, owned by the album artist -- the
    # name album_tracks() can actually match (the Beauty-and-the-Beast
    # empty-album bug).
    hits = [
        {"album": "Beauty and the Beast", "artist": "Ariana Grande",
         "albumArtist": "Various Artists", "pid": "A" * 16},
        {"album": "Beauty and the Beast", "artist": "Celine Dion",
         "albumArtist": "Various Artists", "pid": "B" * 16},
        {"album": "Beauty and the Beast", "artist": "Josh Groban",
         "albumArtist": "Various Artists", "pid": "C" * 16},
    ]
    monkeypatch.setattr(musiclink, "_music", lambda: FakeLibrary(hits))
    albums = musiclink.local_album_search("beauty")
    assert len(albums) == 1
    assert albums[0]["artist"] == "Various Artists"
    assert albums[0]["hits"] == 3


def test_variant_overlap_does_not_double_count(monkeypatch):
    hit = {"album": "Aja", "artist": "Steely Dan", "pid": "A" * 16}
    fake = ScriptedLibrary({})
    fake.by_query = {q: [hit] for q in musiclink._query_variants("aja")}
    monkeypatch.setattr(musiclink, "_music", lambda: fake)
    albums = musiclink.local_album_search("aja")
    assert albums[0]["hits"] == 1          # same pid across variants: one hit


def test_library_search_groups_to_ranked_albums(monkeypatch):
    hits = (
        [{"album": "Aja", "artist": "Steely Dan", "pid": f"A{i:015X}"}
         for i in range(7)]
        + [{"album": "Gaucho", "artist": "Steely Dan", "pid": f"B{i:015X}"}
           for i in range(2)]
        + [{"album": "", "artist": "", "pid": "C" * 16}]
    )
    monkeypatch.setattr(musiclink, "_music", lambda: FakeLibrary(hits))
    albums = musiclink.local_album_search("steely")
    assert [a["album"] for a in albums][:2] == ["Aja", "Gaucho"]
    assert albums[0]["hits"] == 7
    assert albums[0]["pid"] == "A" + "0" * 15   # a member track locates the art
    assert albums[2]["album"] == "unknown album"


def _rack(tv, amp, dac, dac_input="usb"):
    from api.state import DeviceState
    mk = lambda p, f=None: DeviceState(name="x", transport="x", power=p,
                                       fields=f or {})
    return {"tv": mk(tv), "amp": mk(amp),
            "dac": mk(dac, {"input": dac_input})}


def test_scene_music_needs_the_whole_chain():
    from api.state import _inferred_scene
    assert _inferred_scene(_rack(True, True, True)) == "music"
    # Half-woken says nothing rather than pretending.
    assert _inferred_scene(_rack(True, True, False)) is None
    assert _inferred_scene(_rack(True, True, True, dac_input="opt1")) is None


def test_scene_off_needs_everything_down():
    from api.state import _inferred_scene
    assert _inferred_scene(_rack(False, False, False)) == "off"
    assert _inferred_scene(_rack(False, False, None)) is None
