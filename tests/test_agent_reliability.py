from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from api import agentlink, musiclink
from api.main import app
from tests.ask_harness import (
    ProviderResponse, ScriptedProvider, text_reply, tool_reply,
)
from tests.conftest import AUTH


LOCAL_SONG = {
    "source": "library", "kind": "song", "name": "Blue in Green",
    "artist": "Miles Davis", "album": "Kind of Blue",
    "pid": "0000000000000001",
}
CATALOG_SONG = {
    "source": "apple_music", "kind": "song", "name": "So What",
    "artist": "Miles Davis", "id": "catalog-1",
}


@pytest.fixture(autouse=True)
def _isolated_reliability_state(monkeypatch):
    monkeypatch.setattr(agentlink, "_SESSIONS", {})
    monkeypatch.setattr(agentlink, "_SESSION_USAGE", {})
    monkeypatch.setattr(agentlink, "_SELECTIONS", {})
    monkeypatch.setattr(agentlink, "_REQUESTS", agentlink.OrderedDict())
    monkeypatch.setattr(agentlink, "system_prompt", lambda: ("policy", "v1"))
    monkeypatch.setattr(agentlink, "_live_context", lambda: "{}")
    monkeypatch.setattr(agentlink.secrets, "token_hex", lambda _size: "fixedselection01")
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [dict(LOCAL_SONG)])


def _remember(caller="caller", session="session_123"):
    selection = agentlink._remember_selection(
        caller, session, "search_music", {"query": "jazz"},
        {"matches": [LOCAL_SONG, CATALOG_SONG]},
    )
    agentlink._save_session(caller, session, [
        {"role": "user", "content": "find jazz"},
        {"role": "assistant", "content": "I found jazz."},
    ])
    return selection


def test_tool_audit_is_allowlisted_and_bounds_music_candidates():
    audited = agentlink._audit_tool_arguments("curate_music", {
        "description": "happy music", "source": "both", "mode": "append",
        "api_key": "synthetic-key-do-not-log",
        "candidates": [
            {"title": "Song " + str(index), "artist": "Artist",
             "private": "drop me"} for index in range(35)
        ],
    })

    assert audited["description"] == "happy music"
    assert audited["candidate_count"] == 35
    assert len(audited["candidates"]) == 30
    assert audited["candidates"][0] == {
        "title": "Song 0", "artist": "Artist",
    }
    assert "api_key" not in str(audited)
    assert "private" not in str(audited)


def test_explicit_library_add_corrects_read_only_curation(monkeypatch):
    inspect = {
        "description": "周杰伦林俊杰经典歌曲", "source": "service",
        "exclude_played": False, "mode": "inspect",
        "candidates": [{"title": "晴天", "artist": "周杰伦"}],
    }
    add_only = {**inspect, "mode": "add_only"}
    provider = ScriptedProvider([
        tool_reply("curate_music", inspect, "inspect"),
        tool_reply("curate_music", add_only, "add"),
    ])
    calls = []
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "curate_music",
        lambda args: calls.append(args) or {
            "message": "Added 晴天.", "acted": True,
        },
    )

    answer = agentlink.ask(
        "把周杰伦林俊杰经典歌曲加入我的lib里",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_add_lib_01",
    )

    assert calls == [add_only]
    assert answer["acted"] is True
    assert answer["trace"]["model_rounds"] == 2


def test_missing_apple_music_player_is_a_safe_no_op_not_unknown(monkeypatch):
    provider = ScriptedProvider([
        tool_reply("play_music_service", {
            "query": "Sleep Sounds", "artist": "Apple Music Sleep",
            "kind": "playlist", "mode": "replace",
        }, "play-sleep"),
        text_reply("I found Sleep Sounds, but direct playback needs setup."),
    ])
    monkeypatch.setattr(musiclink, "search_catalog", lambda *_args, **_kwargs: [{
        "id": "playlist.sleep", "kind": "playlist", "name": "Sleep Sounds",
        "artist": "Apple Music Sleep",
    }])
    monkeypatch.setattr(musiclink, "playlists", lambda: [])
    monkeypatch.setattr(musiclink, "service_info", lambda: {
        "name": "Apple Music", "source": "apple_music",
        "can_stream_service": False,
    })
    monkeypatch.setattr(
        musiclink, "catalog_tracks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unavailable player must stop before expansion")),
    )

    answer = agentlink.ask(
        "播放Apple music睡眠 playlist", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_sleep_playlist_01",
    )

    assert answer["acted"] is False
    assert answer["action_status"] == "no_op"
    assert answer["outcomes"] == [{
        "tool": "play_music_service", "status": "no_op",
    }]
    assert "needs setup" in answer["message"]


