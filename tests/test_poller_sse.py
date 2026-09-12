"""Issues #26 and #27: one background beat reads the rack; HTTP reads cache.

snapshot() is stubbed by conftest for every test here; the counting stub on
top proves WHO calls it -- the poller on its beat, never the route.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from api import state
from tests.conftest import AUTH


@pytest.fixture
def counting_snapshot(monkeypatch):
    calls = {"n": 0}
    ready = threading.Event()

    def fake_snapshot():
        calls["n"] += 1
        ready.set()
        return {"scene": None, "devices": {"n": calls["n"]}, "implemented": []}

    monkeypatch.setattr(state, "snapshot", fake_snapshot)
    calls["ready"] = ready
    return calls


@pytest.fixture
def client(counting_snapshot):
    with TestClient(api_main.app) as c:
        assert counting_snapshot["ready"].wait(timeout=5)
        yield c


def test_state_is_served_from_the_cache(client, counting_snapshot):
    time.sleep(0.02)   # let the first poll settle into the cache
    before = counting_snapshot["n"]
    for _ in range(5):
        answer = client.get("/api/state", headers=AUTH)
        assert answer.status_code == 200
    after = counting_snapshot["n"]
    # Five requests, zero extra device reads -- the beat's, not the phones'.
    # (At most one beat may have ticked over while the requests ran.)
    assert after - before <= 1
    body = answer.json()
    assert "age" in body
    assert body["devices"]["n"] >= 1


def test_last_known_ignores_valid_json_with_the_wrong_shape(monkeypatch,
                                                            tmp_path):
    path = tmp_path / "rack_state.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(state, "RACK_STATE_FILE", path)

    assert state.last_known() is None


def test_poke_forces_an_early_beat(client, counting_snapshot):
    before = counting_snapshot["n"]
    state.poke()
    deadline = time.monotonic() + 2
    while counting_snapshot["n"] == before:
        assert time.monotonic() < deadline, "poke() never woke the poller"
        time.sleep(0.01)


# The stream tests drive the async generator directly rather than through
# TestClient's streaming shim, whose close blocks on a deliberately endless
# response. The route glue (auth, headers) is thin and covered separately.


async def _next_event(gen, prefix, deadline_s=5.0):
    deadline = time.monotonic() + deadline_s
    async for chunk in gen:
        if chunk.startswith(prefix):
            return chunk
        assert time.monotonic() < deadline, f"no {prefix!r} chunk arrived"
    raise AssertionError("stream ended")


def test_events_stream_opens_with_a_snapshot(client, monkeypatch):
    monkeypatch.setattr(api_main, "SSE_HEARTBEAT", 0.5)
    assert api_main.api_events(identity=None).media_type == "text/event-stream"

    async def scenario():
        gen = api_main._event_stream()
        try:
            return await _next_event(gen, "event: state")
        finally:
            await gen.aclose()

    first = asyncio.run(scenario())
    assert '"devices"' in first
    assert '"seq"' in first


def test_events_pushes_when_the_rack_changes(client, monkeypatch):
    monkeypatch.setattr(api_main, "SSE_HEARTBEAT", 0.5)

    async def scenario():
        gen = api_main._event_stream()
        try:
            first = await _next_event(gen, "event: state")
            # The next beat carries a different snapshot (the counter moved),
            # so a second event must arrive without the client asking.
            state.poke()
            second = await _next_event(gen, "event: state")
            return first, second
        finally:
            await gen.aclose()

    first, second = asyncio.run(scenario())
    assert second != first


def test_events_do_not_push_an_unchanged_poll(monkeypatch):
    monkeypatch.setattr(api_main, "SSE_HEARTBEAT", 0.03)
    snapshot = {"scene": None, "devices": {}, "implemented": []}
    sequence = {"value": 1}
    monkeypatch.setattr(
        state, "latest",
        lambda: (snapshot, sequence["value"], 0.0),
    )

    async def scenario():
        gen = api_main._event_stream()
        try:
            first = await gen.__anext__()
            sequence["value"] = 2  # a completed poll, identical rack facts
            second = await asyncio.wait_for(gen.__anext__(), timeout=0.2)
            return first, second
        finally:
            await gen.aclose()

    first, second = asyncio.run(scenario())
    assert first.startswith("event: state")
    assert second.startswith("event: ping")


def test_events_heartbeat_is_a_named_ping(client, monkeypatch):
    # A comment heartbeat never reaches EventSource JS; the client watchdog
    # (#106) needs a real event on a quiet stream.
    monkeypatch.setattr(api_main, "SSE_HEARTBEAT", 0.05)

    async def scenario():
        gen = api_main._event_stream()
        try:
            await _next_event(gen, "event: state")
            return await _next_event(gen, "event: ping")
        finally:
            await gen.aclose()

    assert asyncio.run(scenario()).startswith("event: ping")


def test_events_waits_out_the_boot_window(monkeypatch):
    # Before the first poll lands the stream must idle at the heartbeat
    # cadence, not busy-spin (#92): with no snapshot and a 0.2s heartbeat,
    # a 0.5s window must yield only a couple of pings, not thousands.
    monkeypatch.setattr(api_main, "SSE_HEARTBEAT", 0.2)
    monkeypatch.setattr(state, "_cached", None)
    monkeypatch.setattr(state, "_cache_seq", 0)

    async def scenario():
        gen = api_main._event_stream()
        chunks = []

        async def pull():
            async for chunk in gen:
                chunks.append(chunk)

        task = asyncio.get_running_loop().create_task(pull())
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await gen.aclose()
        return chunks

    chunks = asyncio.run(scenario())
    assert all(c.startswith("event: ping") for c in chunks)
    assert len(chunks) <= 4


def test_events_requires_auth(client):
    assert client.get("/api/events").status_code == 401


def test_poller_stops_with_the_app(counting_snapshot):
    with TestClient(api_main.app):
        assert counting_snapshot["ready"].wait(timeout=5)
    time.sleep(0.05)
    alive = [t for t in threading.enumerate() if t.name == "avctl-poller"]
    assert not any(t.is_alive() for t in alive)
