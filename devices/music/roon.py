"""Experimental MusicSource projection over the normalized Roon adapter.

This makes the driver shape executable against the fake Core while the live
queue/browse capability matrix is measured.  It deliberately does not reach
into pyRoon private requests to imitate unsupported queue operations.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Any

from devices.config import driver_block
from devices.music import MusicError, MusicSource
from devices.roon.contracts import RoonController
from devices.roon.factory import controller_from_config
from devices.music.virtual_library import SavedItem, VirtualMusicLibrary


class RoonMusic(MusicSource):
    queue_playlist = "Roon zone queue"
    service_name = "Roon / Qobuz"
    service_source = "roon"

    def __init__(self, controller: RoonController, zone_id: str,
                 output_id: str = "",
                 library: VirtualMusicLibrary | None = None,
                 disconnect_grace_seconds: float = 30.0):
        self.controller = controller
        self.zone_id = zone_id
        self.output_id = output_id
        self.virtual_library = library
        self.disconnect_grace_seconds = max(
            0.0, min(120.0, float(disconnect_grace_seconds)))
        self._artwork: dict[str, str] = {}
        self._last_recorded_playback: tuple[str, str, str] | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "RoonMusic":
        mine = driver_block(config, "music", "RoonMusic")
        roon = config.get("roon") or {}
        zone_id = str(mine.get("zone_id") or roon.get("zone_id")
                      or mine.get("output_id") or roon.get("output_id") or "")
        if not zone_id:
            raise ValueError(
                "music.RoonMusic needs a zone_id or output_id")
        output_id = str(mine.get("output_id") or roon.get("output_id") or "")
        library = VirtualMusicLibrary(
            mine.get("library_file") or "~/.avctl/roon_library.sqlite3")
        grace = mine.get("disconnect_grace_seconds", 30)
        return cls(controller_from_config(config), zone_id, output_id, library,
                   disconnect_grace_seconds=float(grace))

    @staticmethod
    def _identity(item: SavedItem | Any) -> tuple[str, str, str]:
        title = getattr(item, "title", "")
        artist = getattr(item, "artist", "")
        album = getattr(item, "album", "")
        return (str(title).casefold(), str(artist).casefold(),
                str(album).casefold())

    def _saved_row(self, item: SavedItem) -> dict[str, Any]:
        if item.image_key:
            self._artwork[item.id] = item.image_key
        return {**item.panel_dict(), "source": "library",
                "provider": item.provider}

    @staticmethod
    def _track_data(item: Any, *, album: str = "") -> dict[str, Any]:
        if isinstance(item, dict):
            return {
                "title": item.get("title") or item.get("name") or "",
                "artist": item.get("artist") or "",
                "album": item.get("album") or album,
                "duration": item.get("duration"),
                "image_key": item.get("image_key"),
                "isrc": item.get("isrc") or "",
                "upc": item.get("upc") or "",
                "metadata": {"source": item.get("source") or "qobuz"},
            }
        return {
            "title": item.title, "artist": item.artist,
            "album": item.album or album, "duration": item.duration,
            "image_key": item.image_key,
            "metadata": {"source": item.source},
        }

    def _service_item(self, item_id: str) -> Any:
        return self.controller.service_item(item_id)

    def _track_with_album(self, item: Any) -> Any:
        """Resolve the containing Qobuz album before saving a loose track.

        Roon's Tracks search rows normally expose a title, a long credit roll,
        and artwork, but omit the album field.  The virtual library cannot
        project an album tile from an empty album, so inspect a few album hits
        and accept only one whose real track list contains this recording.
        This runs only for an explicit library save, never on a foreground
        library read or playback command.
        """
        if str(getattr(item, "album", "") or "").strip():
            return item
        title = str(getattr(item, "title", "") or "").strip()
        artist = str(getattr(item, "artist", "") or "").strip()
        lead_artist = artist.split(",", 1)[0].strip()
        if not title:
            return item
        query = " ".join(value for value in (title, lead_artist) if value)
        try:
            albums = self.controller.search_service(query, ["albums"], 20)
        except (MusicError, RuntimeError, ValueError):
            return item
        image_key = str(getattr(item, "image_key", "") or "")
        if image_key:
            cover_match = next(
                (album for album in albums
                 if str(getattr(album, "image_key", "") or "") == image_key),
                None,
            )
            if cover_match is None and title and title != query:
                try:
                    title_albums = self.controller.search_service(
                        title, ["albums"], 50)
                except (MusicError, RuntimeError, ValueError):
                    title_albums = []
                cover_match = next(
                    (album for album in title_albums
                     if str(getattr(album, "image_key", "") or "") == image_key),
                    None,
                )
            if cover_match is None and lead_artist:
                try:
                    artist_albums = self.controller.search_service(
                        lead_artist, ["albums"], 100)
                except (MusicError, RuntimeError, ValueError):
                    artist_albums = []
                cover_match = next(
                    (album for album in artist_albums
                     if str(getattr(album, "image_key", "") or "") == image_key),
                    None,
                )
            if cover_match is not None:
                return replace(
                    item, album=str(cover_match.title or ""),
                    image_key=image_key)
        for album in albums:
            try:
                tracks = self.controller.service_tracks("album", album.id)
            except (MusicError, RuntimeError, ValueError):
                continue
            match = next((track for track in tracks
                          if str(track.title).casefold() == title.casefold()
                          and (not lead_artist or lead_artist.casefold() in
                               str(track.artist).casefold())), None)
            if match is None:
                continue
            return replace(
                item,
                album=str(album.title or getattr(match, "album", "") or ""),
                image_key=(getattr(item, "image_key", None)
                           or getattr(album, "image_key", None)
                           or getattr(match, "image_key", None)),
            )
        return item

    @staticmethod
    def _candidate_score(saved: SavedItem, candidate: Any) -> float:
        title = str(getattr(candidate, "title", "")).casefold()
        artist = str(getattr(candidate, "artist", "")).casefold()
        album = str(getattr(candidate, "album", "")).casefold()
        if title != saved.title.casefold():
            return -1
        score = 8.0
        if saved.artist and artist == saved.artist.casefold():
            score += 4
        elif saved.artist and saved.artist.casefold() not in artist:
            score -= 3
        if saved.album and album == saved.album.casefold():
            score += 3
        saved_image = str(saved.image_key or "")
        candidate_image = str(getattr(candidate, "image_key", "") or "")
        if saved_image and candidate_image == saved_image:
            # Search rows commonly omit the album and expand contributor
            # credits differently between calls. Roon's artwork key still
            # identifies the exact Qobuz release, so it is a stronger tie
            # breaker than those presentation fields.
            score += 5
        duration = getattr(candidate, "duration", None)
        if saved.duration is not None and duration is not None:
            score += 2 if abs(float(saved.duration) - float(duration)) <= 3 else -2
        return score

    def _find_service_match(self, saved: SavedItem, kind: str) -> Any:
        query = " ".join(value for value in (saved.title, saved.artist) if value)
        candidates = self.controller.search_service(
            query, ["songs" if kind == "track" else kind + "s"], 20)
        ranked = sorted(
            ((self._candidate_score(saved, item), item) for item in candidates),
            key=lambda pair: pair[0], reverse=True)
        if not ranked or ranked[0][0] < 8:
            raise MusicError(
                f"Qobuz can no longer resolve {saved.title} — {saved.artist}")
        if (len(ranked) > 1 and ranked[1][0] == ranked[0][0]
                and ranked[0][1].id != ranked[1][1].id):
            raise MusicError(
                f"Qobuz returned ambiguous copies of {saved.title} — "
                f"{saved.artist}")
        return ranked[0][1]

    def _heal_collection(self, collection_id: str) -> None:
        if self.virtual_library is None:
            raise MusicError("Roon virtual library is not configured")
        collection = self.virtual_library.get(collection_id)
        if collection is None:
            raise MusicError(f"virtual library collection disappeared: {collection_id}")
        match = self._find_service_match(collection, collection.kind)
        if collection.kind == "album":
            detail = self.controller.service_album(match.id)
            live_tracks = list(detail.get("tracks") or [])
        else:
            live_tracks = [row.panel_dict() for row in
                           self.controller.service_tracks("playlist", match.id)]
        saved_tracks = self.virtual_library.collection_tracks(collection_id)
        unused = list(range(len(live_tracks)))
        updates: list[tuple[str, str, str | None]] = []
        for position, saved in enumerate(saved_tracks):
            ranked: list[tuple[float, int, dict[str, Any]]] = []
            for index in unused:
                row = live_tracks[index]
                proxy = type("Candidate", (), {
                    "title": row.get("title") or row.get("name") or "",
                    "artist": row.get("artist") or "",
                    "album": row.get("album") or collection.title,
                    "duration": row.get("duration"),
                })()
                score = self._candidate_score(saved, proxy)
                if index == position:
                    score += 0.5
                ranked.append((score, index, row))
            ranked.sort(key=lambda value: value[0], reverse=True)
            if not ranked or ranked[0][0] < 8:
                self.virtual_library.mark_failure(
                    saved.id, f"track missing while repairing {collection.title}")
                continue
            _, index, row = ranked[0]
            unused.remove(index)
            provider_id = str(row.get("pid") or row.get("id")
                              or row.get("catalog_id") or "")
            if provider_id:
                updates.append((saved.id, provider_id, row.get("image_key")))
        if not updates:
            self.virtual_library.mark_failure(
                collection.id, "no tracks could be repaired")
            raise MusicError(f"Qobuz could not repair {collection.title}")
        self.virtual_library.update_bindings(updates)
        self.virtual_library.update_binding(
            collection.id, match.id, image_key=match.image_key)

    def _resolve_saved(self, saved: SavedItem) -> str:
        try:
            self._service_item(saved.provider_item_id)
            return saved.provider_item_id
        except (RuntimeError, ValueError):
            pass
        try:
            parent = (self.virtual_library.parent(saved.id)
                      if self.virtual_library else None)
            collection_id = saved.collection_id or (parent.id if parent else "")
            if collection_id:
                self._heal_collection(collection_id)
                refreshed = (self.virtual_library.get(saved.id)
                             if self.virtual_library else None)
                if refreshed is not None:
                    self._service_item(refreshed.provider_item_id)
                    return refreshed.provider_item_id
            match = self._find_service_match(saved, "track")
            assert self.virtual_library is not None
            self.virtual_library.update_binding(
                saved.id, match.id, image_key=match.image_key)
            return match.id
        except (MusicError, RuntimeError, ValueError) as exc:
            if self.virtual_library is not None:
                self.virtual_library.mark_failure(saved.id, str(exc),
                                                  ambiguous="ambiguous" in str(exc))
            raise MusicError(str(exc)) from exc

    def _playback_ids(self, ids: list[str]) -> list[str]:
        if self.virtual_library is None:
            return ids
        resolved: list[str] = []
        for item_id in ids:
            saved = self.virtual_library.get(item_id)
            resolved.append(self._resolve_saved(saved) if saved else item_id)
        return resolved

    def _rows(self, items: list[Any]) -> list[dict[str, Any]]:
        rows = []
        for item in items:
            if item.image_key:
                self._artwork[item.id] = item.image_key
            rows.append(item.panel_dict())
        return rows

    def _output(self) -> str:
        if self.output_id:
            return self.output_id
        zone = self.controller.zone(self.zone_id)
        if not zone.output_ids:
            raise MusicError(f"Roon zone {zone.name} has no output")
        return zone.output_ids[0]

    def now_playing(self) -> dict[str, Any]:
        zone = self.controller.zone(self.zone_id)
        item = zone.now_playing
        answer: dict[str, Any] = {
            "state": zone.state,
            "shuffle": zone.shuffle,
            "repeat": zone.repeat,
            "queued": zone.queue_depth,
            "zone_id": zone.id,
            "zone_name": zone.name,
            "revision": zone.revision,
        }
        if item is not None:
            if item.image_key:
                self._artwork[item.id] = item.image_key
            answer.update(item.panel_dict())
            # Today's state/transport projection reads the Apple-era names;
            # provide them during migration while retaining the generic row.
            answer.update({
                "track": item.title,
                "artist": item.artist,
                "album": item.album,
                "duration": item.duration,
                "art": item.image_key,
            })
            identity = self._identity(item)
            if (zone.state == "playing" and self.virtual_library is not None
                    and identity != self._last_recorded_playback):
                try:
                    self.virtual_library.record_play(
                        item.title, item.artist, item.album)
                    binding = (item.id if not item.id.startswith(
                        ("roon-now:", "roon-queue:")) else "")
                    self.virtual_library.record_observed_play(
                        item.source or "roon", binding,
                        self._track_data(item))
                except (OSError, sqlite3.Error):
                    # History is enrichment. A read-only/unavailable history
                    # database must never break transport state or the panel.
                    pass
                self._last_recorded_playback = identity
        try:
            output = self.controller.output(self._output())
            answer.update(volume=output.volume.value,
                          muted=output.volume.muted)
        except (MusicError, RuntimeError, ValueError):
            pass
        return answer

    def library_order(self) -> list[str]:
        virtual = ([item.id for item in self.virtual_library.tracks()]
                   if self.virtual_library else [])
        return virtual + [item.id for item in self.controller.recent()
                          if item.id not in virtual]

    def recently_added(self) -> list[dict[str, Any]]:
        albums = []
        seen: set[tuple[str, str]] = set()
        if self.virtual_library is not None:
            for item in self.virtual_library.album_summaries():
                key = (item.title.casefold(), item.artist.casefold())
                seen.add(key)
                if item.image_key:
                    self._artwork[item.id] = item.image_key
                albums.append({
                    "album": item.title, "artist": item.artist,
                    "pid": item.id, "image_key": item.image_key,
                    "count": len(self.virtual_library.album_tracks(
                        item.title, item.artist)), "source": "library",
                    "provider": item.provider,
                })
        for item in self.controller.library_albums():
            key = (item.title.casefold(), item.artist.casefold())
            if key in seen:
                continue
            seen.add(key)
            if item.image_key:
                self._artwork[item.id] = item.image_key
            albums.append({
                "album": item.title,
                "artist": item.artist,
                "pid": item.id,
                "image_key": item.image_key,
                "count": None,
                "source": item.source,
            })
        return albums

    def recently_added_songs(self) -> list[dict[str, Any]]:
        virtual = ([self._saved_row(item) for item in self.virtual_library.tracks()]
                   if self.virtual_library else [])
        seen = {self._identity(item) for item in
                (self.virtual_library.tracks() if self.virtual_library else [])}
        native = self.controller.recent()
        return virtual + [row for item, row in zip(native, self._rows(native))
            if self._identity(item) not in seen]

    def album_tracks(self, album: str, artist: str = "") -> list[dict[str, Any]]:
        saved = (self.virtual_library.album_tracks(album, artist)
                 if self.virtual_library else [])
        rows = [self._saved_row(item) for item in saved]
        seen = {self._identity(item) for item in saved}
        native = self.controller.album(album, artist)
        rows.extend(row for item, row in zip(native, self._rows(native))
                    if self._identity(item) not in seen)
        return rows

    def search_library(self, query: str) -> list[dict[str, Any]]:
        terms = query.casefold().split()
        items = [item for item in self.controller.library() if all(
            term in f"{item.title} {item.artist} {item.album}".casefold()
            for term in terms)]
        saved = self.virtual_library.search(query) if self.virtual_library else []
        rows = [self._saved_row(item) for item in saved]
        seen = {self._identity(item) for item in saved}
        rows.extend(row for item, row in zip(items, self._rows(items))
                    if self._identity(item) not in seen)
        return rows

    def personal_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return observed Roon plays as verified, directly playable rows."""
        if self.virtual_library is None:
            return []
        cap = min(100, max(1, int(limit)))
        history = self.virtual_library.playback_history(cap)
        if not history:
            return []
        saved_by_identity = {
            self._identity(item): item for item in self.virtual_library.tracks()
        }
        native = self.controller.library()
        native_by_identity = {self._identity(item): item for item in native}
        resolved: dict[int, dict[str, Any]] = {}
        missing: list[tuple[int, dict[str, Any], SavedItem]] = []

        def ranked_row(row: dict[str, Any], values: dict[str, Any]
                       ) -> dict[str, Any]:
            return {
                **values,
                "plays": int(row.get("plays") or 0),
                "lastPlayed": float(row.get("lastPlayed") or 0),
                "firstPlayed": float(row.get("firstPlayed") or 0),
            }

        for index, row in enumerate(history):
            identity = (str(row.get("name") or "").casefold(),
                        str(row.get("artist") or "").casefold(),
                        str(row.get("album") or "").casefold())
            saved = saved_by_identity.get(identity)
            if saved is not None:
                resolved[index] = ranked_row(row, self._saved_row(saved))
                continue
            local = native_by_identity.get(identity)
            if local is not None:
                resolved[index] = ranked_row(row, local.panel_dict())
                continue
            binding = str(row.get("provider_item_id") or "")
            if binding:
                try:
                    item = self._service_item(binding)
                except (MusicError, RuntimeError, ValueError):
                    item = None
                if item is not None:
                    resolved[index] = ranked_row(row, {
                        **item.panel_dict(), "pid": None,
                        "catalog_id": item.id, "source": "service",
                    })
                    continue
            proxy = SavedItem(
                id=str(row.get("history_id") or ""), kind="track",
                provider=str(row.get("provider") or "roon"),
                provider_item_id=binding,
                title=str(row.get("name") or ""),
                artist=str(row.get("artist") or ""),
                album=str(row.get("album") or ""),
                duration=row.get("duration"), image_key=row.get("image_key"),
            )
            missing.append((index, row, proxy))

        # Roon's zone state has display metadata but no stable playback ID.
        # Resolve only unbound observations, in one serialized batch, then
        # persist those handles so later personal-history requests are warm.
        if missing:
            queries = [" ".join(value for value in (
                proxy.title, proxy.artist.split(",", 1)[0]) if value)
                for _index, _row, proxy in missing]
            try:
                groups = self.controller.search_service_many(
                    queries, ["songs"], 5)
            except (MusicError, RuntimeError, ValueError):
                groups = [[] for _ in missing]
            for (index, row, proxy), candidates in zip(missing, groups):
                ranked = sorted(
                    ((self._candidate_score(proxy, item), item)
                     for item in candidates),
                    key=lambda pair: pair[0], reverse=True)
                if not ranked or ranked[0][0] < 8:
                    continue
                item = ranked[0][1]
                self.virtual_library.update_history_binding(
                    str(row.get("history_id") or ""), item.id)
                resolved[index] = ranked_row(row, {
                    **item.panel_dict(), "pid": None,
                    "catalog_id": item.id, "source": "service",
                })
        return [resolved[index] for index in sorted(resolved)][:cap]

    def playlists(self) -> list[dict[str, Any]]:
        virtual = []
        if self.virtual_library is not None:
            virtual = [{"pid": item.id, "name": item.title,
                        "count": len(self.virtual_library.collection_tracks(item.id)),
                        "source": "library", "provider": item.provider}
                       for item in self.virtual_library.playlists()]
        seen = {str(row["name"]).casefold() for row in virtual}
        return virtual + [row for row in self.controller.playlists()
                          if str(row.get("name") or "").casefold() not in seen]

    def playlist_tracks(self, pid: str) -> dict[str, Any] | None:
        if self.virtual_library is not None:
            item = self.virtual_library.get(pid)
            if item is not None and item.kind == "playlist":
                return {"name": item.title, "tracks": [
                    self._saved_row(row)
                    for row in self.virtual_library.collection_tracks(pid)
                ]}
        return self.controller.playlist(pid)

    def queue_playlist_bulk(self, playlist_pid: str, start_pos: int,
                            replace: bool, play: bool) -> None:
        playlist = self.playlist_tracks(playlist_pid)
        if playlist is None:
            raise MusicError(f"Roon playlist not found: {playlist_pid}")
        ids = [str(row["pid"]) for row in playlist.get("tracks", [])]
        offset = max(0, int(start_pos) - 1)
        ids = ids[offset:]
        if not ids:
            raise MusicError("the selected Roon playlist position is empty")
        ids = self._playback_ids(ids)
        if replace:
            self.controller.replace_queue(self.zone_id, ids)
        else:
            self.controller.append_queue(self.zone_id, ids)
        if not play:
            self.controller.transport(self.zone_id, "pause")

    def add_tracks_to_playlist(self, name: str,
                               pids: list[str]) -> dict[str, Any]:
        if self.virtual_library is None:
            raise MusicError("Roon virtual library is not configured")
        result = self.virtual_library.add_tracks_to_playlist(name, pids)
        if not result.get("total"):
            raise MusicError(
                "Roon playlists can currently contain saved avctl library tracks only")
        return result

    def add_items_to_playlist(self, name: str,
                              tracks: list[dict[str, Any]]) -> dict[str, Any]:
        """Atomically save verified Qobuz rows into an avctl playlist."""
        if self.virtual_library is None:
            raise MusicError("Roon virtual library is not configured")
        existing: list[str] = []
        imports: list[dict[str, Any]] = []
        for row in tracks:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("pid") or "")
            if pid and self.virtual_library.get(pid) is not None:
                existing.append(pid)
                continue
            binding = str(row.get("catalog_id") or row.get("id") or pid)
            if not binding:
                continue
            try:
                item = self._service_item(binding)
            except (MusicError, RuntimeError, ValueError) as exc:
                raise MusicError(
                    f"Qobuz could not reverify {row.get('name') or 'a selected track'}"
                ) from exc
            data = self._track_data(item)
            # The selection row came from this driver and may contain album
            # metadata omitted by Roon's track action-list item.
            data["album"] = data.get("album") or row.get("album") or ""
            data["image_key"] = (data.get("image_key")
                                 or self._artwork.get(binding))
            imports.append({**data, "provider_item_id": binding})
        try:
            result = self.virtual_library.add_provider_tracks_to_playlist(
                name, "qobuz", imports, existing)
        except ValueError as exc:
            raise MusicError(str(exc)) from exc
        if not result.get("total"):
            raise MusicError("no verified tracks were added to the Roon playlist")
        return result

    def play_pause(self) -> None:
        self.controller.transport(self.zone_id, "playpause")

    def play(self) -> None:
        self.controller.transport(self.zone_id, "play")

    def pause(self) -> None:
        self.controller.transport(self.zone_id, "pause")

    def next_track(self) -> None:
        self.controller.transport(self.zone_id, "next")

    def previous_track(self) -> None:
        self.controller.transport(self.zone_id, "previous")

    def set_shuffle(self, on: bool) -> None:
        self.controller.set_shuffle(self.zone_id, on)

    def set_repeat_one(self, on: bool) -> None:
        self.controller.set_repeat(self.zone_id, "loop_one" if on else "disabled")

    def play_tracks(self, pids: list[str], start: int = 0) -> None:
        self.controller.replace_queue(self.zone_id, self._playback_ids(pids), start)

    def queue_append(self, pids: list[str]) -> None:
        self.controller.append_queue(self.zone_id, self._playback_ids(pids))

    def play_catalog(self, catalog_ids: list[str]) -> None:
        # A Qobuz item is already a Roon browse item. Streaming it does not
        # require and must not imply adding it to the Roon library.
        self.play_tracks(catalog_ids)

    def queue_catalog(self, catalog_ids: list[str]) -> None:
        self.queue_append(catalog_ids)

    def play_queue(self) -> None:
        if not self.controller.queue(self.zone_id):
            raise MusicError("the selected Roon zone queue is empty")
        self.play()

    def play_whole_library(self) -> None:
        ids = self.library_order()
        if not ids:
            raise MusicError("the Roon library is empty")
        self.play_tracks(ids)

    def clear_queue(self) -> int:
        return self.controller.clear_queue(self.zone_id)

    def quiesce(self) -> int:
        self.pause()
        return self.clear_queue()

    def extract_artwork(self, pid: str, dest: Path) -> None:
        image_key = self._artwork.get(pid)
        if image_key is None and self.virtual_library is not None:
            saved = self.virtual_library.get(pid)
            image_key = saved.image_key if saved is not None else None
        if image_key is None:
            item = next((row for row in self.controller.library()
                         if row.id == pid), None)
            image_key = item.image_key if item is not None else None
        if not image_key:
            raise MusicError(f"no artwork for Roon item {pid}")
        self.controller.artwork(image_key, dest)

    # -- generic discovery-service contract -----------------------------

    def service_info(self, credential: str | None = None) -> dict[str, Any]:
        return {
            "name": self.service_name,
            "source": self.service_source,
            "available": True,
            "authorized": True,
            "personalized": True,
            "can_stream_service": True,
            # Qobuz Browse does not expose a dependable account-library
            # mutation, so avctl owns explicit bookmarks in its local virtual
            # library. Direct Play/Queue never enters this path.
            "can_add_to_library": self.virtual_library is not None,
            "library_kind": "avctl virtual library",
            "can_edit_playlists": self.virtual_library is not None,
            "supports_date_added": "virtual_library",
            "supports_play_history": "observed_playback",
            "batched_search": True,
            "authorization_url": None,
        }

    def queue_engine(self, service_item: bool) -> str:
        # Library and Qobuz are both native Roon browse items feeding the
        # same zone queue, so mixed curation can be materialized in one pass.
        return "roon"

    def placement_batch_limit(self) -> int | None:
        # Each Roon track rebuilds a stateful Browse recipe. Let the generic
        # queue pump place one, yield, and continue in the background instead
        # of turning a button press into an uninterruptible multi-track job.
        return 1

    def stopped_queue_rescue_delay(self) -> float:
        # A Qobuz buffer failure briefly reports the zone as stopped. The
        # generic queue rescue used to treat that as a finished track and
        # immediately replace it with the logical tail. Let Roon reconnect
        # first; a persistent stop still advances after this bounded grace.
        return self.disconnect_grace_seconds

    def valid_library_id(self, item_id: str) -> bool:
        from devices.music import MEDIA_ID_RE
        return bool(MEDIA_ID_RE.fullmatch(str(item_id)))

    @staticmethod
    def _service_kind(kind: str) -> str:
        return "song" if kind == "track" else kind

    def search_service_albums(self, term: str,
                              limit: int = 50) -> list[dict[str, Any]]:
        items = self.controller.search_service(term, ["albums"], limit)
        self._rows(items)
        return [{
            "id": item.id, "album": item.title, "artist": item.artist,
            "art": f"/api/music/artwork/{item.id}" if item.image_key else "",
            "tracks": None, "year": "",
        } for item in items]

    def search_service(self, term: str, kinds: list[str],
                       limit: int = 8) -> list[dict[str, Any]]:
        return self.search_service_many([term], kinds, limit)[0]

    def search_service_many(self, terms: list[str], kinds: list[str],
                            limit: int = 8) -> list[list[dict[str, Any]]]:
        answers: list[list[dict[str, Any]]] = []
        for items in self.controller.search_service_many(terms, kinds, limit):
            self._rows(items)
            answers.append([{
                "kind": self._service_kind(item.kind),
                "id": item.id,
                "name": item.title,
                "artist": item.artist,
                "album": item.album,
                "art": (f"/api/music/artwork/{item.id}"
                        if item.image_key else ""),
            } for item in items])
        return answers

    def service_album(self, item_id: str) -> dict[str, Any]:
        album = self.controller.service_album(item_id)
        tracks = []
        for row in album.get("tracks") or []:
            pid = str(row.get("pid") or "")
            image_key = row.get("image_key")
            if pid and image_key:
                self._artwork[pid] = str(image_key)
            tracks.append({
                **row, "id": pid, "catalog_id": pid,
            })
        image_key = album.get("image_key")
        if image_key:
            self._artwork[item_id] = str(image_key)
        return {
            **album,
            "art": f"/api/music/artwork/{item_id}" if image_key else "",
            "year": "",
            "tracks": tracks,
        }

    def service_tracks(self, kind: str,
                       item_id: str) -> list[dict[str, Any]]:
        items = self.controller.service_tracks(kind, item_id)
        rows = self._rows(items)
        return [{
            **{key: value for key, value in row.items() if key != "pid"},
            "catalog_id": row.get("pid"),
            "id": row.get("pid"),
            "art": (f"/api/music/artwork/{row.get('pid')}"
                    if row.get("image_key") else ""),
        } for row in rows]

    def explore_service(self, credential: str | None = None,
                        limit: int = 10) -> dict[str, Any]:
        sections = []
        for section in self.controller.explore(limit):
            items = section.get("items") or []
            self._rows(items)
            sections.append({
                "id": section.get("id"),
                "title": section.get("title"),
                "kind": "service",
                "items": [{
                    "source": "service",
                    "kind": self._service_kind(item.kind),
                    "id": item.id,
                    "name": item.title,
                    "artist": item.artist,
                    "album": item.album,
                    "art": (f"/api/music/artwork/{item.id}"
                            if item.image_key else ""),
                } for item in items],
            })
        return {"sections": sections, "personalized": True,
                "personal_error": None}

    def add_service_item(self, kind: str, item_id: str,
                         credential: str | None = None,
                         metadata: dict[str, Any] | None = None) -> None:
        if self.virtual_library is None:
            raise MusicError("Roon virtual library is not configured")
        singular = {"song": "track", "songs": "track", "album": "album",
                    "albums": "album", "playlist": "playlist",
                    "playlists": "playlist"}.get(kind)
        if singular is None:
            raise ValueError("service item must be a song, album, or playlist")
        if singular == "track":
            item = self._service_item(item_id)
            data = self._track_data(item)
            hint = metadata if isinstance(metadata, dict) else {}
            hint_title = str(hint.get("name") or hint.get("title") or "").strip()
            hint_artist = str(hint.get("artist") or "").strip()
            hint_album = str(hint.get("album") or "").strip()

            def normalized(value: str) -> str:
                return "".join(value.casefold().split())

            title_matches = (
                hint_title and normalized(hint_title) == normalized(
                    str(data.get("title") or "")))
            actual_lead = str(data.get("artist") or "").split(",", 1)[0]
            hint_lead = hint_artist.split(",", 1)[0]
            artist_matches = (
                not hint_lead
                or normalized(hint_lead) == normalized(actual_lead)
            )
            if (not str(data.get("album") or "").strip()
                    and hint_album and title_matches and artist_matches):
                # The hint is the already verified result returned by this
                # driver moments earlier.  Reusing its album avoids several
                # stateful Roon Browse searches per saved track.
                data["album"] = hint_album
                data["image_key"] = (
                    data.get("image_key") or self._artwork.get(item_id))
            elif not str(data.get("album") or "").strip():
                data = self._track_data(self._track_with_album(item))
            self.virtual_library.add_track(
                "qobuz", item_id, data)
            return
        item = self._service_item(item_id)
        tracks = self.controller.service_tracks(singular, item_id)
        self.virtual_library.add_collection(
            "qobuz", singular, item_id,
            {"title": item.title, "artist": item.artist,
             "album": item.album, "image_key": item.image_key,
             "metadata": {"source": item.source}},
            [{**self._track_data(track, album=item.title),
              "provider_item_id": track.id} for track in tracks],
        )

    def repair_virtual_library(self, *, all_items: bool = False
                               ) -> dict[str, Any]:
        """Deterministically refresh stale Qobuz handles from saved metadata."""
        if self.virtual_library is None:
            raise MusicError("Roon virtual library is not configured")
        repaired: list[str] = []
        failed: list[dict[str, str]] = []
        collections = {
            item.id: item for item in (
                self.virtual_library.albums() + self.virtual_library.playlists())
            if all_items or item.status != "ready"
        }
        for collection in collections.values():
            try:
                self._heal_collection(collection.id)
                repaired.append(collection.id)
            except (MusicError, RuntimeError, ValueError) as exc:
                failed.append({"id": collection.id, "error": str(exc)})
        for item in self.virtual_library.tracks():
            if not all_items and item.status == "ready":
                continue
            parent = self.virtual_library.parent(item.id)
            if parent is not None and parent.id in collections:
                continue
            try:
                self._resolve_saved(item)
                repaired.append(item.id)
            except (MusicError, RuntimeError, ValueError) as exc:
                failed.append({"id": item.id, "error": str(exc)})
        return {"repaired": repaired, "failed": failed,
                "remaining": self.virtual_library.repair_report()}

    def backfill_virtual_library_albums(self) -> dict[str, Any]:
        """Resolve album metadata missing from older loose-track saves.

        Older builds persisted Roon's track search row directly. Qobuz often
        omits the album from that row, so those songs could never project an
        album tile even after their transient playback binding was repaired.
        Re-resolve only albumless tracks and persist the containing album;
        playback-valid rows that still cannot be identified remain untouched.
        """
        if self.virtual_library is None:
            raise MusicError("Roon virtual library is not configured")
        initial = [item for item in self.virtual_library.tracks()
                   if not item.album.strip()]
        repaired = self.virtual_library.backfill_track_albums_from_artwork()
        failed: list[dict[str, str]] = []
        candidates = [item for item in self.virtual_library.tracks()
                      if not item.album.strip()]
        for saved in candidates:
            try:
                # The saved artwork is already a release identity. Resolve
                # its album before re-resolving the track: duplicate tracks
                # across deluxe/remastered editions are otherwise ambiguous.
                resolved = self._track_with_album(saved)
                match = None
                if not str(getattr(resolved, "album", "") or "").strip():
                    match = self._find_service_match(saved, "track")
                    resolved = self._track_with_album(match)
                album = str(getattr(resolved, "album", "") or "").strip()
                if not album:
                    raise MusicError(
                        f"Qobuz did not identify an album for {saved.title}")
                self.virtual_library.update_track_album(
                    saved.id, album,
                    provider_item_id=(match.id if match is not None else None),
                    image_key=(getattr(resolved, "image_key", None)
                               or (getattr(match, "image_key", None)
                                   if match is not None else None)),
                )
                repaired.append(saved.id)
            except (MusicError, RuntimeError, ValueError) as exc:
                failed.append({"id": saved.id, "title": saved.title,
                               "error": str(exc)})
        repaired.extend(
            item_id for item_id in
            self.virtual_library.backfill_track_albums_from_artwork()
            if item_id not in repaired)
        return {"repaired": repaired, "failed": failed,
                "examined": len(initial)}

    def set_system_volume(self, level: int) -> None:
        self.controller.set_volume(self._output(), level)

    def set_system_muted(self, on: bool) -> None:
        self.controller.set_muted(self._output(), on)
