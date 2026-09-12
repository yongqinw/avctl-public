"""Durable provider-neutral bookmarks for streaming music.

Roon's Browse item keys are scoped to a live browse session.  They are good
playback handles, but they are not a library identity and disappear when the
service restarts.  This store owns stable avctl ids and keeps every provider
binding as replaceable metadata.  A driver can therefore heal a stale binding
without changing the item the user saved.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('album','track','playlist')),
    provider TEXT NOT NULL,
    provider_item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    isrc TEXT NOT NULL DEFAULT '',
    upc TEXT NOT NULL DEFAULT '',
    duration REAL,
    image_key TEXT,
    added_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(provider, kind, provider_item_id)
);
CREATE INDEX IF NOT EXISTS items_recent ON items(added_at DESC);
CREATE INDEX IF NOT EXISTS items_identity
ON items(provider, kind, isrc, upc, title, artist);
CREATE TABLE IF NOT EXISTS collection_tracks (
    collection_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    track_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY(collection_id, position),
    UNIQUE(collection_id, track_id)
);
CREATE INDEX IF NOT EXISTS collection_tracks_track
ON collection_tracks(track_id);
CREATE TABLE IF NOT EXISTS playback_history (
    identity TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_item_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    duration REAL,
    image_key TEXT,
    plays INTEGER NOT NULL DEFAULT 0,
    first_played REAL NOT NULL,
    last_played REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS playback_history_rank
ON playback_history(plays DESC, last_played DESC);
"""


@dataclass(frozen=True)
class SavedItem:
    id: str
    kind: str
    provider: str
    provider_item_id: str
    title: str
    artist: str = ""
    album: str = ""
    isrc: str = ""
    upc: str = ""
    duration: float | None = None
    image_key: str | None = None
    added_at: float = 0.0
    updated_at: float = 0.0
    status: str = "ready"
    error: str = ""
    metadata: dict[str, Any] | None = None
    collection_id: str = ""
    position: int = 0

    def panel_dict(self) -> dict[str, Any]:
        metadata = self.metadata or {}
        return {
            "pid": self.id,
            "name": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration": self.duration,
            "image_key": self.image_key,
            "source": self.provider,
            "kind": "song" if self.kind == "track" else self.kind,
            "isrc": self.isrc,
            "upc": self.upc,
            "added": self.added_at * 1000,
            "plays": max(0, int(metadata.get("plays") or 0)),
            "lastPlayed": max(0, float(metadata.get("last_played") or 0)),
            "favorited": metadata.get("favorited") is True,
            "library_status": self.status,
            "collection_id": self.collection_id or None,
        }


def _stable_id(provider: str, kind: str, provider_item_id: str) -> str:
    identity = "\0".join((provider.casefold(), kind, provider_item_id))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"avlib:{kind}:{digest}"


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


