from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from api import phonelink
from devices import config as device_config
from devices.apple_broker import BrokerError, ManagedAppleBroker, validate_url
from devices.music.apple_music import MusicKit


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


def test_default_broker_address_does_not_enroll_the_core(tmp_path):
    config = {"apple_services": {"mode": "managed", "broker": {
        "url": "https://default-broker.example/",
        "state_file": str(tmp_path / "broker.json"),
        "invite": "never-return-this-passphrase",
    }}}

    assert ManagedAppleBroker.enrollment_info(config) == {
        "enrolled": False, "url": "https://default-broker.example",
        "installation_id": None,
    }
    with pytest.raises(BrokerError, match="need enrollment"):
        ManagedAppleBroker.from_config(config)
    assert not (tmp_path / "broker.json").exists()


@pytest.mark.parametrize("url", [
    None, "", "http://broker.example", "invalid", "https://[invalid",
])
def test_missing_or_invalid_default_broker_address_stays_unconfigured(tmp_path, url):
    config = {"apple_services": {"broker": {
        "url": url, "state_file": str(tmp_path / "broker.json"),
    }}}

    assert ManagedAppleBroker.enrollment_info(config) == {
        "enrolled": False, "url": None, "installation_id": None,
    }


def test_enrolled_broker_address_takes_precedence_over_bundled_default(tmp_path):
    config = {"apple_services": {"broker": {
        "url": "https://default-broker.example",
        "state_file": str(tmp_path / "broker.json"),
    }}}
    enrollment = ManagedAppleBroker.enroll(
        config, "https://chosen-broker.example", "synthetic-one-use-passphrase",
        post=lambda *_args, **_kwargs: Response(),
    )

    assert ManagedAppleBroker.enrollment_info(config) == {
        "enrolled": True, "url": "https://chosen-broker.example",
        "installation_id": enrollment["installation_id"],
    }


def test_enrollment_creates_private_core_identity_and_never_saves_invite(
    monkeypatch, tmp_path,
):
    state_file = tmp_path / "state" / "broker.json"
    config = {"apple_services": {"broker": {"state_file": str(state_file)}}}
    captured = {}

    def post(url, **kwargs):
        captured.update(kwargs["json"])
        return Response(payload={"capabilities": {"musickit": True,
                                                    "apns": True}})

    result = ManagedAppleBroker.enroll(
        config, "https://broker.example", "one-use-passphrase", post=post)

    saved = json.loads(state_file.read_text())
    assert result["enrolled"] is True
    assert captured["invite"] == "one-use-passphrase"
    assert "invite" not in saved
    assert state_file.stat().st_mode & 0o777 == 0o600
    key_file = tmp_path / "state" / "apple-broker-key.pem"
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE KEY" in key_file.read_text()
    assert len(base64.urlsafe_b64decode(
        captured["public_key"] + "==")) == 32


def test_client_signs_body_path_nonce_and_timestamp(monkeypatch, tmp_path):
    private_file = tmp_path / "key.pem"
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private = Ed25519PrivateKey.generate()
    private_file.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    client = ManagedAppleBroker(
        "https://broker.example", "a" * 32, str(private_file))
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response(payload={"token": "developer", "expires_at": 12345})

    monkeypatch.setattr("devices.apple_broker.requests.post", post)
    assert client.musickit_token() == ("developer", 12345.0)
    headers = captured["headers"]
    body = captured["data"]
    digest = hashlib.sha256(body).hexdigest()
    message = (f"POST\n/v1/musickit/token\n{headers['X-Avctl-Timestamp']}\n"
               f"{headers['X-Avctl-Nonce']}\n{digest}").encode()
    signature = base64.urlsafe_b64decode(
        headers["X-Avctl-Signature"] + "==")
    Ed25519PublicKey.from_public_bytes(private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw)).verify(signature, message)


def test_broker_url_rejects_plain_remote_http():
    assert validate_url("http://127.0.0.1:8700") == "http://127.0.0.1:8700"
    try:
        validate_url("http://broker.example")
    except BrokerError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("remote HTTP broker was accepted")


def test_musickit_uses_managed_token_provider_and_caches_short_token(
    monkeypatch,
):
    calls = []

    class Broker:
        def musickit_token(self):
            calls.append(True)
            return "managed-token", time.time() + 600

    monkeypatch.setattr(ManagedAppleBroker, "from_config",
                        classmethod(lambda cls, config: Broker()))
    kit = MusicKit.from_config({
        "apple_services": {"mode": "managed"},
        "music": {"AppleMusic": {"storefront": "us"}},
    })
    assert kit and kit.dev_token() == "managed-token"
    assert kit.dev_token() == "managed-token"
    assert len(calls) == 1


def test_disabled_apple_services_do_not_fall_back_to_local_private_key():
    assert MusicKit.from_config({
        "apple_services": {"mode": "disabled"},
        "music": {"AppleMusic": {"team_id": "TEAM",
                                   "key_id": "KEY",
                                   "key_file": "/must/not/be/read"}},
    }) is None


def test_managed_phone_registration_persists_only_broker_reference(
    monkeypatch, tmp_path,
):
    class Broker:
        def register_device(self, kind, token, activity_id, environment):
            assert token == "ab" * 32
            assert environment == "sandbox"
            return "device-reference"

    monkeypatch.setattr(ManagedAppleBroker, "from_config",
                        classmethod(lambda cls, config: Broker()))
    monkeypatch.setattr(device_config, "load_config", lambda: {
        "apple_services": {"mode": "managed", "broker": {
            "apns_environment": "sandbox"}}, "phone": {}})
    monkeypatch.setattr(phonelink, "TOKENS_FILE", tmp_path / "phone.json")

    phonelink.register("widget", "ab" * 32)

    contents = (tmp_path / "phone.json").read_text()
    assert "broker:device-reference" in contents
    assert "abababab" not in contents
