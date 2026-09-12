"""Client for the publisher-hosted Apple capability broker.

The publisher keys never cross this boundary.  A Core owns an Ed25519 key,
enrolls its public half with a one-time invite, and signs each narrow request.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class BrokerError(RuntimeError):
    pass


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    handle, temporary = tempfile.mkstemp(dir=path.parent,
                                         prefix=f".{path.name}-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def validate_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname:
        try:
            if ipaddress.ip_address(parsed.hostname).is_loopback:
                return url
        except ValueError:
            if parsed.hostname == "localhost":
                return url
    raise BrokerError("broker URL must use HTTPS (HTTP is allowed only on loopback)")


class ManagedAppleBroker:
    def __init__(self, url: str, installation_id: str, private_key_file: str,
                 *, timeout: float = 12):
        self.url = validate_url(url)
        self.installation_id = installation_id
        self.private_key_file = Path(private_key_file).expanduser()
        self.timeout = timeout

    @classmethod
    def state_file(cls, config: dict[str, Any]) -> Path:
        block = config.get("apple_services") or {}
        broker = block.get("broker") or {}
        return Path(str(broker.get("state_file") or
                        "~/.avctl/apple-broker.json")).expanduser()

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "ManagedAppleBroker | None":
        block = config.get("apple_services") or {}
        if str(block.get("mode") or "local") != "managed":
            return None
        return cls.from_state(config)

    @classmethod
    def from_state(cls, config: dict[str, Any]) -> "ManagedAppleBroker":
        block = config.get("apple_services") or {}
        state_file = cls.state_file(config)
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return cls(str(state["url"]), str(state["installation_id"]),
                       str(state["private_key_file"]),
                       timeout=float((block.get("broker") or {}).get("timeout") or 12))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BrokerError(f"managed Apple services need enrollment: {exc}") from None

    @classmethod
    def enrollment_info(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Expose the paired address, or a setup default without enrollment."""
        try:
            client = cls.from_state(config)
        except BrokerError:
            broker = (config.get("apple_services") or {}).get("broker") or {}
            try:
                url = validate_url(str(broker.get("url") or ""))
            except (BrokerError, ValueError):
                url = None
            return {"enrolled": False, "url": url,
                    "installation_id": None}
        return {"enrolled": True, "url": client.url,
                "installation_id": client.installation_id}

    @classmethod
    def enroll(cls, config: dict[str, Any], url: str, invite: str,
               label: str = "avctl Core", *, post: Any = requests.post
               ) -> dict[str, Any]:
        url = validate_url(url)
        invite = invite.strip()
        if not invite or len(invite) > 256:
            raise BrokerError("enter a valid one-time broker invite")
        state_file = cls.state_file(config)
        private_file = state_file.with_name("apple-broker-key.pem")
        private = Ed25519PrivateKey.generate()
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        installation_id = secrets.token_hex(16)
        try:
            response = post(f"{url}/v1/enroll", json={
                "invite": invite,
                "installation_id": installation_id,
                "public_key": _b64(public),
                "label": label[:100],
            }, timeout=12)
        except requests.RequestException as exc:
            raise BrokerError(f"broker enrollment failed: {exc}") from None
        if not response.ok:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise BrokerError(detail or f"broker enrollment failed: HTTP {response.status_code}")
        pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        _private_write(private_file, pem)
        _private_write(state_file, (json.dumps({
            "url": url, "installation_id": installation_id,
            "private_key_file": str(private_file),
        }, indent=2) + "\n").encode())
        return {"enrolled": True, "url": url,
                "installation_id": installation_id,
                "capabilities": response.json().get("capabilities") or {}}

    def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(18)
        digest = hashlib.sha256(body).hexdigest()
        message = f"POST\n{path}\n{timestamp}\n{nonce}\n{digest}".encode()
        try:
            private = serialization.load_pem_private_key(
                self.private_key_file.read_bytes(), password=None)
            signature = private.sign(message)
            response = requests.post(
                f"{self.url}{path}", data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Avctl-Installation": self.installation_id,
                    "X-Avctl-Timestamp": timestamp,
                    "X-Avctl-Nonce": nonce,
                    "X-Avctl-Signature": _b64(signature),
                }, timeout=self.timeout)
        except (OSError, ValueError, requests.RequestException) as exc:
            raise BrokerError(f"Apple broker unavailable: {exc}") from None
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not response.ok:
            raise BrokerError(str(result.get("detail") or
                                  f"broker returned HTTP {response.status_code}"))
        return result

    def status(self) -> dict[str, Any]:
        return self._request("/v1/status", {})

    def musickit_token(self) -> tuple[str, float]:
        result = self._request("/v1/musickit/token", {})
        return str(result["token"]), float(result["expires_at"])

    def register_device(self, kind: str, token: str,
                        activity_id: str | None, environment: str) -> str:
        result = self._request("/v1/devices", {
            "kind": kind, "token": token, "activity_id": activity_id,
            "environment": environment,
        })
        return str(result["device_ref"])

    def push(self, device_ref: str, kind: str,
             payload: dict[str, Any]) -> tuple[int, str | None]:
        result = self._request("/v1/push", {
            "device_ref": device_ref, "kind": kind, "payload": payload,
        })
        return int(result["status"]), result.get("reason")
