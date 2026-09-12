#!/usr/bin/env python
"""Snapshot the TV's current picture settings before anything alters them.

An owner's calibration is not recoverable once a scene or a menu walk has
changed it, and some older sets cannot be asked "what were you set to last
week". Read every picture key the firmware will answer and write them to the
private Application Support directory. Run BEFORE implementing anything that
touches picture modes, and re-run deliberately after intentional recalibration.

The file is the restore target: whatever later mechanism changes picture
state (scripted menu presses, most likely -- the settings service refuses
writes on webOS 3.0) must put these values back.

    ./venv/bin/python scripts/tv_picture_baseline.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from devices.config import load_config  # noqa: E402
from devices.tv import LGTelevision  # noqa: E402

BASELINE_FILE = Path(
    "~/Library/Application Support/avctl/tv-picture-baseline.yaml"
).expanduser()

# Everything worth trying, across webOS naming generations. Keys the firmware
# does not recognise are silently absent from the result, so casting a wide
# net costs nothing.
CANDIDATE_KEYS = [
    "pictureMode", "energySaving", "backlight", "brightness", "contrast",
    "color", "sharpness", "tint", "colorTemperature", "gamma",
    "dynamicContrast", "dynamicColor", "superResolution", "noiseReduction",
    "mpegNoiseReduction", "blackLevel", "motionEyeCare", "realCinema",
    "truMotionMode", "truMotionJudder", "truMotionBlur", "colorGamut",
    "peakBrightness", "hdrDynamicToneMapping", "oledLight",
    "colorFilter", "whiteBalanceColorTemperature", "edgeEnhancer",
    "eyeComfortMode", "aspectRatio", "justScan",
]


async def main() -> int:
    config = load_config()
    subnet = (config.get("network") or {}).get("av_subnet")
    broadcast = (str(ipaddress.ip_network(str(subnet), strict=False)
                     .broadcast_address) if subnet else "255.255.255.255")
    tv = LGTelevision.from_config(config, broadcast=broadcast)
    async with tv:
        if not await tv.is_on():
            print("! TV is in standby -- picture settings read as defaults, "
                  "not the calibration. Turn it on and re-run.")
            return 1
        found = await tv.read_picture_settings(CANDIDATE_KEYS)

    if not found:
        print("! nothing readable -- refusing to write an empty baseline")
        return 1

    if BASELINE_FILE.exists():
        previous = yaml.safe_load(BASELINE_FILE.read_text()) or {}
        old = previous.get("settings", {})
        changed = {k: (old.get(k), v) for k, v in found.items() if old.get(k) != v}
        print(f"baseline exists ({previous.get('captured', 'unknown date')}); "
              f"{len(changed)} value(s) differ" if changed else
              "baseline exists; nothing has drifted")
        for key, (was, now) in sorted(changed.items()):
            print(f"  {key}: {was!r} -> {now!r}")

    payload = {
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "The owner's picture calibration, read from the set itself. "
            "This is the restore target for anything that changes picture "
            "state. Do not edit by hand; re-run "
            "scripts/tv_picture_baseline.py after deliberate recalibration."
        ),
        "settings": found,
    }
    BASELINE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    BASELINE_FILE.write_text(yaml.safe_dump(payload, sort_keys=True))
    BASELINE_FILE.chmod(0o600)
    print(f"wrote {len(found)} settings to {BASELINE_FILE}")
    for key in sorted(found):
        print(f"  {key:<28} {found[key]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
