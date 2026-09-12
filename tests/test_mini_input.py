from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import minilink, views
from api.auth import Identity
from api.main import _send_mini_event, app
from tests.conftest import AUTH


WS_HEADERS = {**AUTH, "Origin": "http://testserver"}


class FakeMiniSession:
    def __init__(self, *, permission: bool = True):
        self.status = {
            "type": "status", "available": True,
            "permission": permission,
            "message": "Connected" if permission else "Permission required",
        }
        self.events: list[dict] = []
        self.closed = False

    async def send(self, event: dict) -> None:
        self.events.append(event)

    async def close(self) -> None:
        self.events.append({"type": "release_all"})
        self.closed = True


def test_mini_event_protocol_accepts_only_small_canonical_events():
    assert minilink.validate_event(
        {"type": "move", "dx": 1, "dy": -2}) == {
            "type": "move", "dx": 1.0, "dy": -2.0}
    assert minilink.validate_event(
        {"type": "button", "button": "left", "state": "down",
         "clicks": 2})["clicks"] == 2
    assert minilink.validate_event(
        {"type": "key", "code": "Keyc", "state": "up"})["code"] == "KeyC"
    assert minilink.validate_event(
        {"type": "text", "text": "你好"})["text"] == "你好"
    assert minilink.validate_event(
        {"type": "space", "direction": "next"}) == {
            "type": "space", "direction": "next"}

    invalid = [
        {"type": "move", "dx": float("nan"), "dy": 0},
        {"type": "move", "dx": 501, "dy": 0},
        {"type": "button", "button": "middle", "state": "down"},
        {"type": "key", "code": "F12", "state": "down"},
        {"type": "text", "text": "x" * 257},
        {"type": "text", "text": "secret\x00suffix"},
        {"type": "space", "direction": "up"},
        {"type": "shell", "command": "whoami"},
    ]
    for event in invalid:
        with pytest.raises(minilink.InvalidMiniEvent):
            minilink.validate_event(event)


def test_mini_websocket_authenticates_relays_and_releases(monkeypatch):
    session = FakeMiniSession()

    async def open_session():
        return session

    monkeypatch.setattr(minilink, "open_session", open_session)
    with TestClient(app) as client:
        with client.websocket_connect(
                "/api/mini/input", headers=WS_HEADERS) as socket:
            assert socket.receive_json()["permission"] is True
            socket.send_json({"type": "move", "dx": 3.5, "dy": -1})
            socket.send_json({"type": "text", "text": "晴天"})
            socket.send_json({"type": "space", "direction": "next"})

    assert session.events[:3] == [
        {"type": "move", "dx": 3.5, "dy": -1.0},
        {"type": "text", "text": "晴天"},
        {"type": "space", "direction": "next"},
    ]
    assert session.events[-1] == {"type": "release_all"}
    assert session.closed is True


def test_mini_websocket_rejects_missing_auth_and_cross_site_origin(monkeypatch):
    async def open_session():
        raise AssertionError("an untrusted browser must not reach the helper")

    monkeypatch.setattr(minilink, "open_session", open_session)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as unauthenticated:
            with client.websocket_connect(
                    "/api/mini/input", headers={"Origin": "http://testserver"}):
                pass
        assert unauthenticated.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as cross_site:
            with client.websocket_connect(
                    "/api/mini/input",
                    headers={**AUTH, "Origin": "https://attacker.example"}):
                pass
        assert cross_site.value.code == 1008


def test_mini_websocket_reports_absent_helper_without_fake_success(monkeypatch):
    async def unavailable():
        raise minilink.MiniUnavailable("Mac input helper is not running")

    monkeypatch.setattr(minilink, "open_session", unavailable)
    with TestClient(app) as client:
        with client.websocket_connect(
                "/api/mini/input", headers=WS_HEADERS) as socket:
            status = socket.receive_json()
            assert status == minilink.unavailable_status()
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1013


