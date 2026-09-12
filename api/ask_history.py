"""Private, durable Ask history stored only on the Mac mini.

The database contains conversation text and server-owned music selections, so
it deliberately lives outside the repository with owner-only permissions.  It
never stores Fireworks credentials, authorization headers, or audio.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


HISTORY_FILE = Path(os.environ.get(
    "AVCTL_ASK_HISTORY_FILE", "~/.avctl/ask_history.sqlite3"
)).expanduser()

_LOCK = threading.RLock()
_INITIALIZED: set[Path] = set()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    caller_hash TEXT NOT NULL,
    session_id TEXT NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    selections_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (caller_hash, session_id)
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_hash TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT,
    channel TEXT NOT NULL,
    created_at REAL NOT NULL,
    user_message TEXT NOT NULL,
    assistant_message TEXT NOT NULL,
    acted INTEGER NOT NULL,
    action_status TEXT NOT NULL,
    outcomes_json TEXT NOT NULL,
    trace_json TEXT NOT NULL,
    error_kind TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS turns_request
ON turns(caller_hash, session_id, request_id)
WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS turns_session_time
ON turns(caller_hash, session_id, created_at);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    caller_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(caller_hash, kind, content)
);
CREATE INDEX IF NOT EXISTS memories_caller_time
ON memories(caller_hash, updated_at);
"""


def _identity(caller: str) -> str:
    return hashlib.sha256(caller.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str, fallback: Any) -> Any:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback
    return decoded


def _connect() -> sqlite3.Connection:
    HISTORY_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        HISTORY_FILE.parent.chmod(0o700)
    except OSError:
        pass
    connection = sqlite3.connect(HISTORY_FILE, timeout=5)
    if HISTORY_FILE not in _INITIALIZED:
        connection.executescript(_SCHEMA)
        _INITIALIZED.add(HISTORY_FILE)
    try:
        HISTORY_FILE.chmod(0o600)
    except OSError:
        pass
    return connection


def load_state(caller: str, session_id: str) -> dict[str, Any] | None:
    """Load the active prompt/selection state for one conversation."""
    with _LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT messages_json, selections_json, usage_json "
            "FROM conversations WHERE caller_hash=? AND session_id=? "
            "AND archived=0",
            (_identity(caller), session_id),
        ).fetchone()
    if row is None:
        return None
    messages = _decode(row[0], [])
    selections = _decode(row[1], [])
    usage = _decode(row[2], {})
    return {
        "messages": messages if isinstance(messages, list) else [],
        "selections": selections if isinstance(selections, list) else [],
        "usage": usage if isinstance(usage, dict) else {},
    }


def save_state(caller: str, session_id: str, **fields: Any) -> None:
    """Upsert bounded active state without disturbing fields not supplied."""
    allowed = {
        "messages": "messages_json",
        "selections": "selections_json",
        "usage": "usage_json",
    }
    updates = [(allowed[key], _json(value)) for key, value in fields.items()
               if key in allowed]
    if not updates:
        return
    now = time.time()
    caller_hash = _identity(caller)
    with _LOCK, _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO conversations "
            "(caller_hash,session_id,created_at,updated_at,archived) "
            "VALUES(?,?,?,?,0)",
            (caller_hash, session_id, now, now),
        )
        assignments = ",".join(f"{column}=?" for column, _ in updates)
        values = [value for _, value in updates]
        connection.execute(
            f"UPDATE conversations SET {assignments},updated_at=?,archived=0 "
            "WHERE caller_hash=? AND session_id=?",
            (*values, now, caller_hash, session_id),
        )


def archive(caller: str, session_id: str) -> None:
    """Close prompt state while retaining the conversation for analysis."""
    with _LOCK, _connect() as connection:
        connection.execute(
            "UPDATE conversations SET archived=1,updated_at=? "
            "WHERE caller_hash=? AND session_id=?",
            (time.time(), _identity(caller), session_id),
        )


def record_turn(
    caller: str,
    session_id: str,
    *,
    request_id: str | None,
    channel: str,
    user_message: str,
    assistant_message: str,
    acted: bool,
    action_status: str,
    outcomes: list[dict[str, Any]],
    trace: dict[str, Any],
    error_kind: str = "",
) -> None:
    """Append one completed or failed interaction for later local analysis."""
    with _LOCK, _connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO turns "
            "(caller_hash,session_id,request_id,channel,created_at,user_message,"
            "assistant_message,acted,action_status,outcomes_json,trace_json,"
            "error_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (_identity(caller), session_id, request_id, channel, time.time(),
             user_message[:2000], assistant_message[:6000], int(acted),
             action_status, _json(outcomes), _json(trace), error_kind[:120]),
        )


def conversation(caller: str, session_id: str,
                 limit: int = 100) -> list[dict[str, Any]]:
    """Return display-safe turns, oldest first, without internal identifiers."""
    count = max(1, min(int(limit), 500))
    with _LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT channel,created_at,user_message,assistant_message,acted,"
            "action_status FROM turns WHERE caller_hash=? AND session_id=? "
            "ORDER BY id DESC LIMIT ?",
            (_identity(caller), session_id, count),
        ).fetchall()
    return [{
        "channel": row[0], "created_at": row[1],
        "user": row[2], "assistant": row[3],
        "acted": bool(row[4]), "action_status": row[5],
    } for row in reversed(rows)]


