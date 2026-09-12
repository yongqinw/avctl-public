"""The avctl-owned library makes Qobuz bookmarks durable across Roon sessions."""

from __future__ import annotations

from dataclasses import replace

from api import musiclink
from devices.music.roon import RoonMusic
from devices.music.virtual_library import VirtualMusicLibrary
from devices.roon.mock import MockRoonController


def _music(tmp_path, core=None):
    return RoonMusic(
        core or MockRoonController(), "living-room", "roon-ready",
        VirtualMusicLibrary(tmp_path / "roon-library.sqlite3"),
    )


def _qobuz_album(music: RoonMusic) -> dict:
    return music.search_service_albums("Qobuz Artist", 3)[0]


def test_explicit_album_save_is_immediately_browsable_and_playable(tmp_path):
    core = MockRoonController()
    music = _music(tmp_path, core)
    album = _qobuz_album(music)

    music.add_service_item("albums", album["id"])

    saved_album = music.recently_added()[0]
    assert saved_album["album"] == album["album"]
    assert saved_album["pid"].startswith("avlib:album:")
    tracks = music.album_tracks(saved_album["album"], saved_album["artist"])
    assert tracks and all(row["pid"].startswith("avlib:track:") for row in tracks)

    music.play_tracks([row["pid"] for row in tracks])

    assert [row.title for row in core.queue("living-room")] == [
        row["name"] for row in tracks
    ]
    assert all(row.source == "qobuz" for row in core.queue("living-room"))


def test_virtual_library_advertises_add_and_musiclink_refreshes_immediately(
        tmp_path, monkeypatch):
    music = _music(tmp_path)
    album = _qobuz_album(music)
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    monkeypatch.setattr(musiclink, "SONGS_FILE", tmp_path / "songs.json")

    result = musiclink.add({"kind": "albums", "id": album["id"]})

    assert music.service_info()["can_add_to_library"] is True
    assert music.service_info()["library_kind"] == "avctl virtual library"
    assert result == {"message": "saved to the avctl library"}
    assert musiclink.recent_songs(force=True)[0]["pid"].startswith(
        "avlib:track:")


def test_direct_qobuz_play_never_saves_to_virtual_library(tmp_path):
    core = MockRoonController()
    music = _music(tmp_path, core)
    service = music.search_service("Qobuz Artist", ["songs"], 2)

    music.play_catalog([row["id"] for row in service])

    assert music.virtual_library is not None
    assert music.virtual_library.tracks() == []
    assert music.virtual_library.albums() == []


def test_saving_one_song_projects_its_album_into_the_library_grid(tmp_path):
    music = _music(tmp_path)
    song = music.search_service("Qobuz Artist", ["songs"], 1)[0]

    music.add_service_item("songs", song["id"])

    album = music.recently_added()[0]
    assert album["album"] == song["album"]
    tracks = music.album_tracks(album["album"], album["artist"])
    assert tracks[0]["name"] == song["name"]
    assert tracks[0]["pid"].startswith("avlib:track:")


def test_saving_albumless_roon_search_track_resolves_album_tile(tmp_path):
    class AlbumlessSearchCore(MockRoonController):
        def service_item(self, item_id):
            return replace(super().service_item(item_id), album="")

        def search_service(self, query, kinds, limit=8):
            rows = super().search_service(query, kinds, limit)
            if kinds == ["songs"]:
                return [replace(row, album="") for row in rows]
            return rows

    music = _music(tmp_path, AlbumlessSearchCore())
    song = music.search_service("Qobuz Artist", ["songs"], 1)[0]
    assert song["album"] == ""

    music.add_service_item("songs", song["id"])

    album = music.recently_added()[0]
    assert album["album"] == "Mock Album 1"
    assert music.album_tracks(album["album"], album["artist"])[0][
        "name"] == song["name"]