def test_bulk_library_add_compacts_oversized_plan_into_one_curate_call(
        monkeypatch):
    repeated = [{
        "id": f"separate-add-{index}",
        "type": "function",
        "function": {
            "name": "play_music_service",
            "arguments": {
                "query": f"Song {index}", "artist": "JJ Lin",
                "kind": "song", "mode": "add_only",
            },
        },
    } for index in range(13)]
    curated = {
        "description": "周杰伦和林俊杰经典歌曲",
        "source": "service", "exclude_played": False,
        "mode": "add_only",
        "candidates": [
            {"title": "晴天", "artist": "Jay Chou"},
            {"title": "江南", "artist": "JJ Lin"},
        ],
    }
    provider = ScriptedProvider([
        ProviderResponse({"choices": [{"message": {
            "tool_calls": repeated,
        }}]}),
        tool_reply("curate_music", curated, "curated-batch"),
    ])
    executed = []
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "play_music_service",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("oversized plan must remain atomic")),
    )
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "curate_music",
        lambda args: executed.append(args) or {
            "message": "Added 晴天 and 江南.", "acted": True,
        },
    )

    answer = agentlink.ask(
        "找一些经典的林俊杰和周杰伦的歌曲 加入我的lib",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_bulk_add_01",
    )

    assert agentlink.MAX_AGENT_ROUNDS == 6
    assert executed == [curated]
    assert answer["acted"] is True
    assert answer["trace"]["model_rounds"] == 2
    assert answer["trace"]["mutating_tool_calls"] == 1
    assert answer["trace"]["planning_rejections"] == [{
        "round": 1,
        "code": "too_many_tool_calls",
        "tool_call_count": 13,
        "tool_counts": {"play_music_service": 13},
    }]
    retry_payload = provider.calls[1][1]["json"]
    assert retry_payload["tool_choice"] == {
        "type": "function", "function": {"name": "curate_music"},
    }
    assert "exactly one curate_music call" in str(retry_payload["messages"])


def test_bulk_library_add_recovers_output_limit_with_forced_curate(
        monkeypatch):
    curated = {
        "description": "周杰伦和林俊杰经典歌曲",
        "source": "service", "exclude_played": False,
        "mode": "add_only",
        "candidates": [{"title": "江南", "artist": "JJ Lin"}],
    }
    provider = ScriptedProvider([
        ProviderResponse({"choices": [{
            "finish_reason": "length", "message": {"content": ""},
        }]}),
        tool_reply("curate_music", curated, "curated-after-limit"),
    ])
    executed = []
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "curate_music",
        lambda args: executed.append(args) or {
            "message": "Added 江南.", "acted": True,
        },
    )

    answer = agentlink.ask(
        "找一些经典的林俊杰和周杰伦的歌曲 加入我的lib",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_bulk_add_02",
    )

    assert executed == [curated]
    assert answer["trace"]["model_rounds"] == 2
    assert answer["trace"]["planning_rejections"] == [{
        "round": 1, "code": "output_limit",
    }]
    assert provider.calls[1][1]["json"]["tool_choice"] == {
        "type": "function", "function": {"name": "curate_music"},
    }


def test_explicit_music_action_recovers_text_only_model_answer(monkeypatch):
    curated = {
        "description": "gentle happy music",
        "source": "both", "exclude_played": False, "mode": "append",
        "candidates": [{"title": "Happy", "artist": "Pharrell Williams"}],
    }
    provider = ScriptedProvider([
        text_reply(
            "The caller wants music from the library and service. I should "
            "prepare candidates and put them in Q."),
        tool_reply("curate_music", curated, "curate-after-text"),
    ])
    executed = []
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "curate_music",
        lambda args: executed.append(args) or {
            "message": "Queued Happy.", "acted": True,
        },
    )

    answer = agentlink.ask(
        "在我的资料库和 Apple Music 里找些轻柔开心的歌，混起来放到 Q 里。",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_missing_action_01",
    )

    assert executed == [curated]
    assert answer["acted"] is True
    assert answer["trace"]["model_rounds"] == 2
    assert answer["trace"]["planning_rejections"] == [{
        "round": 1, "code": "missing_action",
    }]


def test_ambiguous_action_can_still_clarify_after_one_missing_action_retry():
    provider = ScriptedProvider([
        text_reply("I am not sure what 'it' refers to."),
        text_reply("Which song do you mean?"),
    ])

    answer = agentlink.ask(
        "play it", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_missing_action_02",
    )

    assert answer["acted"] is False
    assert answer["message"] == "Which song do you mean?"
    assert answer["trace"]["model_rounds"] == 2
    assert answer["trace"]["planning_rejections"] == [{
        "round": 1, "code": "missing_action",
    }]


def test_semantic_music_review_is_request_bound_and_keeps_recording_guards():
    message = "把周杰伦的晴天录音室原版加入我的 lib"
    public = agentlink._remember_curation_review(
        "caller", "session_123", message, mode="add_only",
        description="Jay Chou studio song",
        requests_to_review=[{
            "request_index": 0, "title": "晴天", "artist": "Jay Chou",
            "option_indexes": [0],
        }],
        options=[{
            "id": "real-provider-id", "name": "Sunny Day (Live)",
            "artist": "Jay Chou", "album": "The Invincible Concert Tour",
            "source": "qobuz",
        }], verified_matches=[],
    )

    with pytest.raises(agentlink.ControlRejected, match="not valid"):
        agentlink._resolve_music_review(
            "caller", "session_123", "different request", {
                "review_id": public["review_id"],
                "decisions": [{"request_index": 0, "option_index": 0}],
            })

    with pytest.raises(agentlink.ControlRejected, match="recording-version safeguards"):
        agentlink._resolve_music_review(
            "caller", "session_123", message, {
                "review_id": public["review_id"],
                "decisions": [{"request_index": 0, "option_index": 0}],
            })

    with pytest.raises(agentlink.ControlRejected, match="offered option"):
        agentlink._resolve_music_review(
            "caller", "session_123", message, {
                "review_id": public["review_id"],
                "decisions": [{"request_index": 0, "option_index": 1}],
            })


