"""Runtime settings, every one overridable by environment variable.

The defaults are the safe ones -- loopback bind, and a file root confined to
this repo. Widening either should be a deliberate act, which is why they are
environment variables rather than arguments buried at some call site.
"""

from __future__ import annotations

import os
from pathlib import Path

from devices import config as device_config

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    _CONFIG = device_config.load_config()
except FileNotFoundError:
    _CONFIG = {}
_SERVER = _CONFIG.get("server") or {}

# Loopback only. `tailscale serve` terminates TLS and proxies to us here, so
# the app never occupies a network interface -- see docs/remote-access.md.
BIND = os.environ.get("AVCTL_BIND", "127.0.0.1")
PORT = int(os.environ.get("AVCTL_PORT") or _SERVER.get("port") or 8000)
ADVERTISE = os.environ.get("AVCTL_ADVERTISE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
PUBLIC_URL = os.environ.get("AVCTL_PUBLIC_URL", "").strip()
CORE_ID_FILE = Path(
    os.environ.get(
        "AVCTL_CORE_ID_FILE",
        "~/Library/Application Support/avctl/core-id",
    )
).expanduser()

# Everything the file browser is allowed to see. Resolved once, here, so the
# containment check in files.py has a single fixed anchor to compare against.
ROOT = Path(os.environ.get("AVCTL_ROOT", REPO_ROOT)).expanduser().resolve()

# Tailnet logins permitted, lowercased. Empty means "any tailnet member",
# which is the right default for a three-device tailnet: the network has
# already decided who is allowed in.
ALLOWED_USERS = [
    user.strip().lower()
    for user in os.environ.get("AVCTL_ALLOWED_USERS", "").split(",")
    if user.strip()
]

# Shared secret for the window before Tailscale exists, and for curl. Left
# unset here on purpose: auth.py generates and persists one instead, so there
# is never a configuration in which the app runs with no authentication.
TOKEN = os.environ.get("AVCTL_TOKEN") or None

TOKEN_FILE = Path(
    os.environ.get("AVCTL_TOKEN_FILE", "~/.avctl/token")
).expanduser()

INSTALL_KIND = os.environ.get("AVCTL_INSTALL_KIND", "source").strip() or "source"

# Sync cadence -- the poller beat and the SSE heartbeat. The values live in
# config.yaml (sync:) with the rest of the rack's facts; the env vars stay
# as overrides so tests can spin the loops fast without editing the file.
_SYNC = _CONFIG.get("sync") or {}

# How often the background poller re-reads the rack. 5s keeps a physical
# remote's changes on the phone within a breath; anything a button on the
# phone changes shows up faster, because commands poke the poller directly.
POLL_INTERVAL = float(
    os.environ.get("AVCTL_POLL_INTERVAL") or _SYNC.get("poll_interval") or 5)

# Seconds between heartbeats on a quiet SSE stream. Short enough that a
# phone whose network silently died notices within half a minute.
SSE_HEARTBEAT = float(
    os.environ.get("AVCTL_SSE_HEARTBEAT") or _SYNC.get("sse_heartbeat") or 15)

# Where the deployer publishes the phone app's OTA artifacts (signed .ipa +
# manifest.plist). Outside every repo and every release dir on purpose: a
# server rollback must not take the app download with it, and vice versa.
APP_DIST = Path(
    os.environ.get("AVCTL_APP_DIST", "~/avctl-app-dist")
).expanduser()