def test_mini_websocket_reports_permission_and_releases_session(monkeypatch):
    session = FakeMiniSession(permission=False)

    async def open_session():
        return session

    monkeypatch.setattr(minilink, "open_session", open_session)
    with TestClient(app) as client:
        with client.websocket_connect(
                "/api/mini/input", headers=WS_HEADERS) as socket:
            status = socket.receive_json()
            assert status["available"] is True
            assert status["permission"] is False
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 1013

    assert session.closed is True
    assert session.events == [{"type": "release_all"}]


def test_mini_websocket_serializes_multiple_controllers(monkeypatch):
    sessions: list[FakeMiniSession] = []

    async def open_session():
        session = FakeMiniSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(minilink, "open_session", open_session)
    with TestClient(app) as client:
        with client.websocket_connect(
                "/api/mini/input", headers=WS_HEADERS) as first:
            first.receive_json()
            with client.websocket_connect(
                    "/api/mini/input", headers=WS_HEADERS) as second:
                assert second.receive_json()["permission"] is True
                first.send_json({"type": "move", "dx": 1, "dy": 0})
                second.send_json({"type": "move", "dx": 0, "dy": 1})
                time.sleep(.08)

    assert len(sessions) == 1
    moves = [event for event in sessions[0].events if event["type"] == "move"]
    assert {event["dx"] for event in moves} == {0.0, 1.0}
    assert {event["dy"] for event in moves} == {0.0, 1.0}


def test_server_write_door_is_serial_in_the_production_event_loop():
    in_send = 0
    max_in_send = 0

    class SlowSession(FakeMiniSession):
        async def send(self, event: dict) -> None:
            nonlocal in_send, max_in_send
            in_send += 1
            max_in_send = max(max_in_send, in_send)
            await asyncio.sleep(.02)
            self.events.append(event)
            in_send -= 1

    async def exercise():
        session = SlowSession()
        app.state.mini_input_lock = asyncio.Lock()
        app.state.mini_input_session = session
        await asyncio.gather(
            _send_mini_event(app, session, {"type": "move", "dx": 1}),
            _send_mini_event(app, session, {"type": "move", "dy": 1}),
        )

    asyncio.run(exercise())
    assert max_in_send == 1


def test_mini_panel_is_configurable_and_uses_native_keyboard_contract():
    html = views.remote(Identity("caller", "token"))

    assert "id='page-mini'" in html
    assert "data-tab='mini'" in html
    assert "id='mini-trackpad'" in html
    assert "data-mini-key='Backspace'>delete" in html
    assert "data-mini-key='Escape'>esc" not in html
    assert "data-mini-key='Tab'>tab" in html
    assert html.index("data-mini-key='Tab'") < html.index(
        "data-mini-key='Backspace'")
    assert "data-mini-key='ArrowLeft'" in html
    assert "id='mini-input'" in html
    assert "data-mini-mod='MetaLeft'" in html
    assert "The Mac must be logged in, unlocked" in html
    settings = views.panel_settings()
    assert next(panel for panel in settings["panels"]
                if panel["id"] == "mini")["enabled"] is True


def test_mini_ui_owns_only_trackpad_gestures_and_keeps_ipad_left_stage():
    root = Path(__file__).parents[1]
    script = (root / "api/ui/app.js").read_text()
    style = (root / "api/ui/app.css").read_text()

    assert "miniTrackpad.setPointerCapture(event.pointerId)" in script
    assert "requestAnimationFrame(flushMove)" in script
    assert "event.inputType === 'deleteContentBackward'" in script
    assert "miniSyncViewport" not in script
    assert "mini-vvh" not in style
    assert ".app.mini-typing" not in style
    assert "setMiniActive(name === 'mini')" in script
    assert ".mini-trackpad{" in style and "touch-action:none" in style
    assert "const pointerGain = 3.0" in script
    assert "const precisionPointerGain = 1.8" in script
    assert "const pointerAcceleration = 0.12" in script
    assert "const spaceSwipeThreshold = 42" in script
    assert "direction: dx < 0 ? 'next' : 'previous'" in script
    assert 'case "space":' in (
        root / "mac/InputHelper/main.swift").read_text()
    assert ":root{--rail-w:clamp(420px,42vw,520px)}" in style
    # Music remains the sole panel moved out of the rail on wide layouts.
    assert "insertBefore(mini" not in script
