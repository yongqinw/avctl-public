from __future__ import annotations

import sqlite3
import stat

from fastapi.testclient import TestClient

from api import agentlink, ask_history, musiclink
from api.main import app
from scripts.analyze_ask_history import analyze
from tests.ask_harness import ScriptedProvider, text_reply, tool_reply
from tests.conftest import AUTH


def test_private_history_round_trip_and_archive():
    ask_history.save_state("alice@example.com", "session_123", messages=[
        {"role": "user", "content": "play some jazz"},
        {"role": "assistant", "content": "Playing jazz."},
    ], selections=[{"id": "sel_one"}], usage={"requests": 1})

    state = ask_history.load_state("alice@example.com", "session_123")

    assert state == {
        "messages": [
            {"role": "user", "content": "play some jazz"},
            {"role": "assistant", "content": "Playing jazz."},
        ],
        "selections": [{"id": "sel_one"}],
        "usage": {"requests": 1},
    }
    assert stat.S_IMODE(ask_history.HISTORY_FILE.stat().st_mode) == 0o600
    assert b"alice@example.com" not in ask_history.HISTORY_FILE.read_bytes()

    ask_history.archive("alice@example.com", "session_123")
    assert ask_history.load_state("alice@example.com", "session_123") is None


def test_agent_prompt_selection_and_usage_survive_memory_reset(monkeypatch):
    monkeypatch.setattr(agentlink, "_SESSIONS", {})
    monkeypatch.setattr(agentlink, "_SELECTIONS", {})
    monkeypatch.setattr(agentlink, "_SESSION_USAGE", {})
    monkeypatch.setattr(agentlink.secrets, "token_hex", lambda _size: "persisted")
    monkeypatch.setattr(musiclink, "recent_songs", lambda: [{
        "pid": "0000000000000001", "name": "So What",
        "artist": "Miles Davis",
    }])
    agentlink._save_session("caller", "session_123", [
        {"role": "user", "content": "find jazz"},
        {"role": "assistant", "content": "I found So What."},
    ])
    selection = agentlink._remember_selection(
        "caller", "session_123", "search_music", {"query": "jazz"},
        {"matches": [{
            "source": "library", "kind": "song", "name": "So What",
            "artist": "Miles Davis", "pid": "0000000000000001",
        }]},
    )
    agentlink._record_usage("caller", "session_123", {
        "prompt_tokens": 100, "completion_tokens": 20,
    })

    agentlink._SESSIONS.clear()
    agentlink._SELECTIONS.clear()
    agentlink._SESSION_USAGE.clear()
    agentlink._hydrate_session("caller", "session_123")

    assert agentlink._SESSIONS[("caller", "session_123")][0]["content"] == "find jazz"
    assert agentlink._latest_selection("caller", "session_123")["id"] == selection["id"]
    assert agentlink._session_usage("caller", "session_123")["total_tokens"] == 120


def test_history_api_is_authenticated_and_caller_scoped():
    ask_history.record_turn(
        "token", "session_123", request_id="request_one", channel="voice",
        user_message="播放青花瓷", assistant_message="正在播放青花瓷。",
        acted=True, action_status="succeeded",
        outcomes=[{"tool": "play_music", "status": "succeeded"}],
        trace={"provider_ms": 10},
    )
    ask_history.record_turn(
        "someone-else", "session_123", request_id="request_two",
        channel="text", user_message="secret", assistant_message="hidden",
        acted=False, action_status="no_op", outcomes=[], trace={},
    )

    with TestClient(app) as client:
        denied = client.get("/api/agent/history?session=session_123")
        answer = client.get(
            "/api/agent/history?session=session_123", headers=AUTH)

    assert denied.status_code == 401
    assert answer.status_code == 200
    assert answer.json()["turns"] == [{
        "channel": "voice",
        "created_at": answer.json()["turns"][0]["created_at"],
        "user": "播放青花瓷", "assistant": "正在播放青花瓷。",
        "acted": True, "action_status": "succeeded",
    }]


def test_history_analysis_highlights_retries_and_failures():
    rows = [{
        "created_at": 1, "channel": "voice", "user": "play some jazz",
        "assistant": "Which jazz?", "acted": False,
        "action_status": "no_op", "outcomes": [],
        "trace": {"provider_ms": 100}, "error_kind": "",
    }, {
        "created_at": 2, "channel": "voice", "user": "play jazz now",
        "assistant": "", "acted": False, "action_status": "error",
        "outcomes": [], "trace": {"provider_ms": 200},
        "error_kind": "AgentError",
    }]

    report = analyze(rows)

    assert report["turns"] == 2
    assert report["statuses"] == {"no_op": 1, "error": 1}
    assert report["friction"][0]["repeated"] is True
    assert any("transcription" in row for row in report["suggestions"])


