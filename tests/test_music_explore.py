from __future__ import annotations

from fastapi.testclient import TestClient

from api import musiclink, views
from api.main import app
from devices.music import AppleMusic, MusicError
from tests.conftest import AUTH


def test_explore_ranks_local_affinity_and_caches_catalog(monkeypatch):
    songs = [{
        "pid": "0000000000000001", "name": "Played Often",
        "artist": "One", "album": "First", "plays": 40,
        "favorited": False,
    }, {
        "pid": "0000000000000002", "name": "Favorite",
        "artist": "Two", "album": "Second", "plays": 1,
        "favorited": True,
    }]
    calls = []

    class Kit:
        @staticmethod
        def explore(token, limit):
            calls.append((token, limit))
            return {"personalized": True, "personal_error": None,
                    "sections": [{"id": "rec", "title": "Made for You",
                                  "kind": "recommendation", "items": []}]}

    monkeypatch.setattr(musiclink, "recent_songs", lambda: songs)
    monkeypatch.setattr(musiclink, "_MUSIC", AppleMusic(catalog=Kit()))
    monkeypatch.setattr(musiclink, "_musickit", lambda: Kit())
    monkeypatch.setattr(musiclink, "user_token", lambda: "synthetic-user-token")
    monkeypatch.setattr(musiclink, "_explore_cache", None)

    first = musiclink.explore(10)
    second = musiclink.explore(10)

    assert first["sections"][0]["title"] == "Your Rotation"
    assert [item["name"] for item in first["sections"][0]["items"]] == [
        "Favorite", "Played Often",
    ]
    assert second == first
    assert calls == [("synthetic-user-token", 20)]


def test_explore_catalog_failure_preserves_local_recommendations(monkeypatch):
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [{
        "pid": "0000000000000001", "name": "Local Song",
        "artist": "One", "album": "Local Album", "plays": 3,
    }])

    class BrokenKit:
        @staticmethod
        def explore(_token, limit):
            raise MusicError(f"catalog failed at {limit}")

    monkeypatch.setattr(
        musiclink, "_MUSIC", AppleMusic(catalog=BrokenKit()))
    monkeypatch.setattr(musiclink, "_musickit", lambda: BrokenKit())
    monkeypatch.setattr(musiclink, "user_token", lambda: None)
    monkeypatch.setattr(musiclink, "_explore_cache", None)

    answer = musiclink.explore(8)

    assert answer["catalog_available"] is False
    assert answer["sections"][0]["items"][0]["name"] == "Local Song"


def test_explore_endpoint_is_authenticated_and_read_only(monkeypatch):
    payload = {"sections": [], "authorized": False,
               "personalized": False, "catalog_available": True}
    monkeypatch.setattr(musiclink, "explore", lambda limit: {
        **payload, "limit": limit,
    })

    with TestClient(app) as client:
        denied = client.get("/api/music/explore")
        answer = client.get("/api/music/explore?limit=12", headers=AUTH)

    assert denied.status_code == 401
    assert answer.status_code == 200
    assert answer.json()["limit"] == 12


def test_catalog_search_endpoint_returns_relevant_albums_only(monkeypatch):
    monkeypatch.setattr(musiclink, "search_albums", lambda query: [{
        "id": "album.1", "album": "叶惠美", "matched_by": query,
    }])
    service = {
        "name": "Apple Music", "source": "apple_music",
        "authorized": True,
    }
    monkeypatch.setattr(musiclink, "service_info", lambda: service)

    with TestClient(app) as client:
        denied = client.get("/api/music/search?q=晴天")
        answer = client.get("/api/music/search?q=晴天", headers=AUTH)

    assert denied.status_code == 401
    assert answer.status_code == 200
    assert answer.json() == {
        "albums": [{"id": "album.1", "album": "叶惠美",
                    "matched_by": "晴天"}],
        "authorized": True,
        "service": service,
    }


def test_explore_ui_is_inside_apple_music_search_and_ipad_safe():
    javascript = open("api/ui/app.js", encoding="utf-8").read()
    stylesheet = open("api/ui/app.css", encoding="utf-8").read()

    assert "'/api/music/explore?limit=12'" in javascript
    assert "if (mScope === 'catalog')" in javascript
    assert "explore.hidden = true" in javascript
    assert "item.source === 'library'" in javascript
    assert "kind: item.kind + 's'" in javascript
    assert ".explore-row{display:grid;grid-auto-flow:column" in stylesheet
    assert "#page-music .explore-row" in stylesheet
    assert "agentMessage(data.message, 'agent', Boolean(data.acted), data.selection)" in javascript
    assert "message.className = 'agent-copy'" in javascript
    assert "domain.includes('qobuz') || domain.includes('roon')" in javascript
    assert "font-size:clamp(16px" in stylesheet
    assert ".agent-result b{font-size:15px" in stylesheet


def test_music_panel_header_tracks_the_active_provider():
    html = views._music()
    javascript = open("api/ui/app.js", encoding="utf-8").read()

    assert "id='m-provider-label'>Music</small>" in html
    assert "Music.app on the mini" not in html
    assert "musicService.source === 'apple_music'" in javascript
    assert "musicService.source === 'roon'" in javascript
    assert "? 'Apple Music'" in javascript
    assert "? 'Roon'" in javascript


def test_catalog_search_ui_remains_album_only():
    javascript = open("api/ui/app.js", encoding="utf-8").read()

    assert "function catalogSearchSong(song)" not in javascript
    assert ": albums.map(catalogTile)" in javascript
