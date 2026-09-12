"""Setup manifests, read-only discovery, and user-owned configuration.

Discovery never sends a control command.  The wizard writes only a small,
validated override in Application Support; protocol tables and bundled
defaults stay versioned with the application.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import platform
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from devices import config as device_config
from devices.apple_broker import BrokerError, ManagedAppleBroker
from . import settings

SCHEMA_VERSION = 1
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
_MAC = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_ROON_AUTH_LOCK = threading.RLock()
_ROON_AUTH_API: Any = None
_ROON_AUTH_STARTED = 0.0
_ROON_AUTH_RESULT: dict[str, Any] | None = None


class SetupError(ValueError):
    """A draft cannot safely be activated."""


def _roon_token_file() -> Path:
    block = device_config.load_config().get("roon") or {}
    return Path(str(block.get("token_file") or "~/.avctl/roon-token")).expanduser()


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    handle, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=".roon-token-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(value + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
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


def _stop_roon_authorization() -> None:
    global _ROON_AUTH_API
    api, _ROON_AUTH_API = _ROON_AUTH_API, None
    if api is not None:
        try:
            api.stop()
        except BaseException:
            pass


def start_roon_authorization(
    payload: dict[str, Any], *, api_factory: Any = None,
) -> dict[str, Any]:
    """Start Roon extension authorization without exposing its token."""
    global _ROON_AUTH_API, _ROON_AUTH_STARTED, _ROON_AUTH_RESULT
    if not isinstance(payload, dict):
        raise SetupError("Roon authorization payload must be an object")
    host = _host(payload.get("host"), "Roon host")
    port = _port(payload.get("port"), "Roon port")
    if api_factory is None:
        from roonapi import RoonApi
        api_factory = RoonApi
    from devices.roon.live import APP_INFO

    with _ROON_AUTH_LOCK:
        _stop_roon_authorization()
        _ROON_AUTH_RESULT = None
        try:
            _ROON_AUTH_API = api_factory(
                APP_INFO, None, host, port, blocking_init=False)
        except BaseException as exc:
            raise SetupError(f"could not contact Roon: {exc}") from None
        _ROON_AUTH_STARTED = time.monotonic()
    return {"state": "waiting", "authorized": False,
            "detail": "Authorize avctl in Roon → Settings → Extensions."}


def roon_authorization_status(*, token_file: Path | None = None
                              ) -> dict[str, Any]:
    """Poll authorization and project friendly zones/outputs for the UI."""
    global _ROON_AUTH_RESULT
    with _ROON_AUTH_LOCK:
        if _ROON_AUTH_RESULT is not None:
            return dict(_ROON_AUTH_RESULT)
        api = _ROON_AUTH_API
        if api is None:
            return {"state": "idle", "authorized": False,
                    "detail": "Start Roon authorization first."}
        if time.monotonic() - _ROON_AUTH_STARTED > 120:
            _stop_roon_authorization()
            return {"state": "expired", "authorized": False,
                    "detail": "Roon authorization expired. Try again."}
        if not bool(getattr(api, "ready", False)) or not getattr(api, "token", None):
            return {"state": "waiting", "authorized": False,
                    "detail": "Authorize avctl in Roon → Settings → Extensions."}
        try:
            zones = [{
                "id": str(row.get("zone_id") or key),
                "name": str(row.get("display_name") or key),
                "outputs": [str(item.get("output_id") or "")
                            for item in row.get("outputs", [])
                            if item.get("output_id")],
            } for key, row in dict(api.zones).items()]
            outputs = [{
                "id": str(row.get("output_id") or key),
                "name": str(row.get("display_name") or key),
                "zone_id": str(row.get("zone_id") or ""),
                "volume": isinstance(row.get("volume"), dict),
                "source_control": bool(row.get("source_controls")),
            } for key, row in dict(api.outputs).items()]
        except (RuntimeError, TypeError, ValueError):
            return {"state": "waiting", "authorized": False,
                    "detail": "Authorized; waiting for Roon zones and outputs."}
        if not zones and not outputs:
            return {"state": "waiting", "authorized": False,
                    "detail": "Authorized; waiting for Roon zones and outputs."}
        _write_private(token_file or _roon_token_file(), str(api.token))
        result = {
            "state": "authorized", "authorized": True,
            "core_id": str(getattr(api, "core_id", "") or ""),
            "core_name": str(getattr(api, "core_name", "") or "Roon"),
            "zones": zones, "outputs": outputs,
            "detail": "Roon is authorized. Choose a playback zone below.",
        }
        _ROON_AUTH_RESULT = result
        _stop_roon_authorization()
        return dict(result)


def cancel_roon_authorization() -> dict[str, Any]:
    global _ROON_AUTH_RESULT
    with _ROON_AUTH_LOCK:
        _stop_roon_authorization()
        _ROON_AUTH_RESULT = None
    return {"state": "idle", "authorized": False}


def _current_setup() -> dict[str, Any] | None:
    """Return only installer-owned, non-secret fields for editing a setup."""
    raw = device_config.load_user_config()
    if not raw:
        return None
    music = raw.get("music") or {}
    amp = raw.get("amp") or {}
    dac = raw.get("dac") or {}
    tv = raw.get("tv") or {}
    roon = raw.get("roon") or {}
    blaster = raw.get("blaster") or {}
    scene = ((raw.get("ui") or {}).get("scenes") or [{}])[0]
    apple = raw.get("apple_services") or {}
    voice = raw.get("voice") or {}
    server = raw.get("server") or {}
    apple_info = ManagedAppleBroker.enrollment_info(device_config.load_config())

    def configured_level(block: dict[str, Any], key: str, default: int) -> int:
        """Keep an explicit zero; ``or default`` turns the safe value off."""
        value = block.get(key)
        return default if value is None else int(value)

    amp_presets = amp.get("presets") or {}
    return {
        "server": {"port": int(server.get("port") or settings.PORT)},
        "music": ("roon" if music.get("driver") == "RoonMusic"
                  else "apple_music"),
        "panels": list((raw.get("ui") or {}).get("panels") or ["home"]),
        "roon": {key: roon[key] for key in
                 ("host", "port", "core_id", "zone_id", "output_id")
                 if roon.get(key) is not None},
        "tv": {
            "host": str(tv.get("host") or ""),
            "mac": str(tv.get("mac") or ""),
            "mac_input": str((tv.get("inputs") or {}).get("macmini")
                             or "HDMI_2"),
        },
        "amp": {
            "mode": "roon" if amp.get("driver") == "RoonAmp" else "serial",
            "port": str(amp.get("port") or ""),
            "baud": int(amp.get("baud") or 115200),
        },
        "dac": {
            "mode": "roon" if dac.get("driver") == "RoonDac" else "itach",
            "host": str(blaster.get("host") or ""),
            "ir_port": int((blaster.get("ports") or {}).get("dac") or 1),
        },
        "max_volume": {
            "amp": configured_level(amp, "max_volume", 70),
            "music": configured_level(music, "max_volume", 80),
        },
        "scene_volume": {
            "amp": configured_level(amp_presets, "music", 60),
            "music": configured_level(music, "scene_volume", 56),
        },
        "scene_steps": [str(value) for value in scene.get("steps", [])],
        "apple_services": {
            "mode": str(apple.get("mode") or "local"),
            **apple_info,
        },
        "voice": {"enabled": bool(voice.get("enabled", True))},
    }


def manifest() -> dict[str, Any]:
    """Public driver catalog consumed by the themed setup wizard."""
    from . import voicelink

    return {
        "schema_version": SCHEMA_VERSION,
        "configured": bool(device_config.load_user_config()),
        "current": _current_setup(),
        "apple_broker": ManagedAppleBroker.enrollment_info(
            device_config.load_config()),
        "installation": installation_capabilities(),
        "voice": voicelink.status(),
        "steps": ["check", "music", "panels", "devices", "access", "review"],
        "apple_services": [
            {"id": "local", "label": "Keys on this Core",
             "detail": "Personal owner setup; Apple keys stay on this Mac"},
            {"id": "managed", "label": "Publisher broker",
             "detail": "Use an invite; publisher keys never leave the broker"},
            {"id": "disabled", "label": "Not now",
             "detail": "Music library control still works; managed catalog and pushes stay off"},
        ],
        "music": [
            {"id": "apple_music", "label": "Apple Music",
             "detail": "Music.app library plus the Apple Music catalog",
             "discovery": "local"},
            {"id": "roon", "label": "Roon + Qobuz",
             "detail": "Roon Server, one zone, and its streaming services",
             "discovery": "roon"},
        ],
        "panels": [
            {"id": "home", "label": "Home", "required": True},
            {"id": "music", "label": "Music", "required": False},
            {"id": "agent", "label": "Ask", "required": False},
            {"id": "mini", "label": "Mac mini", "required": False},
            {"id": "tv", "label": "TV", "required": False},
            {"id": "amp", "label": "DAC / Amp", "required": False},
        ],
        "devices": {
            "tv": [
                {"id": "lg_webos", "label": "LG webOS TV",
                 "driver": "LGTelevision", "discovery": "network"},
            ],
            "amp": [
                {"id": "mcintosh_serial", "label": "Serial amplifier",
                 "driver": "McIntoshMAC7200", "discovery": "serial"},
                {"id": "roon", "label": "Roon volume",
                 "driver": "RoonAmp", "discovery": "roon"},
            ],
            "dac": [
                {"id": "itach", "label": "IR DAC through iTach",
                 "driver": "ToppingD900", "discovery": "network"},
                {"id": "roon", "label": "Roon output",
                 "driver": "RoonDac", "discovery": "roon"},
            ],
        },
    }


def installation_capabilities() -> dict[str, Any]:
    """Non-secret first-run facts: what this installation can actually use."""
    system = platform.system()
    architecture = platform.machine()
    mac_version = platform.mac_ver()[0] if system == "Darwin" else ""
    try:
        major = int(mac_version.split(".", 1)[0]) if mac_version else 0
    except ValueError:
        major = 0
    packaged = settings.INSTALL_KIND == "package"
    helper_path = Path(os.environ.get(
        "AVCTL_INPUT_HELPER",
        "/Applications/Avctl Server.app/Contents/Resources/bin/avctl-input-helper",
    )).expanduser()
    phone_ready = all((settings.APP_DIST / name).is_file()
                      for name in ("manifest.plist", "avctl.ipa"))
    music_ready = any(path.exists() for path in (
        Path("/System/Applications/Music.app"), Path("/Applications/Music.app")))
    config = device_config.load_config()
    bridge = Path(str(device_config.driver_block(
        config, "music", "AppleMusic").get("catalog_player") or
        "~/.avctl/bin/AvctlMusicBridge.app/Contents/MacOS/AvctlMusicBridge"
    )).expanduser()
    return {
        "install_kind": settings.INSTALL_KIND,
        "port": settings.PORT,
        "system": system,
        "architecture": architecture,
        "os_version": mac_version,
        "supported_host": system == "Darwin" and architecture == "arm64"
            and major >= 14,
        "components": [
            {"id": "core", "label": "avctl Core", "available": True,
             "detail": "Packaged runtime" if packaged else "Source checkout"},
            {"id": "apple_music", "label": "Apple Music",
             "available": music_ready,
             "detail": "Music.app detected" if music_ready
                       else "Music.app is not available on this Mac"},
            {"id": "apple_music_catalog", "label": "Apple Music catalog playback",
             "available": bridge.is_file() and os.access(bridge, os.X_OK),
             "detail": ("Signed MusicKit bridge installed"
                        if bridge.is_file() else
                        "Signed MusicKit bridge is not installed")},
            {"id": "roon", "label": "Roon",
             "available": importlib.util.find_spec("roonapi") is not None,
             "detail": "Driver installed; Core discovery runs in Music setup"},
            {"id": "mini", "label": "Mac remote input",
             "available": helper_path.is_file() and os.access(helper_path, os.X_OK),
             "detail": ("Input helper installed; Accessibility is verified on test"
                        if helper_path.is_file() else "Input helper is not installed")},
            {"id": "voice", "label": "Local voice transcription",
             "available": importlib.util.find_spec("mlx_whisper") is not None,
             "detail": ("Bundled MLX runtime; model is prepared in Ask setup"
                        if importlib.util.find_spec("mlx_whisper") is not None
                        else "Requires Apple silicon and the bundled MLX runtime")},
            {"id": "phone_app", "label": "iPhone / iPad app",
             "available": phone_ready,
             "detail": ("Signed app build is published on this Core"
                        if phone_ready else
                        "Publisher must register the device UDID and publish a signed build")},
        ],
    }


def configure_input_helper(enabled: bool) -> dict[str, Any]:
    """Enable the optional packaged helper only after the owner selects it."""
    if settings.INSTALL_KIND != "package":
        raise SetupError(
            "source installations manage the input helper through their deployer")
    try:
        from installer import entrypoint
        install_home = os.environ.get("AVCTL_INSTALL_HOME") or None
        result = entrypoint.configure_input_helper(enabled, home=install_home)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SetupError(f"could not configure Mac remote input: {exc}") from None
    return {**result, "detail": (
        "Input helper is running; allow it in Privacy & Security → Accessibility"
        if enabled else "Mac remote input is disabled")}


def authorize_apple_music() -> dict[str, Any]:
    """Run both non-mutating macOS consent checks from the Core session."""
    from devices.music.catalog_player import CatalogPlayer

    config = device_config.load_config()
    block = device_config.driver_block(config, "music", "AppleMusic")
    player = CatalogPlayer(block.get("catalog_player"))
    if not player.available():
        raise SetupError("the signed Apple Music playback bridge is not installed")
    try:
        player.ensure_authorized()
    except Exception as exc:
        raise SetupError(str(exc)) from None
    finally:
        player.close()
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'tell application "Music" to get player state as text'],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"could not verify Music.app access: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or
                  "Music.app access was not granted").strip()
        raise SetupError(detail[:500])
    return {"authorized": True,
            "detail": "Music.app and Apple Music catalog permissions are ready."}


def apple_services(payload: dict[str, Any]) -> dict[str, Any]:
    """Enroll or verify a Core without returning its private signing key."""
    if not isinstance(payload, dict):
        raise SetupError("Apple services payload must be an object")
    action = _text(payload.get("action") or "status", "action", required=True)
    config = device_config.load_config()
    try:
        if action == "enroll":
            return ManagedAppleBroker.enroll(
                config,
                _text(payload.get("url"), "broker URL", required=True),
                _text(payload.get("invite"), "broker invite", required=True),
                _text(payload.get("label") or socket.gethostname(), "Core label"),
            )
        if action == "status":
            client = ManagedAppleBroker.from_state(config)
            result = client.status()
            return {"enrolled": True, "url": client.url,
                    "installation_id": client.installation_id,
                    "capabilities": result.get("capabilities") or {}}
    except BrokerError as exc:
        if action == "status":
            enrollment = ManagedAppleBroker.enrollment_info(config)
            if not enrollment["enrolled"]:
                return {**enrollment, "capabilities": {}}
        raise SetupError(str(exc)) from None
    raise SetupError("Apple services action must be enroll or status")


def _local_prefix() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # connect() selects an interface but sends no packet for UDP.
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    pieces = address.split(".")
    return ".".join(pieces[:3]) if len(pieces) == 4 else None


def _open_ports(host: str, ports: tuple[int, ...]) -> list[int]:
    found: list[int] = []
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=0.16):
                found.append(port)
        except OSError:
            pass
    return found


def discover_network(prefix: str | None = None) -> list[dict[str, Any]]:
    """Find supported listeners on the local /24 without mutating them."""
    prefix = (prefix or _local_prefix() or "").strip().rstrip(".")
    if not re.fullmatch(r"(?:\d{1,3}\.){2}\d{1,3}", prefix):
        raise SetupError("network prefix must look like 192.168.1")
    if any(int(part) > 255 for part in prefix.split(".")):
        raise SetupError("network prefix contains an invalid octet")
    ports = (3000, 3001, 4998)
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        futures = {
            pool.submit(_open_ports, f"{prefix}.{number}", ports):
                f"{prefix}.{number}"
            for number in range(1, 255)
        }
        for future in concurrent.futures.as_completed(futures):
            opened = future.result()
            if not opened:
                continue
            kinds = []
            if 3000 in opened or 3001 in opened:
                kinds.append("lg_webos")
            if 4998 in opened:
                kinds.append("itach")
            rows.append({"host": futures[future], "ports": opened,
                         "kinds": kinds})
    return sorted(rows, key=lambda row: tuple(
        int(part) for part in row["host"].split(".")))


def discover_serial() -> list[dict[str, Any]]:
    """Enumerate serial devices with USB metadata where pyserial supplies it."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [
        {"path": port.device, "name": port.description or port.device,
         "manufacturer": port.manufacturer, "serial": port.serial_number,
         "vid": port.vid, "pid": port.pid}
        for port in sorted(list_ports.comports(), key=lambda row: row.device)
    ]


