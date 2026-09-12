#!/usr/bin/env python3
"""Talk interactively to DeepSeek v4.1 Flash through Fireworks AI.

The API key is loaded only when this script is run. Prefer the environment:

    FIREWORKS_API_KEY=... python scripts/fireworks_chat.py

Without that variable, the script uses ~/.avctl/fireworks-api-key.
Use --key-file to choose another owner-only location.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests


API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
MODEL = "accounts/fireworks/models/deepseek-v4p1-flash"
DEFAULT_KEY_PATH = Path("~/.avctl/fireworks-api-key").expanduser()


class ChatError(RuntimeError):
    """A user-readable configuration or Fireworks API failure."""


def safe_provider_error_detail(detail: Any, api_key: str) -> str | None:
    """Bound and redact untrusted provider error text for a client UI."""
    if not isinstance(detail, str):
        return None
    if api_key:
        detail = detail.replace(api_key, "[redacted]")
    detail = re.sub(
        r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", detail)
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", detail).strip()
    return detail[:300] or None


def load_api_key(key_path: Path) -> str:
    """Load a key from the environment, a file, or a one-file directory."""
    environment_key = os.environ.get("FIREWORKS_API_KEY", "").strip()
    if environment_key:
        return environment_key

    path = key_path.expanduser()
    if path.is_dir():
        try:
            candidates = sorted(
                child for child in path.iterdir()
                if child.is_file() and not child.name.startswith(".")
            )
        except OSError as exc:
            raise ChatError(
                f"could not inspect API key directory {path}: {exc.strerror}"
            ) from None
        if len(candidates) != 1:
            raise ChatError(
                f"{path} must contain exactly one non-hidden key file; "
                "use --key-file to select one explicitly"
            )
        path = candidates[0]

    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ChatError(f"could not read API key from {path}: {exc.strerror}") \
            from None
    if not key:
        raise ChatError(f"API key file is empty: {path}")
    return key


def completion_payload(
    messages: list[dict[str, str]], model: str, max_tokens: int
) -> dict[str, Any]:
    return {
        "model": model,
        "service_tier": "priority",
        "max_tokens": max_tokens,
        "top_k": 40,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "messages": messages,
    }


def request_completion(
    session: requests.Session,
    api_key: str,
    messages: list[dict[str, str]],
    *,
    model: str = MODEL,
    max_tokens: int = 131_072,
    timeout: float = 600,
) -> str:
    """Send one turn without ever logging or returning the credential."""
    try:
        response = session.post(
            API_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=completion_payload(messages, model, max_tokens),
            timeout=(10, timeout),
        )
    except requests.RequestException as exc:
        raise ChatError(
            f"Fireworks request failed ({type(exc).__name__})"
        ) from None

    try:
        data = response.json()
    except requests.JSONDecodeError:
        raise ChatError(
            f"Fireworks returned HTTP {response.status_code} with no JSON body"
        ) from None

    if not response.ok:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        detail = error.get("message") if isinstance(error, dict) else None
        detail = safe_provider_error_detail(detail, api_key)
        raise ChatError(
            f"Fireworks returned HTTP {response.status_code}"
            + (f": {detail}" if detail else "")
        )

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ChatError("Fireworks returned no assistant message") from None
    if not isinstance(content, str) or not content.strip():
        raise ChatError("Fireworks returned an empty assistant message")
    return content


def chat(
    api_key: str,
    *,
    model: str = MODEL,
    max_tokens: int = 131_072,
    timeout: float = 600,
    session: requests.Session | None = None,
) -> None:
    """Run a multi-turn terminal conversation until EOF, Ctrl-C, or /exit."""
    client = session or requests.Session()
    messages: list[dict[str, str]] = []
    print(f"DeepSeek v4.1 Flash via Fireworks ({model})")
    print("Commands: /clear forgets this conversation; /exit quits.\n")

    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            return
        if prompt == "/clear":
            messages.clear()
            print("conversation cleared\n")
            continue

        messages.append({"role": "user", "content": prompt})
        try:
            answer = request_completion(
                client,
                api_key,
                messages,
                model=model,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except ChatError as exc:
            messages.pop()
            print(f"error: {exc}\n", file=sys.stderr)
            continue

        messages.append({"role": "assistant", "content": answer})
        print(f"\nassistant> {answer}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-file",
        type=Path,
        default=DEFAULT_KEY_PATH,
        help="key file, or directory containing exactly one key file",
    )
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-tokens", type=int, default=131_072)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if args.max_tokens < 1:
        parser.error("--max-tokens must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        api_key = load_api_key(args.key_file)
    except ChatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    chat(
        api_key,
        model=args.model,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
