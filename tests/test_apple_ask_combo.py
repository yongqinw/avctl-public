"""Log-derived Ask scenarios through the real AppleMusic service contract."""

from __future__ import annotations

import json
import threading
import time

import pytest

from api import agentlink, musiclink
from devices.music.apple_music import AppleMusic
from tests.ask_harness import ProviderResponse, ReviewingProvider, ScriptedProvider


class Catalog:
    def __init__(self, songs):
        self.songs = list(songs)
        self.searches = []
        self.added = []
        self._lock = threading.Lock()

    def search_catalog(self, term, kinds, limit):
        with self._lock:
            self.searches.append((term, list(kinds), limit))
        folded = " ".join(term.casefold().split())
        return [dict(row) for row in self.songs
                if str(row["name"]).casefold() in folded
                and str(row["artist"]).casefold() in folded][:limit]

    def add_to_library(self, kind, item_id, credential):
        self.added.append((kind, item_id, credential))


class AppleAskMusic(AppleMusic):
    """AppleMusic's catalog contract with Music.app writes kept in memory."""

    def __init__(self, local, catalog):
        super().__init__(catalog=catalog)
        self.local = list(local)
        self.physical = []

    def now_playing(self):
        return {"state": "stopped", "pid": None, "catalog_id": None,
                "track": None, "artist": None, "album": None}

    def recently_added_songs(self):
        return [dict(row) for row in self.local]

    def play_tracks(self, pids, start=0):
        self.physical.append(("play_library", list(pids[start:])))

    def queue_append(self, pids):
        self.physical.append(("queue_library", list(pids)))

    def play_catalog(self, catalog_ids):
        self.physical.append(("play_catalog", list(catalog_ids)))

    def queue_catalog(self, catalog_ids):
        self.physical.append(("queue_catalog", list(catalog_ids)))


def _tool_batch(*calls: tuple[str, dict]) -> ProviderResponse:
    return ProviderResponse({"choices": [{"message": {"tool_calls": [{
        "id": f"apple-combo-{index}", "type": "function",
        "function": {"name": name,
                     "arguments": json.dumps(arguments, ensure_ascii=False)},
    } for index, (name, arguments) in enumerate(calls, 1)]}}]})


@pytest.fixture
def apple_ask(monkeypatch, tmp_path):
    local = [{
        "pid": f"{index:016X}", "name": f"Happy Track {index:02d}",
        "artist": "Local Artist", "album": "Local Joy", "plays": index,
        "added": time.time() * 1000,
    } for index in range(1, 20, 2)]
    service = [{
        "kind": "song", "id": f"song.{index}",
        "name": f"Happy Track {index:02d}", "artist": "Catalog Artist",
        "album": "Catalog Joy",
    } for index in range(2, 21, 2)]
    catalog = Catalog(service)
    music = AppleAskMusic(local, catalog)
    queue = musiclink.QueueController(tmp_path / "apple-ask-queue.json")
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    monkeypatch.setattr(musiclink, "_QUEUE", queue)
    monkeypatch.setattr(musiclink, "_DISPATCH_LOCK", queue.lock)
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    monkeypatch.setattr(musiclink, "_recent_songs_cache", None)
    monkeypatch.setattr(musiclink, "user_token", lambda: "user-token")
    monkeypatch.setattr(agentlink, "_SESSIONS", {})
    monkeypatch.setattr(agentlink, "_SESSION_USAGE", {})
    monkeypatch.setattr(agentlink, "_SELECTIONS", {})
    monkeypatch.setattr(agentlink, "_REQUESTS", agentlink.OrderedDict())
    monkeypatch.setattr(agentlink, "system_prompt", lambda: ("policy", "v1"))
    monkeypatch.setattr(agentlink, "_live_context", lambda: "{}")
    return music, catalog, queue


def test_logged_mixed_happy_music_prompt_uses_apple_catalog_without_import(
        apple_ask, monkeypatch):
    music, catalog, queue = apple_ask
    candidates = [{
        "title": f"Happy Track {index:02d}",
        "artist": "Local Artist" if index % 2 else "Catalog Artist",
    } for index in range(1, 21)]
    provider = ReviewingProvider(_tool_batch(
        ("music_transport", {"action": "shuffle_on"}),
        ("curate_music", {
            "description": "20 happy songs from my library and Apple Music",
            "source": "both", "exclude_played": False,
            "mode": "append", "candidates": candidates,
        }),
    ))
    monkeypatch.setattr(musiclink.random, "shuffle", lambda rows: rows.reverse())

    answer = agentlink.ask(
        "在我的资料库和 Apple Music 里找 20 首快乐的歌，把它们混在一起，"
        "随机放到 Q 后面，不要加入资料库。",
        "session_apple_1", "caller", session=provider,
        api_key="synthetic-key", request_id="apple_mixed_happy_01",
    )

    with queue.lock:
        queued = list(queue.items)
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 2
    assert provider.review is not None
    assert {row["source"] for row in provider.review["options"]} == {
        "apple_music"
    }
    assert queue.shuffle is True
    assert len(queued) == 20
    assert {bool(row.get("catalog_id")) for row in queued} == {False, True}
    assert [row.get("pid") or row.get("catalog_id") for row in queued] == [
        f"song.{index}" if index % 2 == 0 else f"{index:016X}"
        for index in range(20, 0, -1)
    ]
    assert len(catalog.searches) == 10
    assert all(kinds == ["songs"] and limit == 3
               for _term, kinds, limit in catalog.searches)
    assert catalog.added == []
    assert music.physical == [("play_catalog", ["song.20"])]
    assert "Queued 20 songs" in answer["message"]


