#!/usr/bin/env python
"""Pair with the LG TV over SSAP and save the client key securely.

The TV puts an accept prompt on screen the first time an unknown client
connects. Someone has to be in front of it with the remote to press Accept --
this cannot be done headlessly. Once accepted the TV issues a client key,
which is written to ~/.avctl/tv-client-key and reused from then on.

    ./venv/bin/python scripts/pair_tv.py

Re-running when already paired just verifies the stored key still works.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from devices.config import load_config  # noqa: E402
from devices.tv import PairingRequired  # noqa: E402
from devices.tv.lg_webos import LGTelevision  # noqa: E402

# The TV can sit on the prompt for a while before anyone reaches the remote.
CONNECT_TIMEOUT = 60


async def main() -> int:
    config = load_config()
    try:
        tv = LGTelevision.from_config(config)
    except ValueError as exc:
        print(f"! {exc}")
        return 1

    host = tv.host
    already_paired = bool(tv.client_key)
    if already_paired:
        print(f"Verifying stored client key against {host} ...")
    else:
        print(f"Connecting to {host} ...")
        print()
        print("  >>> The TV will show an on-screen prompt. Press Accept on the")
        print("  >>> remote. Waiting up to 60s.")
        print()

    try:
        await asyncio.wait_for(tv.connect(), timeout=CONNECT_TIMEOUT)
    except PairingRequired:
        print("! pairing was not accepted before the prompt expired")
        print("! leave the TV fully on, run this again, and press Accept")
        return 1
    except asyncio.TimeoutError:
        print(f"! timed out connecting to {host}")
        print("! the TV must be fully on -- SSAP does not answer from standby")
        return 1
    except OSError as exc:
        print(f"! could not reach {host}: {exc}")
        return 1

    client = tv.client
    try:
        key = client.client_key
        if not key:
            print("! connected but the TV issued no client key")
            return 1

        if already_paired:
            print("OK -- connected (the key was repaired if necessary).")
        else:
            print("OK -- paired.")
        print(f"   credential saved to {tv.client_key_file}")
        print("   value intentionally not printed; file mode is owner-only")

        info = client.tv_info
        for label, value in (
            ("model", getattr(info, "model_name", None)),
            ("system", getattr(info, "system_name", None)),
            ("webOS", getattr(info, "product_name", None)),
        ):
            if value:
                print(f"   {label}: {value}")

        # Fills in tv.inputs.* in config.yaml, which are still TODO. The scene
        # definitions need the exact input ids, not the friendly labels.
        print()
        print("Inputs reported by the TV:")
        try:
            inputs = await client.get_inputs()
        except Exception as exc:  # noqa: BLE001 - report, do not abort pairing
            print(f"   ! could not read inputs: {exc}")
        else:
            for item in inputs:
                input_id = item.get("id", "?")
                label = item.get("label") or item.get("title") or ""
                connected = "connected" if item.get("connected") else "-"
                print(f"   {input_id:<12} {label:<20} {connected}")

        return 0
    finally:
        with contextlib.suppress(Exception):
            await tv.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
