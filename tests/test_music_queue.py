"""The avctl queue is one persisted truth; Music is only its projection."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api import musiclink, views
from api.auth import Identity
from api.main import app
from tests.conftest import AUTH


PIDS = [f"{n:016X}" for n in range(1, 10)]
PLAYLIST = "A" * 16


class FakeMusic:
    queue_playlist = "avctl"

    def __init__(self):
        self.playlist: list[str] = []
        self.catalog_playlist: list[str] = []
        self.playing: str | None = None
        self.catalog_playing: str | None = None
        self.bulk_calls = []
        self.paused = False

    def pause(self):
        self.paused = True

    def play_tracks(self, pids, start=0):
        self.playlist = list(pids)
        self.playing = pids[0]
        self.catalog_playing = None

    def queue_append(self, pids):
        self.playlist.extend(pids)

    def play_catalog(self, catalog_ids):
        self.catalog_playlist = list(catalog_ids)
        self.catalog_playing = catalog_ids[0]
        self.playing = None

    def queue_catalog(self, catalog_ids):
        self.catalog_playlist.extend(catalog_ids)

    def queue_playlist_bulk(self, pid, start, replace, play):
        self.bulk_calls.append((pid, start, replace, play))

    def playlist_tracks(self, pid):
        if pid != PLAYLIST:
            return None
        return {"name": "Cloud mix", "tracks": [
            {"pid": PIDS[6], "name": "cloud one", "artist": "someone"},
            {"pid": PIDS[7], "name": "cloud two", "artist": "someone"},
        ]}

    def now_playing(self):
        if self.catalog_playing:
            return {"state": "playing", "catalog_id": self.catalog_playing,
                    "queued": len(self.catalog_playlist)}
        return {"state": "playing" if self.playing else "stopped",
                "pid": self.playing, "queued": len(self.playlist)}

    def next_track(self):
        if self.catalog_playing in self.catalog_playlist:
            at = self.catalog_playlist.index(self.catalog_playing)
            if at + 1 < len(self.catalog_playlist):
                self.catalog_playing = self.catalog_playlist[at + 1]
            return
        if self.playing not in self.playlist:
            return
        at = self.playlist.index(self.playing)
        if at + 1 < len(self.playlist):
            self.playing = self.playlist[at + 1]


def test_queue_round_trips_metadata_cursor_and_shuffle(tmp_path):
    path = tmp_path / "queue.json"
    queue = musiclink.QueueController(path)
    items = musiclink._queue_items([
        {"pid": PIDS[0], "name": "first", "artist": "one", "duration": 91},
        {"pid": PIDS[1], "name": "second", "album": "two", "duration": 122},
    ])
    with queue.lock:
        queue.replace_locked(items, 1)
        queue.current = 0
        queue.shuffle = True
        queue._save_locked()

    restored = musiclink.QueueController(path)
    assert restored.materialized == 1
    assert restored.current == 0
    assert restored.shuffle is True
    assert restored.details()["items"] == [{
        "id": items[1]["id"], "pid": PIDS[1], "name": "second",
        "artist": "", "album": "two", "duration": 122,
    }]


def test_queue_ignores_valid_json_with_the_wrong_shape(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("[]", encoding="utf-8")

    restored = musiclink.QueueController(path)

    assert restored.details()["items"] == []


def test_catalog_queue_round_trips_without_a_library_pid(tmp_path):
    path = tmp_path / "queue.json"
    queue = musiclink.QueueController(path)
    items = musiclink._queue_items([{
        "catalog_id": "catalog.1", "name": "stream me",
        "artist": "someone", "art": "https://example.test/cover.jpg",
    }])
    with queue.lock:
        queue.replace_locked(items, 1)

    restored = musiclink.QueueController(path)

    assert restored.details()["items"] == [{
        "id": items[0]["id"], "pid": None, "catalog_id": "catalog.1",
        "name": "stream me", "artist": "someone", "album": "",
        "duration": None, "art": "https://example.test/cover.jpg",
    }]


def test_catalog_items_stream_directly_through_authoritative_queue(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    tracks = [
        {"catalog_id": "catalog.1", "name": "one"},
        {"catalog_id": "catalog.2", "name": "two"},
    ]

    musiclink._dispatch(tracks, replace=True, play=True)

    assert fake.catalog_playlist == ["catalog.1", "catalog.2"]
    assert musiclink._QUEUE.materialized == 2
    assert [item["catalog_id"] for item in musiclink._QUEUE.items] == [
        "catalog.1", "catalog.2",
    ]


def test_mixed_queue_switches_engines_only_after_current_segment(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    items = [
        {"pid": PIDS[0], "name": "local"},
        {"catalog_id": "catalog.1", "name": "stream"},
    ]

    musiclink._dispatch(items, replace=True, play=True)

    assert fake.playlist == [PIDS[0]]
    assert fake.catalog_playlist == []
    assert musiclink._QUEUE.materialized == 1
    with musiclink._QUEUE.lock:
        assert musiclink._materialize_locked(musiclink._QUEUE, 20) is False
    musiclink._QUEUE.observe({"state": "playing", "pid": PIDS[0]})
    musiclink._watch_queue({"state": "stopped", "pid": None})

    assert fake.catalog_playlist == ["catalog.1"]
    assert [item["catalog_id"] for item in musiclink._QUEUE.items] == [
        "catalog.1",
    ]


def test_cursor_advance_revises_the_public_focus_queue():
    items = musiclink._queue_items([
        {"pid": PIDS[0], "name": "first"},
        {"pid": PIDS[1], "name": "second"},
    ])
    with musiclink._QUEUE.lock:
        initial = musiclink._QUEUE.replace_locked(items, 2)

    musiclink._QUEUE.observe({"state": "playing", "pid": PIDS[0]})
    first = musiclink._QUEUE.details()
    assert first["revision"] == initial + 1
    assert first["playing"]["name"] == "first"
    assert [item["name"] for item in first["items"]] == ["second"]

    musiclink._QUEUE.observe({"state": "playing", "pid": PIDS[1]})
    second = musiclink._QUEUE.details()
    assert second["revision"] == initial + 2
    assert second["playing"]["name"] == "second"
    assert second["items"] == []


def test_stopped_queue_waits_for_grace_and_cancels_on_recovery(tmp_path):
    queue = musiclink.QueueController(tmp_path / "queue.json")
    items = musiclink._queue_items([
        {"pid": PIDS[0], "name": "first"},
        {"pid": PIDS[1], "name": "second"},
    ])
    with queue.lock:
        queue.replace_locked(items, 2)
    queue.observe({"state": "playing", "pid": PIDS[0]})

    assert queue.observe(
        {"state": "stopped"}, rescue_delay=15, clock=lambda: 100) is None
    assert queue.observe(
        {"state": "stopped"}, rescue_delay=15, clock=lambda: 114) is None
    queue.observe({"state": "playing", "pid": PIDS[0]}, clock=lambda: 115)
    assert queue.observe(
        {"state": "stopped"}, rescue_delay=15, clock=lambda: 200) is None
    assert queue.observe(
        {"state": "stopped"}, rescue_delay=15, clock=lambda: 215
    )[0]["pid"] == PIDS[1]


def test_live_music_backend_switch_pauses_old_source_and_clears_queue(
        monkeypatch, tmp_path):
    previous = FakeMusic()

    class RoonMusic(FakeMusic):
        @classmethod
        def from_config(cls, _config):
            return cls()

    installed = {}
    backend_file = tmp_path / "music-backend"
    songs_file = tmp_path / "songs.json"
    order_file = tmp_path / "order.json"
    songs_file.write_text("apple ids", encoding="utf-8")
    order_file.write_text("apple ids", encoding="utf-8")
    config = {"music": {"backend_state_file": str(backend_file)}}
    monkeypatch.setattr(musiclink, "_MUSIC", previous)
    monkeypatch.setattr(musiclink.device_config, "load_config", lambda: config)
    monkeypatch.setattr(musiclink, "SONGS_FILE", songs_file)
    monkeypatch.setattr(musiclink, "ORDER_FILE", order_file)
    monkeypatch.setattr(musiclink.registry, "named_driver_class",
                        lambda _category, _driver: RoonMusic)
    monkeypatch.setattr(
        musiclink.registry, "install",
        lambda category, cls, instance: installed.update(
            category=category, cls=cls, instance=instance))
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(
            musiclink._queue_items([{"pid": PIDS[0]}]), 1)

    result = musiclink.set_music_backend("roon")

    assert previous.paused is True
    assert backend_file.read_text(encoding="utf-8") == "RoonMusic\n"
    assert installed["category"] == "music"
    assert isinstance(installed["instance"], RoonMusic)
    assert musiclink._QUEUE.details()["items"] == []
    assert not songs_file.exists() and not order_file.exists()
    assert result["active_backend"] == "roon"


def test_next_materializes_the_logical_tail_before_advancing(monkeypatch):
    fake = FakeMusic()
    fake.playlist = [PIDS[0]]
    fake.playing = PIDS[0]
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_lead", lambda: 1)
    items = musiclink._queue_items([
        {"pid": PIDS[0], "name": "first"},
        {"pid": PIDS[1], "name": "second"},
    ])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 1)

    musiclink.next_track({})

    assert fake.playlist == PIDS[:2]
    assert fake.playing == PIDS[1]
    assert musiclink._QUEUE.materialized == 2


def test_next_advances_the_catalog_player(monkeypatch):
    fake = FakeMusic()
    fake.catalog_playlist = ["catalog.1", "catalog.2"]
    fake.catalog_playing = "catalog.1"
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    items = musiclink._queue_items([
        {"catalog_id": "catalog.1", "name": "first"},
        {"catalog_id": "catalog.2", "name": "second"},
    ])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 2)

    musiclink.next_track({})

    assert fake.catalog_playing == "catalog.2"


def test_next_hands_off_from_library_to_catalog(monkeypatch):
    fake = FakeMusic()
    fake.playlist = [PIDS[0]]
    fake.playing = PIDS[0]
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    items = musiclink._queue_items([
        {"pid": PIDS[0], "name": "local"},
        {"catalog_id": "catalog.1", "name": "stream"},
        {"catalog_id": "catalog.2", "name": "stream two"},
    ])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 1)

    result = musiclink.next_track({})

    assert result == {"message": "playing next track"}
    assert fake.playing is None
    assert fake.catalog_playing == "catalog.1"
    assert fake.catalog_playlist == ["catalog.1", "catalog.2"]
    assert [item["catalog_id"] for item in musiclink._QUEUE.items] == [
        "catalog.1", "catalog.2",
    ]


def test_next_hands_off_from_catalog_to_library(monkeypatch):
    fake = FakeMusic()
    fake.catalog_playlist = ["catalog.1"]
    fake.catalog_playing = "catalog.1"
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    items = musiclink._queue_items([
        {"catalog_id": "catalog.1", "name": "stream"},
        {"pid": PIDS[0], "name": "local"},
    ])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 1)

    musiclink.next_track({})

    assert fake.catalog_playing is None
    assert fake.playing == PIDS[0]
    assert fake.playlist == [PIDS[0]]


def test_next_restarts_logical_tail_when_physical_player_stopped(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    items = musiclink._queue_items([
        {"catalog_id": "catalog.1", "name": "finished"},
        {"catalog_id": "catalog.2", "name": "next"},
    ])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 2)
        musiclink._QUEUE.current = 0

    result = musiclink.next_track({})

    assert result == {"message": "playing next track"}
    assert fake.catalog_playing == "catalog.2"
    assert fake.catalog_playlist == ["catalog.2"]


def test_adding_to_empty_queue_starts_playback(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_tunable", lambda name, default: 20)

    musiclink.queue_add({
        "pid": PIDS[0], "name": "first", "artist": "one", "album": "record",
    })

    assert fake.playlist == [PIDS[0]]
    assert musiclink._QUEUE.items[0]["name"] == "first"
    assert musiclink._QUEUE.items[0]["album"] == "record"


def test_cloud_playlist_waits_behind_older_logical_tail(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "_lead", lambda: 1)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda revision: None)

    musiclink._dispatch(PIDS[:4], replace=True, play=True)
    musiclink.queue_add({"playlist": PLAYLIST})

    assert fake.bulk_calls == []
    assert [item["pid"] for item in musiclink._QUEUE.items] == \
        PIDS[:4] + PIDS[6:8]

    with musiclink._QUEUE.lock:
        musiclink._materialize_locked(musiclink._QUEUE, 20)
        musiclink._materialize_locked(musiclink._QUEUE, 20)
    assert fake.playlist == PIDS[:4]
    assert fake.bulk_calls == [(PLAYLIST, 1, False, False)]
    assert musiclink._QUEUE.materialized == 6


def test_cloud_playlist_tail_resumes_through_its_bulk_source(monkeypatch):
    fake = FakeMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    source = {"kind": "playlist", "pid": PLAYLIST, "start": 1,
              "batch": "cloud-tail"}
    items = musiclink._queue_items([{"pid": PIDS[0], "name": "first"}])
    items += musiclink._queue_items([
        {"pid": PIDS[6], "name": "cloud one"},
        {"pid": PIDS[7], "name": "cloud two"},
    ], source)
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 3)
    musiclink._QUEUE.observe({"state": "playing", "pid": PIDS[0]})

    musiclink._watch_queue({"state": "stopped", "pid": None})

    assert fake.bulk_calls == [(PLAYLIST, 1, True, True)]
    assert [item["pid"] for item in musiclink._QUEUE.items] == PIDS[6:8]


def test_shuffle_is_avctls_preference_not_musics(monkeypatch):
    class NoMusicShuffle(FakeMusic):
        def set_shuffle(self, on):
            raise AssertionError("Music shuffle must stay untouched")

    fake = NoMusicShuffle()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [
        {"pid": PIDS[0], "name": "one"},
        {"pid": PIDS[1], "name": "two"},
        {"pid": PIDS[2], "name": "three"},
    ])
    monkeypatch.setattr(musiclink.random, "shuffle", lambda tracks: tracks.reverse())
    monkeypatch.setattr(musiclink, "_lead", lambda: 20)

    musiclink.toggle_shuffle({})
    musiclink.play_pause({})

    assert musiclink._QUEUE.shuffle is True
    assert fake.playlist == [PIDS[2], PIDS[1], PIDS[0]]


def test_explicit_play_and_pause_never_toggle(monkeypatch):
    class DirectMusic(FakeMusic):
        def __init__(self):
            super().__init__()
            self.state = "paused"
            self.calls = []

        def now_playing(self):
            return {"state": self.state}

        def play(self):
            self.calls.append("play")
            self.state = "playing"

        def pause(self):
            self.calls.append("pause")
            self.state = "paused"

    fake = DirectMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)

    played = musiclink.play({})
    paused = musiclink.pause({})
    already = musiclink.pause({})

    assert fake.calls == ["play", "pause"]
    assert played == {"message": "music playing"}
    assert paused == {"message": "music paused"}
    assert already == {"message": "music already paused", "acted": False}


def test_explicit_pause_fails_safe_when_playback_readback_is_unknown(
        monkeypatch):
    class UnknownMusic(FakeMusic):
        def __init__(self):
            super().__init__()
            self.paused = False

        def now_playing(self):
            return {"state": None}

        def pause(self):
            self.paused = True

    fake = UnknownMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)

    answer = musiclink.pause({})

    assert fake.paused is True
    assert answer == {"message": "music paused"}


def test_explicit_shuffle_handler_sets_instead_of_toggling():
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.shuffle = True

    answer = musiclink.set_shuffle(False)({})
    second = musiclink.set_shuffle(False)({})

    assert musiclink._QUEUE.shuffle is False
    assert answer == {"message": "shuffle off"}
    assert second == {"message": "shuffle off"}


def test_explicit_repeat_handler_turns_repeat_all_off(monkeypatch):
    class RepeatMusic(FakeMusic):
        def __init__(self):
            super().__init__()
            self.repeat = "all"

        def set_repeat_one(self, on):
            self.repeat = "one" if on else "off"

    fake = RepeatMusic()
    monkeypatch.setattr(musiclink, "_MUSIC", fake)

    answer = musiclink.set_repeat_one(False)({})

    assert fake.repeat == "off"
    assert answer == {"message": "repeat off"}


def test_queue_route_and_focus_markup(monkeypatch):
    items = musiclink._queue_items([{"pid": PIDS[0], "name": "first"}])
    with musiclink._QUEUE.lock:
        musiclink._QUEUE.replace_locked(items, 1)
    warmed = []
    monkeypatch.setattr(
        musiclink, "prefetch_queue_artwork", lambda details: warmed.append(details),
    )
    with TestClient(app) as client:
        answer = client.get("/api/music/queue", headers=AUTH)
    assert answer.status_code == 200
    assert answer.json()["items"][0]["name"] == "first"
    assert warmed == [answer.json()]

    html = views.remote(Identity("test", "token"))
    assert "id='rail-stack'" in html
    assert "class='rail-deck'" in html
    assert "id='m-queue'" in html
    assert "id='mq-back'" in html
    assert "aria-label='Back to transport'>&#8617;" in html
    assert "id='mq-clear'" in html
    assert "next&nbsp;&rsaquo;" in html
    assert "class='mq-split'" in html
    assert html.count("js-mq-list") == 2
    assert "data-clear-label='Stop'" in html
    assert html.count("Stop &amp; clear queue") == 1
    # The queue is transport-shell chrome, not part of the Music page.
    assert html.index("id='page-music'") < html.index("id='m-queue'")
    assert html.index("id='m-queue'") < html.index("class='mq-split'")


def test_queue_artwork_is_eager_but_cache_only():
    root = Path(__file__).resolve().parents[1]
    script = (root / "api/ui/app.js").read_text(encoding="utf-8")

    assert "'/api/music/artwork/' + item.pid + '?cached=1'" in script
    assert "cover(item.pid || item.catalog_id, artwork, true)" in script


def test_new_queue_batch_shuffles_once_only_when_transport_enabled(monkeypatch):
    tracks = [{"pid": pid} for pid in PIDS[:3]]
    monkeypatch.setattr(musiclink.random, "shuffle", lambda rows: rows.reverse())

    musiclink._QUEUE.set_shuffle(False)
    assert musiclink.order_queue_batch(tracks) == tracks

    musiclink._QUEUE.set_shuffle(True)
    assert musiclink.order_queue_batch(tracks) == list(reversed(tracks))
    # The helper copies: model-requested shuffle cannot mutate cached library
    # or catalog search results that another panel is currently rendering.
    assert tracks == [{"pid": pid} for pid in PIDS[:3]]


def test_all_selected_music_layout_surfaces_ship_without_parallel_state():
    html = views.remote(Identity("test", "token"))

    assert "class='m-stage'" in html
    assert "class='m-coverflow'" in html
    assert "id='m-coverflow-track'" in html
    # Every layout points at the same live transport and queue painters.
    assert html.count("js-mnow-track") == 2
    assert html.count("js-mq-count") == 2
    # One utility rail owns both states. Search replaces the library scopes
    # in that rail rather than rendering a second navigator below the field.
    assert html.count("id='m-nav'") == 1
    assert html.count("id='m-view-toggle'") == 1
    assert html.count("id='m-refresh'") == 1
    assert html.count("id='m-lib-scope'") == 1
    assert "data-scope='library'" in html
    assert "data-scope='catalog'>Apple Music" in html
    assert html.index("id='m-scope'") < html.index("id='m-search'")
    assert "Console" not in html