class VirtualMusicLibrary:
    """Small SQLite library with atomic writes and stable avctl identities."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        configured = path or os.environ.get(
            "AVCTL_ROON_LIBRARY_FILE", "~/.avctl/roon_library.sqlite3")
        self.path = Path(configured).expanduser()
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return connection

    @staticmethod
    def _item(row: sqlite3.Row, *, collection_id: str = "",
              position: int = 0) -> SavedItem:
        return SavedItem(
            id=str(row["id"]), kind=str(row["kind"]),
            provider=str(row["provider"]),
            provider_item_id=str(row["provider_item_id"]),
            title=str(row["title"]), artist=str(row["artist"]),
            album=str(row["album"]), isrc=str(row["isrc"]),
            upc=str(row["upc"]), duration=row["duration"],
            image_key=row["image_key"], added_at=float(row["added_at"]),
            updated_at=float(row["updated_at"]), status=str(row["status"]),
            error=str(row["error"]), metadata=_metadata(row["metadata_json"]),
            collection_id=collection_id, position=position,
        )

    @staticmethod
    def _values(provider: str, kind: str, provider_item_id: str,
                data: dict[str, Any], now: float) -> tuple[Any, ...]:
        return (
            _stable_id(provider, kind, provider_item_id), kind, provider,
            provider_item_id, str(data.get("title") or data.get("name") or ""),
            str(data.get("artist") or ""), str(data.get("album") or ""),
            str(data.get("isrc") or "").upper(),
            str(data.get("upc") or data.get("barcode") or ""),
            data.get("duration"), data.get("image_key"), now, now,
            "ready", "", json.dumps(data.get("metadata") or {},
                                      ensure_ascii=False, separators=(",", ":")),
        )

    def _upsert(self, connection: sqlite3.Connection, provider: str,
                kind: str, provider_item_id: str, data: dict[str, Any],
                now: float) -> str:
        title = str(data.get("title") or data.get("name") or "")
        artist = str(data.get("artist") or "")
        album = str(data.get("album") or "")
        # Roon Browse ids are hashes of a context recipe, not catalog ids.
        # The recipe can change after a restart or catalog re-licensing. If
        # the same Qobuz item comes back under a new handle, retain the stable
        # avctl id instead of adding a duplicate library row.
        existing = connection.execute(
            "SELECT id,provider_item_id,duration FROM items WHERE provider=? "
            "AND kind=? AND title=? COLLATE NOCASE AND artist=? COLLATE NOCASE "
            "AND album=? COLLATE NOCASE ORDER BY added_at LIMIT 1",
            (provider, kind, title, artist, album),
        ).fetchone()
        if existing is not None:
            old_duration = existing["duration"]
            new_duration = data.get("duration")
            same_duration = (kind != "track" or old_duration is None
                             or new_duration is None
                             or abs(float(old_duration) - float(new_duration)) <= 3)
            if same_duration:
                collision = connection.execute(
                    "SELECT id FROM items WHERE provider=? AND kind=? "
                    "AND provider_item_id=?",
                    (provider, kind, provider_item_id),
                ).fetchone()
                if collision is None or collision["id"] == existing["id"]:
                    connection.execute(
                        "UPDATE items SET provider_item_id=?,updated_at=? "
                        "WHERE id=?",
                        (provider_item_id, now, existing["id"]),
                    )
        values = self._values(provider, kind, provider_item_id, data, now)
        connection.execute(
            "INSERT INTO items "
            "(id,kind,provider,provider_item_id,title,artist,album,isrc,upc,"
            "duration,image_key,added_at,updated_at,status,error,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider,kind,provider_item_id) DO UPDATE SET "
            "title=excluded.title,artist=excluded.artist,album=excluded.album,"
            "isrc=CASE WHEN excluded.isrc<>'' THEN excluded.isrc ELSE items.isrc END,"
            "upc=CASE WHEN excluded.upc<>'' THEN excluded.upc ELSE items.upc END,"
            "duration=COALESCE(excluded.duration,items.duration),"
            "image_key=COALESCE(excluded.image_key,items.image_key),"
            "updated_at=excluded.updated_at,status='ready',error='',"
            "metadata_json=excluded.metadata_json",
            values,
        )
        row = connection.execute(
            "SELECT id FROM items WHERE provider=? AND kind=? "
            "AND provider_item_id=?", (provider, kind, provider_item_id),
        ).fetchone()
        assert row is not None
        return str(row["id"])

    def add_track(self, provider: str, provider_item_id: str,
                  data: dict[str, Any]) -> str:
        now = time.time()
        with self._lock, self._connect() as connection:
            return self._upsert(
                connection, provider, "track", provider_item_id, data, now)

    def add_collection(self, provider: str, kind: str, provider_item_id: str,
                       data: dict[str, Any],
                       tracks: Iterable[dict[str, Any]]) -> str:
        if kind not in {"album", "playlist"}:
            raise ValueError("virtual collection must be an album or playlist")
        now = time.time()
        with self._lock, self._connect() as connection:
            collection_id = self._upsert(
                connection, provider, kind, provider_item_id, data, now)
            connection.execute(
                "DELETE FROM collection_tracks WHERE collection_id=?",
                (collection_id,),
            )
            for position, track in enumerate(tracks, 1):
                binding = str(track.get("provider_item_id")
                              or track.get("id") or track.get("pid") or "")
                if not binding:
                    continue
                normalized = {
                    **track,
                    "title": track.get("title") or track.get("name") or "",
                    "album": track.get("album") or data.get("title")
                             or data.get("album") or "",
                    "image_key": track.get("image_key") or data.get("image_key"),
                }
                track_id = self._upsert(
                    connection, provider, "track", binding, normalized, now)
                connection.execute(
                    "INSERT INTO collection_tracks "
                    "(collection_id,track_id,position) VALUES(?,?,?)",
                    (collection_id, track_id, position),
                )
            return collection_id

    def add_tracks_to_playlist(self, name: str,
                               track_ids: Iterable[str]) -> dict[str, Any]:
        """Create or append an avctl-owned playlist of existing saved tracks.

        A virtual playlist is deliberately only an organizer: it references
        tracks already present in this store and never imports, queues, or
        starts anything. Repeating the same request is idempotent.
        """
        title = str(name or "").strip()
        if not title or len(title) > 100:
            raise ValueError("playlist name must contain 1 to 100 characters")
        if any(ord(char) < 32 for char in title):
            raise ValueError("playlist name contains control characters")
        requested = list(dict.fromkeys(str(item_id) for item_id in track_ids
                                       if str(item_id)))
        if not requested:
            raise ValueError("nothing to add to the playlist")
        with self._lock, self._connect() as connection:
            requested = [track_id for track_id in requested
                         if connection.execute(
                             "SELECT 1 FROM items WHERE id=? AND kind='track'",
                             (track_id,),
                         ).fetchone() is not None]
            if not requested:
                raise ValueError(
                    "playlist tracks must already be in the avctl library")
            return self._add_tracks_to_playlist(
                connection, title, requested, time.time())

    def add_provider_tracks_to_playlist(
        self,
        name: str,
        provider: str,
        tracks: Iterable[dict[str, Any]],
        existing_track_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Atomically bookmark service tracks and attach them to a playlist."""
        title = str(name or "").strip()
        if not title or len(title) > 100:
            raise ValueError("playlist name must contain 1 to 100 characters")
        if any(ord(char) < 32 for char in title):
            raise ValueError("playlist name contains control characters")
        normalized = []
        seen_bindings: set[str] = set()
        for track in tracks:
            if not isinstance(track, dict):
                continue
            binding = str(track.get("provider_item_id") or track.get("id")
                          or track.get("catalog_id") or "")
            if not binding or binding in seen_bindings:
                continue
            seen_bindings.add(binding)
            normalized.append((binding, dict(track)))
        existing = list(dict.fromkeys(
            str(item_id) for item_id in existing_track_ids if str(item_id)))
        if not normalized and not existing:
            raise ValueError("nothing to add to the playlist")
        now = time.time()
        with self._lock, self._connect() as connection:
            requested = [item_id for item_id in existing
                         if connection.execute(
                             "SELECT 1 FROM items WHERE id=? AND kind='track'",
                             (item_id,),
                         ).fetchone() is not None]
            imported_ids = [self._upsert(
                connection, provider, "track", binding, data, now)
                for binding, data in normalized]
            requested.extend(imported_ids)
            result = self._add_tracks_to_playlist(
                connection, title, list(dict.fromkeys(requested)), now)
            result["imported"] = len(set(imported_ids))
            return result

    def _add_tracks_to_playlist(
        self,
        connection: sqlite3.Connection,
        title: str,
        requested: list[str],
        now: float,
    ) -> dict[str, Any]:
        """Attach existing rows using the caller's open transaction."""
        provider_id = "local:" + hashlib.sha256(
            title.casefold().encode()).hexdigest()[:24]
        existing = connection.execute(
            "SELECT * FROM items WHERE kind='playlist' AND provider='avctl' "
            "AND title=? COLLATE NOCASE ORDER BY added_at LIMIT 1",
            (title,),
        ).fetchone()
        created = existing is None
        if existing is None:
            playlist_id = self._upsert(
                connection, "avctl", "playlist", provider_id,
                {"title": title, "metadata": {"local": True}}, now)
        else:
            playlist_id = str(existing["id"])
        present = {str(row["track_id"]) for row in connection.execute(
            "SELECT track_id FROM collection_tracks WHERE collection_id=?",
            (playlist_id,),
        ).fetchall()}
        position = int(connection.execute(
            "SELECT COALESCE(MAX(position),0) FROM collection_tracks "
            "WHERE collection_id=?", (playlist_id,),
        ).fetchone()[0])
        added = 0
        for track_id in requested:
            if track_id in present:
                continue
            position += 1
            connection.execute(
                "INSERT INTO collection_tracks "
                "(collection_id,track_id,position) VALUES(?,?,?)",
                (playlist_id, track_id, position),
            )
            present.add(track_id)
            added += 1
        total = int(connection.execute(
            "SELECT COUNT(*) FROM collection_tracks WHERE collection_id=?",
            (playlist_id,),
        ).fetchone()[0])
        return {"name": title, "created": created, "added": added,
                "total": total, "pid": playlist_id}

    def get(self, item_id: str) -> SavedItem | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._item(row) if row is not None else None

    def albums(self) -> list[SavedItem]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM items WHERE kind='album' "
                "ORDER BY added_at DESC, id"
            ).fetchall()
        return [self._item(row) for row in rows]

    def playlists(self) -> list[SavedItem]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM items WHERE kind='playlist' "
                "ORDER BY added_at DESC, id"
            ).fetchall()
        return [self._item(row) for row in rows]

    def album_summaries(self) -> list[SavedItem]:
        """Saved album collections plus albums implied by loose tracks.

        Saving one Qobuz song must make it visible in both the Songs shelf
        and the album-oriented Library grid. Tracks owned by a saved album
        must never infer more covers: Qobuz can put composers and collaborators
        into each track's artist field, which otherwise splits one album into
        a cover per contributor combination. Truly loose tracks use artwork as
        their strongest available album identity.
        """
        albums = self.albums()
        with self._lock, self._connect() as connection:
            owned = {str(row["track_id"]) for row in connection.execute(
                "SELECT DISTINCT ct.track_id FROM collection_tracks ct "
                "JOIN items c ON c.id=ct.collection_id "
                "WHERE c.kind='album'"
            ).fetchall()}
        seen: set[tuple[str, ...]] = set()
        for item in albums:
            seen.add(("cover", item.image_key) if item.image_key else (
                "metadata", item.title.casefold(), item.artist.casefold()))
        for track in self.tracks():
            if not track.album or track.id in owned:
                continue
            key = (("cover", track.image_key) if track.image_key else
                   ("metadata", track.album.casefold(),
                    track.artist.casefold()))
            if key in seen:
                continue
            seen.add(key)
            albums.append(SavedItem(
                id=track.id, kind="album", provider=track.provider,
                provider_item_id=track.provider_item_id, title=track.album,
                artist=track.artist, album=track.album, isrc=track.isrc,
                upc=track.upc, duration=track.duration,
                image_key=track.image_key, added_at=track.added_at,
                updated_at=track.updated_at, status=track.status,
                error=track.error, metadata=track.metadata,
            ))
        return sorted(albums, key=lambda item: item.added_at, reverse=True)

    def tracks(self) -> list[SavedItem]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT i.*, COALESCE(ct.collection_id,'') AS collection_id, "
                "COALESCE(ct.position,0) AS position FROM items i "
                "LEFT JOIN collection_tracks ct ON ct.track_id=i.id "
                "WHERE i.kind='track' ORDER BY i.added_at DESC, ct.position, i.id"
            ).fetchall()
        seen: set[str] = set()
        answer: list[SavedItem] = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(str(row["id"]))
            answer.append(self._item(
                row, collection_id=str(row["collection_id"]),
                position=int(row["position"])))
        return answer

    def collection_tracks(self, collection_id: str) -> list[SavedItem]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT i.*, ct.position FROM collection_tracks ct "
                "JOIN items i ON i.id=ct.track_id "
                "WHERE ct.collection_id=? ORDER BY ct.position",
                (collection_id,),
            ).fetchall()
        return [self._item(row, collection_id=collection_id,
                           position=int(row["position"])) for row in rows]

    def album_tracks(self, album: str, artist: str = "") -> list[SavedItem]:
        # A saved album collection owns its track order and album-artist
        # identity. Its individual Qobuz tracks may list writers/producers in
        # `artist`, so filtering those rows by the collection artist loses most
        # or all of the album.
        collection_query = (
            "SELECT id FROM items WHERE kind='album' "
            "AND title=? COLLATE NOCASE "
        )
        collection_args: list[Any] = [album]
        if artist:
            collection_query += "AND artist=? COLLATE NOCASE "
            collection_args.append(artist)
        collection_query += "ORDER BY added_at DESC, id LIMIT 1"
        with self._lock, self._connect() as connection:
            collection = connection.execute(
                collection_query, collection_args).fetchone()
        if collection is not None:
            return self.collection_tracks(str(collection["id"]))

        query = (
            "SELECT i.*, COALESCE(ct.collection_id,'') AS collection_id, "
            "COALESCE(ct.position,0) AS position FROM items i "
            "LEFT JOIN collection_tracks ct ON ct.track_id=i.id "
            "WHERE i.kind='track' AND i.album=? COLLATE NOCASE "
        )
        args: list[Any] = [album]
        query += "ORDER BY ct.position, i.added_at, i.id"
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, args).fetchall()

        # For an inferred loose-track cover, the representative artist is
        # only a display fallback. Use its artwork to collect sibling tracks
        # from the same album instead of requiring every contributor string
        # to be identical. With no artwork, preserve the old exact boundary
        # so unrelated albums sharing a title do not collapse together.
        if artist:
            anchors = [row for row in rows
                       if str(row["artist"]).casefold() == artist.casefold()]
            images = {str(row["image_key"]) for row in anchors
                      if row["image_key"]}
            rows = ([row for row in rows
                     if row["image_key"] and str(row["image_key"]) in images]
                    if images else anchors)
        seen: set[str] = set()
        answer: list[SavedItem] = []
        for row in rows:
            item_id = str(row["id"])
            if item_id in seen:
                continue
            seen.add(item_id)
            answer.append(self._item(
                row, collection_id=str(row["collection_id"]),
                position=int(row["position"])))
        return answer

    def parent(self, track_id: str) -> SavedItem | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT c.* FROM collection_tracks ct "
                "JOIN items c ON c.id=ct.collection_id "
                "WHERE ct.track_id=? ORDER BY c.added_at DESC LIMIT 1",
                (track_id,),
            ).fetchone()
        return self._item(row) if row is not None else None

    def search(self, query: str) -> list[SavedItem]:
        terms = [term.casefold() for term in query.split() if term]
        return [item for item in self.tracks() if all(
            term in f"{item.title} {item.artist} {item.album}".casefold()
            for term in terms)]

    def update_binding(self, item_id: str, provider_item_id: str,
                       *, image_key: str | None = None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE items SET provider_item_id=?,"
                "image_key=COALESCE(?,image_key),updated_at=?,status='ready',"
                "error='' WHERE id=?",
                (provider_item_id, image_key, time.time(), item_id),
            )

    def update_track_album(self, item_id: str, album: str, *,
                           provider_item_id: str | None = None,
                           image_key: str | None = None) -> None:
        """Persist a repaired album projection for one loose saved track."""
        title = str(album or "").strip()
        if not title:
            raise ValueError("track album cannot be empty")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT kind FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None or row["kind"] != "track":
                raise ValueError("album metadata can only update a saved track")
            connection.execute(
                "UPDATE items SET album=?,"
                "provider_item_id=COALESCE(?,provider_item_id),"
                "image_key=COALESCE(?,image_key),updated_at=?,"
                "status='ready',error='' WHERE id=?",
                (title, provider_item_id, image_key, time.time(), item_id),
            )

    def backfill_track_albums_from_artwork(self) -> list[str]:
        """Propagate an unambiguous known album across one release cover."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT image_key,CASE WHEN kind='album' THEN title ELSE album END "
                "AS album FROM items WHERE image_key IS NOT NULL "
                "AND trim(image_key)<>'' AND ((kind='album' AND trim(title)<>'') "
                "OR (kind='track' AND trim(album)<>''))"
            ).fetchall()
            albums_by_cover: dict[str, dict[str, str]] = {}
            for row in rows:
                cover = str(row["image_key"])
                album = str(row["album"]).strip()
                albums_by_cover.setdefault(cover, {})[
                    album.casefold()] = album
            repaired: list[str] = []
            now = time.time()
            for cover, names in albums_by_cover.items():
                if len(names) != 1:
                    continue
                album = next(iter(names.values()))
                targets = connection.execute(
                    "SELECT id FROM items WHERE kind='track' "
                    "AND trim(album)='' AND image_key=?", (cover,)
                ).fetchall()
                if not targets:
                    continue
                connection.execute(
                    "UPDATE items SET album=?,updated_at=? WHERE kind='track' "
                    "AND trim(album)='' AND image_key=?", (album, now, cover)
                )
                repaired.extend(str(row["id"]) for row in targets)
        return repaired

    def update_bindings(self, updates: Iterable[tuple[str, str, str | None]]) -> None:
        now = time.time()
        with self._lock, self._connect() as connection:
            for item_id, provider_item_id, image_key in updates:
                connection.execute(
                    "UPDATE items SET provider_item_id=?,"
                    "image_key=COALESCE(?,image_key),updated_at=?,"
                    "status='ready',error='' WHERE id=?",
                    (provider_item_id, image_key, now, item_id),
                )

    def record_play(self, title: str, artist: str = "", album: str = "",
                    *, played_at: float | None = None) -> int:
        """Record one observed playback for matching saved tracks."""
        if not str(title).strip():
            return 0
        query = ("SELECT id,metadata_json FROM items WHERE kind='track' "
                 "AND title=? COLLATE NOCASE ")
        args: list[Any] = [str(title)]
        if album:
            query += "AND album=? COLLATE NOCASE "
            args.append(str(album))
        elif artist:
            query += "AND artist=? COLLATE NOCASE "
            args.append(str(artist))
        rows_updated = 0
        timestamp = float(played_at if played_at is not None else time.time())
        with self._lock, self._connect() as connection:
            for row in connection.execute(query, args).fetchall():
                metadata = _metadata(row["metadata_json"])
                try:
                    plays = max(0, int(metadata.get("plays") or 0))
                except (TypeError, ValueError):
                    plays = 0
                metadata.update(plays=plays + 1, last_played=timestamp)
                connection.execute(
                    "UPDATE items SET metadata_json=?,updated_at=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False,
                                separators=(",", ":")), timestamp, row["id"]),
                )
                rows_updated += 1
        return rows_updated

    def record_observed_play(
        self,
        provider: str,
        provider_item_id: str,
        data: dict[str, Any],
        *,
        played_at: float | None = None,
    ) -> str:
        """Record playback independently from virtual-library membership.

        Roon state contains useful title/artist/album facts even when playback
        started in another Roon client. Keeping those observations separate is
        what lets Ask learn listening history without silently adding tracks to
        the user's library.
        """
        title = str(data.get("title") or data.get("name") or "").strip()
        if not title:
            raise ValueError("observed playback needs a title")
        artist = str(data.get("artist") or "").strip()
        album = str(data.get("album") or "").strip()
        identity = hashlib.sha256("\0".join((
            title.casefold(), artist.casefold(), album.casefold(),
        )).encode()).hexdigest()[:32]
        timestamp = float(played_at if played_at is not None else time.time())
        binding = str(provider_item_id or "")
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO playback_history "
                "(identity,provider,provider_item_id,title,artist,album,duration,"
                "image_key,plays,first_played,last_played) "
                "VALUES(?,?,?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(identity) DO UPDATE SET "
                "provider=excluded.provider,"
                "provider_item_id=CASE WHEN excluded.provider_item_id<>'' "
                "THEN excluded.provider_item_id ELSE playback_history.provider_item_id END,"
                "duration=COALESCE(excluded.duration,playback_history.duration),"
                "image_key=COALESCE(excluded.image_key,playback_history.image_key),"
                "plays=playback_history.plays+1,last_played=excluded.last_played",
                (identity, str(provider or "roon"), binding, title, artist,
                 album, data.get("duration"), data.get("image_key"), timestamp,
                 timestamp),
            )
        return identity

    def playback_history(self, limit: int = 100) -> list[dict[str, Any]]:
        cap = min(500, max(1, int(limit)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM playback_history "
                "ORDER BY plays DESC,last_played DESC LIMIT ?", (cap,),
            ).fetchall()
        return [{
            "history_id": str(row["identity"]),
            "provider": str(row["provider"]),
            "provider_item_id": str(row["provider_item_id"]),
            "name": str(row["title"]),
            "artist": str(row["artist"]),
            "album": str(row["album"]),
            "duration": row["duration"],
            "image_key": row["image_key"],
            "plays": max(0, int(row["plays"])),
            "firstPlayed": float(row["first_played"]),
            "lastPlayed": float(row["last_played"]),
        } for row in rows]

    def update_history_binding(self, history_id: str,
                               provider_item_id: str) -> None:
        binding = str(provider_item_id or "").strip()
        if not binding:
            return
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE playback_history SET provider_item_id=? "
                "WHERE identity=?", (binding, str(history_id)),
            )

    def mark_failure(self, item_id: str, error: str,
                     *, ambiguous: bool = False) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE items SET status=?,error=?,updated_at=? WHERE id=?",
                ("ambiguous" if ambiguous else "missing", error[:1000],
                 time.time(), item_id),
            )

    def repair_report(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM items WHERE status<>'ready' "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [{**self._item(row).panel_dict(),
                 "provider_item_id": row["provider_item_id"],
                 "error": row["error"]} for row in rows]
