#!/usr/bin/env python3
"""Read-only smoke test for avctl's configured live Roon music provider.

The token is consumed internally by ``PyRoonController`` and is never printed.
This script performs no transport, queue, volume, library, or rack mutation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devices import config as device_config
from devices.music.roon import RoonMusic
from devices.roon.live import PyRoonController


def _sample(rows: list[Any], limit: int = 3) -> list[dict[str, str]]:
    answer = []
    for row in rows[:limit]:
        if isinstance(row, dict):
            answer.append({
                "kind": str(row.get("kind") or ""),
                "name": str(row.get("name") or row.get("album") or ""),
                "artist": str(row.get("artist") or ""),
            })
        else:
            answer.append({
                "kind": str(getattr(row, "kind", "")),
                "name": str(getattr(row, "title", "")),
                "artist": str(getattr(row, "artist", "")),
            })
    return answer


def _phase(name: str) -> None:
    print(f"[roon-smoke {time.strftime('%H:%M:%S')}] {name}",
          file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Miles Davis")
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()
    config = device_config.load_config()
    controller = PyRoonController.from_config(config)
    roon = config.get("roon") or {}
    target = str(roon.get("zone_id") or roon.get("output_id") or "")
    if not target:
        raise RuntimeError("roon.zone_id or roon.output_id is required")
    music = RoonMusic(
        controller, target, str(roon.get("output_id") or ""))
    try:
        _phase("now playing")
        state = music.now_playing()
        _phase("library")
        library = music.recently_added_songs()
        library_albums = music.recently_added()
        _phase("playlists")
        playlists = music.playlists()
        _phase("service search")
        service = music.search_service(
            args.query, ["songs", "albums", "playlists"], args.limit)
        albums = [row for row in service if row.get("kind") == "album"]
        _phase("service album")
        album_tracks = (music.service_tracks("album", albums[0]["id"])
                        if albums else [])
        _phase("explore")
        explore = music.explore_service(limit=min(10, max(1, args.limit)))
        summary = {
            "service": music.service_info(),
            "state": {
                key: state.get(key) for key in (
                    "state", "zone_name", "track", "artist", "queued",
                    "volume", "muted")
            },
            "library_tracks": len(library),
            "library_albums": _sample([{
                "kind": "album",
                "name": row.get("album"),
                "artist": row.get("artist"),
            } for row in library_albums]),
            "playlists": len(playlists),
            "search": _sample(service),
            "expanded_album_tracks": len(album_tracks),
            "explore_sections": [
                {"title": str(section.get("title") or ""),
                 "items": len(section.get("items") or [])}
                for section in explore.get("sections") or []
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        _phase("complete")
        return 0
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
