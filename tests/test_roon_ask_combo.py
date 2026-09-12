"""End-to-end Ask plans against the generic queue and a Roon backend.

These scenarios come from real Ask history.  They deliberately keep the
language-model output deterministic while exercising the production handlers,
Roon/Qobuz batch search, and centralized queue together.
"""

from __future__ import annotations

import json

import pytest

from api import agentlink, musiclink
from devices.music import MusicError
from devices.music.roon import RoonMusic
from devices.music.virtual_library import VirtualMusicLibrary
from devices.roon.contracts import MediaItem
from devices.roon.mock import MockRoonController
from tests.ask_harness import ProviderResponse, ReviewingProvider, ScriptedProvider


class AuditedRoonController(MockRoonController):
    """Record provider search shape without replacing the Roon fake."""

    def __init__(self) -> None:
        super().__init__()
        self.batched_searches: list[tuple[list[str], list[str], int]] = []

    def search_service_many(self, queries, kinds, limit=8):
        self.batched_searches.append((list(queries), list(kinds), limit))
        return super().search_service_many(queries, kinds, limit)


class SemanticReviewProvider:
    """Choose translated titles only from the server's numbered options."""

    def __init__(self) -> None:
        self.calls = []
        self.review = None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            return _tool_batch(("curate_music", {
                "description": "Jay Chou studio classics",
                "source": "service", "exclude_played": False,
                "mode": "add_only", "candidates": [
                    {"title": "晴天", "artist": "Jay Chou"},
                    {"title": "青花瓷", "artist": "Jay Chou"},
                    {"title": "明明就", "artist": "Jay Chou"},
                ],
            }))
        messages = kwargs["json"]["messages"]
        tool_message = next(row for row in reversed(messages)
                            if row.get("name") == "curate_music")
        result = json.loads(tool_message["content"])
        self.review = result["semantic_review"]
        wanted = {
            "晴天": "Sunny Day",
            "青花瓷": "Blue and White Porcelain",
            "明明就": "明明就",
        }
        option_by_name = {
            row["name"]: row["option_index"]
            for row in self.review["options"]
        }
        decisions = [{
            "request_index": row["request_index"],
            "option_index": option_by_name[wanted[row["title"]]],
        } for row in self.review["requests"]]
        return _tool_batch(("resolve_music_review", {
            "review_id": self.review["review_id"],
            "decisions": decisions,
        }))