def test_apple_complex_play_request_never_mutates_sync_library(apple_ask):
    _music, catalog, queue = apple_ask
    candidates = [
        {"title": "Happy Track 02", "artist": "Catalog Artist"},
        {"title": "Happy Track 04", "artist": "Catalog Artist"},
        {"title": "Happy Track 06", "artist": "Catalog Artist"},
    ]
    provider = ReviewingProvider(_tool_batch(("curate_music", {
        "description": "new exciting music I have not heard",
        "source": "service", "exclude_played": True,
        "mode": "replace", "candidates": candidates,
    })))

    answer = agentlink.ask(
        "I'm feeling sad. Find exciting Apple Music songs I haven't heard "
        "and play them now, but don't add them to my library.",
        "session_apple_2", "caller", session=provider,
        api_key="synthetic-key", request_id="apple_unheard_play_01",
    )

    assert answer["acted"] is True
    assert len(queue.items) == 3
    assert all(row.get("catalog_id") for row in queue.items)
    assert catalog.added == []
    assert "Playing 3 songs" in answer["message"]


def test_logged_classics_prompt_bulk_adds_through_apple_driver(apple_ask):
    _music, catalog, queue = apple_ask
    classics = [
        {"kind": "song", "id": "jay.sunny", "name": "晴天",
         "artist": "Jay Chou", "album": "叶惠美"},
        {"kind": "song", "id": "jay.nocturne", "name": "夜曲",
         "artist": "Jay Chou", "album": "十一月的蕭邦"},
        {"kind": "song", "id": "jj.river", "name": "江南",
         "artist": "JJ Lin", "album": "第二天堂"},
        {"kind": "song", "id": "jj.millennium", "name": "一千年以后",
         "artist": "JJ Lin", "album": "编号89757"},
    ]
    catalog.songs.extend(classics)
    candidates = [{"title": row["name"], "artist": row["artist"]}
                  for row in classics]
    provider = ReviewingProvider(_tool_batch(("curate_music", {
        "description": "Jay Chou and JJ Lin classics",
        "source": "service", "exclude_played": False,
        "mode": "add_only", "candidates": candidates,
    })))

    answer = agentlink.ask(
        "找一些经典的林俊杰和周杰伦的歌曲加入我的 lib。",
        "session_apple_3", "caller", session=provider,
        api_key="synthetic-key", request_id="apple_classics_add_01",
    )

    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert catalog.added == [
        ("songs", row["id"], "user-token") for row in classics
    ]
    assert queue.items == []
    assert "Added 4 reviewed songs" in answer["message"]


def test_apple_ask_model_review_can_override_the_first_same_title_result(
        apple_ask, monkeypatch):
    _music, catalog, queue = apple_ask
    rows = [
        {"kind": "song", "id": "niche.same-title", "name": "晴天",
         "artist": "Niche Singer, Jay Chou", "album": "Unrelated Album",
         "source": "apple_music"},
        {"kind": "song", "id": "jay.sunny", "name": "Sunny Day",
         "artist": "Jay Chou", "album": "Ye Hui Mei",
         "source": "apple_music"},
        {"kind": "song", "id": "jay.sunny.live",
         "name": "Sunny Day (Live)", "artist": "Jay Chou",
         "album": "The Invincible Concert Tour",
         "source": "apple_music"},
    ]
    monkeypatch.setattr(
        catalog, "search_catalog",
        lambda _term, _kinds, limit: [dict(row) for row in rows[:limit]],
    )
    provider = ReviewingProvider(
        _tool_batch(("curate_music", {
            "description": "Jay Chou studio original",
            "source": "service", "exclude_played": False,
            "mode": "add_only",
            "candidates": [{"title": "晴天", "artist": "Jay Chou"}],
        })),
        option_names={"晴天": "Sunny Day"},
    )

    answer = agentlink.ask(
        "把周杰伦《晴天》的录音室原版加入我的资料库，不要现场版。",
        "session_apple_review", "caller", session=provider,
        api_key="synthetic-key", request_id="apple_semantic_review_01",
    )

    assert answer["acted"] is True
    assert answer["trace"]["model_rounds"] == 2
    assert catalog.added == [("songs", "jay.sunny", "user-token")]
    assert queue.items == []
    assert provider.review is not None
    assert provider.review["options"][0]["name"] == "晴天"
    assert "Sunny Day" in {row["name"] for row in provider.review["options"]}
    assert "Sunny Day (Live)" not in {
        row["name"] for row in provider.review["options"]
    }


def test_apple_ask_plays_tracks_added_this_week_shuffled(
        apple_ask, monkeypatch):
    _music, _catalog, queue = apple_ask
    provider = ScriptedProvider([_tool_batch(
        ("music_transport", {"action": "shuffle_on"}),
        ("play_library_added", {"period": "this_week", "mode": "replace"}),
    )])
    monkeypatch.setattr(musiclink.random, "shuffle", lambda rows: rows.reverse())

    answer = agentlink.ask(
        "把我这周刚加进资料库的歌随机播放。",
        "session_apple_4", "caller", session=provider,
        api_key="synthetic-key", request_id="apple_recent_shuffle_01",
    )

    assert answer["acted"] is True
    assert queue.shuffle is True
    assert len(queue.items) == 10
    assert [row["pid"] for row in queue.items] == [
        f"{index:016X}" for index in range(19, 0, -2)
    ]
    assert "playing 10 tracks added this week" in answer["message"]
