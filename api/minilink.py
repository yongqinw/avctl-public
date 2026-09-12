"""The narrow bridge from the web remote to the logged-in Mac session.

The FastAPI process is intentionally a LaunchDaemon.  Even with ``UserName``
set, that leaves it outside the Aqua session and therefore outside the
WindowServer.  ``avctl-input-helper`` is a LaunchAgent in the logged-in user's
session; this module talks to it through a mode-0600 Unix socket.

The browser-facing protocol is validated here before any event reaches the
helper.  Typed text is never logged, persisted, or included in an exception.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Any


SOCKET_PATH = Path(
    os.environ.get("AVCTL_MINI_SOCKET", "~/.avctl/mac-input.sock")
).expanduser()

CONNECT_TIMEOUT = 0.6
STATUS_TIMEOUT = 1.0
MAX_WIRE_BYTES = 2_048
MAX_TEXT_LENGTH = 256
MAX_DELTA = 500.0

_NAMED_KEYS = {
    "Backspace", "Delete", "Enter", "Escape", "Tab", "Space",
    "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
    "Home", "End", "PageUp", "PageDown",
    "MetaLeft", "AltLeft", "ControlLeft", "ShiftLeft",
}


class MiniUnavailable(RuntimeError):
    """The logged-in helper is absent or did not complete its handshake."""


class InvalidMiniEvent(ValueError):
    """A browser event is outside the deliberately small input protocol."""


def _number(value: Any, name: str, *, limit: float = MAX_DELTA) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidMiniEvent(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or abs(result) > limit:
        raise InvalidMiniEvent(f"{name} is outside the allowed range")
    return result


def _valid_key(code: Any) -> str:
    if not isinstance(code, str):
        raise InvalidMiniEvent("key code must be a string")
    if code in _NAMED_KEYS:
        return code
    if len(code) == 4 and code.startswith("Key") and code[3].isalpha():
        return "Key" + code[3].upper()
    if len(code) == 6 and code.startswith("Digit") and code[5].isdigit():
        return code
    raise InvalidMiniEvent("unsupported key code")


def validate_event(raw: Any) -> dict[str, Any]:
    """Return a canonical helper event or raise without echoing its payload."""
    if not isinstance(raw, dict):
        raise InvalidMiniEvent("input event must be an object")
    kind = raw.get("type")
    if kind == "move":
        return {
            "type": "move",
            "dx": _number(raw.get("dx"), "dx"),
            "dy": _number(raw.get("dy"), "dy"),
        }
    if kind == "scroll":
        return {
            "type": "scroll",
            "dx": _number(raw.get("dx"), "dx"),
            "dy": _number(raw.get("dy"), "dy"),
        }
    if kind == "space":
        direction = raw.get("direction")
        if direction not in {"previous", "next"}:
            raise InvalidMiniEvent("space direction must be previous or next")
        return {"type": "space", "direction": direction}
    if kind == "button":
        button = raw.get("button")
        state = raw.get("state")
        clicks = raw.get("clicks", 1)
        if button not in {"left", "right"}:
            raise InvalidMiniEvent("unsupported mouse button")
        if state not in {"down", "up"}:
            raise InvalidMiniEvent("button state must be down or up")
        if isinstance(clicks, bool) or clicks not in {1, 2}:
            raise InvalidMiniEvent("click count must be 1 or 2")
        return {"type": "button", "button": button,
                "state": state, "clicks": clicks}
    if kind == "key":
        state = raw.get("state")
        if state not in {"down", "up"}:
            raise InvalidMiniEvent("key state must be down or up")
        return {"type": "key", "code": _valid_key(raw.get("code")),
                "state": state}
    if kind == "text":
        value = raw.get("text")
        if (not isinstance(value, str) or not value
                or len(value) > MAX_TEXT_LENGTH or "\x00" in value):
            raise InvalidMiniEvent("text must contain 1 to 256 safe characters")
        return {"type": "text", "text": value}
    if kind == "release_all":
        return {"type": "release_all"}
    raise InvalidMiniEvent("unsupported input event")


def unavailable_status(message: str = "Mac input helper is not running") -> dict:
    return {
        "type": "status",
        "available": False,
        "permission": False,
        "message": message,
    }


class MiniSession:
    """One remote's stream to the helper; the API serializes all sessions."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, status: dict[str, Any]):
        self.reader = reader
        self.writer = writer
        self.status = status
        self._closed = False

    async def send(self, event: dict[str, Any]) -> None:
        if self._closed:
            raise MiniUnavailable("Mac input helper connection closed")
        wire = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        encoded = (wire + "\n").encode("utf-8")
        if len(encoded) > MAX_WIRE_BYTES:
            raise InvalidMiniEvent("input event is too large")
        self.writer.write(encoded)
        try:
            await self.writer.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            self._closed = True
            raise MiniUnavailable("Mac input helper disconnected") from exc

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self.send({"type": "release_all"})
        except (MiniUnavailable, InvalidMiniEvent):
            pass
        self._closed = True
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (BrokenPipeError, ConnectionError, OSError):
            pass


async def open_session() -> MiniSession:
    """Connect and require an explicit helper status before accepting input."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SOCKET_PATH)), CONNECT_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, FileNotFoundError,
            PermissionError, OSError) as exc:
        raise MiniUnavailable("Mac input helper is not running") from exc

    try:
        writer.write(b'{"type":"status"}\n')
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), STATUS_TIMEOUT)
        if not line or len(line) > MAX_WIRE_BYTES:
            raise MiniUnavailable("Mac input helper did not answer")
        status = json.loads(line)
        if (not isinstance(status, dict) or status.get("type") != "status"
                or not isinstance(status.get("available"), bool)
                or not isinstance(status.get("permission"), bool)):
            raise MiniUnavailable("Mac input helper returned an invalid status")
        canonical = {
            "type": "status",
            "available": status["available"],
            "permission": status["permission"],
            "message": str(status.get("message") or "")[:200],
        }
        return MiniSession(reader, writer, canonical)
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError,
            MiniUnavailable) as exc:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        if isinstance(exc, MiniUnavailable):
            raise
        raise MiniUnavailable("Mac input helper did not answer") from exc
