#!/usr/bin/env python3
"""Non-destructive smoke test for an installed Core.

The script authenticates with the Core's owner token without printing it,
sets every reachable output to volume zero before any provider query, and
restores the previous volume in a ``finally`` block.  It never starts
playback, changes the queue/library, switches music backends, enables
Tailscale Serve, or saves setup.  Apple Music/Roon and Ask are exercised by
read-only discovery/search prompts against whichever backend is active.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class SmokeFailure(RuntimeError):
    """The installed Core did not satisfy a safe acceptance check."""


class CoreClient:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, path: str, payload: dict[str, Any] | None = None
                ) -> dict[str, Any]:
        body = (json.dumps(payload).encode("utf-8")
                if payload is not None else None)
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise SmokeFailure(
                f"{request.method} {path} returned {exc.code}: {detail}") from None
        except OSError as exc:
            raise SmokeFailure(f"{request.method} {path} failed: {exc}") from None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise SmokeFailure(f"{request.method} {path} did not return JSON") from None
        if not isinstance(value, dict):
            raise SmokeFailure(f"{request.method} {path} returned the wrong shape")
        return value


def _token(explicit: str | None, token_file: Path) -> str:
    value = explicit or os.environ.get("AVCTL_TEST_TOKEN", "")
    if not value:
        try:
            value = token_file.expanduser().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
    if not value:
        raise SmokeFailure(
            "set AVCTL_TEST_TOKEN or pass --token-file; the token is never printed")
    return value


def _fields(state: dict[str, Any], device: str) -> dict[str, Any]:
    row = (state.get("devices") or {}).get(device) or {}
    fields = row.get("fields") or {}
    return fields if isinstance(fields, dict) else {}


def _volume(state: dict[str, Any], device: str) -> int | None:
    value = _fields(state, device).get("volume")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _set_volume(client: CoreClient, command: str, level: int) -> None:
    result = client.request("/api/cmd", {
        "cmd": command, "args": {"level": level},
    })
    if result.get("status") not in {"ok", "accepted"}:
        raise SmokeFailure(f"{command} was not accepted: {result.get('status')}")


def _wait_volume(client: CoreClient, device: str, expected: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _volume(client.request("/api/state"), device) == expected:
            return
        time.sleep(0.25)
    raise SmokeFailure(f"{device} volume did not reach {expected}")


def _search(client: CoreClient, query: str) -> dict[str, Any]:
    encoded = urllib.parse.urlencode({"q": query, "limit": 6})
    return client.request(f"/api/music/search?{encoded}")


def run(client: CoreClient, query: str, include_network: bool) -> None:
    initial = client.request("/api/state")
    implemented = set(initial.get("implemented") or [])
    original = {
        "amp": _volume(initial, "amp"),
        "music": _volume(initial, "music"),
    }
    zeroed: list[tuple[str, str, int]] = []
    try:
        for device, command in (
            ("amp", "amp.vol.set"), ("music", "music.vol.set"),
        ):
            level = original[device]
            if level is None or command not in implemented:
                continue
            _set_volume(client, command, 0)
            _wait_volume(client, device, 0)
            zeroed.append((device, command, level))

        setup = client.request("/api/setup")
        if not setup.get("music") or not setup.get("devices"):
            raise SmokeFailure("setup manifest is missing drivers")
        discoveries = ["music", "serial", "access"]
        if include_network:
            discoveries.append("network")
        for kind in discoveries:
            result = client.request("/api/setup/discover", {"kind": kind})
            if result.get("kind") != kind:
                raise SmokeFailure(f"{kind} discovery returned the wrong kind")

        backend = client.request("/api/settings/music")
        active = str(backend.get("active_backend") or "")
        if active not in {"apple_music", "roon"}:
            raise SmokeFailure(f"unexpected active music backend: {active or 'none'}")
        search = _search(client, query)
        if not any(key in search for key in ("albums", "songs", "results")):
            raise SmokeFailure("music search returned no recognized result collections")

        ask = client.request("/api/agent", {
            "message": (
                f"Search my library and the active music service for {query}. "
                "Tell me the best matches, but do not play, queue, add, remove, "
                "or change any device."
            ),
            "session": "installer-smoke-" + uuid.uuid4().hex[:16],
            "request_id": "installer_smoke_" + uuid.uuid4().hex,
        })
        if ask.get("status") != "ok" or not str(ask.get("reply") or "").strip():
            raise SmokeFailure("Ask did not return a usable read-only answer")
        print(json.dumps({
            "status": "ok",
            "active_backend": active,
            "zeroed_outputs": [device for device, _command, _level in zeroed],
            "discoveries": discoveries,
            "ask_acted": bool(ask.get("acted")),
        }, indent=2))
    finally:
        for device, command, level in reversed(zeroed):
            try:
                _set_volume(client, command, level)
                _wait_volume(client, device, level)
            except SmokeFailure as exc:
                print(f"WARNING: could not restore {device} volume: {exc}",
                      file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get(
        "AVCTL_TEST_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token-file", type=Path,
                        default=Path("~/.avctl/token"))
    parser.add_argument("--query", default="Miles Davis")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--network", action="store_true",
                        help="also scan the local /24 for TV and iTach")
    args = parser.parse_args()
    try:
        run(CoreClient(args.url, _token(None, args.token_file), args.timeout),
            args.query, args.network)
    except SmokeFailure as exc:
        print(f"installer live acceptance failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
