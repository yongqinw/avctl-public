"""Locating and loading the files in configs/.

Everything that reads configuration goes through here, so the path is written
down once rather than in every script.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = REPO_ROOT / "configs"

CONFIG_FILE = CONFIGS_DIR / "defaults.yaml"
USER_CONFIG_FILE = Path(
    os.environ.get(
        "AVCTL_CONFIG_FILE",
        "~/Library/Application Support/avctl/config.yaml",
    )
).expanduser()
MAC7200_PROTOCOL_FILE = CONFIGS_DIR / "mac7200_protocol.yaml"

_MUSIC_DRIVERS = {
    "apple_music": "AppleMusic",
    "applemusic": "AppleMusic",
    "roon": "RoonMusic",
    "roonmusic": "RoonMusic",
}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def merged(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay user-owned configuration on bundled defaults."""
    result = dict(base)
    for key, value in override.items():
        previous = result.get(key)
        if isinstance(previous, dict) and isinstance(value, dict):
            result[key] = merged(previous, value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    """Bundled defaults overlaid by the installer-owned user config.

    Existing developer deployments remain compatible because the override is
    optional.  Packaged installs can ship sanitized defaults and keep every
    discovered host, selected driver, and safety cap outside the app bundle.
    """
    base = _load(CONFIG_FILE)
    try:
        override = _load(USER_CONFIG_FILE)
    except FileNotFoundError:
        return base
    if not isinstance(override, dict):
        raise ValueError(f"user config must be a mapping: {USER_CONFIG_FILE}")
    return merged(base, override)


def load_user_config() -> dict[str, Any]:
    """Read only values explicitly saved by the setup UI."""
    try:
        value = _load(USER_CONFIG_FILE)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"user config must be a mapping: {USER_CONFIG_FILE}")
    return value


def save_user_config(value: dict[str, Any]) -> Path:
    """Atomically persist a complete, validated setup override."""
    if not isinstance(value, dict):
        raise ValueError("user config must be a mapping")
    destination = USER_CONFIG_FILE
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    handle, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=".config-", suffix=".yaml")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            yaml.safe_dump(value, output, sort_keys=False, allow_unicode=True)
        os.replace(temporary, destination)
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
    return destination


def load_mac7200_protocol() -> dict[str, Any]:
    """The MAC7200 RS-232 command map."""
    return _load(MAC7200_PROTOCOL_FILE)


def driver_block(config: dict[str, Any], category: str, driver: str
                 ) -> dict[str, Any]:
    """A driver's view of its category block: generic keys, with the
    driver's own sub-block laid over them.

    The shape this enables in config.yaml:

        music:
          driver: AppleMusic
          queue_window: 20        # generic -- any source
          AppleMusic:             # this driver's own business
            team_id: ...

    Category-level keys are what ANY driver of the kind would need;
    protocol credentials and mechanism settings live under the driver's
    class name, so switching `driver:` leaves no stranded keys pretending
    to configure the new one -- the old sub-block just goes quiet. The
    overlay (rather than a bare sub-block read) keeps old flat configs
    working: a key found at either level answers.
    """
    block = config.get(category) or {}
    own = block.get(driver) or {}
    return {**block, **own}


def configured_driver(config: dict[str, Any], category: str,
                      default: str) -> str:
    """Resolve a category driver, including the persisted music selector."""
    block = config.get(category) or {}
    configured = str(block.get("driver") or default)
    if category != "music":
        return configured
    environment = os.environ.get("AVCTL_MUSIC_BACKEND", "").strip()
    if environment:
        return _MUSIC_DRIVERS.get(environment.casefold(), environment)
    state_file = block.get("backend_state_file")
    if not state_file:
        return configured
    try:
        saved = Path(str(state_file)).expanduser().read_text(
            encoding="utf-8").strip()
    except FileNotFoundError:
        return configured
    except OSError as exc:
        raise ValueError(f"could not read saved music backend: {exc}") from None
    return _MUSIC_DRIVERS.get(saved.casefold(), saved) if saved else configured


def save_music_driver(config: dict[str, Any], driver: str) -> Path:
    """Atomically persist the music driver selected in Settings."""
    if os.environ.get("AVCTL_MUSIC_BACKEND", "").strip():
        raise ValueError("music backend is managed by AVCTL_MUSIC_BACKEND")
    normalized = _MUSIC_DRIVERS.get(str(driver).casefold(), str(driver))
    if normalized not in {"AppleMusic", "RoonMusic"}:
        raise ValueError("music backend must be AppleMusic or RoonMusic")
    block = config.get("music") or {}
    path = Path(str(block.get("backend_state_file")
                    or "~/.avctl/music-backend")).expanduser()
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary.write_text(normalized + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as exc:
        raise ValueError(f"could not save music backend: {exc}") from None
    return path


def unresolved(config: dict[str, Any] | None = None) -> list[str]:
    """Dotted paths of every value still left as TODO.

    Most of this project is blocked on facts that can only be read off the
    hardware, so it's worth being able to ask what is still unknown rather than
    discovering it when a scene half-fires.
    """
    config = load_config() if config is None else config
    found: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{trail}.{key}" if trail else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")
        elif node == "TODO":
            found.append(trail)

    walk(config, "")
    return found


def pad_mac(mac: str) -> str:
    """Zero-pad each octet: 2:0:0:0:0:1 -> 02:00:00:00:00:01.

    Lives here because both the TV and the player need Wake-on-LAN, and both
    read their MAC from `arp -an`, which prints octets unpadded -- the raw
    string builds a magic packet the device silently ignores.
    """
    parts = mac.split(":")
    if len(parts) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return ":".join(p.rjust(2, "0").lower() for p in parts)