def test_semantic_music_review_can_reject_every_service_candidate():
    message = "播放我想听的那首歌"
    public = agentlink._remember_curation_review(
        "caller", "session_123", message, mode="replace",
        description="ambiguous song",
        requests_to_review=[{
            "request_index": 0, "title": "同名歌曲", "artist": "目标歌手",
            "option_indexes": [0],
        }],
        options=[{
            "id": "different-song", "name": "同名歌曲", "artist": "另一位歌手",
            "album": "Unrelated Album", "source": "apple_music",
        }], verified_matches=[],
    )

    result = agentlink._resolve_music_review(
        "caller", "session_123", message, {
            "review_id": public["review_id"], "decisions": [],
        })

    assert result == {
        "message": ("I couldn't identify a sufficiently certain recording, "
                    "so I changed nothing."),
        "acted": False,
    }
    assert public["review_id"] not in agentlink._CURATION_REVIEWS


def test_recovery_round_cannot_exceed_turn_mutation_budget(monkeypatch):
    first_batch = [{
        "id": f"first-{index}", "type": "function",
        "function": {
            "name": "music_transport", "arguments": {"action": "next"},
        },
    } for index in range(8)]
    oversized_retry = [{
        "id": f"retry-{index}", "type": "function",
        "function": {
            "name": "music_transport", "arguments": {"action": "next"},
        },
    } for index in range(5)]
    provider = ScriptedProvider([
        ProviderResponse({"choices": [{"message": {
            "tool_calls": first_batch,
        }}]}),
        ProviderResponse({"choices": [{"message": {
            "tool_calls": oversized_retry,
        }}]}),
    ])
    executed = []
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "music_transport",
        lambda args: executed.append(args) or {
            "message": "No queue change.", "acted": False,
        },
    )

    with pytest.raises(agentlink.AgentError, match="per-turn action limit"):
        agentlink.ask(
            "skip unavailable tracks and try the next one",
            "session_123", "caller", session=provider,
            api_key="synthetic-key", request_id="request_action_cap_01",
        )

    assert len(executed) == 8


def test_discovery_returns_opaque_selection_without_persistent_ids(monkeypatch):
    provider = ScriptedProvider([
        tool_reply("search_music", {
            "query": "jazz", "source": "both", "kind": "song",
        }),
        text_reply("Here are the jazz results."),
    ])
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "search_music",
        lambda _args: {"message": "found jazz",
                       "matches": [LOCAL_SONG, CATALOG_SONG]},
    )

    answer = agentlink.ask(
        "find some jazz", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_find_01",
    )

    assert answer["selection"] == {
        "id": "sel_fixedselection01", "description": "jazz", "count": 2,
        "actionable_local_songs": 1,
        "items": [
            {"source": "library", "kind": "song",
             "name": "Blue in Green", "artist": "Miles Davis"},
            {"source": "apple_music", "kind": "song",
             "name": "So What", "artist": "Miles Davis"},
        ],
    }
    assert "pid" not in str(answer["selection"]).casefold()
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 2
    assert answer["trace"]["tool_audit"] == [{
        "round": 1,
        "status": "succeeded",
        "arguments": {
            "tool": "search_music", "query": "jazz",
            "source": "both", "kind": "song",
        },
    }]


def test_selection_keeps_thirty_results_and_expires(monkeypatch):
    matches = [{
        "source": "library", "kind": "song", "name": f"Song {index}",
        "artist": "Artist", "pid": f"{index + 1:016X}",
    } for index in range(30)]
    selection = agentlink._remember_selection(
        "caller", "session_123", "curate_music", {"description": "thirty"},
        {"matches": matches},
    )

    assert selection is not None
    assert len(selection["items"]) == 30
    assert agentlink._latest_selection("caller", "session_123")["id"] == selection["id"]

    monkeypatch.setattr(agentlink.time, "time", lambda: (
        selection["created_at"] + agentlink.SELECTION_TTL_SECONDS + 1))
    assert agentlink._latest_selection("caller", "session_123") is None


def test_rejected_prewrite_tool_gets_one_model_correction(monkeypatch):
    provider = ScriptedProvider([
        tool_reply("set_input", {"device": "tv", "input": "banana"}, "bad"),
        tool_reply("set_input", {"device": "tv", "input": "hdmi2"}, "fixed"),
    ])
    commands = []
    monkeypatch.setattr(
        agentlink, "_run_command",
        lambda command, values=None: commands.append(command)
        or {"message": "TV input is HDMI 2", "acted": True},
    )

    answer = agentlink.ask(
        "switch the TV to HDMI 2", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_repair_01",
    )

    assert commands == ["tv.input.hdmi2"]
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 2
    retry_messages = provider.calls[1][1]["json"]["messages"]
    failure = [row for row in retry_messages if row.get("role") == "tool"][0]
    assert '"code": "control_rejected"' in failure["content"]
    assert '"safe_to_retry": true' in failure["content"]


def test_terminal_noop_can_be_replanned_once(monkeypatch):
    provider = ScriptedProvider([
        tool_reply("music_transport", {"action": "play"}, "noop"),
        tool_reply("music_transport", {"action": "next"}, "fixed"),
    ])
    calls = []

    def transport(args):
        calls.append(args["action"])
        if args["action"] == "play":
            return {"message": "nothing to resume", "acted": False}
        return {"message": "skipped", "acted": True}

    monkeypatch.setitem(agentlink.TOOL_HANDLERS, "music_transport", transport)
    answer = agentlink.ask(
        "skip this", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_noop_repair_01",
    )

    assert calls == ["play", "next"]
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 2