def test_bulk_roon_save_reuses_verified_album_hints_and_isolates_failures(
        tmp_path, monkeypatch):
    class AlbumlessItemCore(MockRoonController):
        def __init__(self):
            super().__init__()
            self.album_searches = 0

        def service_item(self, item_id):
            return replace(super().service_item(item_id), album="")

        def search_service(self, query, kinds, limit=8):
            if kinds == ["albums"]:
                self.album_searches += 1
            return super().search_service(query, kinds, limit)

    core = AlbumlessItemCore()
    music = _music(tmp_path, core)
    songs = music.search_service("Qobuz Artist", ["songs"], 3)
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    refreshes = []
    original_refresh = musiclink.refresh
    monkeypatch.setattr(
        musiclink, "refresh",
        lambda args: refreshes.append(dict(args)) or original_refresh(args),
    )

    result = musiclink.add_many([
        {"kind": "songs", **songs[0]},
        {"kind": "songs", "id": "missing:qobuz:item",
         "name": "Missing", "artist": "Nobody", "album": "Nowhere"},
        {"kind": "songs", **songs[1]},
    ])

    assert result["added_ids"] == [songs[0]["id"], songs[1]["id"]]
    assert result["failed_ids"] == ["missing:qobuz:item"]
    assert refreshes == [{}]
    assert core.album_searches == 0
    assert {(row.title, row.album) for row in music.virtual_library.tracks()} == {
        (songs[0]["name"], songs[0]["album"]),
        (songs[1]["name"], songs[1]["album"]),
    }


def test_backfill_repairs_album_projection_for_older_loose_track(tmp_path):
    core = MockRoonController()
    music = _music(tmp_path, core)
    live = core.search_service("Mock Track 6", ["songs"], 1)[0]
    assert live.album == "Mock Album 2"
    saved_id = music.virtual_library.add_track("qobuz", live.id, {
        "title": live.title, "artist": live.artist, "album": "",
        "duration": live.duration, "image_key": live.image_key,
    })
    assert music.virtual_library.album_summaries() == []

    result = music.backfill_virtual_library_albums()

    assert result == {"repaired": [saved_id], "failed": [], "examined": 1}
    assert music.virtual_library.get(saved_id).album == "Mock Album 2"
    assert music.recently_added()[0]["album"] == "Mock Album 2"


def test_saved_artwork_disambiguates_same_title_service_copies(tmp_path):
    music = _music(tmp_path)
    saved = music.virtual_library.add_track("qobuz", "old-binding", {
        "title": "Same Song", "artist": "Artist", "album": "",
        "image_key": "wanted-cover",
    })
    item = music.virtual_library.get(saved)
    wrong = type("Candidate", (), {
        "title": "Same Song", "artist": "Artist", "album": "",
        "duration": None, "image_key": "other-cover",
    })()
    right = type("Candidate", (), {
        "title": "Same Song", "artist": "Artist", "album": "",
        "duration": None, "image_key": "wanted-cover",
    })()

    assert music._candidate_score(item, right) > music._candidate_score(
        item, wrong)


def test_album_backfill_propagates_only_unambiguous_release_artwork(tmp_path):
    library = VirtualMusicLibrary(tmp_path / "library.sqlite3")
    known = library.add_track("qobuz", "known", {
        "title": "Known", "album": "One Album", "image_key": "same-cover",
    })
    sibling = library.add_track("qobuz", "sibling", {
        "title": "Sibling", "album": "", "image_key": "same-cover",
    })
    library.add_track("qobuz", "collision-a", {
        "title": "A", "album": "First", "image_key": "reused-cover",
    })
    library.add_track("qobuz", "collision-b", {
        "title": "B", "album": "Second", "image_key": "reused-cover",
    })
    ambiguous = library.add_track("qobuz", "ambiguous", {
        "title": "Unknown", "album": "", "image_key": "reused-cover",
    })

    assert library.backfill_track_albums_from_artwork() == [sibling]
    assert library.get(known).album == "One Album"
    assert library.get(sibling).album == "One Album"
    assert library.get(ambiguous).album == ""


