"""The island's escape hatch: `background: true` on POST /api/cmd.

A Live Activity intent lives for seconds; scene.off can hold the rack for
45s of TV wake. The flag turns the route into accept-and-keep-working: 202
now, the outcome delivered the way every client already learns outcomes --
the poller and the SSE feed.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from api import commands, main, state
from tests.conftest import AUTH


class SlowCmd:
    id = "test.slow"
    device = "test"
    label = "A slow command"
    note = ""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def handler(self, args):
        self.entered.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return {"message": "done at last"}


def test_background_answers_202_before_the_handler_finishes(monkeypatch):
    slow = SlowCmd()
    monkeypatch.setattr(commands, "get",
                        lambda cid: slow if cid == "test.slow" else None)
    poked = threading.Event()
    monkeypatch.setattr(state, "poke", poked.set)

    with TestClient(main.app) as client:
        answer = client.post(
            "/api/cmd", json={"cmd": "test.slow", "background": True},
            headers=AUTH)

    # The route answered while the handler was still held.
    assert answer.status_code == 202
    assert answer.json()["status"] == "accepted"
    assert slow.entered.wait(timeout=5)
    assert not slow.finished.is_set()

    # And the handler still ran to completion afterwards, ending in a poke
    # so the SSE feed reports what actually happened.
    slow.release.set()
    assert slow.finished.wait(timeout=5)
    assert poked.wait(timeout=5)


def test_background_failure_still_pokes(monkeypatch):
    class FailingCmd(SlowCmd):
        def handler(self, args):
            raise RuntimeError("the device did not answer")

    monkeypatch.setattr(commands, "get",
                        lambda cid: FailingCmd() if cid == "test.slow" else None)
    poked = threading.Event()
    monkeypatch.setattr(state, "poke", poked.set)

    with TestClient(main.app) as client:
        answer = client.post(
            "/api/cmd", json={"cmd": "test.slow", "background": True},
            headers=AUTH)

    assert answer.status_code == 202
    # A 202 was already sent, so the failure's only witness is the next
    # poll -- which the thread must still trigger.
    assert poked.wait(timeout=5)