def test_queue_can_be_inspected_without_mutation(monkeypatch):
    monkeypatch.setattr(musiclink, "queue_details", lambda: {
        "shuffle": True, "playing": {"name": "So What", "artist": "Miles Davis"},
        "items": [{"name": "Freddie Freeloader", "artist": "Miles Davis"}],
    })

    result = agentlink._inspect_queue({})

    assert result["queue"]["count"] == 1
    assert result["queue"]["shuffle"] is True
    assert "Playing · So What — Miles Davis" in result["message"]
    assert "1. Freddie Freeloader — Miles Davis" in result["message"]


def test_model_can_open_music_explore_on_the_callers_ui(monkeypatch):
    from api import views
    monkeypatch.setattr(views, "panel_order", lambda: ["home", "music", "agent"])
    provider = ScriptedProvider([tool_reply("show_panel", {
        "panel": "music", "music_view": "explore",
    })])

    answer = agentlink.ask(
        "open Explore", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_show_panel_01",
    )

    assert answer["ui"] == {"panel": "music", "music_view": "explore"}
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"


def test_curator_catalog_failure_does_not_cancel_later_candidates(monkeypatch):
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [])
    attempts = []

    def search(query, _kinds, limit):
        attempts.append(query)
        if query.startswith("Broken"):
            raise RuntimeError("temporary catalog failure")
        return [{"id": "catalog-good", "name": "Happy", "artist": "Good"}]

    monkeypatch.setattr(musiclink, "search_catalog", search)
    result = agentlink._curate_music({
        "description": "happy", "source": "apple_music",
        "exclude_played": False, "mode": "inspect",
        "candidates": [
            {"title": "Broken", "artist": "Service"},
            {"title": "Happy", "artist": "Good"},
        ],
    })

    assert attempts.count("Broken Service") == 2
    assert any(row["id"] == "catalog-good" for row in result["matches"])
    assert "Apple Music unavailable · Broken — Service" in result["message"]


def test_curator_honors_source_and_unheard_filter(monkeypatch):
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [{
        "pid": "0000000000000001", "name": "Heard Song", "artist": "Known",
        "plays": 9,
    }])
    monkeypatch.setattr(musiclink, "search_catalog", lambda *_args, **_kwargs: [
        {"id": "heard", "name": "Heard Song", "artist": "Known"},
        {"id": "new", "name": "New Song", "artist": "New Artist"},
    ])

    result = agentlink._curate_music({
        "description": "new to me", "source": "apple_music",
        "exclude_played": True, "mode": "inspect",
        "candidates": [{"title": "Heard Song", "artist": "Known"}],
    })

    assert not result["matches"]
    assert "already played" in result["message"]


def test_explore_music_uses_real_sections_and_source_filter(monkeypatch):
    monkeypatch.setattr(musiclink, "explore", lambda _limit: {
        "authorized": True, "personalized": True,
        "catalog_available": True,
        "sections": [{"title": "Your Rotation", "items": [LOCAL_SONG]}, {
            "title": "Made for You", "items": [CATALOG_SONG],
        }],
    })

    result = agentlink._explore_music({
        "source": "apple_music", "limit": 8,
    })

    assert result["matches"] == [{
        "source": "apple_music", "kind": "song", "name": "So What",
        "artist": "Miles Davis", "album": "", "id": "catalog-1",
    }]
    assert "Made for You: So What — Miles Davis" in result["message"]
    assert "Blue in Green" not in result["message"]


def test_broad_recommendation_directly_creates_verified_selection(monkeypatch):
    provider = ScriptedProvider([
        tool_reply("explore_music", {"source": "both", "limit": 8}),
        text_reply("I found a few things you may like."),
    ])
    monkeypatch.setattr(musiclink, "explore", lambda _limit: {
        "authorized": True, "personalized": True,
        "catalog_available": True,
        "sections": [{"title": "Your Rotation", "items": [LOCAL_SONG]}, {
            "title": "Made for You", "items": [CATALOG_SONG],
        }],
    })

    answer = agentlink.ask(
        "find me some music I might like", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_explore_01",
    )

    assert answer["selection"]["count"] == 2
    assert answer["selection"]["actionable_local_songs"] == 1
    assert answer["trace"]["model_rounds"] == 2
    assert len(provider.calls) == 2


def test_apple_music_recommend_and_play_streams_without_library_mutation(
        monkeypatch):
    provider = ScriptedProvider([
        tool_reply("explore_music", {
            "source": "apple_music", "limit": 8,
        }),
        tool_reply("act_on_selection", {
            "selection_id": "sel_fixedselection01", "action": "play",
            "playlist": "",
        }),
    ])
    dispatched = []
    monkeypatch.setattr(musiclink, "explore", lambda _limit: {
        "authorized": True, "personalized": True,
        "catalog_available": True,
        "sections": [{"title": "Made for You", "items": [CATALOG_SONG]}],
    })
    monkeypatch.setattr(
        musiclink, "add",
        lambda _item: (_ for _ in ()).throw(
            AssertionError("discovery must not mutate the library")),
    )
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatched.append(
            (tracks, replace, play)) or len(tracks),
    )

    answer = agentlink.ask(
        "帮我找点歌，然后在 Apple Music 里找，然后再播放",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_explore_play_01",
    )

    assert answer["acted"] is True
    assert answer["selection"]["count"] == 1
    assert len(dispatched) == 1
    assert dispatched[0][0][0]["catalog_id"] == CATALOG_SONG["id"]
    assert dispatched[0][1:] == (True, True)
    assert "playing" in answer["message"].casefold()
    assert answer["trace"]["model_rounds"] == 2


