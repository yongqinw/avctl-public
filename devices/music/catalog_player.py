"""Persistent JSON-line client for the signed macOS MusicKit player helper."""

from __future__ import annotations

import json
from pathlib import Path
import selectors
import subprocess
import threading
import uuid
from typing import Any, Callable

from devices.music import MusicAuthorizationRequired, MusicError


DEFAULT_BRIDGE = Path(
    "~/.avctl/bin/AvctlMusicBridge.app/Contents/MacOS/AvctlMusicBridge"
).expanduser()


class CatalogPlayer:
    """Own one long-lived ApplicationMusicPlayer process.

    ApplicationMusicPlayer stops when its host exits, so invoking a Swift CLI
    once per command is not enough. The API keeps this signed helper alive and
    exchanges one request/reply JSON object per line under a single lock.
    """

    def __init__(self, executable: str | Path | None = None,
                 timeout: float = 15.0,
                 developer_token_provider: Callable[[], str] | None = None):
        self.executable = Path(executable or DEFAULT_BRIDGE).expanduser()
        self.timeout = max(1.0, float(timeout))
        self.developer_token_provider = developer_token_provider
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.active = False
        self.authorized = False

    def available(self) -> bool:
        """Whether direct catalog playback can start on this Mac."""
        return self.executable.is_file()

    def _start_locked(self) -> subprocess.Popen[str]:
        process = self.process
        if process is not None and process.poll() is None:
            return process
        if not self.available():
            raise MusicError(
                "direct Apple Music playback needs the signed "
                "AvctlMusicBridge installed by the deployer")
        try:
            process = subprocess.Popen(
                [str(self.executable), "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise MusicError(f"could not start AvctlMusicBridge: {exc}") from None
        if process.stdin is None or process.stdout is None:
            process.kill()
            raise MusicError("AvctlMusicBridge did not open its control pipes")
        self.process = process
        self.authorized = False
        return process

    def _exchange(self, action: str,
                  catalog_ids: list[str] | None = None,
                  *, timeout: float | None = None) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        payload = {"id": request_id, "action": action}
        if catalog_ids is not None:
            payload["catalog_ids"] = [str(value) for value in catalog_ids]
        with self.lock:
            if (catalog_ids is not None
                    and self.developer_token_provider is not None):
                payload["developer_token"] = self.developer_token_provider()
            process = self._start_locked()
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self.process = None
                self.active = False
                self.authorized = False
                raise MusicError("AvctlMusicBridge stopped before the command") from None

            selector = selectors.DefaultSelector()
            try:
                selector.register(process.stdout, selectors.EVENT_READ)
                wait = self.timeout if timeout is None else timeout
                if not selector.select(wait):
                    process.kill()
                    self.process = None
                    self.active = False
                    self.authorized = False
                    raise MusicError("AvctlMusicBridge did not answer in time")
                line = process.stdout.readline()
            finally:
                selector.close()
            if not line:
                self.process = None
                self.active = False
                self.authorized = False
                raise MusicError("AvctlMusicBridge exited without an answer")
            try:
                reply = json.loads(line)
            except (TypeError, ValueError):
                raise MusicError("AvctlMusicBridge returned invalid JSON") from None
            if not isinstance(reply, dict) or reply.get("id") != request_id:
                raise MusicError("AvctlMusicBridge returned a mismatched answer")
            if not reply.get("ok"):
                detail = str(reply.get("error") or "catalog playback failed")
                if reply.get("code") == "authorization_required":
                    self.authorized = False
                    raise MusicAuthorizationRequired(detail[:400])
                raise MusicError(detail[:240])
            state = reply.get("state")
            return state if isinstance(state, dict) else {}

    def ensure_authorized(self) -> None:
        """Request visible first-use consent before any player is disturbed."""
        with self.lock:
            process = self.process
            if (self.authorized and process is not None
                    and process.poll() is None):
                return
            self.authorized = False
            # A human may need to respond to the macOS consent sheet. This is
            # intentionally longer than an ordinary bridge command timeout.
            self._exchange("authorize", timeout=max(60.0, self.timeout))
            self.authorized = True

    def replace(self, catalog_ids: list[str]) -> dict[str, Any]:
        self.ensure_authorized()
        state = self._exchange("replace", catalog_ids)
        self.active = True
        return state

    def append(self, catalog_ids: list[str]) -> dict[str, Any]:
        if not self.active:
            return self.replace(catalog_ids)
        return self._exchange("append", catalog_ids)

    def command(self, action: str) -> dict[str, Any]:
        state = self._exchange(action)
        if action in {"clear", "stop"}:
            self.active = False
        return state

    def state_if_active(self) -> dict[str, Any] | None:
        with self.lock:
            if not self.active:
                return None
        state = self._exchange("state")
        if state.get("state") == "stopped":
            # Do not mask Music.app forever after the app-scoped player
            # naturally reaches its end. The stopped snapshot is still
            # returned once so QueueController can rescue a logical tail.
            with self.lock:
                self.active = False
        return state

    def deactivate(self) -> None:
        with self.lock:
            active = self.active
        if active:
            self.command("stop")

    def close(self) -> None:
        """End this helper process; used by short-lived setup checks."""
        with self.lock:
            process, self.process = self.process, None
            self.active = False
            self.authorized = False
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