def search_turns(caller: str, query: str = "", days: int = 90,
                 limit: int = 12) -> list[dict[str, Any]]:
    """Retrieve caller-scoped past turns; Fireworks interprets their meaning."""
    cutoff = time.time() - max(1, min(int(days), 3650)) * 86400
    count = max(1, min(int(limit), 30))
    term = " ".join(str(query).split()).strip()[:200]
    clauses = ["caller_hash=?", "created_at>=?"]
    values: list[Any] = [_identity(caller), cutoff]
    if term:
        clauses.append(
            "(instr(lower(user_message),lower(?))>0 OR "
            "instr(lower(assistant_message),lower(?))>0)")
        values.extend([term, term])
    values.append(count)
    with _LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT channel,created_at,user_message,assistant_message,acted,"
            "action_status FROM turns WHERE " + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()
        if term and not rows:
            # A caller rarely recalls their exact old wording. Retrieve a
            # bounded private candidate window, then rank locally; no history
            # or embedding leaves the Mac mini.
            candidates = connection.execute(
                "SELECT channel,created_at,user_message,assistant_message,acted,"
                "action_status FROM turns WHERE caller_hash=? AND created_at>=? "
                "ORDER BY id DESC LIMIT 200",
                (_identity(caller), cutoff),
            ).fetchall()

            def grams(value: str) -> set[str]:
                normalized = "".join(str(value).casefold().split())
                for phrase, concept in (
                    ("混在一起", "混合"), ("混合歌曲", "混合"),
                    ("随机顺序", "随机"), ("随机播放", "随机"),
                    ("shuffle", "random"), ("mix", "random"),
                ):
                    normalized = normalized.replace(phrase, concept)
                words = set(str(value).casefold().split())
                words.update(normalized[index:index + 2]
                             for index in range(max(0, len(normalized) - 1)))
                return {item for item in words if item}

            wanted = grams(term)
            ranked = []
            for ordinal, row in enumerate(candidates):
                text = f"{row[2]} {row[3]}"
                available = grams(text)
                overlap = (len(wanted & available) / len(wanted)
                           if wanted else 0.0)
                sequence = SequenceMatcher(
                    None, term.casefold(), text.casefold()).ratio()
                score = max(overlap, sequence)
                if score >= 0.20:
                    ranked.append((score, -ordinal, row))
            ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
            rows = [row for _, _, row in ranked[:count]]
    return [{
        "channel": row[0], "created_at": row[1],
        "user": row[2], "assistant": row[3],
        "acted": bool(row[4]), "action_status": row[5],
    } for row in rows]


def analysis_rows(days: int = 30) -> list[dict[str, Any]]:
    """Read recent turns for the local, offline history-analysis script."""
    cutoff = time.time() - max(1, min(int(days), 3650)) * 86400
    with _LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT channel,created_at,user_message,assistant_message,acted,"
            "action_status,outcomes_json,trace_json,error_kind "
            "FROM turns WHERE created_at>=? ORDER BY id",
            (cutoff,),
        ).fetchall()
    return [{
        "channel": row[0], "created_at": row[1],
        "user": row[2], "assistant": row[3], "acted": bool(row[4]),
        "action_status": row[5], "outcomes": _decode(row[6], []),
        "trace": _decode(row[7], {}), "error_kind": row[8],
    } for row in rows]


def remember(caller: str, kind: str, content: str) -> dict[str, str]:
    """Store one Fireworks-selected durable preference or recurring context."""
    if kind not in {"preference", "routine", "correction", "context"}:
        raise ValueError("invalid memory kind")
    cleaned = " ".join(str(content).split()).strip()[:300]
    if not cleaned:
        raise ValueError("memory content is required")
    caller_hash = _identity(caller)
    memory_id = "mem_" + hashlib.sha256(
        f"{caller_hash}\0{kind}\0{cleaned}".encode("utf-8")
    ).hexdigest()[:16]
    now = time.time()
    with _LOCK, _connect() as connection:
        connection.execute(
            "INSERT INTO memories "
            "(id,caller_hash,kind,content,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(caller_hash,kind,content) "
            "DO UPDATE SET updated_at=excluded.updated_at",
            (memory_id, caller_hash, kind, cleaned, now, now),
        )
        stale = connection.execute(
            "SELECT id FROM memories WHERE caller_hash=? "
            "ORDER BY updated_at DESC LIMIT -1 OFFSET 40",
            (caller_hash,),
        ).fetchall()
        if stale:
            connection.executemany(
                "DELETE FROM memories WHERE id=? AND caller_hash=?",
                [(row[0], caller_hash) for row in stale],
            )
    return {"id": memory_id, "kind": kind, "content": cleaned}


def forget(caller: str, memory_id: str) -> bool:
    """Forget one exact caller-scoped memory selected by Fireworks."""
    with _LOCK, _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM memories WHERE id=? AND caller_hash=?",
            (memory_id, _identity(caller)),
        )
    return cursor.rowcount > 0


def memories(caller: str, limit: int = 40) -> list[dict[str, str]]:
    count = max(1, min(int(limit), 40))
    with _LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT id,kind,content FROM memories WHERE caller_hash=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (_identity(caller), count),
        ).fetchall()
    return [{"id": row[0], "kind": row[1], "content": row[2]}
            for row in rows]