def test_explore_playback_failure_is_reported_as_unknown(monkeypatch):
    provider = ScriptedProvider([
        tool_reply("explore_music", {"source": "both", "limit": 8}),
        tool_reply("act_on_selection", {
            "selection_id": "sel_fixedselection01", "action": "play",
            "playlist": "",
        }),
    ])
    monkeypatch.setattr(musiclink, "explore", lambda _limit: {
        "authorized": False, "personalized": False,
        "catalog_available": False,
        "sections": [{"title": "Your Rotation", "items": [
            LOCAL_SONG,
            {**LOCAL_SONG, "pid": "0000000000000002", "name": "So Blue"},
        ]}],
    })
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("driver acknowledgement failed")),
    )
    pokes = []
    monkeypatch.setattr(agentlink.state, "poke", lambda: pokes.append(True))

    answer = agentlink.ask(
        "find me music I might like and play it",
        "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_explore_unknown_01",
    )

    assert answer["action_status"] == "unknown"
    assert "status is unknown" in answer["message"]
    assert pokes == [True]


def test_followup_queues_verified_local_and_catalog_ids(monkeypatch):
    selection = _remember()
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "queue", "playlist": "",
    })])
    dispatches = []
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatches.append(
            ([track.get("pid") or track.get("catalog_id") for track in tracks],
             replace, play)),
    )

    answer = agentlink.ask(
        "queue those", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_queue_01",
    )

    assert dispatches == [
        ([LOCAL_SONG["pid"], CATALOG_SONG["id"]], False, False),
    ]
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert "skipped" not in answer["message"]
    assert answer["trace"]["model_rounds"] == 1


@pytest.mark.parametrize("message", [
    "把那些加到Q里我听", "好的，把他们放到 q 里", "add those to Q",
])
def test_q_shorthand_is_an_explicit_queue_followup(monkeypatch, message):
    selection = _remember()
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "queue", "playlist": "",
    })])
    dispatches = []
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatches.append(
            ([track.get("pid") or track.get("catalog_id") for track in tracks],
             replace, play)),
    )

    answer = agentlink.ask(
        message, "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_q_followup_01")

    assert dispatches == [
        ([LOCAL_SONG["pid"], CATALOG_SONG["id"]], False, False),
    ]
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 1


def test_catalog_selection_reverifies_an_existing_local_playlist(monkeypatch):
    playlist = {"pid": "0000000000000042", "name": "Jazz Friends"}
    monkeypatch.setattr(musiclink, "playlists", lambda: [playlist])
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "jazz"},
        {"matches": [{
            "source": "apple_music", "kind": "playlist",
            "name": "Jazz Friends", "artist": "Miles Davis",
            "id": "catalog-playlist-1",
        }]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find jazz"},
        {"role": "assistant", "content": "I found Jazz Friends."},
    ])
    commands = []
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "queue", "playlist": "",
    })])
    monkeypatch.setattr(
        agentlink, "_run_command",
        lambda command, values: commands.append((command, values))
        or {"message": "queued playlist"},
    )

    answer = agentlink.ask(
        "把它加到Q里", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_q_playlist_01")

    assert selection is not None
    assert commands == [("music.queue_add", {"playlist": playlist["pid"]})]
    assert answer["action_status"] == "succeeded"


def test_catalog_only_queue_streams_without_importing(monkeypatch):
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "So What"},
        {"matches": [CATALOG_SONG]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find So What"},
        {"role": "assistant", "content": "I found So What."},
    ])
    monkeypatch.setattr(
        musiclink, "add",
        lambda _item: (_ for _ in ()).throw(
            AssertionError("queue follow-up must not import catalog music")),
    )
    dispatches = []
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "queue", "playlist": "",
    })])
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatches.append(
            ([track.get("catalog_id") for track in tracks], replace, play)),
    )

    answer = agentlink.ask(
        "把那个加到Q里", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_q_catalog_01")

    assert dispatches == [([CATALOG_SONG["id"]], False, False)]
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"


def test_catalog_selection_reports_music_consent_as_known_rejection(monkeypatch):
    from devices.music import MusicAuthorizationRequired

    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "sleep"},
        {"matches": [{
            "source": "apple_music", "kind": "playlist",
            "name": "Sleep Sounds", "artist": "Apple Music Sleep",
            "id": "playlist.sleep",
        }]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find a sleep playlist"},
        {"role": "assistant", "content": "I found Sleep Sounds."},
    ])
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [])
    monkeypatch.setattr(musiclink, "playlists", lambda: [])
    monkeypatch.setattr(musiclink, "catalog_tracks", lambda *_args: [{
        "catalog_id": "song.sleep.1", "name": "Rain",
    }])
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            MusicAuthorizationRequired(
                "Apple Music permission is off on the Mac mini."
            )
        ),
    )
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])

    answer = agentlink.ask(
        "播放啊", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_sleep_consent_01",
    )

    assert answer["acted"] is False
    assert answer["action_status"] == "rejected"
    assert answer["outcomes"] == [
        {"tool": "act_on_selection", "status": "rejected"},
    ]
    assert "permission is off" in answer["message"]
    assert answer["trace"]["model_rounds"] == 1