def _tool_batch(*calls: tuple[str, dict]) -> ProviderResponse:
    return ProviderResponse({"choices": [{"message": {"tool_calls": [{
        "id": f"roon-combo-{index}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    } for index, (name, arguments) in enumerate(calls, 1)]}}]})


@pytest.fixture
def roon_ask(monkeypatch, tmp_path):
    core = AuditedRoonController()
    library = VirtualMusicLibrary(tmp_path / "roon-ask-library.sqlite3")
    music = RoonMusic(core, "living-room", "roon-ready", library)
    queue = musiclink.QueueController(tmp_path / "roon-ask-queue.json")
    monkeypatch.setattr(musiclink, "_MUSIC", music)
    monkeypatch.setattr(musiclink, "_QUEUE", queue)
    monkeypatch.setattr(musiclink, "_DISPATCH_LOCK", queue.lock)
    # Queue projection is tested independently.  Keeping the background pump
    # stopped makes the exact logical batch deterministic here.
    monkeypatch.setattr(musiclink, "_start_pump_locked", lambda _revision: None)
    monkeypatch.setattr(musiclink, "_recent_songs_cache", None)
    monkeypatch.setattr(agentlink, "_SESSIONS", {})
    monkeypatch.setattr(agentlink, "_SESSION_USAGE", {})
    monkeypatch.setattr(agentlink, "_SELECTIONS", {})
    monkeypatch.setattr(agentlink, "_REQUESTS", agentlink.OrderedDict())
    monkeypatch.setattr(agentlink, "system_prompt", lambda: ("policy", "v1"))
    monkeypatch.setattr(agentlink, "_live_context", lambda: "{}")
    return core, queue, library


def test_logged_mixed_happy_music_prompt_batches_roon_and_shuffles_queue(
        roon_ask, monkeypatch):
    core, queue, _library = roon_ask
    candidates = [{
        "title": f"Mock Track {index}",
        "artist": "Fixture Artist" if index % 2 else "Qobuz Artist",
    } for index in range(1, 21)]
    provider = ReviewingProvider(_tool_batch(
        ("music_transport", {"action": "shuffle_on"}),
        ("curate_music", {
            "description": "20 happy songs from my library and Qobuz",
            "source": "both", "exclude_played": False,
            "mode": "append", "candidates": candidates,
        }),
    ))
    monkeypatch.setattr(musiclink.random, "shuffle", lambda rows: rows.reverse())

    answer = agentlink.ask(
        "在我的资料库里找一些快乐的歌曲，也在 Qobuz 里找一些快乐的歌曲，"
        "弄个二三十首，把它们混在一起，随机放到 Q 后面，不要加入资料库。",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="roon_mixed_happy_01",
    )

    with queue.lock:
        queued = list(queue.items)
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 2
    assert [row["arguments"]["tool"]
            for row in answer["trace"]["tool_audit"]] == [
        "music_transport", "curate_music", "resolve_music_review",
    ]
    assert queue.shuffle is True
    assert len(queued) == 20
    assert {bool(row.get("catalog_id")) for row in queued} == {False, True}
    assert [row.get("pid") or row.get("catalog_id") for row in queued] == [
        f"mock:{'library' if index % 2 else 'qobuz'}:{index - 1:02d}"
        for index in range(20, 0, -1)
    ]
    assert core.batched_searches == [
        (["Qobuz Artist"], ["songs"], 30),
    ]
    assert "Queued 20 songs" in answer["message"]


def test_logged_new_and_old_jazz_prompt_replaces_roon_queue_once(
        roon_ask, monkeypatch):
    core, queue, _library = roon_ask
    jazz = [
        MediaItem(
            id=f"jazz:{source}:{index}", title=title, artist=artist,
            album=album, source=source, kind="track", added_at=20_000 - index,
        )
        for index, (source, title, artist, album) in enumerate([
            ("library", "So What", "Miles Davis", "Kind of Blue"),
            ("qobuz", "Giant Steps", "John Coltrane", "Giant Steps"),
            ("library", "Take Five", "Dave Brubeck", "Time Out"),
            ("qobuz", "Lingus", "Snarky Puppy", "We Like It Here"),
            ("library", "Cantaloupe Island", "Herbie Hancock", "Empyrean Isles"),
            ("qobuz", "Change of the Guard", "Kamasi Washington", "The Epic"),
        ])
    ]
    core._items.extend(jazz)  # noqa: SLF001 - add real search rows to fake Core
    candidates = [{"title": row.title, "artist": row.artist} for row in jazz]
    provider = ReviewingProvider(_tool_batch(
        ("music_transport", {"action": "shuffle_on"}),
        ("curate_music", {
            "description": "new and old jazz",
            "source": "both", "exclude_played": False,
            "mode": "replace", "candidates": candidates,
        }),
    ))
    monkeypatch.setattr(musiclink.random, "shuffle", lambda rows: rows.reverse())

    answer = agentlink.ask(
        "Hey, I'm feeling jazzy today. Some new jazz, old jazz, shuffle them, "
        "play them right now.",
        "session_456", "caller", session=provider,
        api_key="synthetic-key", request_id="roon_jazz_mix_01",
    )

    with queue.lock:
        queued = list(queue.items)
    assert answer["acted"] is True
    assert len(queued) == 6
    assert queue.shuffle is True
    assert [row.get("pid") or row.get("catalog_id") for row in queued] == [
        row.id for row in reversed(jazz)
    ]
    assert core.zone("living-room").now_playing.id == jazz[-1].id
    assert core.batched_searches == [
        ([
            "Giant Steps John Coltrane",
            "Lingus Snarky Puppy",
            "Change of the Guard Kamasi Washington",
        ], ["songs"], 3),
    ]
    assert "Playing 6 songs" in answer["message"]


def test_logged_classics_library_prompt_saves_only_studio_roon_matches(
        roon_ask):
    core, queue, library = roon_ask
    rows = [
        MediaItem(
            id=item_id, title=title, artist=artist, album=album,
            source="qobuz", kind="track", added_at=30_000 - index,
        )
        for index, (item_id, title, artist, album) in enumerate([
            ("jay:sunny:live", "晴天", "Jay Chou",
             "Jay Chou The Invincible Concert Tour"),
            ("jay:sunny:studio", "晴天", "Jay Chou", "叶惠美"),
            ("jay:nocturne:live", "夜曲", "Jay Chou",
             "JAY 2007 The World Tours"),
            ("jay:nocturne:studio", "夜曲", "Jay Chou", "十一月的蕭邦"),
            ("jj:river:live", "江南 (现场)", "JJ Lin",
             "2011 I AM 世界巡回演唱会"),
            ("jj:river:studio", "江南", "JJ Lin", "第二天堂"),
            ("jj:millennium:live", "一千年以后", "JJ Lin",
             "JJ20 World Tour"),
            ("jj:millennium:studio", "一千年以后", "JJ Lin", "编号89757"),
        ])
    ]
    core._items.extend(rows)  # noqa: SLF001 - add real search rows to fake Core
    candidates = [
        {"title": "晴天", "artist": "Jay Chou"},
        {"title": "夜曲", "artist": "Jay Chou"},
        {"title": "江南", "artist": "JJ Lin"},
        {"title": "一千年以后", "artist": "JJ Lin"},
    ]
    provider = ReviewingProvider(_tool_batch(("curate_music", {
        "description": "Jay Chou and JJ Lin studio classics",
        "source": "service", "exclude_played": False,
        "mode": "add_only", "candidates": candidates,
    })))

    answer = agentlink.ask(
        "找一些经典的林俊杰和周杰伦的歌曲加入我的 lib，录音室版本，不要 live。",
        "session_789", "caller", session=provider,
        api_key="synthetic-key", request_id="roon_studio_library_01",
    )

    saved = library.tracks()
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert {row.provider_item_id for row in saved} == {
        "jay:sunny:studio", "jay:nocturne:studio",
        "jj:river:studio", "jj:millennium:studio",
    }
    assert all("live" not in row.provider_item_id for row in saved)
    assert all("tour" not in row.album.casefold() for row in saved)
    assert queue.items == []
    assert core.batched_searches == [
        (["Jay Chou", "JJ Lin"], ["songs"], 30),
    ]
    assert "Added 4 reviewed songs" in answer["message"]


def test_roon_ask_plays_virtual_tracks_added_this_week_shuffled(
        roon_ask, monkeypatch):
    core, queue, library = roon_ask
    core._items.extend([  # noqa: SLF001 - saved rows need playable source ids
        MediaItem(
            id=f"recent:{index}", title=f"Recently Saved {index}",
            artist="Fixture Artist", album="Recent Album", source="qobuz",
            kind="track",
        )
        for index in range(1, 5)
    ])
    saved = [
        library.add_track("qobuz", f"recent:{index}", {
            "name": f"Recently Saved {index}", "artist": "Fixture Artist",
            "album": "Recent Album",
        })
        for index in range(1, 5)
    ]
    provider = ScriptedProvider([_tool_batch(
        ("music_transport", {"action": "shuffle_on"}),
        ("play_library_added", {"period": "this_week", "mode": "replace"}),
    )])
    monkeypatch.setattr(musiclink.random, "shuffle", lambda rows: rows.reverse())

    answer = agentlink.ask(
        "Play what I added this week and shuffle it.",
        "session_roon_4", "caller", session=provider,
        api_key="synthetic-key", request_id="roon_recent_shuffle_01",
    )

    assert answer["acted"] is True
    assert queue.shuffle is True
    # The virtual library reads newest-first; the deterministic shuffle below
    # reverses that physical order without losing any stable avctl ids.
    assert [row["pid"] for row in queue.items] == saved
    assert "playing 4 tracks added this week" in answer["message"]


def test_roon_ask_uses_second_model_round_for_translated_qobuz_titles(
        roon_ask):
    core, queue, library = roon_ask
    translated = [
        MediaItem(
            id="jay:sunny:studio", title="Sunny Day", artist="Jay Chou",
            album="Ye Hui Mei", source="qobuz", kind="track"),
        MediaItem(
            id="jay:sunny:live", title="Sunny Day (Live)",
            artist="Jay Chou", album="The Invincible Concert Tour",
            source="qobuz", kind="track"),
        MediaItem(
            id="jay:porcelain:studio", title="Blue and White Porcelain",
            artist="Vincent Fang, Jay Chou, Baby C", album="On the Run!",
            source="qobuz", kind="track"),
        MediaItem(
            id="jay:porcelain:cover", title="Blue and White Porcelain",
            artist="Cover Singer, Jay Chou", album="Jay Chou Tribute",
            source="qobuz", kind="track"),
        MediaItem(
            id="jay:obvious:studio", title="明明就", artist="Jay Chou",
            album="Opus 12", source="qobuz", kind="track"),
    ]
    core._items.extend(translated)  # noqa: SLF001 - live Qobuz result shapes

    def semantic_search(queries, kinds, limit=8):
        core.batched_searches.append((list(queries), list(kinds), limit))
        # Live Roon returns English/localized equivalents even when the input
        # contains the Chinese title; the generic mock uses substring search.
        return [list(translated[:limit]) for _query in queries]

    core.search_service_many = semantic_search
    provider = SemanticReviewProvider()

    answer = agentlink.ask(
        "找周杰伦的晴天、青花瓷和明明就录音室原版加入我的 lib。",
        "session_roon_review", "caller", session=provider,
        api_key="synthetic-key", request_id="roon_semantic_review_01",
    )

    assert answer["acted"] is True
    assert answer["trace"]["model_rounds"] == 2
    assert [row["arguments"]["tool"]
            for row in answer["trace"]["tool_audit"]] == [
        "curate_music", "resolve_music_review",
    ]
    assert {row.provider_item_id for row in library.tracks()} == {
        "jay:sunny:studio", "jay:porcelain:studio", "jay:obvious:studio",
    }
    assert queue.items == []
    assert provider.review is not None
    assert all("id" not in row for row in provider.review["options"])
    assert "Sunny Day (Live)" not in {
        row["name"] for row in provider.review["options"]
    }
    assert core.batched_searches == [(["Jay Chou"], ["songs"], 30)]
    assert provider.review["already_verified"] == 0
    assert "Added 3 reviewed songs" in answer["message"]


def test_roon_ask_adds_service_only_selection_to_playlist_atomically(
        roon_ask):
    core, queue, library = roon_ask
    music = musiclink._music()  # noqa: SLF001 - installed fixture backend
    rows = music.search_service("Qobuz Artist", ["songs"], 2)
    result = {
        "matches": [{
            "source": "roon", "kind": "song", "id": row["id"],
            "name": row["name"], "artist": row["artist"],
            "album": row["album"], "art": row["art"],
        } for row in rows],
    }
    selection = agentlink._remember_selection(  # noqa: SLF001
        "caller", "session_playlist", "search_music",
        {"query": "Qobuz Artist"}, result)
    assert selection is not None

    answer = agentlink._act_on_selection(  # noqa: SLF001
        "caller", "session_playlist", {
            "selection_id": selection["id"],
            "action": "add_to_playlist", "playlist": "Qobuz Picks",
        })

    playlist = next(item for item in library.playlists()
                    if item.title == "Qobuz Picks")
    saved = library.collection_tracks(playlist.id)
    assert answer["acted"] is True
    assert "saved 2 from Roon / Qobuz" in answer["message"]
    assert [item.provider_item_id for item in saved] == [
        row["id"] for row in rows]
    assert all(item.provider == "qobuz" for item in saved)
    assert queue.items == []
    assert len(core.queue("living-room")) == 36


def test_roon_playlist_import_resolves_every_service_row_before_writing(
        roon_ask):
    _core, _queue, library = roon_ask
    music = musiclink._music()  # noqa: SLF001 - installed fixture backend
    row = music.search_service("Qobuz Artist", ["songs"], 1)[0]

    with pytest.raises(MusicError, match="could not reverify"):
        music.add_items_to_playlist("Must Stay Empty", [
            {"catalog_id": row["id"], **row},
            {"catalog_id": "missing:qobuz:track", "name": "Missing"},
        ])

    assert library.tracks() == []
    assert library.playlists() == []
