#!/usr/bin/env python3
"""Report or deterministically repair avctl's Qobuz virtual library."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devices.config import driver_block, load_config
from devices.music.roon import RoonMusic
from devices.music.virtual_library import VirtualMusicLibrary
from devices.roon.factory import reset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repair", action="store_true",
        help="connect to Roon and repair only items currently marked failed")
    parser.add_argument(
        "--all", action="store_true",
        help="with --repair, re-resolve every saved item")
    parser.add_argument(
        "--backfill-albums", action="store_true",
        help="resolve and persist albums missing from older loose-track saves")
    args = parser.parse_args()
    config = load_config()
    block = driver_block(config, "music", "RoonMusic")
    library = VirtualMusicLibrary(
        block.get("library_file") or "~/.avctl/roon_library.sqlite3")
    if not args.repair and not args.backfill_albums:
        print(json.dumps({"failures": library.repair_report()}, indent=2,
                         ensure_ascii=False))
        return 0
    try:
        music = RoonMusic.from_config(config)
        result = (music.backfill_virtual_library_albums()
                  if args.backfill_albums
                  else music.repair_virtual_library(all_items=args.all))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result["failed"] else 0
    finally:
        reset()


if __name__ == "__main__":
    raise SystemExit(main())