def test_catalog_selection_still_streams_when_local_scan_fails(monkeypatch):
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "So What"},
        {"matches": [CATALOG_SONG]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find So What"},
        {"role": "assistant", "content": "I found So What."},
    ])
    monkeypatch.setattr(
        musiclink, "recent_songs",
        lambda: (_ for _ in ()).throw(RuntimeError("Music.app unavailable")),
    )
    dispatched = []
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatched.append(
            (tracks, replace, play)) or len(tracks),
    )

    answer = agentlink.ask(
        "play it", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_catalog_without_library_01")

    assert answer["acted"] is True
    assert dispatched[0][0][0]["catalog_id"] == CATALOG_SONG["id"]


def test_catalog_album_expansion_does_not_report_negative_skips(monkeypatch):
    album = {
        "source": "apple_music", "kind": "album", "name": "Kind of Blue",
        "artist": "Miles Davis", "id": "catalog-album-1",
    }
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "Kind of Blue"},
        {"matches": [album]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find Kind of Blue"},
        {"role": "assistant", "content": "I found the album."},
    ])
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [])
    monkeypatch.setattr(musiclink, "catalog_tracks", lambda *_args: [
        {"catalog_id": "catalog.1", "name": "So What"},
        {"catalog_id": "catalog.2", "name": "Freddie Freeloader"},
    ])
    monkeypatch.setattr(
        musiclink, "_dispatch", lambda tracks, replace, play: len(tracks))
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])

    answer = agentlink.ask(
        "play it", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_catalog_album_01")

    assert answer["acted"] is True
    assert "playing 2 songs" in answer["message"].casefold()
    assert "skipped" not in answer["message"].casefold()


def test_plural_catalog_containers_expand_into_one_queue_batch(monkeypatch):
    albums = [{
        "source": "apple_music", "kind": "album", "name": name,
        "artist": artist, "id": catalog_id,
    } for name, artist, catalog_id in (
        ("Heavy Rotation", "Apple Music", "album-1"),
        ("Your Essentials", "Apple Music", "album-2"),
    )]
    selection = agentlink._remember_selection(
        "caller", "session_123", "explore_music",
        {"description": "new music"}, {"matches": albums},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find new music"},
        {"role": "assistant", "content": "I found two albums."},
    ])
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [])
    monkeypatch.setattr(
        musiclink, "catalog_tracks",
        lambda _kind, catalog_id: [{
            "catalog_id": catalog_id + "-song",
            "name": "Song from " + catalog_id,
        }],
    )
    dispatched = []
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatched.append(
            ([track["catalog_id"] for track in tracks], replace, play)),
    )
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])

    answer = agentlink.ask(
        "play them all", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_plural_albums_01",
    )

    assert dispatched == [
        (["album-1-song", "album-2-song"], True, True),
    ]
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert "playing 2 songs" in answer["message"].casefold()


def test_mixed_selection_preserves_verified_result_order(monkeypatch):
    catalog_first = {**CATALOG_SONG, "id": "catalog-first"}
    catalog_last = {
        **CATALOG_SONG, "id": "catalog-last", "name": "All Blues",
    }
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "jazz"},
        {"matches": [catalog_first, LOCAL_SONG, catalog_last]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find jazz"},
        {"role": "assistant", "content": "I found three songs."},
    ])
    dispatched = []
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatched.extend(
            track.get("pid") or track.get("catalog_id") for track in tracks),
    )

    answer = agentlink.ask(
        "play those", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_mixed_order_01")

    assert answer["acted"] is True
    assert dispatched == [
        "catalog-first", LOCAL_SONG["pid"], "catalog-last",
    ]


def test_explicit_catalog_selection_imports_songs_then_plays(monkeypatch):
    monkeypatch.setattr(
        musiclink, "service_info", lambda: {
            "name": "Apple Music", "can_add_to_library": True})
    second_song = {
        "source": "apple_music", "kind": "song", "name": "Helpless",
        "artist": "Yoshiaki Fujisawa", "id": "catalog-2",
    }
    album = {
        "source": "apple_music", "kind": "album", "name": "Anime Score",
        "artist": "Yoshiaki Fujisawa", "id": "catalog-album-1",
    }
    local_by_id = {
        "catalog-1": {
            **LOCAL_SONG, "name": "So What", "pid": "0000000000000002",
        },
        "catalog-2": {
            **LOCAL_SONG, "name": "Helpless",
            "artist": "Yoshiaki Fujisawa", "pid": "0000000000000003",
        },
    }
    library = []
    additions = []
    dispatches = []
    monkeypatch.setattr(
        musiclink, "recent_songs", lambda force=False: list(library))

    def add_many(items):
        for item in items:
            additions.append({"kind": item["kind"], "id": item["id"]})
            library.append(dict(local_by_id[item["id"]]))
        return {"added_ids": [item["id"] for item in items],
                "failed_ids": []}

    monkeypatch.setattr(musiclink, "add_many", add_many)
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatches.append(
            ([track.get("pid") or track.get("catalog_id") for track in tracks],
             replace, play)),
    )
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "anime"},
        {"matches": [CATALOG_SONG, second_song, album]},
    )
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find anime music"},
        {"role": "assistant", "content": "I found three results."},
    ])
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "add_and_play",
        "playlist": "",
    })])

    answer = agentlink.ask(
        "那就把那些歌放到本地，然后播放", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_import_play_01")

    assert additions == [
        {"kind": "songs", "id": "catalog-1"},
        {"kind": "songs", "id": "catalog-2"},
    ]
    assert dispatches == [
        (["0000000000000002", "0000000000000003"], True, True),
    ]
    assert answer["acted"] is True
    assert answer["action_status"] == "succeeded"
    assert answer["trace"]["model_rounds"] == 1
    assert "Now playing 2 songs" in answer["message"]
    assert "added 2 from Apple Music" in answer["message"]


def test_angry_mandarin_play_command_still_plays_selection(monkeypatch):
    selection = _remember()
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])
    dispatches = []
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatches.append(
            ([track.get("pid") or track.get("catalog_id") for track in tracks],
             replace, play)),
    )
    message = "哎呦，操你妈，老子都说了，你TM赶紧给我播!"

    answer = agentlink.ask(
        message, "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_angry_play_01")

    assert dispatches == [
        ([LOCAL_SONG["pid"], CATALOG_SONG["id"]], True, True),
    ]
    assert answer["acted"] is True
    assert answer["trace"]["model_rounds"] == 1


