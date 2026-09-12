"""Configurable model transports for Ask.

The provider only moves an OpenAI-compatible conversation to a model and
normalises operational metadata.  It never interprets intent or executes an
avctl tool; those responsibilities stay in :mod:`api.agentlink` so changing a
model cannot change the rack's safety boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from devices import config as device_config


DEFAULT_PROFILE = "fireworks-priority"
DEFAULT_FIREWORKS_URL = "https://api.fireworks.ai/inference/v1"
DEFAULT_FIREWORKS_MODEL = \
    "accounts/fireworks/models/deepseek-v4p1-flash"
KIMI_K3_MODEL = "accounts/fireworks/models/kimi-k3"
DEFAULT_KEY_PATH = Path("~/.avctl/fireworks-api-key").expanduser()
ACTIVE_PROFILE_FILE = Path(
    os.environ.get("AVCTL_AGENT_PROFILE_FILE", "~/.avctl/agent_profile")
).expanduser()

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DRIVERS = {"fireworks", "openai-compatible"}
_SWITCH_LOCK = threading.Lock()
_SLOTS: dict[str, threading.BoundedSemaphore] = {}
_SLOTS_LOCK = threading.Lock()


class ProviderError(RuntimeError):
    """A bounded, credential-safe provider configuration or request error."""


@dataclass(frozen=True)
class Pricing:
    input_per_million: float = 0
    cached_input_per_million: float = 0
    output_per_million: float = 0

    @property
    def available(self) -> bool:
        return any((self.input_per_million,
                    self.cached_input_per_million,
                    self.output_per_million))


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    driver: str
    base_url: str
    model: str
    api_key_env: str | None
    api_key_file: Path | None
    service_tier: str | None
    timeout_seconds: float
    max_tokens: int
    parallel_tool_calls: bool
    concurrency: int
    pricing: Pricing

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def label(self) -> str:
        return self.name.replace("-", " ").replace("_", " ").title()

    @property
    def provider_name(self) -> str:
        return "Fireworks" if self.driver == "fireworks" else self.label


@dataclass(frozen=True)
class ProviderReply:
    body: dict[str, Any]
    elapsed_ms: int


def _bounded_detail(detail: Any, credential: str) -> str | None:
    if not isinstance(detail, str):
        return None
    if credential:
        detail = detail.replace(credential, "[redacted]")
    detail = re.sub(
        r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", detail)
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", detail).strip()
    return detail[:300] or None


def _default_config() -> dict[str, Any]:
    """Keep an old config deploy working while the new block rolls out."""
    return {
        "active_profile": DEFAULT_PROFILE,
        "profiles": {
            DEFAULT_PROFILE: {
                "driver": "fireworks",
                "base_url": DEFAULT_FIREWORKS_URL,
                "model": DEFAULT_FIREWORKS_MODEL,
                "credential": {
                    "env": "FIREWORKS_API_KEY",
                    "file": str(DEFAULT_KEY_PATH),
                },
                "service_tier": "priority",
                "timeout_seconds": 300,
                "max_tokens": 100000,
                "parallel_tool_calls": True,
                "concurrency": 4,
                "pricing": {
                    "input_per_million": 0.21,
                    "cached_input_per_million": 0.042,
                    "output_per_million": 0.42,
                },
            },
            "kimi-k3": {
                "driver": "fireworks",
                "base_url": DEFAULT_FIREWORKS_URL,
                "model": KIMI_K3_MODEL,
                "credential": {
                    "env": "FIREWORKS_API_KEY",
                    "file": str(DEFAULT_KEY_PATH),
                },
                "service_tier": "priority",
                "timeout_seconds": 300,
                # Kimi separates reasoning from its final answer. Give it
                # enough room to finish both phases on tool-heavy turns.
                "max_tokens": 100000,
                "parallel_tool_calls": True,
                "concurrency": 4,
                "pricing": {
                    "input_per_million": 3.75,
                    "cached_input_per_million": 0.375,
                    "output_per_million": 18.75,
                },
            },
        },
    }


def _number(value: Any, default: float, low: float, high: float,
            label: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ProviderError(f"agent profile {label} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ProviderError(f"agent profile {label} must be a number") from None
    if not low <= parsed <= high:
        raise ProviderError(
            f"agent profile {label} must be between {low:g} and {high:g}")
    return parsed


def _profile(name: str, raw: Any) -> ProviderProfile:
    if not _PROFILE_NAME.fullmatch(name):
        raise ProviderError(f"invalid agent profile name: {name!r}")
    if not isinstance(raw, dict):
        raise ProviderError(f"agent profile {name} must be a mapping")
    driver = str(raw.get("driver") or "").strip().lower()
    if driver not in _DRIVERS:
        raise ProviderError(
            f"agent profile {name} has unsupported driver {driver!r}")
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    parsed_url = urlparse(base_url)
    if (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc
            or parsed_url.username is not None or parsed_url.password is not None
            or parsed_url.query or parsed_url.fragment):
        raise ProviderError(f"agent profile {name} has an invalid base_url")
    model = str(raw.get("model") or "").strip()
    if not model or len(model) > 300:
        raise ProviderError(f"agent profile {name} requires a valid model")
    credential = raw.get("credential") or {}
    if not isinstance(credential, dict):
        raise ProviderError(f"agent profile {name} credential must be a mapping")
    env_name = str(credential.get("env") or "").strip() or None
    if env_name is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}",
                                                  env_name):
        raise ProviderError(f"agent profile {name} has an invalid credential env")
    file_value = str(credential.get("file") or "").strip()
    key_file = Path(file_value).expanduser() if file_value else None
    service_tier = str(raw.get("service_tier") or "").strip() or None
    timeout = _number(raw.get("timeout_seconds"), 60, 1, 600,
                      f"{name}.timeout_seconds")
    max_tokens = int(_number(raw.get("max_tokens"), 1024, 128, 131072,
                             f"{name}.max_tokens"))
    concurrency = int(_number(raw.get("concurrency"), 4, 1, 32,
                              f"{name}.concurrency"))
    parallel = raw.get("parallel_tool_calls", True)
    if not isinstance(parallel, bool):
        raise ProviderError(
            f"agent profile {name}.parallel_tool_calls must be true or false")
    raw_pricing = raw.get("pricing") or {}
    if not isinstance(raw_pricing, dict):
        raise ProviderError(f"agent profile {name} pricing must be a mapping")
    pricing = Pricing(
        _number(raw_pricing.get("input_per_million"), 0, 0, 1_000_000,
                f"{name}.pricing.input_per_million"),
        _number(raw_pricing.get("cached_input_per_million"), 0, 0, 1_000_000,
                f"{name}.pricing.cached_input_per_million"),
        _number(raw_pricing.get("output_per_million"), 0, 0, 1_000_000,
                f"{name}.pricing.output_per_million"),
    )
    return ProviderProfile(
        name=name, driver=driver, base_url=base_url, model=model,
        api_key_env=env_name, api_key_file=key_file,
        service_tier=service_tier, timeout_seconds=timeout,
        max_tokens=max_tokens, parallel_tool_calls=parallel,
        concurrency=concurrency, pricing=pricing,
    )


def profiles() -> dict[str, ProviderProfile]:
    try:
        block = device_config.load_config().get("agent") or _default_config()
    except FileNotFoundError:
        block = _default_config()
    if not isinstance(block, dict) or not isinstance(block.get("profiles"), dict):
        raise ProviderError("config.yaml agent.profiles must be a mapping")
    parsed = {str(name): _profile(str(name), raw)
              for name, raw in block["profiles"].items()}
    if not parsed:
        raise ProviderError("config.yaml agent.profiles cannot be empty")
    return parsed


def configured_default() -> str:
    try:
        block = device_config.load_config().get("agent") or _default_config()
    except FileNotFoundError:
        block = _default_config()
    return str(block.get("active_profile") or DEFAULT_PROFILE).strip()


def active_profile_name() -> str:
    known = profiles()
    environment = os.environ.get("AVCTL_AGENT_PROFILE", "").strip()
    if environment:
        if environment not in known:
            raise ProviderError(
                f"AVCTL_AGENT_PROFILE names unknown profile {environment!r}")
        return environment
    try:
        saved = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        saved = ""
    except OSError as exc:
        raise ProviderError(
            f"could not read the active agent profile: {exc.strerror}") from None
    selected = saved or configured_default()
    if selected not in known:
        raise ProviderError(f"active agent profile {selected!r} does not exist")
    return selected


def active_profile() -> ProviderProfile:
    known = profiles()
    name = active_profile_name()
    return known[name]


def set_active_profile(name: str) -> ProviderProfile:
    if os.environ.get("AVCTL_AGENT_PROFILE", "").strip():
        raise ProviderError("the active agent profile is managed by the environment")
    known = profiles()
    if name not in known:
        raise ProviderError(f"unknown agent profile {name!r}")
    with _SWITCH_LOCK:
        try:
            ACTIVE_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = ACTIVE_PROFILE_FILE.with_name(
                ACTIVE_PROFILE_FILE.name + ".tmp")
            temporary.write_text(name + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(ACTIVE_PROFILE_FILE)
        except OSError as exc:
            raise ProviderError(
                f"could not save the active agent profile: {exc.strerror}") from None
    return known[name]


def _credential(profile: ProviderProfile, override: str | None) -> str:
    if override is not None:
        return override.strip()
    if profile.api_key_env:
        environment = os.environ.get(profile.api_key_env, "").strip()
        if environment:
            return environment
    if profile.api_key_file is None:
        # Local OpenAI-compatible servers commonly require no real secret.
        return ""
    path = profile.api_key_file
    if path.is_dir():
        try:
            candidates = sorted(
                child for child in path.iterdir()
                if child.is_file() and not child.name.startswith("."))
        except OSError as exc:
            raise ProviderError(
                f"could not inspect credential directory: {exc.strerror}") from None
        if len(candidates) != 1:
            raise ProviderError(
                "the credential directory must contain exactly one non-hidden file")
        path = candidates[0]
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProviderError(
            f"could not read the agent credential: {exc.strerror}") from None
    if not value:
        raise ProviderError("the agent credential is empty")
    return value


def credential_status(profile: ProviderProfile) -> str:
    """Report the configured source without opening or returning a secret."""
    if profile.api_key_env and os.environ.get(profile.api_key_env, "").strip():
        return "environment"
    if profile.api_key_file is not None:
        try:
            return ("file" if profile.api_key_file.is_file()
                    and profile.api_key_file.stat().st_size > 0 else "missing")
        except OSError:
            return "missing"
    return "not required"


def save_credential(name: str, value: str) -> str:
    """Store a provider secret locally without ever returning its contents."""
    known = profiles()
    profile = known.get(name)
    if profile is None:
        raise ProviderError(f"unknown agent profile {name!r}")
    if profile.api_key_env and os.environ.get(profile.api_key_env, "").strip():
        raise ProviderError("this profile credential is managed by the environment")
    destination = profile.api_key_file
    if destination is None:
        raise ProviderError("this profile does not require a credential")
    secret = value.strip()
    if not secret or len(secret) > 8192 or "\x00" in secret:
        raise ProviderError("enter a valid provider credential")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    handle, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(secret + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise
    return "file"


class OpenAICompatibleProvider:
    def __init__(self, profile: ProviderProfile,
                 *, session: Any, api_key: str | None = None,
                 slot_override: Any = None):
        self.profile = profile
        self.session = session
        self.credential = _credential(profile, api_key)
        self.slot_override = slot_override

    def _payload(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], caller_key: str
                 ) -> tuple[dict[str, Any], dict[str, str]]:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "max_tokens": self.profile.max_tokens,
            "temperature": 0,
            "messages": list(messages),
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": self.profile.parallel_tool_calls,
        }
        headers = {"Accept": "application/json",
                   "Content-Type": "application/json"}
        if self.credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        return payload, headers

    def _slot(self) -> threading.BoundedSemaphore:
        if self.slot_override is not None:
            return self.slot_override
        with _SLOTS_LOCK:
            slot = _SLOTS.get(self.profile.name)
            if slot is None:
                slot = threading.BoundedSemaphore(self.profile.concurrency)
                _SLOTS[self.profile.name] = slot
            return slot

    def complete(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], caller_key: str,
                 *, tool_choice: Any = "auto") -> ProviderReply:
        payload, headers = self._payload(messages, tools, caller_key)
        payload["tool_choice"] = tool_choice
        slot = self._slot()
        elapsed = 0.0
        data: Any = None
        response: Any = None
        for attempt in range(2):
            if not slot.acquire(blocking=False):
                raise ProviderError(
                    "Ask is busy with other requests; try again in a moment")
            started = time.perf_counter()
            try:
                try:
                    response = self.session.post(
                        self.profile.chat_url, headers=headers, json=payload,
                        timeout=(5, self.profile.timeout_seconds))
                finally:
                    elapsed += time.perf_counter() - started
                    slot.release()
            except requests.ConnectionError as exc:
                if attempt == 0:
                    continue
                raise ProviderError(
                    f"{self.profile.provider_name} request failed "
                    f"({type(exc).__name__})") from None
            except requests.RequestException as exc:
                raise ProviderError(
                    f"{self.profile.provider_name} request failed "
                    f"({type(exc).__name__})") from None
            try:
                data = response.json()
            except (requests.JSONDecodeError, ValueError):
                if attempt == 0 and (response.ok
                                     or response.status_code >= 500):
                    continue
                raise ProviderError(
                    f"{self.profile.provider_name} returned HTTP "
                    f"{response.status_code} with no JSON body") from None
            if response.status_code in {429, 502, 503, 504} and attempt == 0:
                continue
            break
        if response is None or not isinstance(data, dict):
            raise ProviderError(
                f"{self.profile.provider_name} returned an invalid response")
        if not response.ok:
            detail: Any = None
            provider_error = data.get("error")
            if isinstance(provider_error, dict):
                detail = provider_error.get("message")
            elif isinstance(provider_error, str):
                detail = provider_error
            elif isinstance(data.get("detail"), str):
                detail = data["detail"]
            detail = _bounded_detail(detail, self.credential)
            raise ProviderError(
                f"{self.profile.provider_name} returned HTTP "
                f"{response.status_code}"
                + (f": {detail}" if detail else ""))
        return ProviderReply(data, round(elapsed * 1000))


class FireworksProvider(OpenAICompatibleProvider):
    def _payload(self, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]], caller_key: str
                 ) -> tuple[dict[str, Any], dict[str, str]]:
        payload, headers = super()._payload(messages, tools, caller_key)
        # A caller may switch models without starting a new process. Keep
        # Fireworks' sticky routing and prompt cache strictly model-scoped so
        # a Kimi turn can never influence the following DeepSeek turn (or the
        # reverse). Include the model as well as the profile name because an
        # operator can repoint a profile in config while the service is live.
        profile_key = hashlib.sha256(
            f"{self.profile.name}\0{self.profile.model}".encode()
        ).hexdigest()[:16]
        affinity = f"avctl-{profile_key}-{caller_key}"
        if self.profile.service_tier:
            payload["service_tier"] = self.profile.service_tier
        payload["prompt_cache_key"] = affinity
        payload["prompt_cache_isolation_key"] = profile_key
        payload["user"] = caller_key
        headers["x-session-affinity"] = affinity
        return payload, headers


def provider(*, session: Any, api_key: str | None = None,
             slot_override: Any = None, profile_name: str | None = None) \
        -> OpenAICompatibleProvider:
    if profile_name is None:
        selected = active_profile()
    else:
        known = profiles()
        if profile_name not in known:
            raise ProviderError(f"unknown agent profile {profile_name!r}")
        selected = known[profile_name]
    cls = FireworksProvider if selected.driver == "fireworks" \
        else OpenAICompatibleProvider
    return cls(selected, session=session, api_key=api_key,
               slot_override=slot_override)


def test_profile(name: str) -> dict[str, Any]:
    """Verify transport plus structured tool calling without an avctl action."""
    tool = {
        "type": "function",
        "function": {
            "name": "avctl_connection_ready",
            "description": "Confirm this provider can return tool calls.",
            "parameters": {"type": "object", "properties": {},
                           "additionalProperties": False},
        },
    }
    with requests.Session() as session:
        selected = provider(session=session, profile_name=name)
        reply = selected.complete(
            [{"role": "system", "content": (
                "This is a connection test. Call avctl_connection_ready "
                "exactly once and return no prose.")},
             {"role": "user", "content": "Verify tool calling now."}],
            [tool], "connection-test",
            tool_choice={"type": "function",
                         "function": {"name": "avctl_connection_ready"}},
        )
    try:
        message = reply.body["choices"][0]["message"]
        calls = message["tool_calls"]
        function = calls[0]["function"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError(
            "the provider connected, but structured tool calling was not verified"
        ) from None
    if (not isinstance(calls, list) or len(calls) != 1
            or not isinstance(function, dict)
            or function.get("name") != "avctl_connection_ready"):
        raise ProviderError(
            "the provider connected, but returned the wrong test tool")
    return {
        "profile": selected.profile.name,
        "model": selected.profile.model,
        "latency_ms": reply.elapsed_ms,
        "tool_calling": True,
    }


def public_settings() -> dict[str, Any]:
    known = profiles()
    active = active_profile_name()
    managed = bool(os.environ.get("AVCTL_AGENT_PROFILE", "").strip())
    return {
        "active_profile": active,
        "managed": managed,
        "profiles": [{
            "id": item.name,
            "label": item.label,
            "driver": item.driver,
            "model": item.model,
            "base_url": item.base_url,
            "credential": credential_status(item),
            "service_tier": item.service_tier,
            "parallel_tool_calls": item.parallel_tool_calls,
            "cost_available": item.pricing.available,
        } for item in known.values()],
    }