def test_album_resolution_falls_back_to_artist_catalog_by_exact_cover(tmp_path):
    class ArtistCatalogCore(MockRoonController):
        def search_service(self, query, kinds, limit=8):
            if kinds == ["albums"] and "Mock Track" in query:
                return []
            return super().search_service(query, kinds, limit)

    core = ArtistCatalogCore()
    music = _music(tmp_path, core)
    live = core.search_service("Mock Track 6", ["songs"], 1)[0]
    albumless = replace(live, album="")

    resolved = music._track_with_album(albumless)

    assert resolved.album == "Mock Album 2"
    assert resolved.image_key == live.image_key


def test_album_collection_does_not_split_cover_by_track_contributors(tmp_path):
    library = VirtualMusicLibrary(tmp_path / "library.sqlite3")
    album_id = library.add_collection("qobuz", "album", "capricorn", {
        "title": "Capricorn", "artist": "Jay Chou",
        "image_key": "capricorn-cover",
    }, [{
        "provider_item_id": "dragon-rider", "title": "Dragon Rider",
        "artist": "Vincent Fang, Jay Chou, Baby C", "album": "Capricorn",
        "image_key": "capricorn-cover",
    }, {
        "provider_item_id": "rice-field", "title": "Rice Field",
        "artist": "Jay Chou, Yanis Huang", "album": "Capricorn",
        "image_key": "capricorn-cover",
    }])

    summaries = library.album_summaries()
    tracks = library.album_tracks("Capricorn", "Jay Chou")

    assert [(item.id, item.title, item.artist) for item in summaries] == [
        (album_id, "Capricorn", "Jay Chou"),
    ]
    assert [item.title for item in tracks] == ["Dragon Rider", "Rice Field"]


def test_loose_tracks_with_one_cover_infer_one_album(tmp_path):
    library = VirtualMusicLibrary(tmp_path / "library.sqlite3")
    library.add_track("qobuz", "loose-one", {
        "title": "One", "artist": "Writer, Singer", "album": "Loose Album",
        "image_key": "shared-cover",
    })
    library.add_track("qobuz", "loose-two", {
        "title": "Two", "artist": "Singer, Producer", "album": "Loose Album",
        "image_key": "shared-cover",
    })

    summaries = library.album_summaries()
    tracks = library.album_tracks("Loose Album", summaries[0].artist)

    assert len(summaries) == 1
    assert [item.title for item in tracks] == ["One", "Two"]


def test_saved_playlist_uses_stable_ids_and_can_fill_the_roon_queue(tmp_path):
    core = MockRoonController()
    music = _music(tmp_path, core)
    playlist = music.search_service("happy", ["playlists"], 1)[0]

    music.add_service_item("playlists", playlist["id"])

    saved = music.playlists()[0]
    assert saved["pid"].startswith("avlib:playlist:")
    detail = music.playlist_tracks(saved["pid"])
    assert detail is not None and detail["tracks"]
    music.queue_playlist_bulk(saved["pid"], 2, replace=True, play=True)
    assert core.queue("living-room")[0].title == detail["tracks"][1]["name"]


def test_virtual_user_playlist_appends_saved_tracks_without_playing(tmp_path):
    core = MockRoonController()
    music = _music(tmp_path, core)
    album = _qobuz_album(music)
    music.add_service_item("albums", album["id"])
    tracks = music.recently_added_songs()
    queue_before = [row.id for row in core.queue("living-room")]

    first = music.add_tracks_to_playlist(
        "This Week", [tracks[0]["pid"], tracks[1]["pid"]])
    repeated = music.add_tracks_to_playlist(
        "this week", [tracks[0]["pid"], tracks[1]["pid"]])

    assert first["created"] is True and first["added"] == 2
    assert repeated["created"] is False and repeated["added"] == 0
    assert repeated["total"] == 2
    playlist = next(row for row in music.playlists()
                    if row["name"].casefold() == "this week")
    detail = music.playlist_tracks(playlist["pid"])
    assert detail is not None
    assert [row["pid"] for row in detail["tracks"]] == [
        tracks[0]["pid"], tracks[1]["pid"],
    ]
    assert [row.id for row in core.queue("living-room")] == queue_before