@pytest.mark.parametrize("message", [
    "不要把那些歌放到本地，只是播放",
    "don't put those songs locally, just play them",
])
def test_negated_local_import_never_authorizes_library_change(message):
    assert agentlink._requested_library_add(message) is False


def test_duplicate_negative_receipts_are_collapsed():
    receipt = "I couldn't resolve that."
    assert agentlink._join_receipts([receipt, receipt]) == receipt


def test_prompt_prefers_clarification_and_does_not_moralize():
    prompt = agentlink.STATIC_SYSTEM_PROMPT
    assert "The selected model is the sole intent planner" in prompt
    assert "never depend on exact command phrasing" in prompt
    assert "Emotion, profanity, repetition, or urgency does not negate" in prompt
    assert "two or three closest real choices" in prompt
    assert "reason to scold, moralize, or stop helping" in prompt
    assert "Never claim that you searched, found, recommended" in prompt
    assert '"Q" or "q"' in prompt


def test_fireworks_asks_one_useful_question_for_ambiguous_discovery():
    provider = ScriptedProvider([
        text_reply("你想让我找音乐、你的资料库内容，还是某个设备或场景？"),
    ])
    answer = agentlink.ask(
        "我操你妈的，老子说你找点东西。", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_clarify_01")

    assert answer["acted"] is False
    assert answer["message"] == "你想让我找音乐、你的资料库内容，还是某个设备或场景？"
    assert answer["trace"]["model_rounds"] == 1
    assert len(provider.calls) == 1


def test_fireworks_owns_selection_reference_semantics(monkeypatch):
    selection = _remember()
    dispatches = []
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda tracks, replace, play: dispatches.append(
            ([track.get("pid") or track.get("catalog_id") for track in tracks],
             replace, play)),
    )
    wrong = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])

    first = agentlink.ask(
        "play Beethoven", "session_123", "caller", session=wrong,
        api_key="synthetic-key", request_id="request_wrong_object",
    )

    assert dispatches == [
        ([LOCAL_SONG["pid"], CATALOG_SONG["id"]], True, True),
    ]
    assert first["action_status"] == "succeeded"

    bare = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "play", "playlist": "",
    })])
    allowed = agentlink.ask(
        "播放啊 傻逼", "session_123", "caller", session=bare,
        api_key="synthetic-key", request_id="request_bare_play",
    )

    assert dispatches == [
        ([LOCAL_SONG["pid"], CATALOG_SONG["id"]], True, True),
        ([LOCAL_SONG["pid"], CATALOG_SONG["id"]], True, True),
    ]
    assert allowed["action_status"] == "succeeded"


@pytest.mark.parametrize("caller,session", [
    ("other-caller", "session_123"),
    ("caller", "session_999"),
])
def test_selection_id_cannot_cross_caller_or_conversation(
        monkeypatch, caller, session):
    selection = _remember()
    provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "queue", "playlist": "",
    })])
    monkeypatch.setattr(
        musiclink, "_dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not dispatch")),
    )

    answer = agentlink.ask(
        "queue those", session, caller, session=provider,
        api_key="synthetic-key", request_id="request_scope_01",
    )

    assert answer["acted"] is False
    assert answer["action_status"] == "rejected"
    assert "latest verified result" in answer["message"]


def test_fireworks_owns_playlist_write_intent(monkeypatch):
    selection = _remember()
    saves = []
    monkeypatch.setattr(
        musiclink, "add_tracks_to_playlist",
        lambda name, tracks: saves.append(
            (name, [track.get("pid") or track.get("catalog_id")
                    for track in tracks]))
        or {"message": "created playlist"},
    )
    first_provider = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "add_to_playlist",
        "playlist": "Road Trip",
    })])

    first = agentlink.ask(
        "what about those?", "session_123", "caller", session=first_provider,
        api_key="synthetic-key", request_id="request_save_no",
    )

    assert saves == [("Road Trip", [LOCAL_SONG["pid"], CATALOG_SONG["id"]])]
    assert first["action_status"] == "succeeded"

    allowed = ScriptedProvider([tool_reply("act_on_selection", {
        "selection_id": selection["id"], "action": "add_to_playlist",
        "playlist": "Road Trip",
    })])
    answer = agentlink.ask(
        "save those to my Road Trip playlist", "session_123", "caller",
        session=allowed, api_key="synthetic-key",
        request_id="request_save_yes",
    )

    assert saves == [
        ("Road Trip", [LOCAL_SONG["pid"], CATALOG_SONG["id"]]),
        ("Road Trip", [LOCAL_SONG["pid"], CATALOG_SONG["id"]]),
    ]
    assert answer["action_status"] == "succeeded"


