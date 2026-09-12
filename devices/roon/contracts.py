"""Source-neutral state and commands shared by every Roon integration.

The public Roon libraries expose mutable dictionaries updated from callback
threads.  Nothing above this boundary is allowed to retain those dictionaries.
Adapters copy them into these immutable values and expose capabilities before
a panel or rack driver offers a control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class RoonError(RuntimeError):
    """A Roon command failed or the selected Core/zone disappeared."""


class RoonCapabilityError(RoonError):
    """The selected output honestly does not provide this operation."""


@dataclass(frozen=True)
class MediaItem:
    id: str
    title: str
    artist: str = ""
    album: str = ""
    duration: float | None = None
    image_key: str | None = None
    source: str = "library"
    kind: str = "track"
    added_at: float = 0.0

    def panel_dict(self) -> dict:
        """Compatibility projection for today's Music panel."""
        return {
            "pid": self.id,
            "name": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration": self.duration,
            "image_key": self.image_key,
            "source": self.source,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class VolumeState:
    mode: str = "fixed"  # number | db | incremental | fixed
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    muted: bool | None = None
    readback: str = "none"  # confirmed | estimated | none
    safety_max: float | None = None


@dataclass(frozen=True)
class OutputState:
    id: str
    name: str
    zone_id: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    volume: VolumeState = field(default_factory=VolumeState)
    standby: bool | None = None
    inputs: tuple[str, ...] = ()
    selected_input: str | None = None


@dataclass(frozen=True)
class ZoneState:
    id: str
    name: str
    state: str = "stopped"
    now_playing: MediaItem | None = None
    output_ids: tuple[str, ...] = ()
    shuffle: bool = False
    repeat: str = "disabled"
    queue_depth: int = 0
    revision: int = 0


class RoonController(Protocol):
    """The contract consumed by Music/DAC/amp drivers and fake/live adapters."""

    def zone(self, zone_id: str) -> ZoneState: ...
    def output(self, output_id: str) -> OutputState: ...
    def zones(self) -> list[ZoneState]: ...
    def outputs(self) -> list[OutputState]: ...
    def library(self) -> list[MediaItem]: ...
    def library_albums(self) -> list[MediaItem]: ...
    def recent(self) -> list[MediaItem]: ...
    def search(self, query: str) -> list[MediaItem]: ...
    def album(self, name: str, artist: str = "") -> list[MediaItem]: ...
    def playlists(self) -> list[dict]: ...
    def playlist(self, playlist_id: str) -> dict | None: ...
    def search_service(self, query: str, kinds: list[str],
                       limit: int = 8) -> list[MediaItem]: ...
    def search_service_many(self, queries: list[str], kinds: list[str],
                            limit: int = 8) -> list[list[MediaItem]]: ...
    def service_item(self, item_id: str) -> MediaItem: ...
    def service_album(self, item_id: str) -> dict: ...
    def service_tracks(self, kind: str, item_id: str) -> list[MediaItem]: ...
    def explore(self, limit: int = 10) -> list[dict]: ...
    def add_to_library(self, item_id: str) -> None: ...
    def queue(self, zone_id: str) -> list[MediaItem]: ...
    def replace_queue(self, zone_id: str, ids: list[str], start: int = 0) -> None:
        """Replace with ids[start:] and begin at its first item."""
        ...
    def append_queue(self, zone_id: str, ids: list[str]) -> None: ...
    def clear_queue(self, zone_id: str) -> int: ...
    def transport(self, zone_id: str, control: str) -> None: ...
    def set_shuffle(self, zone_id: str, enabled: bool) -> None: ...
    def set_repeat(self, zone_id: str, mode: str) -> None: ...
    def artwork(self, image_key: str, destination: Path) -> None: ...
    def set_volume(self, output_id: str, level: float) -> float: ...
    def step_volume(self, output_id: str, delta: float) -> float: ...
    def set_muted(self, output_id: str, muted: bool) -> bool: ...
    def set_standby(self, output_id: str, standby: bool) -> bool: ...
    def select_input(self, output_id: str, input_name: str) -> bool: ...
