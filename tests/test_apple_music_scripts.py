"""The driver's scripts reach osascript in the language they are written in.

_osascript defaults to JavaScript (the read paths are JXA), so every classic
AppleScript write path must SAY lang="AppleScript" -- forgetting it feeds
`tell application "Music"` to the JS parser, which dies on the second word.
That exact miss shipped inside #87 and broke every playlist tap until found
live (2026-08-10). These tests pin the language per script shape, with the
transport stubbed out: nothing here talks to the real Music.app.
"""

from __future__ import annotations

import io
import json

import pytest

from api import musiclink
from devices.music import MusicAuthorizationRequired, MusicError
from devices.music import apple_music
from devices.music.apple_music import AppleMusic, MusicKit
from devices.music.catalog_player import CatalogPlayer


@pytest.fixture
def sent(monkeypatch):
    """Capture (script, lang) for every would-be osascript run."""
    calls: list[tuple[str, str]] = []

    def fake(self, script, lang="JavaScript", timeout=None):
        calls.append((script, lang))
        return ""

    monkeypatch.setattr(AppleMusic, "_osascript", fake)
    return calls


def test_queue_playlist_bulk_speaks_applescript(sent):
    AppleMusic().queue_playlist_bulk("00DEC0DEDEADBEEF", 1,
                                     replace=True, play=True)
    script, lang = sent[-1]
    assert script.startswith('tell application "Music"')
    assert lang == "AppleScript", \
        "classic AppleScript fed to the JS parser dies on 'application'"


def test_every_tell_block_declares_applescript(sent):
    """The general rule, applied to whatever the driver sends: a script that
    opens with `tell` must never ride the JavaScript default."""
    music = AppleMusic()
    music.queue_playlist_bulk("00DEC0DEDEADBEEF", 3, replace=False, play=False)
    music.set_system_volume(56)
    music.set_system_muted(True)
    for script, lang in sent:
        if script.lstrip().startswith(("tell ", "set volume")):
            assert lang == "AppleScript", script.splitlines()[0]


def test_user_playlist_write_is_deduplicated_and_never_plays(monkeypatch):
    calls = []

    def fake(self, script, lang="JavaScript", timeout=None):
        calls.append((script, lang, timeout))
        return "2\t3\ttrue"

    monkeypatch.setattr(AppleMusic, "_osascript", fake)
    result = AppleMusic().add_tracks_to_playlist(
        '本周 "分享"', ["00DEC0DEDEADBEEF", "10DEC0DEDEADBEEF"])

    script, lang, timeout = calls[0]
    assert 'user playlist "本周 \\"分享\\""' in script
    assert script.count("duplicate (every track of library playlist 1") == 2
    assert "count of (every track of destinationPlaylist" in script
    assert "play destinationPlaylist" not in script
    assert 'user playlist "avctl"' not in script
    assert (lang, timeout) == ("AppleScript", 60.0)
    assert result == {
        "name": '本周 "分享"', "created": True, "added": 2, "total": 3,
    }


