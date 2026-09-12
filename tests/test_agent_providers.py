from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api import agent_providers
from api.main import app
from tests.conftest import AUTH


class FakeResponse:
    status_code = 200
    ok = True

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.body)


def config(*, active="fireworks-priority"):
    return {"agent": {
        "active_profile": active,
        "profiles": {
            "fireworks-priority": {
                "driver": "fireworks",
                "base_url": "https://fireworks.invalid/v1",
                "model": "deepseek-test",
                "credential": {"env": "SYNTHETIC_FIREWORKS_KEY"},
                "service_tier": "priority",
                "max_tokens": 10000,
                "pricing": {"input_per_million": 1,
                            "cached_input_per_million": .1,
                            "output_per_million": 2},
            },
            "kimi-k3": {
                "driver": "fireworks",
                "base_url": "https://fireworks.invalid/v1",
                "model": "accounts/fireworks/models/kimi-k3",
                "credential": {"env": "SYNTHETIC_FIREWORKS_KEY"},
                "service_tier": "priority",
                "max_tokens": 10000,
                "parallel_tool_calls": True,
            },
            "local": {
                "driver": "openai-compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "local-test",
                "parallel_tool_calls": False,
            },
        },
    }}


def test_fireworks_profile_keeps_only_its_transport_extensions(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    session = FakeSession({"choices": [{"message": {"content": "ok"}}]})
    selected = agent_providers.provider(
        session=session, api_key="synthetic-key")

    selected.complete([{"role": "user", "content": "hello"}], [], "caller")

    url, request = session.calls[0]
    assert url == "https://fireworks.invalid/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer synthetic-key"
    assert request["headers"]["x-session-affinity"].endswith("-caller")
    assert request["json"]["service_tier"] == "priority"
    assert request["json"]["prompt_cache_key"].endswith("-caller")
    assert request["json"]["prompt_cache_isolation_key"]
    assert request["json"]["max_tokens"] == 10000


def test_openai_compatible_profile_omits_fireworks_fields(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    agent_providers.set_active_profile("local")
    session = FakeSession({"choices": [{"message": {"content": "ok"}}]})
    selected = agent_providers.provider(session=session)

    selected.complete([{"role": "user", "content": "hello"}], [], "caller")

    url, request = session.calls[0]
    assert url == "http://127.0.0.1:11434/v1/chat/completions"
    assert "Authorization" not in request["headers"]
    assert "service_tier" not in request["json"]
    assert "prompt_cache_key" not in request["json"]
    assert "user" not in request["json"]
    assert request["json"]["parallel_tool_calls"] is False


def test_kimi_k3_uses_the_same_fireworks_transport(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    agent_providers.set_active_profile("kimi-k3")
    session = FakeSession({"choices": [{"message": {"content": "ok"}}]})
    selected = agent_providers.provider(
        session=session, api_key="synthetic-shared-key")

    selected.complete([{"role": "user", "content": "hello"}], [], "caller")

    url, request = session.calls[0]
    assert url == "https://fireworks.invalid/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer synthetic-shared-key"
    assert request["json"]["model"] == "accounts/fireworks/models/kimi-k3"
    assert request["json"]["service_tier"] == "priority"
    assert request["json"]["max_tokens"] == 10000


def test_fireworks_cache_affinity_is_isolated_between_models(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    first_session = FakeSession({"choices": [{"message": {"content": "ok"}}]})
    second_session = FakeSession({"choices": [{"message": {"content": "ok"}}]})

    deepseek = agent_providers.provider(
        session=first_session, api_key="synthetic-key",
        profile_name="fireworks-priority")
    kimi = agent_providers.provider(
        session=second_session, api_key="synthetic-key",
        profile_name="kimi-k3")
    deepseek.complete([], [], "same-caller")
    kimi.complete([], [], "same-caller")

    deepseek_request = first_session.calls[0][1]
    kimi_request = second_session.calls[0][1]
    assert (deepseek_request["json"]["prompt_cache_key"]
            != kimi_request["json"]["prompt_cache_key"])
    assert (deepseek_request["json"]["prompt_cache_isolation_key"]
            != kimi_request["json"]["prompt_cache_isolation_key"])
    assert (deepseek_request["headers"]["x-session-affinity"]
            != kimi_request["headers"]["x-session-affinity"])


def test_shipped_profiles_keep_deepseek_default_and_share_credential_source():
    for block in (
        agent_providers.device_config.load_config()["agent"],
        agent_providers._default_config(),
    ):
        deepseek = block["profiles"]["fireworks-priority"]
        kimi = block["profiles"]["kimi-k3"]
        assert block["active_profile"] == "fireworks-priority"
        assert deepseek["model"] == "accounts/fireworks/models/deepseek-v4p1-flash"
        assert deepseek["service_tier"] == "priority"
        assert kimi["model"] == "accounts/fireworks/models/kimi-k3"
        assert kimi["credential"] == deepseek["credential"]
        assert deepseek["timeout_seconds"] == kimi["timeout_seconds"] == 300
        assert deepseek["max_tokens"] == kimi["max_tokens"] == 100000
        assert kimi["pricing"] == {
            "input_per_million": 3.75,
            "cached_input_per_million": 0.375,
            "output_per_million": 18.75,
        }


def test_profile_switch_is_atomic_and_survives_reload(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)

    selected = agent_providers.set_active_profile("local")

    assert selected.name == "local"
    assert agent_providers.active_profile_name() == "local"
    assert agent_providers.ACTIVE_PROFILE_FILE.read_text().strip() == "local"
    assert agent_providers.ACTIVE_PROFILE_FILE.stat().st_mode & 0o777 == 0o600


def test_public_settings_never_return_credential_values(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    monkeypatch.setenv("SYNTHETIC_FIREWORKS_KEY", "must-not-leak")

    payload = agent_providers.public_settings()

    rendered = json.dumps(payload)
    assert "must-not-leak" not in rendered
    assert payload["profiles"][0]["credential"] == "environment"


def test_installer_saves_provider_credential_privately_without_echo(
    monkeypatch, tmp_path,
):
    key_file = tmp_path / "private" / "provider-key"
    configured = config()
    configured["agent"]["profiles"]["fireworks-priority"]["credential"] = {
        "file": str(key_file),
    }
    monkeypatch.setattr(agent_providers.device_config, "load_config",
                        lambda: configured)

    assert agent_providers.public_settings()["profiles"][0]["credential"] == "missing"
    assert agent_providers.save_credential(
        "fireworks-priority", "synthetic-installer-key") == "file"

    assert key_file.read_text().strip() == "synthetic-installer-key"
    assert key_file.stat().st_mode & 0o777 == 0o600
    rendered = json.dumps(agent_providers.public_settings())
    assert "synthetic-installer-key" not in rendered
    assert agent_providers.public_settings()["profiles"][0]["credential"] == "file"


def test_agent_credential_endpoint_requires_owner_and_never_echoes_secret(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(agent_providers, "save_credential",
                        lambda profile, value: calls.append((profile, value)) or "file")
    client = TestClient(app)

    denied = client.post("/api/settings/agent/credential", json={
        "profile": "fireworks-priority", "credential": "synthetic-secret",
    })
    saved = client.post("/api/settings/agent/credential", headers=AUTH, json={
        "profile": "fireworks-priority", "credential": "synthetic-secret",
    })

    assert denied.status_code == 401
    assert saved.json() == {"status": "ok", "profile": "fireworks-priority",
                            "credential": "file"}
    assert calls == [("fireworks-priority", "synthetic-secret")]
    assert "synthetic-secret" not in saved.text


def test_settings_api_switches_next_profile(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    client = TestClient(app)

    denied = client.get("/api/settings/agent")
    before = client.get("/api/settings/agent", headers=AUTH)
    changed = client.post(
        "/api/settings/agent", headers=AUTH, json={"profile": "local"})
    after = client.get("/api/settings/agent", headers=AUTH)

    assert denied.status_code == 401
    assert before.json()["active_profile"] == "fireworks-priority"
    assert changed.json()["active_profile"] == "local"
    assert after.json()["active_profile"] == "local"


def test_connection_probe_is_explicit_and_side_effect_free(monkeypatch):
    monkeypatch.setattr(agent_providers.device_config, "load_config", config)
    called = []
    monkeypatch.setattr(agent_providers, "test_profile", lambda name: (
        called.append(name) or {"profile": name, "model": "local-test",
                                "latency_ms": 12, "tool_calling": True}))
    client = TestClient(app)

    response = client.post(
        "/api/settings/agent/test", headers=AUTH, json={"profile": "local"})

    assert response.status_code == 200
    assert response.json()["tool_calling"] is True
    assert called == ["local"]


def test_invalid_profile_url_fails_before_any_request(monkeypatch):
    broken = config()
    broken["agent"]["profiles"]["local"]["base_url"] = "file:///tmp/model"
    monkeypatch.setattr(agent_providers.device_config,
                        "load_config", lambda: broken)

    with pytest.raises(agent_providers.ProviderError, match="invalid base_url"):
        agent_providers.profiles()