def test_playlist_adapter_deduplicates_and_rejects_nonlocal_ids(monkeypatch):
    writes = []

    class Source:
        @staticmethod
        def add_tracks_to_playlist(name, pids):
            writes.append((name, pids))
            return {"created": True, "added": len(pids), "total": len(pids)}

    monkeypatch.setattr(musiclink, "_music", lambda: Source())
    answer = musiclink.add_tracks_to_playlist("Road Trip", [
        LOCAL_SONG, dict(LOCAL_SONG), {"pid": "not-local", "name": "bad"},
    ])

    assert writes == [("Road Trip", [LOCAL_SONG["pid"]])]
    assert answer["message"].startswith("created Road Trip and added 1 song")


def test_request_id_replay_runs_terminal_action_once(monkeypatch):
    calls = []
    provider = ScriptedProvider([tool_reply(
        "music_transport", {"action": "next"})])
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "music_transport",
        lambda _args: calls.append("next") or {"message": "next"},
    )

    first = agentlink.ask(
        "next", "session_123", "caller", session=provider,
        api_key="synthetic-key",
        request_id="request_replay_01")
    second = agentlink.ask(
        "next", "session_123", "caller", session=provider,
        api_key="synthetic-key",
        request_id="request_replay_01")

    assert calls == ["next"]
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["request_id"] == "request_replay_01"
    assert second["message"] == first["message"]


def test_request_id_cannot_be_reused_for_a_different_command(monkeypatch):
    provider = ScriptedProvider([tool_reply(
        "music_transport", {"action": "next"})])
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "music_transport",
        lambda _args: {"message": "next"},
    )
    agentlink.ask("next", "session_123", "caller", session=provider,
                  api_key="synthetic-key",
                  request_id="request_collision_01")

    with pytest.raises(ValueError, match="different command"):
        agentlink.ask("previous", "session_123", "caller", session=provider,
                      api_key="synthetic-key",
                      request_id="request_collision_01")


def test_concurrent_duplicate_waits_for_single_owner(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    provider = ScriptedProvider([tool_reply(
        "music_transport", {"action": "next"})])

    def action(_args):
        calls.append("next")
        entered.set()
        assert release.wait(2)
        return {"message": "next"}

    monkeypatch.setitem(agentlink.TOOL_HANDLERS, "music_transport", action)
    answers = []

    def invoke():
        answers.append(agentlink.ask(
            "next", "session_123", "caller", session=provider,
            api_key="synthetic-key",
            request_id="request_concurrent_01"))

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(2)
    second.join(2)

    assert calls == ["next"]
    assert len(answers) == 2
    assert sorted(answer["replayed"] for answer in answers) == [False, True]


def test_driver_failure_is_reported_as_unknown_without_leaking_detail(
        monkeypatch):
    provider = ScriptedProvider([tool_reply("set_power", {
        "device": "tv", "state": "on",
    })])
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "set_power",
        lambda _args: (_ for _ in ()).throw(
            RuntimeError("private device address")),
    )
    monkeypatch.setattr(agentlink.state, "poke", lambda: None)

    answer = agentlink.ask(
        "turn the TV on", "session_123", "caller", session=provider,
        api_key="synthetic-key", request_id="request_unknown_01",
    )

    assert answer["action_status"] == "unknown"
    assert answer["outcomes"] == [{"tool": "set_power", "status": "unknown"}]
    assert "check before retrying" in answer["message"]
    assert "private device address" not in str(answer)


def test_reset_forgets_selection_and_completed_request(monkeypatch):
    _remember()
    provider = ScriptedProvider([
        tool_reply("music_transport", {"action": "next"}),
        tool_reply("music_transport", {"action": "next"}),
    ])
    calls = []
    monkeypatch.setitem(
        agentlink.TOOL_HANDLERS, "music_transport",
        lambda _args: calls.append(True) or {"message": "next"},
    )
    agentlink.ask("next", "session_123", "caller", session=provider,
                  api_key="synthetic-key",
                  request_id="request_reset_01")

    agentlink.reset_session("caller", "session_123")
    agentlink.ask("next", "session_123", "caller", session=provider,
                  api_key="synthetic-key",
                  request_id="request_reset_01")

    assert agentlink._latest_selection("caller", "session_123") is None
    assert calls == [True, True]


def test_agent_api_passes_validated_request_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        agentlink, "ask",
        lambda message, session_id, caller, **kwargs: calls.append(
            (message, session_id, caller, kwargs.get("request_id")))
        or {"message": "done", "acted": True,
            "request_id": kwargs["request_id"]},
    )

    with TestClient(app) as client:
        answer = client.post("/api/agent", headers=AUTH, json={
            "message": "next", "session": "session_123",
            "request_id": "request_api_01",
        })
        invalid = client.post("/api/agent", headers=AUTH, json={
            "message": "next", "session": "session_123",
            "request_id": "bad id",
        })

    assert answer.json()["request_id"] == "request_api_01"
    assert calls == [("next", "session_123", "token", "request_api_01")]
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid request id"


def test_typed_and_voice_clients_issue_request_ids():
    javascript = open("api/ui/app.js", encoding="utf-8").read()
    swift = open("app/avctl/VoiceCapture.swift", encoding="utf-8").read()

    assert "request_id: newAgentSession()" in javascript
    assert '"request_id": UUID().uuidString' in swift
    assert javascript.count("}, 300000);") >= 2
    assert 'path: "/api/agent/voice", timeout: 300' in swift
