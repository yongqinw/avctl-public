"""Shared ground rules for the suite.

Two things every test relies on:

* AVCTL_TOKEN is pinned before any api module is imported, so auth never
  falls through to generating (or worse, reading) ~/.avctl/token.
* api.state.snapshot is replaced for the whole session. The real one talks
  to the TV, the amp and Music.app; a unit test that reaches hardware is a
  smoke test that got lost. Tests that care about snapshot contents install
  their own stub on top and restore this one.
"""

from __future__ import annotations

import os

os.environ.setdefault("AVCTL_TOKEN", "test-token")
os.environ.setdefault("AVCTL_POLL_INTERVAL", "0.1")
os.environ.setdefault("AVCTL_ADVERTISE", "0")
os.environ.setdefault(
    "AVCTL_ROON_EXTENSION_ID_FILE", "/private/tmp/avctl-tests-no-roon-id")

import pytest  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


def stub_snapshot() -> dict:
    """The smallest thing main.py can serve without asking any device."""
    return {
        "scene": None,
        "devices": {},
        "implemented": [],
    }


@pytest.fixture(autouse=True)
def _no_hardware(monkeypatch, tmp_path):
    """Every test runs with snapshot stubbed; opt back in never."""
    from api import (agent_providers, agentlink, ask_history, musiclink, state,
                     views, voicelink)
    from devices import config as device_config
    from devices.music import AppleMusic

    monkeypatch.setattr(state, "snapshot", stub_snapshot)
    monkeypatch.setattr(
        ask_history, "HISTORY_FILE", tmp_path / "ask_history.sqlite3")
    monkeypatch.setattr(
        agent_providers, "ACTIVE_PROFILE_FILE", tmp_path / "agent_profile")
    monkeypatch.setattr(
        views, "PANEL_SETTINGS_FILE", tmp_path / "ui_panels.json")
    monkeypatch.setattr(
        device_config, "USER_CONFIG_FILE", tmp_path / "config.yaml")
    # Queue tests must never read or write the user's persisted queue, and a
    # pump left sleeping by one test must see its own controller invalidated
    # before monkeypatch restores the real Music source.
    queue = musiclink.QueueController(tmp_path / "music_queue.json")
    monkeypatch.setattr(musiclink, "_QUEUE", queue)
    monkeypatch.setattr(musiclink, "_DISPATCH_LOCK", queue.lock)
    # recent_songs() falls back to this on-disk cache before consulting the
    # installed fake driver.  Point it at each test's sandbox so test order
    # can never leak the developer's Apple library into Roon scenarios (or
    # let a unit test overwrite the real cache).
    monkeypatch.setattr(
        musiclink, "SONGS_FILE", tmp_path / "recent_songs.json")
    monkeypatch.setattr(musiclink, "_recent_songs_cache", None)
    # The shipped config may deliberately select a live network provider.
    # Unit tests are not deployment smoke tests: keep their default music
    # seam side-effect-free, while provider-specific tests install their own
    # fake explicitly.
    monkeypatch.setattr(musiclink, "_MUSIC", AppleMusic())
    # Service capability checks must never depend on the developer's real
    # Music user token. Tests that exercise library mutation opt in by
    # stubbing service_info explicitly.
    monkeypatch.setattr(
        musiclink, "USER_TOKEN_FILE", tmp_path / "music_user_token")
    # TestClient enters the real app lifespan. Never download/load a multi-GB
    # MLX model or scan the user's Music library merely because an API unit
    # test started the service.
    monkeypatch.setattr(agentlink, "warmup", lambda: None)
    monkeypatch.setattr(agentlink, "_CURATION_REVIEWS", {})
    monkeypatch.setattr(voicelink, "warmup", lambda: None)
    # Queue responses schedule real Music.app artwork extraction. Individual
    # artwork tests opt back into the saved implementation; unrelated API
    # tests must never leave a daemon worker touching hardware after teardown.
    monkeypatch.setattr(musiclink, "prefetch_queue_artwork", lambda _data: None)
    yield
    with queue.lock:
        queue.revision += 1
        queue.items = []
        queue.materialized = 0
        queue.pump_revision = None
        queue.condition.notify_all()
    state.stop_poller()