def test_web_client_persists_only_opaque_session_and_restores_history():
    javascript = open("api/ui/app.js", encoding="utf-8").read()

    assert "avctl-agent-session" in javascript
    assert "/api/agent/history?session=" in javascript
    assert "localStorage.setItem(AGENT_SESSION_KEY, value)" in javascript
    assert "user_message" not in javascript


def test_turn_request_id_is_idempotent():
    values = dict(
        caller="caller", session_id="session_123", request_id="request_same",
        channel="text", user_message="next", assistant_message="Skipped.",
        acted=True, action_status="succeeded", outcomes=[], trace={},
    )
    ask_history.record_turn(**values)
    ask_history.record_turn(**values)

    with sqlite3.connect(ask_history.HISTORY_FILE) as connection:
        count = connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert count == 1


def test_fireworks_manages_durable_memory_and_future_prompt(monkeypatch):
    monkeypatch.setattr(agentlink, "system_prompt", lambda: ("policy", "v1"))
    monkeypatch.setattr(agentlink, "_live_context", lambda: "{}")
    remember_provider = ScriptedProvider([tool_reply("manage_memory", {
        "action": "remember", "kind": "preference",
        "content": "Prefer local-library versions when both sources match.",
        "memory_id": "",
    })])

    answer = agentlink.ask(
        "以后本地和 Apple Music 都有的话优先本地", "session_123", "caller",
        session=remember_provider, api_key="synthetic-key",
        request_id="request_memory_one",
    )

    assert answer["action_status"] == "succeeded"
    stored = ask_history.memories("caller")
    assert stored[0]["content"].startswith("Prefer local-library")

    next_provider = ScriptedProvider([text_reply("Understood.")])
    agentlink.ask(
        "现在给我找一首", "session_456", "caller", session=next_provider,
        api_key="synthetic-key", request_id="request_memory_two",
    )
    prompt = next_provider.calls[0][1]["json"]["messages"][-1]["content"]
    assert "<personal_memory>" in prompt
    assert stored[0]["id"] in prompt
    assert stored[0]["content"] in prompt


def test_memory_forget_is_exact_and_caller_scoped():
    memory = ask_history.remember("caller", "correction", "Q means playback queue")

    assert ask_history.forget("other", memory["id"]) is False
    assert ask_history.memories("caller") == [memory]
    assert ask_history.forget("caller", memory["id"]) is True
    assert ask_history.memories("caller") == []


def test_fireworks_can_recall_real_older_turns_without_crossing_callers(
        monkeypatch):
    ask_history.record_turn(
        "caller", "old_session", request_id="request_old", channel="voice",
        user_message="我说 Q 就是播放队列", assistant_message="记住了。",
        acted=False, action_status="no_op", outcomes=[], trace={},
    )
    ask_history.record_turn(
        "other", "other_session", request_id="request_private", channel="text",
        user_message="private other-user request", assistant_message="hidden",
        acted=False, action_status="no_op", outcomes=[], trace={},
    )
    monkeypatch.setattr(agentlink, "system_prompt", lambda: ("policy", "v1"))
    monkeypatch.setattr(agentlink, "_live_context", lambda: "{}")
    provider = ScriptedProvider([
        tool_reply("recall_history", {"query": "Q", "days": 90, "limit": 10}),
        text_reply("你之前说 Q 指播放队列。"),
    ])

    answer = agentlink.ask(
        "我之前说 Q 是什么意思？", "session_123", "caller",
        session=provider, api_key="synthetic-key",
        request_id="request_recall_one",
    )

    assert answer["message"] == "你之前说 Q 指播放队列。"
    assert answer["trace"]["model_rounds"] == 2
    tool_messages = [row for row in provider.calls[1][1]["json"]["messages"]
                     if row.get("role") == "tool"]
    assert "我说 Q 就是播放队列" in tool_messages[0]["content"]
    assert "private other-user request" not in tool_messages[0]["content"]


def test_history_recall_finds_paraphrased_chinese_wording():
    ask_history.record_turn(
        "caller", "old_session", request_id="request_semantic_old",
        channel="text", user_message="以后我说混在一起就是打开随机播放",
        assistant_message="记住了。", acted=False, action_status="no_op",
        outcomes=[], trace={},
    )
    ask_history.record_turn(
        "other", "other_session", request_id="request_semantic_private",
        channel="text", user_message="随机播放是我的秘密",
        assistant_message="hidden", acted=False, action_status="no_op",
        outcomes=[], trace={},
    )

    rows = ask_history.search_turns(
        "caller", "我之前对混合歌曲和随机顺序怎么说的", days=90, limit=5)

    assert rows
    assert rows[0]["user"] == "以后我说混在一起就是打开随机播放"
    assert all("秘密" not in row["user"] for row in rows)
