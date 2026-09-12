"""Bounded, serialized adapter around the third-party synchronous pyRoon API."""

from __future__ import annotations

import copy
from concurrent.futures import Future
from dataclasses import dataclass
import hashlib
import itertools
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import tempfile
from typing import Any, Callable, TypeVar

import requests

from devices.roon.contracts import (
    MediaItem,
    OutputState,
    RoonCapabilityError,
    RoonError,
    VolumeState,
    ZoneState,
)

T = TypeVar("T")


def _extension_id() -> str:
    """Resolve the stable installation identity without baking in an owner."""
    environment = os.environ.get("AVCTL_ROON_EXTENSION_ID", "").strip()
    if environment:
        return environment
    path = Path(os.environ.get(
        "AVCTL_ROON_EXTENSION_ID_FILE", "~/.avctl/roon-extension-id"
    )).expanduser()
    try:
        persisted = path.read_text(encoding="utf-8").strip()
    except OSError:
        persisted = ""
    return persisted or "org.avctl.core"


APP_INFO = {
    # Roon tokens are scoped to the extension identity. New installations use
    # the public ID; a personal deployment preserves an already-authorized ID
    # in the external file resolved above.
    "extension_id": _extension_id(),
    "display_name": "avctl",
    "display_version": "6.0.0-dev",
    "publisher": "avctl",
    "email": "roon@avctl.local",
}


@dataclass(frozen=True)
class _BrowseStep:
    index: int
    title: str
    subtitle: str


@dataclass(frozen=True)
class _MediaLocator:
    """A durable recipe for rebuilding one context-bound Roon item key."""

    path: tuple[str, ...]
    steps: tuple[_BrowseStep, ...]
    query: str = ""
    hierarchy: str = "browse"
    source: str = "roon"
    kind: str = "track"
    album: str = ""
    title: str = ""
    subtitle: str = ""


