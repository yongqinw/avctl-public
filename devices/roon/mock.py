"""Deterministic fake Roon Core used to build panels without Roon hardware."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import threading

from devices.roon.contracts import (
    MediaItem,
    OutputState,
    RoonCapabilityError,
    RoonError,
    VolumeState,
    ZoneState,
)


class MockRoonController:
    """One in-memory Core with library, Qobuz, queues, zones and outputs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revision = 1
        self._items = self._fixture_items()
        self._queues: dict[str, list[str]] = {
            "living-room": [item.id for item in self._items[:36]],
        }
        self._cursor = {"living-room": 0}
        self._zones = {
            "living-room": ZoneState(
                id="living-room",
                name="Living Room",
                state="playing",
                now_playing=self._items[0],
                output_ids=("roon-ready",),
                queue_depth=36,
                revision=self._revision,
            )
        }
        self._outputs = {
            "roon-ready": OutputState(
                id="roon-ready",
                name="Mock Roon Ready Amp",
                zone_id="living-room",
                capabilities=frozenset({
                    "volume.set", "volume.step", "mute.set",
                    "power.standby", "power.wake", "input.select",
                }),
                volume=VolumeState(
                    mode="number", value=42, minimum=0, maximum=100,
                    step=1, muted=False, readback="confirmed",
                ),
                standby=False,
                inputs=("roon", "usb", "optical"),
                selected_input="roon",
            ),
            "usb-dac": OutputState(
                id="usb-dac",
                name="Mock Fixed USB DAC",
                zone_id="living-room",
                capabilities=frozenset(),
                volume=VolumeState(mode="fixed"),
            ),
            "incremental": OutputState(
                id="incremental",
                name="Mock Incremental Endpoint",
                zone_id="living-room",
                capabilities=frozenset({"volume.step", "mute.set"}),
                volume=VolumeState(
                    mode="incremental", step=1, muted=False,
                    readback="none",
                ),
            ),
        }
        self._playlists = [{
            "pid": "mock-playlist-happy",
            "name": "Happy Mix",
            "count": 12,
        }]

    @staticmethod
    def _fixture_items() -> list[MediaItem]:
        items = []
        for index in range(40):
            source = "library" if index % 2 == 0 else "qobuz"
            items.append(MediaItem(
                id=f"mock:{source}:{index:02d}",
                title=f"Mock Track {index + 1}",
                artist="Fixture Artist" if source == "library" else "Qobuz Artist",
                album=f"Mock Album {index // 5 + 1}",
                duration=180 + index,
                image_key=f"mock-image-{index // 5}",
                source=source,
                added_at=float(10_000 - index),
            ))
        return items

    def _bump(self, zone_id: str) -> None:
        self._revision += 1
        queue = self._queues[zone_id]
        cursor = self._cursor[zone_id]
        playing = self._find(queue[cursor]) if queue and cursor < len(queue) else None
        state = self._zones[zone_id]
        self._zones[zone_id] = replace(
            state,
            now_playing=playing,
            queue_depth=max(0, len(queue) - cursor),
            revision=self._revision,
        )

    def _find(self, item_id: str) -> MediaItem:
        for item in self._items:
            if item.id == item_id:
                return item
        raise RoonError(f"Roon item not found: {item_id}")

    def _need(self, output_id: str, capability: str) -> OutputState:
        output = self.output(output_id)
        if capability not in output.capabilities:
            raise RoonCapabilityError(
                f"{output.name} does not support {capability}")
        return output

    def _zone_key(self, target: str) -> str:
        if target in self._zones:
            return target
        output = self._outputs.get(target)
        if output is not None and output.zone_id in self._zones:
            return output.zone_id
        raise RoonError(f"Roon zone/output not found: {target}")

    def zone(self, zone_id: str) -> ZoneState:
        with self._lock:
            zone_id = self._zone_key(zone_id)
            try:
                return self._zones[zone_id]
            except KeyError:
                raise RoonError(f"Roon zone not found: {zone_id}") from None

    def output(self, output_id: str) -> OutputState:
        with self._lock:
            try:
                return self._outputs[output_id]
            except KeyError:
                raise RoonError(f"Roon output not found: {output_id}") from None

    def zones(self) -> list[ZoneState]:
        with self._lock:
            return list(self._zones.values())

    def outputs(self) -> list[OutputState]:
        with self._lock:
            return list(self._outputs.values())

    def library(self) -> list[MediaItem]:
        return [item for item in self._items if item.source == "library"]

    def library_albums(self) -> list[MediaItem]:
        albums: dict[tuple[str, str], MediaItem] = {}
        for item in self.library():
            key = (item.album, item.artist)
            current = albums.get(key)
            if current is None or item.added_at > current.added_at:
                albums[key] = MediaItem(
                    id=self._album_id(item.album, item.artist),
                    title=item.album,
                    artist=item.artist,
                    image_key=item.image_key,
                    source="library",
                    kind="album",
                    added_at=item.added_at,
                )
        return sorted(
            albums.values(), key=lambda item: item.added_at, reverse=True)

    def recent(self) -> list[MediaItem]:
        return sorted(self.library(), key=lambda item: item.added_at, reverse=True)

    def search(self, query: str) -> list[MediaItem]:
        terms = query.casefold().split()
        return [item for item in self._items if all(
            term in f"{item.title} {item.artist} {item.album}".casefold()
            for term in terms
        )]

    def album(self, name: str, artist: str = "") -> list[MediaItem]:
        return [item for item in self._items
                if item.album == name and (not artist or item.artist == artist)]

    def playlists(self) -> list[dict]:
        return [dict(row) for row in self._playlists]

    def playlist(self, playlist_id: str) -> dict | None:
        if playlist_id != "mock-playlist-happy":
            return None
        return {"name": "Happy Mix", "tracks": [
            item.panel_dict() for item in self._items[:12]
        ]}

    def search_service(self, query: str, kinds: list[str],
                       limit: int = 8) -> list[MediaItem]:
        hits = [item for item in self.search(query) if item.source == "qobuz"]
        out: list[MediaItem] = []
        if "songs" in kinds:
            out.extend(hits)
        if "albums" in kinds:
            seen: set[tuple[str, str]] = set()
            for item in hits:
                key = (item.album, item.artist)
                if key in seen:
                    continue
                seen.add(key)
                out.append(MediaItem(
                    id=self._album_id(item.album, item.artist), title=item.album,
                    artist=item.artist, image_key=item.image_key,
                    source="qobuz", kind="album"))
        if "playlists" in kinds and "happy" in query.casefold():
            out.append(MediaItem(
                id="mock-playlist-happy", title="Happy Mix",
                source="qobuz", kind="playlist"))
        return out[:max(1, int(limit))]

    def search_service_many(self, queries: list[str], kinds: list[str],
                            limit: int = 8) -> list[list[MediaItem]]:
        return [self.search_service(query, kinds, limit) for query in queries]

    def service_item(self, item_id: str) -> MediaItem:
        try:
            item = self._find(item_id)
        except RoonError:
            if item_id == "mock-playlist-happy":
                return MediaItem(id=item_id, title="Happy Mix",
                                 source="qobuz", kind="playlist")
            try:
                album = self.service_album(item_id)
            except RoonError:
                raise RoonError(
                    f"Roon service item not found: {item_id}") from None
            return MediaItem(
                id=item_id, title=str(album.get("album") or ""),
                artist=str(album.get("artist") or ""),
                image_key=album.get("image_key"), source="qobuz", kind="album")
        return item

    def service_album(self, item_id: str) -> dict:
        groups: dict[str, list[MediaItem]] = {}
        for item in self._items:
            if item.source == "qobuz":
                groups.setdefault(self._album_id(item.album, item.artist), []).append(item)
        tracks = groups.get(item_id) or []
        if not tracks:
            raise RoonError(f"Roon album not found: {item_id}")
        name = tracks[0].album
        return {"id": item_id, "album": name,
                "artist": tracks[0].artist if tracks else "",
                "image_key": tracks[0].image_key if tracks else None,
                "tracks": [item.panel_dict() for item in tracks]}

    @staticmethod
    def _album_id(name: str, artist: str) -> str:
        digest = hashlib.sha256(f"{artist}\0{name}".encode()).hexdigest()[:16]
        return f"mock:album:{digest}"

    def service_tracks(self, kind: str, item_id: str) -> list[MediaItem]:
        if kind == "album":
            ids = {str(row["pid"]) for row in
                   self.service_album(item_id).get("tracks") or []}
            return [item for item in self._items if item.id in ids]
        if kind == "playlist":
            playlist = self.playlist(item_id)
            if playlist is None:
                return []
            ids = {str(row["pid"]) for row in playlist.get("tracks") or []}
            return [item for item in self._items if item.id in ids]
        raise ValueError("Roon service container must be an album or playlist")

    def explore(self, limit: int = 10) -> list[dict]:
        items = [item for item in self._items if item.source == "qobuz"]
        return [{"id": "mock-qobuz", "title": "Qobuz Discoveries",
                 "items": items[:max(1, int(limit))]}]

    def add_to_library(self, item_id: str) -> None:
        with self._lock:
            item = self._find(item_id)
            if item.source == "library":
                return
            self._items.append(replace(
                item, id=item.id + ":library", source="library",
                added_at=max((row.added_at for row in self._items), default=0) + 1))

    def queue(self, zone_id: str) -> list[MediaItem]:
        with self._lock:
            zone_id = self._zone_key(zone_id)
            return [self._find(item_id) for item_id in self._queues[zone_id]]

    def replace_queue(self, zone_id: str, ids: list[str], start: int = 0) -> None:
        if not ids:
            raise ValueError("Roon queue cannot be replaced with no items")
        if not 0 <= start < len(ids):
            raise ValueError("Roon queue start is out of range")
        with self._lock:
            zone_id = self._zone_key(zone_id)
            for item_id in ids:
                self._find(item_id)
            self._queues[zone_id] = list(ids[start:])
            self._cursor[zone_id] = 0
            self._zones[zone_id] = replace(self._zones[zone_id], state="playing")
            self._bump(zone_id)

    def append_queue(self, zone_id: str, ids: list[str]) -> None:
        with self._lock:
            zone_id = self._zone_key(zone_id)
            for item_id in ids:
                self._find(item_id)
            self._queues[zone_id].extend(ids)
            self._bump(zone_id)

    def clear_queue(self, zone_id: str) -> int:
        with self._lock:
            zone_id = self._zone_key(zone_id)
            count = len(self._queues[zone_id])
            self._queues[zone_id] = []
            self._cursor[zone_id] = 0
            self._zones[zone_id] = replace(self._zones[zone_id], state="stopped")
            self._bump(zone_id)
            return count

    def transport(self, zone_id: str, control: str) -> None:
        with self._lock:
            zone_id = self._zone_key(zone_id)
            zone = self._zones[zone_id]
            if control in {"play", "pause", "stop"}:
                state = {"play": "playing", "pause": "paused", "stop": "stopped"}[control]
                self._zones[zone_id] = replace(zone, state=state)
            elif control in {"next", "previous"}:
                direction = 1 if control == "next" else -1
                end = max(0, len(self._queues[zone_id]) - 1)
                self._cursor[zone_id] = min(end, max(0, self._cursor[zone_id] + direction))
            elif control == "playpause":
                self._zones[zone_id] = replace(
                    zone, state="paused" if zone.state == "playing" else "playing")
            else:
                raise ValueError(f"unsupported Roon transport control: {control}")
            self._bump(zone_id)

    def set_shuffle(self, zone_id: str, enabled: bool) -> None:
        with self._lock:
            zone_id = self._zone_key(zone_id)
            self._zones[zone_id] = replace(self._zones[zone_id], shuffle=enabled)
            self._bump(zone_id)

    def set_repeat(self, zone_id: str, mode: str) -> None:
        if mode not in {"disabled", "loop", "loop_one"}:
            raise ValueError(f"unsupported Roon repeat mode: {mode}")
        with self._lock:
            zone_id = self._zone_key(zone_id)
            self._zones[zone_id] = replace(self._zones[zone_id], repeat=mode)
            self._bump(zone_id)

    def artwork(self, image_key: str, destination: Path) -> None:
        destination.write_bytes((f"mock-roon-artwork:{image_key}").encode())

    def set_volume(self, output_id: str, level: float) -> float:
        with self._lock:
            output = self._need(output_id, "volume.set")
            volume = output.volume
            assert volume.minimum is not None and volume.maximum is not None
            value = max(volume.minimum, min(volume.maximum, float(level)))
            self._outputs[output_id] = replace(
                output, volume=replace(volume, value=value))
            return value

    def step_volume(self, output_id: str, delta: float) -> float:
        with self._lock:
            output = self._need(output_id, "volume.step")
            if output.volume.value is None:
                raise RoonCapabilityError(
                    f"{output.name} has no absolute volume readback")
            return self.set_volume(output_id, output.volume.value + delta)

    def set_muted(self, output_id: str, muted: bool) -> bool:
        with self._lock:
            output = self._need(output_id, "mute.set")
            self._outputs[output_id] = replace(
                output, volume=replace(output.volume, muted=muted))
            return muted

    def set_standby(self, output_id: str, standby: bool) -> bool:
        capability = "power.standby" if standby else "power.wake"
        with self._lock:
            output = self._need(output_id, capability)
            changed = output.standby is not standby
            self._outputs[output_id] = replace(output, standby=standby)
            return changed

    def select_input(self, output_id: str, input_name: str) -> bool:
        with self._lock:
            output = self._need(output_id, "input.select")
            if input_name not in output.inputs:
                raise ValueError(
                    f"{input_name!r} is not one of {list(output.inputs)}")
            changed = output.selected_input != input_name
            self._outputs[output_id] = replace(output, selected_input=input_name)
            return changed