def discover_music() -> list[dict[str, Any]]:
    rows = [{
        "id": "apple_music", "label": "Apple Music",
        "available": Path("/System/Applications/Music.app").exists()
            or Path("/Applications/Music.app").exists(),
        "cores": [],
    }]
    cores: list[dict[str, Any]] = []
    error: str | None = None
    try:
        from roonapi import RoonDiscovery
        discovery = RoonDiscovery(None)
        try:
            found = discovery.all()
        finally:
            discovery.stop()
        cores = [{"host": str(host), "port": int(port)}
                 for host, port in dict.fromkeys(found)]
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        error = str(exc)
    rows.append({"id": "roon", "label": "Roon + Qobuz",
                 "available": bool(cores), "cores": cores,
                 "error": error})
    return rows


def tailscale_status() -> dict[str, Any]:
    executable = _tailscale_executable()
    if not executable:
        return {"installed": False, "online": False, "url": None,
                "serve": False, "detail": "Install Tailscale on the Core Mac."}
    try:
        result = subprocess.run(
            [executable, "status", "--json"], capture_output=True, text=True,
            timeout=5, check=False)
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"installed": True, "online": False, "url": None,
                "serve": False, "detail": str(exc)}
    own = payload.get("Self") or {}
    dns_name = str(own.get("DNSName") or "").rstrip(".")
    online = bool(own.get("Online", payload.get("BackendState") == "Running"))
    serve_port: int | None = None
    try:
        serve = subprocess.run(
            [executable, "serve", "status", "--json"], capture_output=True,
            text=True, timeout=5, check=False)
        serve_payload = (json.loads(serve.stdout or "{}")
                         if serve.returncode == 0 else {})
        for authority, site in (serve_payload.get("Web") or {}).items():
            if not str(authority).endswith(":443"):
                continue
            if dns_name and authority != f"{dns_name}:443":
                continue
            proxy = str((((site or {}).get("Handlers") or {}).get("/") or {})
                        .get("Proxy") or "")
            match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d+)/?", proxy)
            if match:
                serve_port = int(match.group(1))
                break
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    served = serve_port is not None
    return {
        "installed": True, "online": online,
        "url": f"https://{dns_name}" if dns_name else None,
        "serve": served, "serve_port": serve_port,
        "detail": ("Ready for private app access" if online and served else
                   "Join the tailnet and enable Tailscale Serve for avctl."),
    }


