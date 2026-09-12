"""The music source, as the panel and the dispatch see it.

`MusicSource` is the interface api/musiclink.py drives: transport, the
queue playlist, library reads, artwork, and the scene steps. The dispatch/
drip machinery sits entirely ABOVE this interface -- it paces calls to
play_tracks/queue_append/now_playing and never knows what implements them
-- which is what makes a future Roon driver (4.0) a config line rather
than a rewrite: implement this, inherit the queue behavior.

The panel's SHAPE is part of the contract: a library grid newest-first,
album views, search, a transport bar with a queue. A source that cannot do
one of these raises, and the surrounding honesty machinery (501s, dimmed
keys) says so rather than pretending.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

# Track persistent IDs are 16 hex chars for Apple Music; kept here because
# routes validate against it before ids reach any driver.
PID_RE = re.compile(r"^[0-9A-F]{16}$")
CATALOG_ID_RE = re.compile(r"^[0-9A-Za-z.\-]{1,128}$")
MEDIA_ID_RE = re.compile(r"^[0-9A-Za-z:._\-]{1,160}$")


class MusicError(RuntimeError):
    """The source failed, timed out, or refused the request."""


class MusicAuthorizationRequired(MusicError):
    """Playback was rejected before mutation because user consent is off."""


class MusicSource(ABC):
    # The user-visible name of the queue playlist, where one exists.
    queue_playlist: str = "queue"

    # The Music panel has two domains: the user's library and an optional
    # discovery/streaming service.  The API layer deliberately talks only to
    # these generic names.  AppleMusic maps them to MusicKit; RoonMusic maps
    # them to the Roon Browse hierarchy (including Qobuz).
    service_name: str = "Music service"
    service_source: str = "service"

    def service_info(self, credential: str | None = None) -> dict[str, Any]:
        """Describe the configured discovery service and its honest verbs."""
        return {
            "name": self.service_name,
            "source": self.service_source,
            "available": False,
            "authorized": False,
            "personalized": False,
            "can_stream_service": False,
            "can_add_to_library": False,
            "can_edit_playlists": False,
            "supports_date_added": False,
            "supports_play_history": False,
            "batched_search": False,
            "authorization_url": None,
        }

    def search_service_albums(self, term: str,
                              limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError("this source has no discovery service")

    def search_service(self, term: str, kinds: list[str],
                       limit: int = 8) -> list[dict[str, Any]]:
        raise NotImplementedError("this source has no discovery service")

    def search_service_many(self, terms: list[str], kinds: list[str],
                            limit: int = 8) -> list[list[dict[str, Any]]]:
        """Resolve several independent searches, preserving input order.

        The default keeps stateless providers simple. Stateful providers such
        as Roon can override this to reuse one remote Browse context.
        """
        return [self.search_service(term, kinds, limit) for term in terms]

    def personal_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Playable observed history not already encoded in library rows."""
        return []

    def service_album(self, item_id: str) -> dict[str, Any]:
        raise NotImplementedError("this source has no discovery service")

    def service_tracks(self, kind: str,
                       item_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError("this source has no discovery service")

    def explore_service(self, credential: str | None = None,
                        limit: int = 10) -> dict[str, Any]:
        return {"sections": [], "personalized": False,
                "personal_error": None}

    def add_service_item(self, kind: str, item_id: str,
                         credential: str | None = None,
                         metadata: dict[str, Any] | None = None) -> None:
        raise NotImplementedError(
            f"{self.service_name} does not support library mutation")

    def service_developer_token(self) -> str:
        raise NotImplementedError(
            f"{self.service_name} has no browser authorization flow")

    def queue_engine(self, service_item: bool) -> str:
        """Physical player used by one logical queue item."""
        return "catalog" if service_item else "library"

    def placement_batch_limit(self) -> int | None:
        """Maximum tracks one synchronous queue placement should contain.

        Fast local players can accept the generic configured batch. Stateful
        remote providers may return a smaller cap so the central queue pump
        yields between expensive mutations and transport remains responsive.
        """
        return None

    def stopped_queue_rescue_delay(self) -> float:
        """Seconds a stopped player may recover before avctl advances it."""
        return 0.0

    def valid_library_id(self, item_id: str) -> bool:
        """Whether an opaque id belongs to this provider's library domain.

        The default keeps Apple Music's long-standing persistent-id boundary;
        providers with their own opaque references override it.
        """
        return bool(PID_RE.fullmatch(str(item_id)))

    # -- reads ------------------------------------------------------------

    @abstractmethod
    def now_playing(self) -> dict[str, Any]:
        """Player state, current track, shuffle, repeat and queue depth."""

    @abstractmethod
    def library_order(self) -> list[str]:
        """Every track id, newest-added first."""

    @abstractmethod
    def recently_added(self) -> list[dict[str, Any]]:
        """Albums newest-first, the shelf the grid renders."""

    @abstractmethod
    def album_tracks(self, album: str, artist: str = "") -> list[dict[str, Any]]: ...

    @abstractmethod
    def search_library(self, query: str) -> list[dict[str, Any]]: ...

    # Concrete, not abstract: a source without playlists answers
    # NotImplementedError and the route says "coming soon" -- the designed
    # degradation, not a failure (see README, adding a music service).
    def playlists(self) -> list[dict[str, Any]]:
        raise NotImplementedError("this source has no playlists")

    def playlist_tracks(self, pid: str) -> dict[str, Any] | None:
        raise NotImplementedError("this source has no playlists")

    def queue_playlist_bulk(self, playlist_pid: str, start_pos: int,
                            replace: bool, play: bool) -> None:
        raise NotImplementedError("this source has no playlists")

    def add_tracks_to_playlist(self, name: str,
                               pids: list[str]) -> dict[str, Any]:
        """Create/append a user playlist without changing playback."""
        raise NotImplementedError("this source cannot edit playlists")

    def add_items_to_playlist(self, name: str,
                              tracks: list[dict[str, Any]]) -> dict[str, Any]:
        """Add verified rows to a playlist without touching playback.

        Local-only providers retain the persistent-id implementation. A
        streaming provider may override this seam to atomically bookmark
        service rows before attaching them to its provider-neutral playlist.
        """
        pids = [str(track.get("pid") or "") for track in tracks
                if isinstance(track, dict) and track.get("pid")]
        return self.add_tracks_to_playlist(name, pids)

    # -- transport --------------------------------------------------------

    @abstractmethod
    def play_pause(self) -> None: ...

    def play(self) -> None:
        """Resume without toggling; optional for intent-bearing controls."""
        raise NotImplementedError("this source cannot explicitly resume")

    def pause(self) -> None:
        """Pause without toggling; optional for intent-bearing controls."""
        raise NotImplementedError("this source cannot explicitly pause")

    @abstractmethod
    def next_track(self) -> None: ...

    @abstractmethod
    def previous_track(self) -> None: ...

    @abstractmethod
    def set_shuffle(self, on: bool) -> None: ...

    @abstractmethod
    def set_repeat_one(self, on: bool) -> None: ...

    # -- the queue --------------------------------------------------------

    @abstractmethod
    def play_tracks(self, pids: list[str], start: int = 0) -> None:
        """Rebuild the queue from these ids and play."""

    @abstractmethod
    def queue_append(self, pids: list[str]) -> None: ...

    def play_catalog(self, catalog_ids: list[str]) -> None:
        """Replace playback with configured-service songs."""
        raise NotImplementedError("this source cannot stream catalog songs")

    def queue_catalog(self, catalog_ids: list[str]) -> None:
        """Append configured-service songs to active service playback."""
        raise NotImplementedError("this source cannot stream catalog songs")

    @abstractmethod
    def play_queue(self) -> None:
        """Play the queue from the top -- the stopped-with-a-queue case."""

    @abstractmethod
    def play_whole_library(self) -> None:
        """The shuffle-everything fast path."""

    @abstractmethod
    def clear_queue(self) -> int:
        """Stop and empty; returns how many tracks were dropped."""

    @abstractmethod
    def quiesce(self) -> int:
        """Pause and empty the queue; the everything-off scene's step."""

    # -- the rest of the panel -------------------------------------------

    @abstractmethod
    def extract_artwork(self, pid: str, dest: Path) -> None: ...

    # Optional: these three are really about the MACHINE the source runs
    # on (the mini's screen and output volume), which a networked source
    # like Roon would not own. Leave them alone and the keys dim.

    def show_player(self) -> None:
        """The music scene's screen step: the big player on the TV."""
        raise NotImplementedError("this source has no player screen to show")

    def set_system_volume(self, level: int) -> None:
        """The machine's own output level -- the knob the user rides."""
        raise NotImplementedError("this source does not own an output volume")

    def set_system_muted(self, on: bool) -> None:
        raise NotImplementedError("this source does not own an output volume")


from devices.music.apple_music import AppleMusic, MusicKit  # noqa: E402
from devices.music.roon import RoonMusic  # noqa: E402

__all__ = [
    "AppleMusic", "CATALOG_ID_RE", "MEDIA_ID_RE", "MusicError", "MusicKit",
    "MusicSource", "PID_RE", "RoonMusic",
]
