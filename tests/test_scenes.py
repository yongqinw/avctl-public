"""Issue #17: two scenes must never drive the rack at once.

The blocking fake stands in for _rack, so the first scene is genuinely
mid-flight when the second press arrives -- the exact double-press the phone
produces when Music mode looks stuck and the user reaches for Off.
"""

from __future__ import annotations

import threading

import pytest

from api import commands
from api.commands import SceneBusyError


@pytest.fixture
def held_scene(monkeypatch):
    """Run scene.music on a thread and hold it inside _rack until released."""
    entered = threading.Event()
    release = threading.Event()

    def fake_rack(*jobs, stagger=0.0):
        entered.set()
        release.wait(timeout=5)
        return {"message": "fake scene done"}

    monkeypatch.setattr(commands, "_rack", fake_rack)
    thread = threading.Thread(
        target=commands.COMMANDS["scene.music"].handler, args=({},))
    thread.start()
    assert entered.wait(timeout=5)
    yield
    release.set()
    thread.join(timeout=5)


def test_second_scene_is_refused_not_queued(held_scene):
    with pytest.raises(SceneBusyError):
        commands.COMMANDS["scene.off"].handler({})


def test_scene_lock_releases_after_the_scene(monkeypatch):
    monkeypatch.setattr(commands, "_rack",
                        lambda *jobs, stagger=0.0: {"message": "ok"})
    commands.COMMANDS["scene.off"].handler({})
    commands.COMMANDS["scene.music"].handler({})   # would raise if leaked


def test_route_answers_409(held_scene, monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from tests.conftest import AUTH

    with TestClient(app) as client:
        answer = client.post("/api/cmd", json={"cmd": "scene.off"},
                             headers=AUTH)
    assert answer.status_code == 409
    assert "already running" in answer.json()["detail"]