class PyRoonController:
    """Own exactly one RoonApi instance on a dedicated command thread.

    pyRoon waits synchronously and updates public dictionaries from its socket
    callback thread. Callers see copied immutable state and all public API calls
    are serialized here, never on FastAPI's event loop or in panel code.
    """

    def __init__(
        self,
        *,
        token_file: str | Path = "~/.avctl/roon-token",
        host: str | None = None,
        port: int | None = None,
        core_id: str | None = None,
        selected_output: str | None = None,
        timeout: float = 8.0,
        connect_timeout: float = 15.0,
        reconnect_after: float = 3.0,
        max_volume: float = 80,
        snapshot_file: str | Path | None = None,
        snapshot_ttl: float = 300,
        api_factory: Callable[..., Any] | None = None,
        discovery_factory: Callable[..., Any] | None = None,
    ) -> None:
        if bool(host) != bool(port):
            raise ValueError("Roon host and port must be configured together")
        if not 0 <= float(max_volume) <= 100:
            raise ValueError("Roon max_volume must be in 0-100")
        self.token_file = Path(token_file).expanduser()
        self.host = host
        self.port = int(port) if port is not None else None
        self.core_id = core_id
        self.selected_output = selected_output
        self.timeout = max(0.5, float(timeout))
        self.connect_timeout = max(1.0, float(connect_timeout))
        self.reconnect_after = max(0.1, min(float(reconnect_after), 30.0))
        self.max_volume = float(max_volume)
        self.snapshot_file = (Path(snapshot_file).expanduser()
                              if snapshot_file else None)
        self.snapshot_ttl = max(1.0, float(snapshot_ttl))
        self._api_factory = api_factory
        self._discovery_factory = discovery_factory
        # Browse is stateful, so one worker still owns the pyRoon connection.
        # Priority plus small queue-placement operations lets transport jump
        # ahead between Browse mutations instead of waiting behind an entire
        # album or Ask-generated queue.
        self._commands: queue.PriorityQueue = queue.PriorityQueue(maxsize=64)
        self._command_sequence = itertools.count()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._state_lock = threading.RLock()
        self._zones: dict[str, dict[str, Any]] = {}
        self._outputs: dict[str, dict[str, Any]] = {}
        self._revision = 0
        self._connect_error: BaseException | None = None
        self._media_refs: dict[str, _MediaLocator] = {}
        self._media_items: dict[str, MediaItem] = {}
        self._service_search_cache: dict[
            tuple[str, tuple[str, ...], int], tuple[float, list[MediaItem]]
        ] = {}
        self._service_album_cache: dict[str, tuple[float, dict]] = {}
        self._snapshot: dict[str, tuple[float, Any]] = {}
        self._snapshot_refreshing: set[str] = set()
        self._snapshot_write_lock = threading.Lock()
        self._queue_rows: dict[str, list[dict[str, Any]]] = {}
        self._queue_hidden: set[str] = set()
        self._load_snapshot()
        self._thread = threading.Thread(
            target=self._run, name="avctl-roon", daemon=True)
        self._thread.start()
        # SOOD discovery has its own five-second receive window. It must not
        # consume the separate registration/subscription deadline or a healthy
        # Core can be declared dead while pyRoon is still connecting.
        startup_timeout = self.connect_timeout + (6.0 if host is None else 0.0)
        if not self._ready.wait(startup_timeout):
            self.close()
            raise RoonError("Roon did not connect before the startup deadline")
        if self._connect_error is not None:
            raise RoonError(f"Roon connection failed: {self._connect_error}")

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "PyRoonController":
        block = config.get("roon") or {}
        music = config.get("music") or {}
        return cls(
            token_file=block.get("token_file") or "~/.avctl/roon-token",
            host=block.get("host"),
            port=block.get("port"),
            core_id=block.get("core_id"),
            selected_output=(block.get("output_id") or block.get("zone_id")),
            timeout=block.get("command_timeout", 8),
            connect_timeout=block.get("connect_timeout", 15),
            reconnect_after=block.get("reconnect_after_seconds", 3),
            max_volume=block.get("max_volume", music.get("max_volume", 80)),
            snapshot_file=(block.get("snapshot_file")
                           or "~/.avctl/roon_snapshot.json"),
            snapshot_ttl=block.get("snapshot_ttl", 300),
        )

    @staticmethod
    def _encode_snapshot(value: Any) -> Any:
        if isinstance(value, MediaItem):
            return {
                "__type__": "media",
                "id": value.id,
                "title": value.title,
                "artist": value.artist,
                "album": value.album,
                "duration": value.duration,
                "image_key": value.image_key,
                "source": value.source,
                "kind": value.kind,
                "added_at": value.added_at,
            }
        if isinstance(value, list):
            return [PyRoonController._encode_snapshot(item) for item in value]
        if isinstance(value, tuple):
            return [PyRoonController._encode_snapshot(item) for item in value]
        if isinstance(value, dict):
            return {str(key): PyRoonController._encode_snapshot(item)
                    for key, item in value.items()}
        return value

    @staticmethod
    def _decode_snapshot(value: Any) -> Any:
        if isinstance(value, list):
            return [PyRoonController._decode_snapshot(item) for item in value]
        if isinstance(value, dict):
            if value.get("__type__") == "media":
                return MediaItem(
                    id=str(value.get("id") or ""),
                    title=str(value.get("title") or ""),
                    artist=str(value.get("artist") or ""),
                    album=str(value.get("album") or ""),
                    duration=value.get("duration"),
                    image_key=value.get("image_key"),
                    source=str(value.get("source") or "library"),
                    kind=str(value.get("kind") or "track"),
                    added_at=float(value.get("added_at") or 0),
                )
            return {str(key): PyRoonController._decode_snapshot(item)
                    for key, item in value.items()}
        return value

    @staticmethod
    def _encode_locator(locator: _MediaLocator) -> dict[str, Any]:
        return {
            "path": list(locator.path),
            "steps": [{"index": step.index, "title": step.title,
                       "subtitle": step.subtitle}
                      for step in locator.steps],
            "query": locator.query,
            "hierarchy": locator.hierarchy,
            "source": locator.source,
            "kind": locator.kind,
            "album": locator.album,
            "title": locator.title,
            "subtitle": locator.subtitle,
        }

    @staticmethod
    def _decode_locator(value: Any) -> _MediaLocator:
        if not isinstance(value, dict):
            raise ValueError("Roon snapshot locator must be an object")
        raw_steps = value.get("steps") or []
        return _MediaLocator(
            path=tuple(str(part) for part in value.get("path") or []),
            steps=tuple(_BrowseStep(
                int(step.get("index") or 0),
                str(step.get("title") or ""),
                str(step.get("subtitle") or ""),
            ) for step in raw_steps if isinstance(step, dict)),
            query=str(value.get("query") or ""),
            hierarchy=str(value.get("hierarchy") or "browse"),
            source=str(value.get("source") or "roon"),
            kind=str(value.get("kind") or "track"),
            album=str(value.get("album") or ""),
            title=str(value.get("title") or ""),
            subtitle=str(value.get("subtitle") or ""),
        )

    def _load_snapshot(self) -> None:
        """Restore display data and durable Browse recipes without Roon I/O."""
        if self.snapshot_file is None:
            return
        try:
            raw = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            if raw.get("version") != 1:
                return
            collections = raw.get("collections") or {}
            refs = raw.get("refs") or {}
            items = raw.get("items") or {}
            decoded = {
                str(key): (float(row["written_at"]),
                           self._decode_snapshot(row.get("value")))
                for key, row in collections.items()
                if isinstance(row, dict) and "written_at" in row
            }
            decoded_refs = {
                str(item_id): self._decode_locator(locator)
                for item_id, locator in refs.items()
            }
            decoded_items = {
                str(item_id): self._decode_snapshot(item)
                for item_id, item in items.items()
            }
            if not all(isinstance(item, MediaItem)
                       for item in decoded_items.values()):
                return
        except (OSError, ValueError, TypeError, KeyError):
            return
        with self._state_lock:
            self._snapshot = decoded
            self._media_refs.update(decoded_refs)
            self._media_items.update(decoded_items)

    def _write_snapshot(self) -> None:
        if self.snapshot_file is None:
            return
        with self._state_lock:
            payload = {
                "version": 1,
                "collections": {
                    key: {"written_at": written_at,
                          "value": self._encode_snapshot(value)}
                    for key, (written_at, value) in self._snapshot.items()
                },
                "refs": {
                    item_id: self._encode_locator(locator)
                    for item_id, locator in self._media_refs.items()
                },
                "items": {
                    item_id: self._encode_snapshot(item)
                    for item_id, item in self._media_items.items()
                },
            }
        with self._snapshot_write_lock:
            try:
                self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)
                handle, temporary = tempfile.mkstemp(
                    dir=self.snapshot_file.parent,
                    prefix=".roon-snapshot-",
                )
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False,
                              separators=(",", ":"))
                os.replace(temporary, self.snapshot_file)
            except OSError:
                try:
                    os.unlink(temporary)
                except (OSError, UnboundLocalError):
                    pass

    def _store_snapshot(self, key: str, value: T) -> T:
        with self._state_lock:
            self._snapshot[key] = (time.time(), copy.deepcopy(value))
        self._write_snapshot()
        return copy.deepcopy(value)

    def _refresh_snapshot(self, key: str,
                          loader: Callable[[int], T]) -> None:
        try:
            self._store_snapshot(key, loader(20))
        except (RoonError, RuntimeError, ValueError):
            # Stale display data is more useful than turning a transient Core
            # problem into an empty Music panel.
            pass
        finally:
            with self._state_lock:
                self._snapshot_refreshing.discard(key)

    def _snapshot_read(self, key: str,
                       loader: Callable[[int], T]) -> T:
        with self._state_lock:
            cached = self._snapshot.get(key)
            stale = (cached is not None
                     and time.time() - cached[0] >= self.snapshot_ttl)
            should_refresh = stale and key not in self._snapshot_refreshing
            if should_refresh:
                self._snapshot_refreshing.add(key)
            value = copy.deepcopy(cached[1]) if cached is not None else None
        if cached is not None:
            if should_refresh:
                threading.Thread(
                    target=self._refresh_snapshot,
                    args=(key, loader),
                    name=f"avctl-roon-refresh-{hashlib.sha256(key.encode()).hexdigest()[:8]}",
                    daemon=True,
                ).start()
            return value
        return self._store_snapshot(key, loader(10))

    def _discover(self) -> tuple[str, int]:
        if self.host and self.port:
            return self.host, self.port
        if self._discovery_factory is None:
            from roonapi import RoonDiscovery
            factory = RoonDiscovery
        else:
            factory = self._discovery_factory
        discovery = factory(self.core_id)
        try:
            found = discovery.all()
        finally:
            discovery.stop()
        unique = list(dict.fromkeys(
            (str(host), int(port)) for host, port in found))
        if not unique:
            raise RoonError("no Roon Server discovered")
        if len(unique) > 1 and not self.core_id:
            raise RoonError("multiple Roon Servers discovered; select a core_id")
        return unique[0]

    def _token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise RoonError(
                "Roon is not authorized; run scripts/roon_probe.py first") from None
        if not token:
            raise RoonError("the Roon token file is empty")
        return token

    def _capture(self, api: Any) -> None:
        try:
            zones = copy.deepcopy(api.zones)
            outputs = copy.deepcopy(api.outputs)
        except RuntimeError:
            # A callback may be extending a dictionary at exactly this point.
            # The next callback or command completion will take a full copy.
            return
        with self._state_lock:
            self._zones = zones
            self._outputs = outputs
            self._revision += 1

    def _capture_queue(self, target: str, body: Any) -> None:
        """Copy pyRoon's queue callback before its socket thread moves on."""
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return
        with self._state_lock:
            self._queue_rows[target] = copy.deepcopy(body["items"])
            self._revision += 1

    def _connect_api(self, factory: Callable[..., Any]) -> tuple[Any, bool]:
        """Create and subscribe one client owned by the worker thread."""
        api = None
        try:
            host, port = self._discover()
            api = factory(APP_INFO, self._token(), host, port,
                          blocking_init=False)
            deadline = time.monotonic() + self.connect_timeout
            while not self._api_ready(api) and not self._stop.is_set():
                if time.monotonic() >= deadline:
                    raise RoonError("Roon authorization/connection timed out")
                time.sleep(0.05)
            if self._stop.is_set():
                raise RoonError("Roon worker is stopping")
            api.register_state_callback(
                lambda *_, client=api: self._capture(client))
            queue_subscribed = False
            if self.selected_output and hasattr(api, "register_queue_callback"):
                api.register_queue_callback(
                    lambda body: self._capture_queue(self.selected_output, body),
                    self.selected_output,
                )
                queue_subscribed = True
            state_deadline = min(deadline, time.monotonic() + 3.0)
            while not api.outputs and time.monotonic() < state_deadline:
                time.sleep(0.05)
            self._capture(api)
            return api, queue_subscribed
        except BaseException:
            if api is not None:
                try:
                    api.stop()
                except BaseException:
                    pass
            raise

    def _run(self) -> None:
        api = None
        try:
            if self._api_factory is None:
                from roonapi import RoonApi
                factory = RoonApi
            else:
                factory = self._api_factory
            api, queue_subscribed = self._connect_api(factory)
        except BaseException as exc:  # expose startup failure to constructor
            self._connect_error = exc
            self._ready.set()
            return
        self._ready.set()
        disconnected_since: float | None = None
        retry_delay = 0.25
        while not self._stop.is_set():
            # pyRoon recreates its socket and its built-in zone/output
            # subscriptions after a disconnect, but caller-owned queue
            # subscriptions belong to the old socket. Its public `ready`
            # transition is the only supported connection lifecycle signal;
            # remember the down phase and restore the queue callback once the
            # new registration completes.
            if not self._api_ready(api):
                queue_subscribed = False
                now = time.monotonic()
                if disconnected_since is None:
                    disconnected_since = now
                if now - disconnected_since >= self.reconnect_after:
                    try:
                        api.stop()
                    except BaseException:
                        pass
                    try:
                        api, queue_subscribed = self._connect_api(factory)
                    except BaseException:
                        # A Core restart or network transition can outlast one
                        # complete client construction. Keep the serialized
                        # command queue intact and rebuild until close() or a
                        # later connection succeeds.
                        if self._stop.wait(retry_delay):
                            break
                        retry_delay = min(retry_delay * 2, 2.0)
                        disconnected_since = (
                            time.monotonic() - self.reconnect_after)
                        continue
                    disconnected_since = None
                    retry_delay = 0.25
                    continue
                # Do not dequeue a command while the old client is down. This
                # gives pyRoon a brief self-reconnect window and lets a queued
                # command execute on the rebuilt client rather than fail on the
                # stale websocket.
                self._stop.wait(0.05)
                continue
            elif (not queue_subscribed and self.selected_output
                  and hasattr(api, "register_queue_callback")):
                try:
                    api.register_queue_callback(
                        lambda body: self._capture_queue(
                            self.selected_output, body),
                        self.selected_output,
                    )
                except BaseException:
                    # The socket may still be completing registration. Retry
                    # on the next worker beat; commands remain independently
                    # bounded by _invoke.
                    pass
                else:
                    queue_subscribed = True
            else:
                disconnected_since = None
                retry_delay = 0.25
            try:
                command = self._commands.get(timeout=0.25)
            except queue.Empty:
                continue
            priority, sequence, operation, function, future = command
            if future.cancelled():
                continue
            if not self._api_ready(api):
                # The socket may have dropped after the readiness check but
                # before queue.get() returned. Preserve both the command and
                # its ordering so the outer loop can recover the connection.
                self._commands.put((priority, sequence, operation, function,
                                    future))
                continue
            try:
                if future.cancelled():
                    continue
                if not self._api_ready(api):
                    raise RoonError("connection is not ready")
                result = function(api)
                self._capture(api)
                if not future.cancelled():
                    future.set_result(result)
            except BaseException as exc:
                if not future.cancelled():
                    future.set_exception(
                        RoonError(f"Roon {operation} failed: {exc}"))
        if api is not None:
            try:
                api.stop()
            except BaseException:
                pass

    @staticmethod
    def _api_ready(api: Any) -> bool:
        if not bool(getattr(api, "ready", False)):
            return False
        # pyRoon 0.1.x exposes no public reconnect-ready signal. Its private
        # socket is the same object its own request implementation consults,
        # so this narrowly-scoped compatibility check prevents calls during
        # the otherwise invisible reconnect gap. Test doubles and future
        # clients without this attribute retain the public-ready behavior.
        socket = getattr(api, "_roonsocket", None)
        return socket is None or bool(getattr(socket, "connected", False))

    def _invoke(self, operation: str, function: Callable[[Any], T], *,
                priority: int = 10) -> T:
        if self._connect_error is not None:
            raise RoonError(f"Roon is unavailable: {self._connect_error}")
        if self._stop.is_set() or not self._thread.is_alive():
            raise RoonError("Roon worker is stopped")
        future: Future[T] = Future()
        try:
            self._commands.put((int(priority), next(self._command_sequence),
                                operation, function, future), timeout=0.25)
        except queue.Full:
            raise RoonError("Roon command queue is full") from None
        try:
            return future.result(timeout=self.timeout)
        except TimeoutError:
            future.cancel()
            raise RoonError(f"Roon {operation} timed out") from None

    def close(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    @property
    def revision(self) -> int:
        with self._state_lock:
            return self._revision

    @staticmethod
    def _percent(value: float, minimum: float, maximum: float) -> float:
        return 0.0 if maximum == minimum else (value - minimum) * 100 / (maximum - minimum)

    def output(self, output_id: str) -> OutputState:
        with self._state_lock:
            try:
                raw = copy.deepcopy(self._outputs[output_id])
            except KeyError:
                raise RoonError(f"Roon output not found: {output_id}") from None
        capabilities: set[str] = set()
        volume_raw = raw.get("volume")
        if isinstance(volume_raw, dict):
            capabilities.update({"volume.set", "volume.step", "mute.set"})
            minimum = float(volume_raw.get("min", 0))
            maximum = float(volume_raw.get("max", 100))
            value = self._percent(float(volume_raw.get("value", minimum)), minimum, maximum)
            limits = [self.max_volume]
            for key in ("hard_limit_max", "soft_limit"):
                if isinstance(volume_raw.get(key), (int, float)):
                    limits.append(self._percent(float(volume_raw[key]), minimum, maximum))
            volume = VolumeState(
                mode=str(volume_raw.get("type") or "number"),
                value=value,
                minimum=0,
                maximum=100,
                step=max(0.1, self._percent(
                    minimum + float(volume_raw.get("step", 1)), minimum, maximum)),
                muted=bool(volume_raw.get("is_muted")),
                readback="confirmed",
                safety_max=max(0, min(limits)),
            )
        else:
            volume = VolumeState(mode="fixed")
        source_controls = raw.get("source_controls") or []
        if any(row.get("supports_standby") for row in source_controls):
            capabilities.update({"power.standby", "power.wake"})
        statuses = {str(row.get("status")) for row in source_controls}
        standby = (True if statuses == {"standby"} else
                   False if statuses and "indeterminate" not in statuses else None)
        return OutputState(
            id=output_id,
            name=str(raw.get("display_name") or output_id),
            zone_id=str(raw.get("zone_id") or ""),
            capabilities=frozenset(capabilities),
            volume=volume,
            standby=standby,
        )

    def zone(self, zone_id: str) -> ZoneState:
        with self._state_lock:
            raw = copy.deepcopy(self._zones.get(zone_id))
            outputs = copy.deepcopy(self._outputs)
            revision = self._revision
            if raw is None:
                selected = outputs.get(zone_id)
                linked_zone = (str(selected.get("zone_id") or "")
                               if isinstance(selected, dict) else "")
                if linked_zone:
                    raw = copy.deepcopy(self._zones.get(linked_zone))
        if raw is None:
            matched = [row for row in outputs.values()
                       if row.get("output_id") == zone_id or row.get("zone_id") == zone_id]
            if not matched:
                raise RoonError(f"Roon zone/output not found: {zone_id}")
            return ZoneState(
                id=zone_id,
                name=str(matched[0].get("display_name") or zone_id),
                state="stopped",
                output_ids=tuple(str(row["output_id"]) for row in matched),
                revision=revision,
            )
        now = raw.get("now_playing") or {}
        lines = now.get("three_line") or {}
        item = None
        if lines.get("line1"):
            # State/output refreshes advance the controller revision even
            # when playback did not move. A revision-based media id made the
            # browser treat every poll as a new song and reload its cover.
            # Roon does not expose a persistent now-playing id, so derive one
            # from the stable media facts it does expose.
            identity = "\0".join(str(value or "") for value in (
                zone_id,
                lines.get("line1"),
                lines.get("line2"),
                lines.get("line3"),
                now.get("length"),
                now.get("image_key"),
            ))
            digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
            item = MediaItem(
                id=f"roon-now:{digest}",
                title=str(lines.get("line1") or ""),
                artist=str(lines.get("line2") or ""),
                album=str(lines.get("line3") or ""),
                duration=now.get("length"),
                image_key=now.get("image_key"),
                source="roon",
            )
        settings = raw.get("settings") or {}
        with self._state_lock:
            hidden = (zone_id in self._queue_hidden
                      or any(str(output.get("output_id")) in self._queue_hidden
                             for output in raw.get("outputs", [])))
            subscribed = (self._queue_rows.get(zone_id)
                          or next((self._queue_rows.get(str(output.get("output_id")))
                                   for output in raw.get("outputs", [])
                                   if self._queue_rows.get(str(output.get("output_id")))
                                   is not None), None))
        return ZoneState(
            id=str(raw.get("zone_id") or zone_id),
            name=str(raw.get("display_name") or zone_id),
            state=str(raw.get("state") or "stopped"),
            now_playing=item,
            output_ids=tuple(str(row["output_id"]) for row in raw.get("outputs", [])),
            shuffle=bool(settings.get("shuffle")),
            repeat=str(settings.get("loop") or "disabled"),
            queue_depth=(0 if hidden else len(subscribed)
                         if subscribed is not None
                         else int(raw.get("queue_items_remaining") or 0)),
            revision=revision,
        )

    def outputs(self) -> list[OutputState]:
        with self._state_lock:
            ids = list(self._outputs)
        return [self.output(output_id) for output_id in ids]

    def zones(self) -> list[ZoneState]:
        with self._state_lock:
            zone_ids = list(self._zones)
            idle_output_ids = [
                output_id for output_id, raw in self._outputs.items()
                if raw.get("zone_id") not in self._zones
            ]
        return ([self.zone(zone_id) for zone_id in zone_ids]
                + [self.zone(output_id) for output_id in idle_output_ids])

    _LINK = re.compile(r"\[\[[^|\]]+\|([^\]]+)\]\]")
    _TRACK_NUMBER = re.compile(r"^\s*\d+\.\s+")

    @classmethod
    def _text(cls, value: Any) -> str:
        return cls._LINK.sub(r"\1", str(value or "")).strip()

    def _target(self, api: Any, requested: str | None = None) -> str:
        target = requested or self.selected_output or next(iter(api.outputs), None)
        if not target:
            raise RoonError("Roon has no output to scope browsing")
        return str(target)

    @staticmethod
    def _browse_opts(target: str, hierarchy: str = "browse", **extra: Any
                     ) -> dict[str, Any]:
        return {"zone_or_output_id": target, "hierarchy": hierarchy, **extra}

    @classmethod
    def _load(cls, api: Any, target: str, count: int = 500,
              hierarchy: str = "browse") -> list[dict]:
        """Load a complete bounded list instead of silently truncating at 100."""
        cap = min(2000, max(0, int(count)))
        rows: list[dict] = []
        offset = 0
        while len(rows) < cap:
            page = min(100, cap - len(rows))
            result = api.browse_load(cls._browse_opts(
                target, hierarchy, count=page, offset=offset)) or {}
            batch = list(result.get("items") or [])
            rows.extend(batch)
            total = (result.get("list") or {}).get("count")
            if not batch or len(batch) < page:
                break
            offset += len(batch)
            if isinstance(total, int) and offset >= total:
                break
        return rows

    @classmethod
    def _matching_row(cls, rows: list[dict], step: _BrowseStep) -> dict:
        exact = [row for row in rows
                 if cls._text(row.get("title")) == cls._text(step.title)
                 and cls._text(row.get("subtitle")) == cls._text(step.subtitle)]
        if exact:
            return exact[0]
        title_matches = [row for row in rows
                         if cls._text(row.get("title")) == cls._text(step.title)]
        if len(title_matches) == 1:
            return title_matches[0]
        if 0 <= step.index < len(rows):
            candidate = rows[step.index]
            if cls._text(candidate.get("title")) == cls._text(step.title):
                return candidate
        raise RoonCapabilityError(
            f"Roon browse item moved or disappeared: {step.title}")

    @classmethod
    def _enter_title(cls, api: Any, target: str, title: str,
                     hierarchy: str = "browse") -> None:
        rows = cls._load(api, target, hierarchy=hierarchy)
        row = next((item for item in rows
                    if cls._text(item.get("title")) == cls._text(title)), None)
        if row is None:
            raise RoonCapabilityError(f"Roon browse item not found: {title}")
        api.browse_browse(cls._browse_opts(
            target, hierarchy, item_key=row["item_key"]))

    @classmethod
    def _step(cls, index: int, row: dict) -> _BrowseStep:
        return _BrowseStep(index, str(row.get("title") or ""),
                           str(row.get("subtitle") or ""))

    def _open(self, api: Any, locator: _MediaLocator,
              *, enter_item: bool = False) -> tuple[str, dict]:
        target = self._target(api)
        api.browse_browse(self._browse_opts(
            target, locator.hierarchy, pop_all=True))
        for title in locator.path:
            self._enter_title(api, target, title, locator.hierarchy)
        if locator.query:
            api.browse_browse(self._browse_opts(
                target, locator.hierarchy, input=locator.query))
        if not locator.steps:
            raise RoonCapabilityError("Roon media reference has no item")
        for step in locator.steps[:-1]:
            row = self._matching_row(
                self._load(api, target, hierarchy=locator.hierarchy), step)
            api.browse_browse(self._browse_opts(
                target, locator.hierarchy, item_key=row["item_key"]))
        row = self._matching_row(
            self._load(api, target, hierarchy=locator.hierarchy),
            locator.steps[-1])
        if enter_item:
            api.browse_browse(self._browse_opts(
                target, locator.hierarchy, item_key=row["item_key"]))
        return target, row

    def _remember_rows(
        self,
        rows: list[dict],
        *,
        path: tuple[str, ...],
        query: str = "",
        prefix: tuple[_BrowseStep, ...] = (),
        source: str,
        kind: str,
        album: str = "",
        limit: int = 500,
    ) -> list[MediaItem]:
        refs: dict[str, _MediaLocator] = {}
        items: list[MediaItem] = []
        for index, row in enumerate(rows[:limit]):
            raw_title = str(row.get("title") or "")
            if not raw_title or raw_title.casefold().startswith("play "):
                continue
            title = self._TRACK_NUMBER.sub("", self._text(raw_title))
            artist = self._text(row.get("subtitle"))
            locator = _MediaLocator(
                path=path,
                query=query,
                steps=prefix + (self._step(index, row),),
                source=source,
                kind=kind,
                album=album,
                title=title,
                subtitle=artist,
            )
            digest = hashlib.sha256(repr(locator).encode()).hexdigest()[:24]
            item_id = f"roon-{kind}:{digest}"
            refs[item_id] = locator
            items.append(MediaItem(
                id=item_id,
                title=title,
                artist=artist,
                album=album if kind == "track" else "",
                duration=row.get("length"),
                image_key=row.get("image_key"),
                source=source,
                kind=kind,
            ))
        with self._state_lock:
            self._media_refs.update(refs)
            self._media_items.update({item.id: item for item in items})
            if len(self._media_refs) > 4000:
                keep = dict(list(self._media_refs.items())[-2000:])
                self._media_refs = keep
                self._media_items = {
                    item_id: self._media_items[item_id]
                    for item_id in keep if item_id in self._media_items
                }
        return items

    def _browse_path(self, api: Any, path: tuple[str, ...], *,
                     query: str = "", category: str = "",
                     count: int = 500) -> tuple[list[dict], tuple[_BrowseStep, ...]]:
        target = self._target(api)
        api.browse_browse(self._browse_opts(target, pop_all=True))
        for title in path:
            self._enter_title(api, target, title)
        if query:
            api.browse_browse(self._browse_opts(target, input=query))
        prefix: tuple[_BrowseStep, ...] = ()
        if category:
            rows = self._load(api, target)
            row = next((item for item in rows
                        if self._text(item.get("title")) == category), None)
            if row is None:
                return [], ()
            prefix = (self._step(rows.index(row), row),)
            api.browse_browse(self._browse_opts(
                target, item_key=row["item_key"]))
        return self._load(api, target, count=count), prefix

    def _search_kind(self, query: str, category: str, kind: str,
                     limit: int = 250, source: str = "roon") -> list[MediaItem]:
        def browse(api: Any) -> list[MediaItem]:
            rows, prefix = self._browse_path(
                api, ("Library", "Search"), query=query,
                category=category, count=limit)
            return self._remember_rows(
                rows, path=("Library", "Search"), query=query,
                prefix=prefix, source=source, kind=kind, limit=limit)
        return self._invoke(f"search.{kind}", browse)

    def search(self, query: str) -> list[MediaItem]:
        return self._search_kind(query, "Tracks", "track")

    def library(self) -> list[MediaItem]:
        def load(priority: int) -> list[MediaItem]:
            def browse(api: Any) -> list[MediaItem]:
                rows, _ = self._browse_path(
                    api, ("Library", "Tracks"), count=2000)
                return self._remember_rows(
                    rows, path=("Library", "Tracks"), source="library",
                    kind="track", limit=2000)
            return self._invoke("library", browse, priority=priority)

        return self._snapshot_read("library.tracks", load)

    def library_albums(self) -> list[MediaItem]:
        """Return the last complete album snapshot and refresh stale data.

        Roon exposes its library as a paged, stateful Browse tree. UI reads
        must not replay that tree; the first scan is persisted and later
        refreshes happen behind the stale snapshot.
        """
        def load(priority: int) -> list[MediaItem]:
            def browse(api: Any) -> list[MediaItem]:
                rows, _ = self._browse_path(
                    api, ("Library", "Albums"), count=2000)
                return self._remember_rows(
                    rows, path=("Library", "Albums"), source="library",
                    kind="album", limit=2000)
            return self._invoke("library.albums", browse, priority=priority)

        return self._snapshot_read("library.albums", load)

    def recent(self) -> list[MediaItem]:
        # Roon exposes Library/Tracks but no stable date-added field or
        # Recently Added hierarchy. The persisted snapshot preserves Roon's
        # displayed order without a foreground rescan.
        return self.library()

    def _cached_container_tracks(self, key: str, locator: _MediaLocator,
                                 operation: str) -> list[MediaItem]:
        def load(priority: int) -> list[MediaItem]:
            return self._invoke(
                operation,
                lambda api: self._container_tracks(api, locator),
                priority=priority,
            )
        return self._snapshot_read(key, load)

    def _container_tracks(self, api: Any, locator: _MediaLocator,
                          limit: int = 2000,
                          prefer_current_search: bool = False
                          ) -> list[MediaItem]:
        target = self._target(api)
        steps = locator.steps
        restore_levels = 0
        if prefer_current_search and locator.path == ("Library", "Search"):
            # search_service deliberately leaves Roon at the query's category
            # list. The common UI path is search -> tap album, so reuse that
            # verified context and avoid replaying root/Library/Search/input.
            # Another client may have browsed in between; exact row matching
            # makes that harmless and the durable recipe below is the fallback.
            try:
                for step in locator.steps:
                    row = self._matching_row(self._load(api, target), step)
                    result = api.browse_browse(self._browse_opts(
                        target, item_key=row["item_key"])) or {}
                    if result.get("is_error"):
                        raise RoonCapabilityError(
                            str(result.get("message") or "browse failed"))
                    restore_levels += 1
            except (KeyError, RoonCapabilityError):
                # The partial fast path may already have entered a level.
                # _open starts with pop_all, so it also repairs that context.
                restore_levels = 0
                target, _ = self._open(api, locator, enter_item=True)
        else:
            target, _ = self._open(api, locator, enter_item=True)
        # Search albums sometimes insert a one-row version/edition wrapper.
        try:
            for _ in range(4):
                rows = self._load(api, target, count=limit)
                playable = [row for row in rows
                            if row.get("hint") in {"action", "action_list"}
                            and not str(row.get("title") or "").casefold().startswith("play ")]
                if playable or not rows:
                    break
                if len(rows) != 1 or rows[0].get("hint") != "list":
                    break
                steps = steps + (self._step(0, rows[0]),)
                api.browse_browse(self._browse_opts(
                    target, item_key=rows[0]["item_key"]))
                if restore_levels:
                    restore_levels += 1
            return self._remember_rows(
                rows, path=locator.path, query=locator.query, prefix=steps,
                source=locator.source, kind="track", album=locator.title,
                limit=limit)
        finally:
            if restore_levels:
                # Put the shared session back at the query categories so the
                # next result can use the same fast path too.
                api.browse_browse(self._browse_opts(
                    target, pop_levels=restore_levels))

    def album(self, name: str, artist: str = "") -> list[MediaItem]:
        candidates = self.library_albums()
        chosen = next((item for item in candidates
                       if item.title.casefold() == name.casefold()
                       and (not artist or artist.casefold() in
                            item.artist.casefold())), None)
        if chosen is None:
            return []
        with self._state_lock:
            locator = self._media_refs.get(chosen.id)
        if locator is None:
            raise RoonError(f"Roon album reference is unknown: {chosen.id}")
        return self._cached_container_tracks(
            f"library.album:{chosen.id}", locator, "album")

    def playlists(self) -> list[dict]:
        def load(priority: int) -> list[dict]:
            def browse(api: Any) -> list[dict]:
                rows, _ = self._browse_path(api, ("Playlists",), count=1000)
                items = self._remember_rows(
                    rows, path=("Playlists",), source="roon",
                    kind="playlist", limit=1000)
                return [{"pid": item.id, "name": item.title,
                         "count": int((re.search(r"(\d+)", item.artist) or
                                       [None, 0])[1]),
                         "art": item.id if item.image_key else None}
                        for item in items]
            return self._invoke("playlists", browse, priority=priority)

        return self._snapshot_read("library.playlists", load)

    def playlist(self, playlist_id: str) -> dict | None:
        with self._state_lock:
            locator = self._media_refs.get(playlist_id)
        if locator is None or locator.kind != "playlist":
            # Populate durable refs after a daemon restart before deciding the
            # requested playlist disappeared.
            self.playlists()
            with self._state_lock:
                locator = self._media_refs.get(playlist_id)
        if locator is None or locator.kind != "playlist":
            return None
        tracks = self._cached_container_tracks(
            f"library.playlist:{playlist_id}", locator, "playlist")
        return {"name": locator.title, "tracks": [row.panel_dict() for row in tracks]}

    @staticmethod
    def _interleave_search_buckets(buckets: list[list[MediaItem]],
                                   cap: int) -> list[MediaItem]:
        found: list[MediaItem] = []
        index = 0
        while len(found) < cap and any(index < len(rows) for rows in buckets):
            for rows in buckets:
                if index < len(rows):
                    found.append(rows[index])
                    if len(found) >= cap:
                        break
            index += 1
        return found

    def search_service(self, query: str, kinds: list[str],
                       limit: int = 8) -> list[MediaItem]:
        return self.search_service_many([query], kinds, limit)[0]

    def search_service_many(self, queries: list[str], kinds: list[str],
                            limit: int = 8) -> list[list[MediaItem]]:
        """Search several terms in one worker job with isolated Browse trees.

        Roon Browse is stateful. Reusing the previous result context is not
        safe: when a later query has no matches, some Cores leave the old rows
        visible, and stamping them with the later query creates convincing but
        false media references. Keep batching on the command thread, but reset
        to root and reopen Library/Search before every input.
        """
        singular = {
            "songs": ("Tracks", "track"),
            "albums": ("Albums", "album"),
            "playlists": ("Playlists", "playlist"),
        }
        requested_kinds = tuple(kind for kind in kinds if kind in singular)
        cap = max(1, int(limit))
        now = time.monotonic()
        normalized = [str(query).strip() for query in queries]
        answers: list[list[MediaItem] | None] = [None] * len(normalized)
        missing: dict[tuple[str, tuple[str, ...], int], tuple[str, list[int]]] = {}
        with self._state_lock:
            for index, query in enumerate(normalized):
                cache_key = (query.casefold(), requested_kinds, cap)
                cached = self._service_search_cache.get(cache_key)
                if cached is not None and cached[0] > now:
                    answers[index] = copy.deepcopy(cached[1])
                else:
                    entry = missing.setdefault(cache_key, (query, []))
                    entry[1].append(index)

        pending = list(missing.items())
        # Each isolated query replays a few Browse round trips. Keep worker
        # jobs small enough to finish within the normal command deadline and
        # to give transport commands a priority boundary between searches.
        for offset in range(0, len(pending), 2):
            chunk = pending[offset:offset + 2]

            def browse(api: Any) -> list[list[MediaItem]]:
                target = self._target(api)
                chunk_answers: list[list[MediaItem]] = []
                for _cache_key, (query, _indexes) in chunk:
                    api.browse_browse(self._browse_opts(target, pop_all=True))
                    self._enter_title(api, target, "Library")
                    self._enter_title(api, target, "Search")
                    result = api.browse_browse(
                        self._browse_opts(target, input=query)) or {}
                    if result.get("is_error"):
                        raise RoonCapabilityError(
                            f"Roon search failed for {query!r}")
                    category_rows = self._load(api, target)
                    buckets: list[list[MediaItem]] = []
                    for requested in requested_kinds:
                        category, kind = singular[requested]
                        row = next((item for item in category_rows
                                    if self._text(item.get("title")) == category),
                                   None)
                        if row is None:
                            buckets.append([])
                            continue
                        prefix = (self._step(category_rows.index(row), row),)
                        api.browse_browse(self._browse_opts(
                            target, item_key=row["item_key"]))
                        rows = self._load(api, target, count=cap)
                        buckets.append(self._remember_rows(
                            rows, path=("Library", "Search"), query=query,
                            prefix=prefix, source="qobuz", kind=kind,
                            limit=cap))
                        api.browse_browse(self._browse_opts(
                            target, pop_levels=1))
                    chunk_answers.append(
                        self._interleave_search_buckets(buckets, cap))
                return chunk_answers

            loaded = self._invoke("search.service.batch", browse)
            expires = time.monotonic() + 30.0
            with self._state_lock:
                for (cache_key, (_query, indexes)), found in zip(chunk, loaded):
                    self._service_search_cache[cache_key] = (
                        expires, copy.deepcopy(found))
                    for index in indexes:
                        answers[index] = copy.deepcopy(found)
                while len(self._service_search_cache) > 32:
                    oldest = min(
                        self._service_search_cache,
                        key=lambda key: self._service_search_cache[key][0])
                    self._service_search_cache.pop(oldest, None)
        return [answer or [] for answer in answers]

    def service_album(self, item_id: str) -> dict:
        now = time.monotonic()
        with self._state_lock:
            cached = self._service_album_cache.get(item_id)
        if cached is not None and cached[0] > now:
            return copy.deepcopy(cached[1])
        with self._state_lock:
            locator = self._media_refs.get(item_id)
        if locator is None or locator.kind != "album":
            raise RoonError(f"Roon album reference is unknown: {item_id}")
        tracks = self._invoke(
            "service.album", lambda api: self._container_tracks(
                api, locator, prefer_current_search=True))
        image_key = next((row.image_key for row in tracks if row.image_key), None)
        detail = {
            "id": item_id,
            "album": locator.title,
            "artist": locator.subtitle,
            "image_key": image_key,
            "tracks": [row.panel_dict() for row in tracks],
        }
        with self._state_lock:
            self._service_album_cache[item_id] = (
                time.monotonic() + 300.0, copy.deepcopy(detail))
            if len(self._service_album_cache) > 64:
                oldest = min(self._service_album_cache,
                             key=lambda key: self._service_album_cache[key][0])
                self._service_album_cache.pop(oldest, None)
        return detail

    def service_item(self, item_id: str) -> MediaItem:
        with self._state_lock:
            item = self._media_items.get(item_id)
        if item is None or item.source == "library":
            raise RoonError(f"Roon service item reference is unknown: {item_id}")
        return copy.deepcopy(item)

    def service_tracks(self, kind: str, item_id: str) -> list[MediaItem]:
        if kind == "album":
            return [MediaItem(
                id=str(row["pid"]), title=str(row.get("name") or ""),
                artist=str(row.get("artist") or ""),
                album=str(row.get("album") or ""),
                duration=row.get("duration"), image_key=row.get("image_key"),
                source=str(row.get("source") or "qobuz"),
            ) for row in self.service_album(item_id).get("tracks") or []]
        if kind == "playlist":
            playlist = self.playlist(item_id)
            if playlist is None:
                return []
            return [MediaItem(
                id=str(row["pid"]), title=str(row.get("name") or ""),
                artist=str(row.get("artist") or ""),
                album=str(row.get("album") or ""),
                duration=row.get("duration"), image_key=row.get("image_key"),
                source=str(row.get("source") or "qobuz"),
            ) for row in playlist.get("tracks") or []]
        raise ValueError("Roon service container must be an album or playlist")

    def explore(self, limit: int = 10) -> list[dict]:
        cap = min(20, max(1, int(limit)))
        def load(priority: int) -> list[dict]:
            candidates = (
                ("Qobuz grand selection", ("Qobuz", "New Releases",
                                            "Qobuz grand selection")),
                ("Still Trending", ("Qobuz", "New Releases",
                                    "Still Trending")),
                ("Top albums on Qobuz", ("Qobuz", "New Releases",
                                          "Top albums on Qobuz")),
                ("Qobuz Playlists", ("Qobuz", "Playlists")),
            )
            sections: list[dict] = []
            for title, path in candidates:
                def browse(api: Any, browse_path: tuple[str, ...] = path
                           ) -> list[MediaItem]:
                    try:
                        rows, _ = self._browse_path(
                            api, browse_path, count=cap)
                    except RoonCapabilityError:
                        return []
                    kind = ("playlist" if browse_path[-1] == "Playlists"
                            else "album")
                    return self._remember_rows(
                        rows, path=browse_path, source="qobuz", kind=kind,
                        limit=cap)
                items = self._invoke(
                    f"explore.{title}", browse, priority=priority)
                if items:
                    sections.append({
                        "id": hashlib.sha256(title.encode()).hexdigest()[:12],
                        "title": title, "items": items,
                    })
            return sections

        return self._snapshot_read(f"service.explore:{cap}", load)

    def add_to_library(self, item_id: str) -> None:
        with self._state_lock:
            locator = self._media_refs.get(item_id)
        if locator is None:
            raise RoonError(f"Roon media reference is unknown: {item_id}")
        self._invoke(
            "library.add",
            lambda api: self._action(api, locator,
                                     ("Add To Library", "Add to Library")),
        )

    @classmethod
    def _queue_media(cls, row: dict, index: int) -> MediaItem:
        lines = row.get("three_line") or row.get("two_line") or {}
        return MediaItem(
            id=f"roon-queue:{row.get('queue_item_id', index)}",
            title=cls._text(lines.get("line1") or row.get("title")),
            artist=cls._text(lines.get("line2") or row.get("subtitle")),
            album=cls._text(lines.get("line3")),
            duration=row.get("length"),
            image_key=row.get("image_key"),
            source="roon",
        )

    def queue(self, zone_id: str) -> list[MediaItem]:
        with self._state_lock:
            if zone_id in self._queue_hidden or self.selected_output in self._queue_hidden:
                return []
            rows = copy.deepcopy(
                self._queue_rows.get(zone_id)
                or self._queue_rows.get(str(self.selected_output)) or [])
        return [self._queue_media(row, index) for index, row in enumerate(rows)]

    def _action(self, api: Any, locator: _MediaLocator,
                wanted: tuple[str, ...]) -> None:
        target, _ = self._open(api, locator, enter_item=True)
        for _ in range(5):
            rows = self._load(api, target, count=100)
            action = next((row for row in rows
                           if self._text(row.get("title")) in wanted
                           and row.get("hint") in {None, "action"}), None)
            if action is not None:
                result = api.browse_browse(self._browse_opts(
                    target, item_key=action["item_key"]))
                if isinstance(result, dict) and result.get("is_error"):
                    raise RoonError(str(result.get("message") or "action failed"))
                return
            wrapper = next((row for row in rows
                            if row.get("hint") == "action_list"), None)
            if wrapper is None:
                break
            api.browse_browse(self._browse_opts(
                target, item_key=wrapper["item_key"]))
        raise RoonCapabilityError(
            f"Roon item does not offer {' or '.join(wanted)}")

    def _refs(self, ids: list[str]) -> list[_MediaLocator]:
        with self._state_lock:
            missing = [item_id for item_id in ids if item_id not in self._media_refs]
            refs = [self._media_refs[item_id] for item_id in ids
                    if item_id in self._media_refs]
        if missing:
            raise RoonError("Roon media reference expired or is unknown: "
                            + ", ".join(missing[:3]))
        return refs

    def replace_queue(self, zone_id: str, ids: list[str], start: int = 0) -> None:
        if not ids:
            raise ValueError("Roon queue cannot be replaced with no items")
        if not 0 <= start < len(ids):
            raise ValueError("Roon queue start is out of range")
        ordered = ids[start:]
        refs = self._refs(ordered)
        # One work item per placement is intentional. A long queue used to be
        # one uninterruptible _invoke: after the caller timed out the worker
        # silently kept rebuilding Browse paths, and Next/volume timed out
        # behind it. The generic avctl pump limits Roon to a one-track lead,
        # while these boundaries let priority transport run between tracks.
        self._invoke(
            "queue.replace.play",
            lambda api: self._action(api, refs[0], ("Play Now",)),
            priority=5,
        )
        for locator in refs[1:]:
            self._invoke(
                "queue.replace.append",
                lambda api, ref=locator: self._action(
                    api, ref, ("Queue", "Add Next")),
                priority=5,
            )
        with self._state_lock:
            self._queue_hidden.discard(zone_id)
            self._queue_hidden.discard(str(self.selected_output))

    def append_queue(self, zone_id: str, ids: list[str]) -> None:
        if not ids:
            return
        refs = self._refs(ids)
        with self._state_lock:
            hidden = (zone_id in self._queue_hidden
                      or self.selected_output in self._queue_hidden)
        offset = 0
        if hidden:
            # Roon has no public empty-queue request. Play Now is the only
            # supported operation that atomically discards the old queue;
            # pause immediately so append-to-an-empty-Q remains staged.
            def stage(api: Any) -> None:
                self._action(api, refs[0], ("Play Now",))
                api.playback_control(zone_id, "pause")
            self._invoke("queue.append.stage", stage, priority=5)
            offset = 1
        for locator in refs[offset:]:
            self._invoke(
                "queue.append",
                lambda api, ref=locator: self._action(
                    api, ref, ("Queue", "Add Next")),
                priority=5,
            )
        with self._state_lock:
            self._queue_hidden.discard(zone_id)
            self._queue_hidden.discard(str(self.selected_output))

    def clear_queue(self, zone_id: str) -> int:
        count = len(self.queue(zone_id))
        self.transport(zone_id, "stop")
        # Transport v2 exposes queue subscription and play_from_here, but no
        # remove/clear request. Mask the stopped physical queue until the next
        # Play Now replacement; this keeps every avctl read/control honest and
        # prevents Play from resurrecting it through RoonMusic.play_queue().
        with self._state_lock:
            self._queue_hidden.add(zone_id)
            if self.selected_output:
                self._queue_hidden.add(str(self.selected_output))
            self._revision += 1
        return count

    def transport(self, zone_id: str, control: str) -> None:
        allowed = {"play", "pause", "playpause", "stop", "previous", "next"}
        if control not in allowed:
            raise ValueError(f"unsupported Roon transport control: {control}")
        result = self._invoke(
            f"transport.{control}",
            lambda api: api.playback_control(zone_id, control),
            priority=0)
        if result is None:
            raise RoonError(f"Roon did not confirm transport.{control}")

    def set_shuffle(self, zone_id: str, enabled: bool) -> None:
        if self._invoke("shuffle", lambda api: api.shuffle(zone_id, enabled),
                        priority=0) is None:
            raise RoonError("Roon did not confirm shuffle")

    def set_repeat(self, zone_id: str, mode: str) -> None:
        if mode not in {"disabled", "loop", "loop_one"}:
            raise ValueError(f"unsupported Roon repeat mode: {mode}")
        if self._invoke("repeat", lambda api: api.repeat(zone_id, mode),
                        priority=0) is None:
            raise RoonError("Roon did not confirm repeat")

    def artwork(self, image_key: str, destination: Path) -> None:
        # get_image only constructs the Core URL. Do that tiny pyRoon access
        # on its owner thread, then release the worker before network I/O.
        # A slow cover can no longer block Next, volume, search, or Browse.
        url = self._invoke("artwork.url", lambda api: api.get_image(image_key),
                           priority=20)
        response = requests.get(url, timeout=self.timeout)
        response.raise_for_status()
        destination.write_bytes(response.content)

    def set_volume(self, output_id: str, level: float) -> float:
        output = self.output(output_id)
        if "volume.set" not in output.capabilities:
            raise RoonCapabilityError(f"{output.name} has fixed volume")
        maximum = output.volume.safety_max
        target = max(0.0, min(float(maximum if maximum is not None else 100),
                              float(level)))
        result = self._invoke(
            "volume.set", lambda api: api.set_volume_percent(output_id, target),
            priority=0)
        if result is None:
            raise RoonError("Roon did not confirm volume")
        return target

    def step_volume(self, output_id: str, delta: float) -> float:
        output = self.output(output_id)
        if output.volume.value is None:
            raise RoonCapabilityError(f"{output.name} has no volume readback")
        return self.set_volume(output_id, output.volume.value + float(delta))

    def set_muted(self, output_id: str, muted: bool) -> bool:
        output = self.output(output_id)
        if "mute.set" not in output.capabilities:
            raise RoonCapabilityError(f"{output.name} cannot mute")
        if self._invoke("mute", lambda api: api.mute(output_id, muted),
                        priority=0) is None:
            raise RoonError("Roon did not confirm mute")
        return muted

    def set_standby(self, output_id: str, standby: bool) -> bool:
        output = self.output(output_id)
        capability = "power.standby" if standby else "power.wake"
        if capability not in output.capabilities:
            raise RoonCapabilityError(f"{output.name} does not support {capability}")
        method = "standby" if standby else "convenience_switch"
        result = self._invoke(method, lambda api: getattr(api, method)(output_id),
                              priority=0)
        if result is None:
            raise RoonError(f"Roon did not confirm {method}")
        return True

    def select_input(self, output_id: str, input_name: str) -> bool:
        raise RoonCapabilityError(
            "Roon source controls are not generic DAC input selectors")
