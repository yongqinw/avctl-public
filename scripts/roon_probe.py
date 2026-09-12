#!/usr/bin/env python3
"""Discover, authorize and summarize a Roon Server without changing playback."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from roonapi import RoonApi, RoonDiscovery
from devices.roon.live import APP_INFO


def _read_token(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".roon-token-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _discover(core_id: str | None) -> tuple[str, int]:
    discovery = RoonDiscovery(core_id)
    try:
        found = discovery.all()
    finally:
        discovery.stop()
    unique = list(dict.fromkeys((str(host), int(port)) for host, port in found))
    if not unique:
        raise RuntimeError("no Roon Server discovered on the local network")
    if len(unique) > 1:
        choices = ", ".join(f"{host}:{port}" for host, port in unique)
        raise RuntimeError(f"multiple Roon Servers found; pass --host/--port: {choices}")
    return unique[0]


def _safe_summary(api: RoonApi) -> dict:
    zones = []
    for zone in api.zones.values():
        now = zone.get("now_playing") or {}
        lines = now.get("three_line") or {}
        zones.append({
            "id": zone.get("zone_id"),
            "name": zone.get("display_name"),
            "state": zone.get("state"),
            "outputs": [row.get("output_id") for row in zone.get("outputs", [])],
            "now_playing": lines.get("line1"),
        })
    outputs = []
    for output in api.outputs.values():
        volume = output.get("volume")
        outputs.append({
            "id": output.get("output_id"),
            "name": output.get("display_name"),
            "zone_id": output.get("zone_id"),
            "volume": ({key: volume.get(key) for key in
                        ("type", "min", "max", "step", "value", "is_muted")}
                       if isinstance(volume, dict) else {"type": "fixed"}),
            "source_controls": [
                {"key": row.get("control_key"),
                 "name": row.get("display_name"),
                 "status": row.get("status")}
                for row in output.get("source_controls", [])
            ],
        })
    return {
        "core": {"id": api.core_id, "name": api.core_name},
        "zones": zones,
        "outputs": outputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--core-id")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument(
        "--token-file", type=Path,
        default=Path("~/.avctl/roon-token").expanduser())
    args = parser.parse_args()
    if bool(args.host) != bool(args.port):
        parser.error("--host and --port must be provided together")
    host, port = ((args.host, args.port) if args.host
                  else _discover(args.core_id))
    token = _read_token(args.token_file)
    api = RoonApi(APP_INFO, token, host, port, blocking_init=False)
    try:
        if token is None:
            print("Authorize 'avctl' in Roon > Settings > Extensions.",
                  flush=True)
        deadline = time.monotonic() + max(1.0, args.timeout)
        while not api.ready and time.monotonic() < deadline:
            time.sleep(0.1)
        if not api.ready or not api.token:
            raise RuntimeError("Roon authorization timed out")
        # With non-blocking initialization, `ready` means registration is
        # complete; the initial zone/output subscription snapshots arrive on
        # the socket thread immediately afterward. Give them a bounded grace
        # window instead of printing a misleading empty Core.
        state_deadline = min(deadline, time.monotonic() + 3.0)
        while (not api.zones or not api.outputs) and time.monotonic() < state_deadline:
            time.sleep(0.05)
        _write_token(args.token_file, api.token)
        print(json.dumps(_safe_summary(api), ensure_ascii=False, indent=2))
        print(f"Token saved with mode 0600 at {args.token_file}")
        return 0
    finally:
        api.stop()


if __name__ == "__main__":
    raise SystemExit(main())