def test_ask_recent_playlist_executes_against_roon_virtual_library(
        tmp_path, monkeypatch):
    music = _music(tmp_path)
    album = _qobuz_album(music)
    music.add_service_item("albums", album["id"])
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    monkeypatch.setattr(musiclink, "SONGS_FILE", tmp_path / "songs.json")
    musiclink.refresh({})

    result = musiclink.add_added_to_playlist({
        "period": "today", "playlist": "Saved Today",
    })

    assert result["message"].startswith("created Saved Today")
    assert any(row["name"] == "Saved Today" for row in music.playlists())


def test_roon_records_saved_track_play_history_once_per_track_change(tmp_path):
    music = _music(tmp_path)
    album = _qobuz_album(music)
    music.add_service_item("albums", album["id"])
    tracks = music.recently_added_songs()

    music.play_tracks([tracks[0]["pid"], tracks[1]["pid"]])
    music.now_playing()
    music.now_playing()
    first = {row["name"]: row for row in music.recently_added_songs()}

    music.next_track()
    music.now_playing()
    second = {row["name"]: row for row in music.recently_added_songs()}

    assert first[tracks[0]["name"]]["plays"] == 1
    assert first[tracks[1]["name"]]["plays"] == 0
    assert second[tracks[0]["name"]]["plays"] == 1
    assert second[tracks[1]["name"]]["plays"] == 1
    assert second[tracks[1]["name"]]["lastPlayed"] > 0


def test_stale_roon_track_handles_are_healed_as_one_album(tmp_path):
    first = _music(tmp_path)
    album = _qobuz_album(first)
    first.add_service_item("albums", album["id"])
    saved = first.virtual_library
    assert saved is not None
    stable_ids = [row.id for row in saved.tracks()]
    old_bindings = [row.provider_item_id for row in saved.tracks()]

    restarted = MockRoonController()
    restarted._items = [  # noqa: SLF001 - simulate new Roon browse recipes
        replace(item, id=item.id + ":new") if item.source == "qobuz" else item
        for item in restarted._items  # noqa: SLF001
    ]
    music = _music(tmp_path, restarted)

    music.play_tracks(stable_ids)

    new_bindings = [row.provider_item_id for row in saved.tracks()]
    assert new_bindings != old_bindings
    assert all(binding.endswith(":new") for binding in new_bindings)
    assert all(row.status == "ready" for row in saved.tracks())
    assert [row.id for row in restarted.queue("living-room")] == new_bindings


def test_readding_new_roon_handle_keeps_the_stable_avctl_id(tmp_path):
    library = VirtualMusicLibrary(tmp_path / "library.sqlite3")
    first = library.add_track("qobuz", "roon-old", {
        "title": "So What", "artist": "Miles Davis",
        "album": "Kind of Blue", "duration": 545,
    })
    second = library.add_track("qobuz", "roon-new", {
        "title": "So What", "artist": "Miles Davis",
        "album": "Kind of Blue", "duration": 546,
    })

    assert second == first
    assert library.tracks()[0].provider_item_id == "roon-new"
    assert len(library.tracks()) == 1


def test_store_persists_collection_metadata_and_repair_failures(tmp_path):
    path = tmp_path / "library.sqlite3"
    library = VirtualMusicLibrary(path)
    album_id = library.add_collection("qobuz", "album", "album-old", {
        "title": "Kind of Blue", "artist": "Miles Davis", "upc": "123",
    }, [{
        "provider_item_id": "track-old", "title": "So What",
        "artist": "Miles Davis", "album": "Kind of Blue", "isrc": "US-ABC",
    }])
    track = library.collection_tracks(album_id)[0]
    library.mark_failure(track.id, "catalog copy disappeared")

    reopened = VirtualMusicLibrary(path)

    assert reopened.albums()[0].upc == "123"
    assert reopened.collection_tracks(album_id)[0].isrc == "US-ABC"
    assert reopened.repair_report()[0]["library_status"] == "missing"
