"""Bonjour advertisement for native first-launch Core discovery."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import settings


class CoreAdvertisement:
    def __init__(self) -> None:
        self._zeroconf = None
        self._info = None

    @staticmethod
    def _core_id() -> str:
        path = settings.CORE_ID_FILE
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            value = ""
        if value:
            return value
        value = str(uuid.uuid4())
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(value + "\n")
        return value

    @staticmethod
    def _tailscale_url() -> str | None:
        candidates = [shutil.which("tailscale"),
                      "/Applications/Tailscale.app/Contents/MacOS/Tailscale"]
        executable = next((item for item in candidates
                           if item and Path(item).exists()), None)
        if not executable:
            return None
        try:
            result = subprocess.run(
                [executable, "status", "--json"], capture_output=True,
                text=True, timeout=2, check=False)
            payload = json.loads(result.stdout) if result.returncode == 0 else {}
            dns = str((payload.get("Self") or {}).get("DNSName") or "").rstrip(".")
            return f"https://{dns}" if dns else None
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return None

    @staticmethod
    def _local_ip() -> str | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.0.2.1", 9))
            value = str(sock.getsockname()[0])
            return value if value and not value.startswith("127.") else None
        except OSError:
            return None
        finally:
            sock.close()

    def start(self) -> bool:
        if not settings.ADVERTISE:
            return False
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            print("  bonjour         inert (zeroconf is not installed)", flush=True)
            return False
        url = settings.PUBLIC_URL or self._tailscale_url()
        local_ip = self._local_ip()
        if not url and local_ip and not settings.BIND.startswith("127."):
            url = f"http://{local_ip}:{settings.PORT}"
        if not url or not local_ip:
            print("  bonjour         inert (no reachable Core URL)", flush=True)
            return False
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        hostname = socket.gethostname().split(".", 1)[0]
        info = ServiceInfo(
            "_avctl._tcp.local.", f"{hostname}._avctl._tcp.local.",
            addresses=[socket.inet_aton(local_ip)], port=port,
            properties={"id": self._core_id(), "url": url,
                        "version": "1"},
            server=f"{hostname}.local.",
        )
        zeroconf = None
        try:
            zeroconf = Zeroconf()
            zeroconf.register_service(info)
        except Exception as exc:
            if zeroconf is not None:
                try:
                    zeroconf.close()
                except Exception:
                    pass
            # Discovery is a convenience for first launch, never a dependency
            # of the rack controller. A busy mDNS event loop or broken network
            # interface must not take down the Core HTTP service.
            print(f"  bonjour         inert ({type(exc).__name__})", flush=True)
            return False
        self._zeroconf = zeroconf
        self._info = info
        return True

    def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            self._zeroconf.unregister_service(self._info)
        finally:
            self._zeroconf.close()
            self._zeroconf = None
            self._info = None
