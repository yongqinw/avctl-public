from __future__ import annotations

import pytest
import requests

from scripts import fireworks_chat


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_environment_key_wins_without_touching_key_path(monkeypatch, tmp_path):
    monkeypatch.setenv("FIREWORKS_API_KEY", "synthetic-environment-key")
    missing = tmp_path / "must-not-be-read"

    assert fireworks_chat.load_api_key(missing) == "synthetic-environment-key"


def test_one_file_key_directory_is_supported(monkeypatch, tmp_path):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    (tmp_path / ".note").write_text("ignored", encoding="utf-8")
    (tmp_path / "key").write_text("synthetic-file-key\n", encoding="utf-8")

    assert fireworks_chat.load_api_key(tmp_path) == "synthetic-file-key"


def test_ambiguous_key_directory_is_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    (tmp_path / "one").write_text("first", encoding="utf-8")
    (tmp_path / "two").write_text("second", encoding="utf-8")

    with pytest.raises(fireworks_chat.ChatError, match="exactly one"):
        fireworks_chat.load_api_key(tmp_path)


def test_completion_uses_requested_model_and_keeps_history():
    session = FakeSession(FakeResponse(200, {
        "choices": [{"message": {"content": "I remember."}}],
    }))
    messages = [
        {"role": "user", "content": "My rack is called Atlas."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "What is its name?"},
    ]

    answer = fireworks_chat.request_completion(
        session, "synthetic-request-key", messages
    )

    assert answer == "I remember."
    url, request = session.calls[0]
    assert url == fireworks_chat.API_URL
    assert request["headers"]["Authorization"] == \
        "Bearer synthetic-request-key"
    assert request["json"] == {
        "model": "accounts/fireworks/models/deepseek-v4p1-flash",
        "service_tier": "priority",
        "max_tokens": 131_072,
        "top_k": 40,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "messages": messages,
    }


def test_api_errors_are_short_and_do_not_include_the_key():
    session = FakeSession(FakeResponse(401, {
        "error": {"message": "invalid authentication"},
    }))

    with pytest.raises(fireworks_chat.ChatError) as caught:
        fireworks_chat.request_completion(
            session,
            "synthetic-secret-that-must-not-appear",
            [{"role": "user", "content": "hello"}],
        )

    assert str(caught.value) == \
        "Fireworks returned HTTP 401: invalid authentication"
    assert "synthetic-secret" not in str(caught.value)


def test_provider_error_detail_redacts_bearers_controls_and_long_bodies():
    leaked = "synthetic-secret-that-must-not-appear"
    session = FakeSession(FakeResponse(401, {
        "error": {"message": (
            f"bad Authorization Bearer {leaked}\x00\n" + "x" * 500)},
    }))

    with pytest.raises(fireworks_chat.ChatError) as caught:
        fireworks_chat.request_completion(
            session, leaked, [{"role": "user", "content": "hello"}],
        )

    detail = str(caught.value)
    assert leaked not in detail
    assert "Bearer [redacted]" in detail
    assert "\x00" not in detail
    assert len(detail) <= len("Fireworks returned HTTP 401: ") + 300


def test_request_exceptions_do_not_need_a_real_response(monkeypatch):
    class BrokenSession:
        def post(self, *args, **kwargs):
            raise requests.Timeout("timed out")

    with pytest.raises(fireworks_chat.ChatError, match="request failed"):
        fireworks_chat.request_completion(
            BrokenSession(),
            "synthetic-key",
            [{"role": "user", "content": "hello"}],
        )