def _tailscale_executable() -> str | None:
    candidates = [shutil.which("tailscale"),
                  "/opt/homebrew/bin/tailscale",
                  "/usr/local/bin/tailscale",
                  "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
    return next((value for value in candidates
                 if value and Path(value).exists()), None)


def enable_tailscale_serve(port: Any = None) -> dict[str, Any]:
    """Publish loopback Core only after the owner presses Enable in Setup."""
    listen_port = _listen_port(settings.PORT if port is None else port)
    executable = _tailscale_executable()
    if not executable:
        raise SetupError("Install Tailscale on this Mac first")
    status = tailscale_status()
    if not status["online"]:
        raise SetupError("Sign in to Tailscale on this Mac first")
    try:
        result = subprocess.run(
            [executable, "serve", "--bg", "--https=443", str(listen_port)],
            capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"could not enable Tailscale Serve: {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "tailscale serve failed").strip()
        raise SetupError(detail[:500])
    return tailscale_status()


def restart_packaged_core() -> None:
    """Exit after the HTTP response; launchd KeepAlive starts the new graph."""
    if settings.INSTALL_KIND != "package":
        raise SetupError("automatic restart is available in packaged installs")
    configured = device_config.load_config()
    port = _listen_port((configured.get("server") or {}).get("port", settings.PORT))
    access = tailscale_status()
    if access.get("serve") and access.get("serve_port") != port:
        # BackgroundTasks runs after the response is sent. Retargeting here
        # keeps the current setup request alive, then launchd brings Core up on
        # the new port before the browser retries the stable tailnet URL.
        enable_tailscale_serve(port)
    time.sleep(0.5)
    os.kill(os.getpid(), signal.SIGTERM)


def discovery(kind: str, prefix: str | None = None) -> dict[str, Any]:
    if kind == "music":
        return {"kind": kind, "candidates": discover_music()}
    if kind == "serial":
        return {"kind": kind, "candidates": discover_serial()}
    if kind == "network":
        return {"kind": kind, "candidates": discover_network(prefix)}
    if kind == "access":
        return {"kind": kind, "status": tailscale_status()}
    raise SetupError(
        "discovery kind must be music, serial, network, access, or mini")


def _text(value: Any, name: str, *, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise SetupError(f"{name} is required")
    if len(result) > 512:
        raise SetupError(f"{name} is too long")
    return result


def _host(value: Any, name: str) -> str:
    result = _text(value, name, required=True)
    if not _HOST.fullmatch(result):
        raise SetupError(f"{name} is not a valid host")
    return result


def _port(value: Any, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise SetupError(f"{name} must be a port number") from None
    if not 1 <= result <= 65535:
        raise SetupError(f"{name} must be in 1-65535")
    return result


def _listen_port(value: Any) -> int:
    result = _port(value, "Core listening port")
    if result < 1024:
        raise SetupError("Core listening port must be in 1024-65535")
    return result


def _ensure_listen_port_available(port: int) -> None:
    if port == settings.PORT:
        return
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise SetupError(f"Core listening port {port} is unavailable: {exc}") from None
    finally:
        probe.close()


def _level(value: Any, name: str, ceiling: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise SetupError(f"{name} must be a number") from None
    if not 0 <= result <= ceiling:
        raise SetupError(f"{name} must be in 0-{ceiling}")
    return result


def compile_override(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn the UI's closed setup vocabulary into driver configuration."""
    if not isinstance(payload, dict):
        raise SetupError("setup payload must be an object")
    server_payload = payload.get("server") or {}
    if not isinstance(server_payload, dict):
        raise SetupError("server must be an object")
    listen_port = _listen_port(server_payload.get("port", settings.PORT))
    music = _text(payload.get("music"), "music", required=True)
    if music not in {"apple_music", "roon"}:
        raise SetupError("music must be apple_music or roon")
    raw_panels = payload.get("panels") or ["home"]
    if not isinstance(raw_panels, list):
        raise SetupError("panels must be a list")
    allowed_panels = {"home", "music", "agent", "mini", "tv", "amp"}
    panels = list(dict.fromkeys(_text(value, "panel", required=True)
                                for value in raw_panels))
    if "home" not in panels or any(value not in allowed_panels
                                    for value in panels):
        raise SetupError(
            "panels must include home and contain known panel ids")

    max_volume = payload.get("max_volume") or {}
    if not isinstance(max_volume, dict):
        raise SetupError("max_volume must be an object")
    amp_max = _level(max_volume.get("amp", 70), "amp max volume", 70)
    music_max = _level(max_volume.get("music", 80), "music max volume", 80)
    scene_volume = payload.get("scene_volume") or {}
    if not isinstance(scene_volume, dict):
        raise SetupError("scene_volume must be an object")
    amp_scene = _level(scene_volume.get("amp", min(60, amp_max)),
                       "amp scene volume", amp_max)
    music_scene = _level(scene_volume.get("music", min(56, music_max)),
                         "music scene volume", music_max)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "server": {"port": listen_port},
        "music": {"driver": "AppleMusic" if music == "apple_music"
                  else "RoonMusic", "max_volume": music_max,
                  "scene_volume": music_scene},
    }
    voice_payload = payload.get("voice") or {}
    if not isinstance(voice_payload, dict):
        raise SetupError("voice must be an object")
    result["voice"] = {
        "enabled": bool(voice_payload.get("enabled", "agent" in panels)),
    }
    apple_payload = payload.get("apple_services") or {}
    if not isinstance(apple_payload, dict):
        raise SetupError("apple_services must be an object")
    apple_mode = _text(apple_payload.get("mode") or "local",
                       "Apple services mode", required=True)
    if apple_mode not in {"local", "managed", "disabled"}:
        raise SetupError("Apple services mode must be local, managed, or disabled")
    result["apple_services"] = {"mode": apple_mode}
    amp_payload = payload.get("amp") or {}
    dac_payload = payload.get("dac") or {}
    roon_rack = (isinstance(amp_payload, dict)
                 and amp_payload.get("mode") == "roon") or (
                    isinstance(dac_payload, dict)
                    and dac_payload.get("mode") == "roon")
    if music == "roon" or roon_rack or payload.get("roon"):
        roon = payload.get("roon") or {}
        if not isinstance(roon, dict):
            raise SetupError("roon must be an object")
        block: dict[str, Any] = {}
        if roon.get("host"):
            block["host"] = _host(roon["host"], "Roon host")
        if roon.get("port"):
            block["port"] = _port(roon["port"], "Roon port")
        for key in ("core_id", "zone_id", "output_id"):
            if roon.get(key):
                block[key] = _text(roon[key], f"Roon {key}", required=True)
        if music == "roon" and not (block.get("zone_id") or block.get("output_id")):
            raise SetupError("Roon needs a zone or output id")
        if roon_rack and not block.get("output_id"):
            raise SetupError("Roon DAC / amp control needs an output id")
        result["roon"] = block

    tv = payload.get("tv")
    if tv:
        if not isinstance(tv, dict):
            raise SetupError("tv must be an object")
        mac = _text(tv.get("mac"), "TV MAC", required=True)
        if not _MAC.fullmatch(mac):
            raise SetupError("TV MAC must contain six colon-separated bytes")
        mac_input = _text(tv.get("mac_input") or "HDMI_2", "TV Mac input",
                          required=True)
        result["tv"] = {"driver": "LGTelevision",
                        "host": _host(tv.get("host"), "TV host"),
                        "mac": mac.lower(),
                        "inputs": {"macmini": mac_input}}

    amp = payload.get("amp")
    if amp:
        if not isinstance(amp, dict):
            raise SetupError("amp must be an object")
        mode = _text(amp.get("mode"), "amp mode", required=True)
        if mode == "serial":
            try:
                baud = int(amp.get("baud") or 115200)
            except (TypeError, ValueError):
                raise SetupError("amp baud must be a number") from None
            if not 1200 <= baud <= 1_000_000:
                raise SetupError("amp baud is outside the supported range")
            result["amp"] = {
                "driver": "McIntoshMAC7200",
                "port": _text(amp.get("port"), "amp serial port", required=True),
                "baud": baud,
                "max_volume": amp_max,
                "presets": {"music": amp_scene},
            }
        elif mode == "roon":
            result["amp"] = {"driver": "RoonAmp", "max_volume": amp_max,
                             "presets": {"music": amp_scene}}
        else:
            raise SetupError("amp mode must be serial or roon")

    dac = payload.get("dac")
    if dac:
        if not isinstance(dac, dict):
            raise SetupError("dac must be an object")
        mode = _text(dac.get("mode"), "DAC mode", required=True)
        if mode == "itach":
            try:
                ir_port = int(dac.get("ir_port") or 1)
            except (TypeError, ValueError):
                raise SetupError("iTach IR port must be a number") from None
            if ir_port not in {1, 2, 3}:
                raise SetupError("iTach IR port must be 1, 2, or 3")
            result["dac"] = {"driver": "ToppingD900"}
            result["blaster"] = {
                "host": _host(dac.get("host"), "iTach host"),
                "ports": {"dac": ir_port},
            }
        elif mode == "roon":
            result["dac"] = {"driver": "RoonDac"}
        else:
            raise SetupError("DAC mode must be itach or roon")

    scene_steps = []
    if tv:
        scene_steps.append("tv.to_mac")
    if dac and isinstance(dac, dict) and dac.get("mode") == "itach":
        scene_steps.append("dac.to_usb")
    if amp:
        scene_steps.append("amp.music_level")
    scene_steps.extend(("mini.volume", "mini.player"))
    result["ui"] = {
        "panels": panels,
        "scenes": [{
            "id": "music", "label": "Music mode", "glyph": "♫",
            "note": " · ".join(step.split(".")[0] for step in scene_steps),
            "steps": scene_steps, "stagger": 0.5,
        }],
    }
    return result


def activate(payload: dict[str, Any]) -> dict[str, Any]:
    compiled = compile_override(payload)
    _ensure_listen_port_available(compiled["server"]["port"])
    if compiled["apple_services"]["mode"] == "managed":
        try:
            ManagedAppleBroker.from_state(device_config.load_config())
        except BrokerError as exc:
            raise SetupError(str(exc)) from None
    current = device_config.load_user_config()
    saved = device_config.merged(current, compiled)
    destination = device_config.save_user_config(saved)
    if settings.INSTALL_KIND == "package":
        configure_input_helper("mini" in compiled["ui"]["panels"])
    return {"schema_version": SCHEMA_VERSION, "restart_required": True,
            "path": str(destination), "config": compiled}