def test_user_playlist_write_refuses_internal_queue(monkeypatch):
    called = False

    def fake(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(AppleMusic, "_osascript", fake)

    with pytest.raises(ValueError, match="queue is not a user playlist"):
        AppleMusic(queue_playlist="avctl").add_tracks_to_playlist(
            "AVCTL", ["00DEC0DEDEADBEEF"])

    assert called is False


def test_direct_play_and_pause_use_distinct_idempotent_jxa(sent):
    music = AppleMusic()

    music.play()
    music.pause()

    play_script, play_lang = sent[-2]
    pause_script, pause_lang = sent[-1]
    assert "m.play();" in play_script
    assert "m.playpause();" not in play_script
    assert "m.pause();" in pause_script
    assert "m.playpause();" not in pause_script
    assert play_lang == pause_lang == "JavaScript"


def test_catalog_transport_routes_next_to_musickit_not_music_app(sent):
    class Bridge:
        active = True

        def __init__(self):
            self.commands = []

        def command(self, action):
            self.commands.append(action)
            return {}

    music = AppleMusic()
    bridge = Bridge()
    music.catalog_player = bridge

    music.next_track()

    assert bridge.commands == ["next"]
    assert sent == []


def test_catalog_play_pauses_music_app_then_starts_signed_helper(sent):
    class Bridge:
        active = False

        def __init__(self):
            self.replacements = []
            self.authorized = False

        def ensure_authorized(self):
            self.authorized = True

        def replace(self, catalog_ids):
            assert self.authorized is True
            self.replacements.append(list(catalog_ids))
            self.active = True
            return {}

    music = AppleMusic()
    bridge = Bridge()
    music.catalog_player = bridge

    music.play_catalog(["song.1", "song.2"])

    assert bridge.replacements == [["song.1", "song.2"]]
    assert "m.pause();" in sent[0][0]


def test_catalog_play_does_not_pause_music_when_consent_is_off(sent):
    class Bridge:
        active = False

        def ensure_authorized(self):
            raise MusicAuthorizationRequired("enable Apple Music access")

    music = AppleMusic()
    music.catalog_player = Bridge()

    with pytest.raises(MusicAuthorizationRequired,
                       match="enable Apple Music access"):
        music.play_catalog(["song.1"])

    assert sent == []


def test_catalog_player_authorizes_once_per_helper_process(monkeypatch):
    class RunningProcess:
        exit_code = None

        def poll(self):
            return self.exit_code

    player = CatalogPlayer("/synthetic/helper")
    calls = []

    def exchange(action, **kwargs):
        calls.append((action, kwargs))
        player.process = RunningProcess()
        return {}

    monkeypatch.setattr(player, "_exchange", exchange)

    player.ensure_authorized()
    player.ensure_authorized()

    assert calls == [("authorize", {"timeout": 60.0})]

    player.process.exit_code = 1
    player.ensure_authorized()

    assert calls == [
        ("authorize", {"timeout": 60.0}),
        ("authorize", {"timeout": 60.0}),
    ]


def test_finished_catalog_helper_stops_masking_music_app(monkeypatch):
    player = CatalogPlayer("/synthetic/helper")
    player.active = True
    monkeypatch.setattr(
        player, "_exchange", lambda _action: {"state": "stopped"})

    assert player.state_if_active() == {"state": "stopped"}
    assert player.active is False
    assert player.state_if_active() is None


def test_catalog_player_reports_whether_signed_helper_is_installed(tmp_path):
    executable = tmp_path / "AvctlMusicBridge"
    player = CatalogPlayer(executable)

    assert player.available() is False
    executable.write_text("synthetic helper", encoding="utf-8")
    assert player.available() is True


def test_catalog_player_supplies_server_developer_token_only_in_memory(monkeypatch):
    class Process:
        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO(
                '{"id":"request-id","ok":true,"state":{}}\n')

        def poll(self):
            return None

    class Selector:
        def register(self, *_args):
            pass

        def select(self, _timeout):
            return [(object(), object())]

        def close(self):
            pass

    process = Process()
    player = CatalogPlayer(
        "/synthetic/helper",
        developer_token_provider=lambda: "synthetic-developer-token",
    )
    monkeypatch.setattr(player, "_start_locked", lambda: process)
    monkeypatch.setattr("devices.music.catalog_player.uuid.uuid4",
                        lambda: type("ID", (), {"hex": "request-id"})())
    monkeypatch.setattr("devices.music.catalog_player.selectors.DefaultSelector",
                        Selector)

    player._exchange("replace", ["song.1"])

    payload = json.loads(process.stdin.getvalue())
    assert payload == {
        "id": "request-id",
        "action": "replace",
        "catalog_ids": ["song.1"],
        "developer_token": "synthetic-developer-token",
    }


def test_library_song_scan_carries_personal_affinity():
    script = apple_music._JXA_RECENT_SONGS

    assert "t.playedCount()" in script
    assert "t.playedDate()" in script
    assert "t.favorited()" in script


def test_apple_music_owns_the_generic_discovery_service_contract():
    class Catalog:
        def search_albums(self, term, limit):
            return [{"id": "album.1", "album": term, "limit": limit}]

        def search_catalog(self, term, kinds, limit):
            return [{"id": "song.1", "name": term, "kind": "song",
                     "kinds": kinds, "limit": limit}]

        def album_detail(self, item_id):
            return {"id": item_id, "album": "Album", "tracks": []}

        def playlist_tracks(self, item_id):
            return [{"id": item_id, "name": "Track"}]

        def explore(self, credential, limit):
            return {"credential": credential, "limit": limit, "sections": []}

        def add_to_library(self, kind, item_id, credential):
            self.added = (kind, item_id, credential)

        def dev_token(self):
            return "developer-token"

    catalog = Catalog()
    music = AppleMusic(catalog=catalog, catalog_player=__file__)

    assert music.service_info("user-token")["can_add_to_library"] is True
    assert music.service_info("user-token")["can_stream_service"] is True
    assert music.search_service_albums("Blue", 4)[0]["id"] == "album.1"
    assert music.search_service("So What", ["songs"], 3)[0]["id"] == "song.1"
    assert music.service_album("album.1")["album"] == "Album"
    assert music.service_tracks("playlist", "playlist.1")[0]["id"] == "playlist.1"
    assert music.explore_service("user-token", 7)["limit"] == 7
    music.add_service_item("songs", "song.1", "user-token")
    assert catalog.added == ("songs", "song.1", "user-token")
    assert music.service_developer_token() == "developer-token"


def test_bulk_apple_save_continues_after_one_catalog_failure(monkeypatch):
    class Catalog:
        def __init__(self):
            self.added = []

        def add_to_library(self, kind, item_id, credential):
            if item_id == "song.bad":
                raise MusicError("temporary catalog failure")
            self.added.append((kind, item_id, credential))

    catalog = Catalog()
    monkeypatch.setattr(musiclink, "_MUSIC", AppleMusic(catalog=catalog))
    monkeypatch.setattr(musiclink, "user_token", lambda: "user-token")

    result = musiclink.add_many([
        {"kind": "songs", "id": "song.good.1", "name": "One"},
        {"kind": "songs", "id": "song.bad", "name": "Broken"},
        {"kind": "songs", "id": "song.good.2", "name": "Two"},
    ])

    assert result["added_ids"] == ["song.good.1", "song.good.2"]
    assert result["failed_ids"] == ["song.bad"]
    assert catalog.added == [
        ("songs", "song.good.1", "user-token"),
        ("songs", "song.good.2", "user-token"),
    ]


def test_catalog_playlist_can_be_added_to_sync_library(monkeypatch):
    calls = []

    class Accepted:
        status_code = 202

    monkeypatch.setattr(apple_music.requests, "post",
                        lambda url, **kwargs: calls.append((url, kwargs)) or Accepted())
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    music.add_to_library("playlists", "pl.123", "synthetic-user-token")

    assert calls[0][1]["params"] == {"ids[playlists]": "pl.123"}


def test_catalog_search_returns_editorial_playlists(monkeypatch):
    class SearchAnswer:
        ok = True

        def json(self):
            return {"results": {"playlists": {"data": [{
                "id": "pl.hits", "attributes": {
                    "name": "Today's Hits", "curatorName": "Apple Music",
                },
            }]}}}

    monkeypatch.setattr(apple_music.requests, "get",
                        lambda *args, **kwargs: SearchAnswer())
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    assert music.search_catalog("top hits", ["playlists"], 3) == [{
        "kind": "playlist", "id": "pl.hits", "name": "Today's Hits",
        "artist": "Apple Music", "album": None,
    }]


@pytest.mark.parametrize(("query", "song", "album"), [
    ("晴天", "晴天", "叶惠美"),
    ("彩虹", "彩虹", "我很忙"),
])
def test_album_search_promotes_album_containing_exact_keyword_song(
        monkeypatch, query, song, album):
    class SearchAnswer:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    answers = iter([
        SearchAnswer({"results": {"suggestions": [{
            "kind": "topResults", "content": {
                "type": "songs",
                    "id": "song.canonical", "attributes": {
                        "name": song, "artistName": "Jay Chou",
                        "albumName": album,
                    },
                    "relationships": {"albums": {"data": [{
                        "id": "album.canonical", "attributes": {
                            "name": album, "artistName": "Jay Chou",
                            "trackCount": 11, "releaseDate": "2003-07-31",
                            "artwork": {"url": "https://art/{w}x{h}.jpg"},
                        },
                    }]}},
                },
            },
        ]}}),
        SearchAnswer({"results": {"albums": {"data": [{
                    "id": "album.niche", "attributes": {
                        "name": query + "以后", "artistName": "Niche Artist",
                        "trackCount": 9, "releaseDate": "2024-01-02",
                    },
        }]}}}),
    ])

    calls = []
    monkeypatch.setattr(
        apple_music.requests, "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(answers),
    )
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    results = music.search_albums(query)

    assert [(row["album"], row["artist"]) for row in results] == [
        (album, "Jay Chou"), (query + "以后", "Niche Artist"),
    ]
    assert results[0]["art"] == "https://art/300x300.jpg"
    assert calls[0][1]["params"] == {
        "term": query, "kinds": "topResults", "types": "songs,albums",
        "include[songs]": "albums", "limit": 10,
    }
    assert calls[1][1]["params"] == {
        "term": query, "types": "albums", "limit": 25,
    }


def test_album_search_preserves_apple_top_result_order_for_albums_and_songs(
        monkeypatch):
    class Answer:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    answers = iter([
        Answer({"results": {"suggestions": [{
            "kind": "topResults", "content": {
                "type": "albums", "id": "album.apple-first",
                "attributes": {"name": "Apple First", "artistName": "One"},
            },
        }, {
            "kind": "topResults", "content": {
                "type": "songs", "id": "song.apple-second",
                "attributes": {"name": "Apple Second"},
                "relationships": {"albums": {"data": [{
                    "id": "album.apple-second", "type": "albums",
                    "attributes": {"name": "Song Parent", "artistName": "Two"},
                }]}},
            },
        }]}}),
        Answer({"results": {"albums": {"data": [{
            "id": "album.keyword", "attributes": {
                "name": "Keyword Match", "artistName": "Three",
            },
        }]}}}),
    ])
    calls = []
    monkeypatch.setattr(
        apple_music.requests, "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(answers),
    )
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    results = music.search_albums("keyword")

    assert [row["id"] for row in results] == [
        "album.apple-first", "album.apple-second", "album.keyword",
    ]
    assert len(calls) == 2


def test_relevant_album_search_keeps_the_second_page_of_old_results(monkeypatch):
    class SearchAnswer:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    first_albums = [{
        "id": f"album.{number}",
        "attributes": {"name": f"Related {number}", "artistName": "Artist"},
    } for number in range(25)]
    answers = iter([
        SearchAnswer({"results": {"suggestions": []}}),
        SearchAnswer({"results": {
            "albums": {"data": first_albums},
        }}),
        SearchAnswer({"results": {"albums": {"data": [{
            "id": "album.25",
            "attributes": {"name": "Related 25", "artistName": "Artist"},
        }, {
            "id": "album.26",
            "attributes": {"name": "Related 26", "artistName": "Artist"},
        }]}}}),
    ])
    calls = []
    monkeypatch.setattr(
        apple_music.requests, "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or next(answers),
    )
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    results = music.search_albums("晴天")

    assert len(results) == 27
    assert results[0]["id"] == "album.0"
    assert results[-1]["id"] == "album.26"
    assert calls[2][1]["params"] == {
        "term": "晴天", "types": "albums", "limit": 25, "offset": 25,
    }


def test_catalog_playlist_tracks_follow_pagination(monkeypatch):
    calls = []

    class Answer:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    answers = iter([
        Answer({"data": [{"id": "song.1", "attributes": {
            "name": "First", "artistName": "Artist",
            "durationInMillis": 123000,
            "artwork": {"url": "https://art/{w}x{h}.jpg"},
        }}], "next": "/v1/catalog/us/playlists/pl.1/tracks?offset=1"}),
        Answer({"data": [{"id": "song.2", "attributes": {
            "name": "Second", "artistName": "Artist",
        }}]}),
    ])
    monkeypatch.setattr(
        apple_music.requests, "get",
        lambda url, **kwargs: calls.append((url, kwargs)) or next(answers),
    )
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    tracks = music.playlist_tracks("pl.1")

    assert [track["catalog_id"] for track in tracks] == ["song.1", "song.2"]
    assert tracks[0]["duration"] == 123
    assert tracks[0]["art"] == "https://art/400x400.jpg"
    assert calls[1][0].startswith("https://api.music.apple.com/")
    assert calls[1][1]["params"] is None


def test_explore_combines_personal_recommendations_and_public_charts(
        monkeypatch):
    calls = []

    class Answer:
        status_code = 200
        ok = True

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if url == apple_music.RECOMMENDATIONS_URL:
            recommendation = {
                "id": "rec.1", "attributes": {
                    "title": {"stringForDisplay": "Made for You"},
                }, "relationships": {"contents": {"data": [{
                    "id": "album.1", "type": "albums", "attributes": {
                        "name": "Discovery", "artistName": "Someone",
                        "artwork": {"url": "https://art/{w}x{h}.jpg"},
                    },
                }]}},
            }
            return Answer({"data": [recommendation]})
        return Answer({"results": {"songs": [{
            "name": "Top Songs", "data": [{
                "id": "song.1", "type": "songs", "attributes": {
                    "name": "A Real Hit", "artistName": "An Artist",
                },
            }],
        }]}})

    monkeypatch.setattr(apple_music.requests, "get", get)
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    result = music.explore("synthetic-user-token", limit=5)

    assert result["personalized"] is True
    assert [section["title"] for section in result["sections"]] == [
        "Made for You", "Top Songs",
    ]
    assert result["sections"][0]["items"][0]["art"] == (
        "https://art/400x400.jpg")
    assert calls[0][1]["headers"]["Music-User-Token"] == (
        "synthetic-user-token")
    assert calls[1][1]["params"]["types"] == "songs,albums,playlists"


def test_explore_expired_personal_token_still_returns_charts(monkeypatch):
    class Answer:
        ok = True

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.ok = status_code < 400

        def json(self):
            return self.payload

    answers = iter([
        Answer(403, {}),
        Answer(200, {"results": {"playlists": [{
            "name": "Top Playlists", "data": [{
                "id": "pl.1", "type": "playlists", "attributes": {
                    "name": "Today's Music", "curatorName": "Apple Music",
                },
            }],
        }]}}),
    ])
    monkeypatch.setattr(apple_music.requests, "get",
                        lambda *_args, **_kwargs: next(answers))
    music = MusicKit("team", "key", "/unused")
    monkeypatch.setattr(music, "dev_token", lambda: "synthetic-dev-token")

    result = music.explore("expired", limit=5)

    assert result["personalized"] is False
    assert result["personal_error"] == "Apple Music authorization expired"
    assert result["sections"][0]["items"][0]["name"] == "Today's Music"
