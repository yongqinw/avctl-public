"""Provider-neutral music seam used by state, Music panel, and Ask.

The configured ``MusicSource`` owns both the personal-library domain and its
discovery-service domain. AppleMusic maps those to Music.app + MusicKit;
RoonMusic maps them to Roon Library + Roon/Qobuz. No route, command, or Ask
tool initializes one of those implementations directly.

Shaped like disclink: a provider singleton behind a lock, a safe_state() that
never raises, and plain functions the command table points at. It also owns:

* cached library shelves so panel paging and Ask prompt construction share one
  bounded scan;
* a serialized artwork cache warmer; and
* one authoritative logical queue above provider-specific physical queues.

Apple's optional Music User Token (add-to-library) lives at
~/.avctl/music_user_token, chmod 600, next to the app token -- it is an
account credential and never belongs in configs/.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import tempfile
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Callable

from devices import config as device_config
from devices import registry
from devices.music import (CATALOG_ID_RE, MEDIA_ID_RE, PID_RE, MusicError,
                           MusicKit, MusicSource)

_LOCK = threading.Lock()
_MUSIC: MusicSource | None = None
_KIT: MusicKit | None = None
_KIT_LOADED = False

_RECENT_TTL = 600.0
_recent_cache: tuple[float, list[dict[str, Any]]] | None = None
_recent_songs_cache: tuple[float, list[dict[str, Any]]] | None = None
_RECENT_SONGS_SCAN_LOCK = threading.Lock()
_EXPLORE_TTL = 300.0
_EXPLORE_LOCK = threading.Lock()
_explore_cache: tuple[float, bool, dict[str, Any]] | None = None

# One osascript at a time for artwork: sixty lazy-loading <img>s on a cold
# cache must queue, not stampede Music.app.
_ARTWORK_GATE = threading.Semaphore(1)
_ARTWORK_WARM_LOCK = threading.Lock()
_ARTWORK_WARM_PENDING: dict[str, None] = {}
_ARTWORK_WARM_MISSING: set[str] = set()
_ARTWORK_WARM_RUNNING = False
_ARTWORK_WARM_LIMIT = 30

USER_TOKEN_FILE = Path("~/.avctl/music_user_token").expanduser()


def _music() -> MusicSource:
    global _MUSIC
    with _LOCK:
        if _MUSIC is None:
            _MUSIC = registry.device("music")
        return _MUSIC


def _musickit() -> MusicKit | None:
    """Deprecated compatibility seam; catalog operations live on MusicSource."""
    global _KIT, _KIT_LOADED
    with _LOCK:
        if not _KIT_LOADED:
            _KIT = MusicKit.from_config(device_config.load_config())
            _KIT_LOADED = True
        return _KIT


_ORIGINAL_MUSICKIT = _musickit


def service_info() -> dict[str, Any]:
    """The provider-neutral discovery half of the selected MusicSource."""
    return _music().service_info(user_token())


_MUSIC_BACKENDS = {
    "apple_music": ("AppleMusic", "Apple Music",
                    "Music.app library and Apple Music catalog"),
    "roon": ("RoonMusic", "Roon + Qobuz",
             "Roon library, zones, queue, and Qobuz catalog"),
}


def music_backend_settings() -> dict[str, Any]:
    """Safe Settings projection for the two configured music drivers."""
    config = device_config.load_config()
    try:
        active_driver = device_config.configured_driver(
            config, "music", "AppleMusic")
    except ValueError as exc:
        raise MusicError(str(exc)) from None
    with _LOCK:
        if _MUSIC is not None:
            active_driver = type(_MUSIC).__name__
    active = next((backend for backend, (driver, _label, _detail)
                   in _MUSIC_BACKENDS.items() if driver == active_driver), "")
    return {
        "active_backend": active,
        "managed": bool(os.environ.get("AVCTL_MUSIC_BACKEND", "").strip()),
        "backends": [
            {"id": backend, "driver": driver, "label": label,
             "detail": detail}
            for backend, (driver, label, detail) in _MUSIC_BACKENDS.items()
        ],
    }


def set_music_backend(backend: str) -> dict[str, Any]:
    """Persist and atomically install a music driver selected in Settings."""
    global _MUSIC, _KIT, _KIT_LOADED, _recent_cache, _recent_songs_cache
    global _explore_cache

    backend_id = str(backend).strip().casefold()
    selected = _MUSIC_BACKENDS.get(backend_id)
    if selected is None:
        raise MusicError("music backend must be apple_music or roon")
    current_settings = music_backend_settings()
    if current_settings["managed"]:
        raise MusicError("music backend is managed by AVCTL_MUSIC_BACKEND")
    if current_settings["active_backend"] == backend_id:
        return current_settings
    driver, _label, _detail = selected
    config = device_config.load_config()
    try:
        cls = registry.named_driver_class("music", driver)
        candidate = cls.from_config(config)
    except (registry.RegistryError, MusicError, RuntimeError, ValueError) as exc:
        raise MusicError(f"could not start {driver}: {exc}") from None

    with _LOCK:
        previous = _MUSIC
    try:
        device_config.save_music_driver(config, driver)
    except ValueError as exc:
        raise MusicError(str(exc)) from None

    if previous is not None and previous is not candidate:
        try:
            previous.pause()
        except (MusicError, RuntimeError, ValueError):
            # The old backend may be the disconnected source being escaped.
            # A failed best-effort pause must not trap Settings on it.
            pass

    registry.install("music", cls, candidate)
    with _LOCK:
        _MUSIC = candidate
        _KIT = None
        _KIT_LOADED = False
        _recent_cache = None
        _recent_songs_cache = None
        _explore_cache = None
    with _ARTWORK_WARM_LOCK:
        _ARTWORK_WARM_PENDING.clear()
        _ARTWORK_WARM_MISSING.clear()
    # These disk caches contain provider-specific opaque ids. Reusing an
    # Apple persistent id as a Roon media handle (or vice versa) would make
    # the first Play after a switch fail in a particularly confusing way.
    for cache_file in (SONGS_FILE, ORDER_FILE):
        try:
            cache_file.unlink(missing_ok=True)
        except OSError:
            pass
    with _QUEUE.lock:
        _QUEUE.clear_locked()
    return music_backend_settings()


def _artwork_dir() -> Path:
    block = device_config.load_config().get("music") or {}
    configured = block.get("artwork_cache") or "~/.avctl/artwork"
    return Path(configured).expanduser()


def safe_state() -> dict[str, Any]:
    """Player facts for the snapshot. Degrades to unknowns, never raises.

    On failure the error text goes out verbatim in `detail`: if the daemon
    context cannot send Apple Events, the TCC error number showing up on the
    phone *is* the diagnostic.
    """
    queue = _QUEUE.details()
    empty = {"state": None, "track": None, "artist": None, "album": None,
             "shuffle": queue["shuffle"], "repeat": None, "position": None,
             "duration": None, "volume": None, "muted": None, "pid": None,
             "catalog_id": None, "art": None,
             "queued": queue["count"], "queue_revision": queue["revision"]}
    try:
        now = _music().now_playing()
    except (MusicError, ValueError) as exc:
        return {"online": False, **empty, "detail": str(exc)[:200]}

    _watch_queue(now, expected_revision=queue["revision"])
    playing_pid, playing_service_id = _QUEUE.playback_ids(now)
    if now.get("state") == "not_running":
        return {"online": True, **empty, "state": "not_running",
                "volume": now.get("volume"), "muted": now.get("muted"),
                "detail": "Music is not running"}
    return {
        "online": True,
        "state": now.get("state"),
        "track": now.get("track"),
        "artist": now.get("artist"),
        "album": now.get("album"),
        # avctl shuffles the logical order before playback. Music's own
        # shuffle stays off because it freezes Up Next at a playlist snapshot
        # and strands everything the queue pump appends later.
        "shuffle": _QUEUE.shuffle,
        "repeat": now.get("repeat"),
        "position": now.get("position"),
        "duration": now.get("duration"),
        "pid": playing_pid,
        "catalog_id": playing_service_id,
        "art": now.get("art"),
        "volume": now.get("volume"),
        "muted": now.get("muted"),
        # NOT Music's raw playlist count -- that one keeps played tracks and
        # misses the pump's pending tail. queue_remaining() reconciles it
        # against the dispatch bookkeeping.
        "queued": queue_remaining(now),
        "queue_revision": _QUEUE.revision,
        "detail": now.get("state") or "unknown",
    }


# --- data for the GET routes ---------------------------------------------


def page_size() -> int:
    block = device_config.load_config().get("music") or {}
    try:
        return max(1, int(block.get("recent_limit") or 60))
    except (TypeError, ValueError):
        return 60


def recent(force: bool = False) -> list[dict[str, Any]]:
    """The FULL recently-added list, cached -- the route slices pages off it,
    so scrolling deeper never re-runs the library scan."""
    global _recent_cache
    with _LOCK:
        if (not force and _recent_cache
                and time.monotonic() - _recent_cache[0] < _RECENT_TTL):
            return _recent_cache[1]
    albums = _music().recently_added()
    with _LOCK:
        _recent_cache = (time.monotonic(), albums)
    return albums


# The song view's order, on disk like library_order() and for the same
# reason: a tap (and the Songs tab's first paint after a restart) should
# never wait on a scan, and a stale ranking is bounded rather than
# arbitrary. Outside the release dir, like every other piece of state.
SONGS_FILE = Path("~/.avctl/recent_songs.json").expanduser()


def recent_songs(force: bool = False) -> list[dict[str, Any]]:
    """The FULL library as songs, newest-added first -- memory first, then
    the disk copy (survives restarts), then the real scan. The route slices
    pages off it, and play_recent orders from it."""
    global _recent_songs_cache
    with _LOCK:
        if (not force and _recent_songs_cache
                and time.monotonic() - _recent_songs_cache[0] < _RECENT_TTL):
            return _recent_songs_cache[1]
    # Startup prompt warmup, the Music panel, and a voice request can arrive
    # together. Only one of them should launch the full-library AppleScript;
    # followers recheck the cache after the leader completes.
    with _RECENT_SONGS_SCAN_LOCK:
        with _LOCK:
            if (not force and _recent_songs_cache
                    and time.monotonic() - _recent_songs_cache[0] < _RECENT_TTL):
                return _recent_songs_cache[1]
        if not force:
            try:
                cached = json.loads(SONGS_FILE.read_text(encoding="utf-8"))
                songs = cached.get("songs")
                if (time.time() - cached["written_at"] < _RECENT_TTL
                        and isinstance(songs, list) and songs
                        and all(isinstance(row, dict) for row in songs)):
                    with _LOCK:
                        _recent_songs_cache = (time.monotonic(), songs)
                    return songs
            except (OSError, ValueError, KeyError, TypeError):
                pass
        songs = _music().recently_added_songs()
        try:
            SONGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=SONGS_FILE.parent,
                                            prefix=".recent_songs-")
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump({"written_at": time.time(), "songs": songs}, fh)
            os.replace(temp, SONGS_FILE)
        except OSError:
            pass   # a cache that cannot be written is still a working list
        with _LOCK:
            _recent_songs_cache = (time.monotonic(), songs)
        return songs


def personal_history(limit: int = 100) -> list[dict[str, Any]]:
    """Provider-observed playable history, separate from library membership."""
    try:
        return _music().personal_history(min(500, max(1, int(limit))))
    except (AttributeError, MusicError, NotImplementedError, OSError,
            RuntimeError, TypeError, ValueError):
        # History improves ranking but is never required for playback. If a
        # Roon reconnect or a damaged optional history store prevents the read,
        # Ask can still fall back to the normal library snapshot.
        return []


def album_tracks(album: str, artist: str = "") -> list[dict[str, Any]]:
    return _music().album_tracks(album, artist)


def search_library_tracks(query: str) -> list[dict[str, Any]]:
    """The full local-search result used by non-HTML clients."""
    return _search_all_variants(query)


def artwork_file(pid: str) -> Path:
    """The cached artwork path, extracting on first miss.

    Raises FileNotFoundError when the track has no artwork (-> 404) and
    MusicError when Music.app cannot be asked at all (-> 502).
    """
    path = _artwork_dir() / f"{pid}.img"
    if path.exists():
        return path
    with _ARTWORK_GATE:
        if path.exists():  # extracted while we waited at the gate
            return path
        try:
            _music().extract_artwork(pid, path)
        except MusicError as exc:
            if "no artwork" in str(exc):
                raise FileNotFoundError(pid)
            raise
    return path


def cached_artwork_file(pid: str) -> Path:
    """Return artwork only when Music has already populated avctl's cache.

    Queue views can contain hundreds of tracks.  They should ask for every
    cached cover immediately, but a cache miss must not turn opening the
    queue into hundreds of serialized Apple Events against Music.app.
    """
    path = _artwork_dir() / f"{pid}.img"
    if not path.exists():
        raise FileNotFoundError(pid)
    return path


def _warm_queued_artwork() -> None:
    """Drain cache misses through one short-lived, serialized worker."""
    global _ARTWORK_WARM_RUNNING
    while True:
        with _ARTWORK_WARM_LOCK:
            try:
                pid = next(iter(_ARTWORK_WARM_PENDING))
            except StopIteration:
                _ARTWORK_WARM_RUNNING = False
                return
        missing = False
        try:
            artwork_file(pid)
        except FileNotFoundError:
            missing = True
        except Exception:  # noqa: BLE001 - keep the shared worker drainable
            # A transient Music/TCC failure can be retried on the next queue
            # view. Only a definite "no artwork" result is remembered.
            pass
        finally:
            with _ARTWORK_WARM_LOCK:
                _ARTWORK_WARM_PENDING.pop(pid, None)
                if missing:
                    _ARTWORK_WARM_MISSING.add(pid)


def prefetch_queue_artwork(snapshot: dict[str, Any]) -> None:
    """Warm uncached local queue covers without delaying the queue response.

    Catalog rows already carry an Apple artwork URL. Local rows are admitted
    to one bounded, de-duplicated worker so repeatedly opening a long queue
    cannot stampede Music.app with Apple Events.
    """
    global _ARTWORK_WARM_RUNNING
    rows = [snapshot.get("playing"), *(snapshot.get("items") or [])]
    start_worker = False
    with _ARTWORK_WARM_LOCK:
        room = max(0, _ARTWORK_WARM_LIMIT - len(_ARTWORK_WARM_PENDING))
        if room:
            for item in rows:
                if room <= 0:
                    break
                if not isinstance(item, dict) or item.get("art"):
                    continue
                pid = str(item.get("pid") or "")
                if (not MEDIA_ID_RE.match(pid) or pid in _ARTWORK_WARM_PENDING
                        or pid in _ARTWORK_WARM_MISSING
                        or (_artwork_dir() / f"{pid}.img").exists()):
                    continue
                _ARTWORK_WARM_PENDING[pid] = None
                room -= 1
        if _ARTWORK_WARM_PENDING and not _ARTWORK_WARM_RUNNING:
            _ARTWORK_WARM_RUNNING = True
            start_worker = True
    if start_worker:
        threading.Thread(
            target=_warm_queued_artwork,
            name="avctl-queue-artwork",
            daemon=True,
        ).start()


# --- command handlers -----------------------------------------------------


# The library ranked newest-added first, kept on disk so pressing play never
# waits on a scan. Outside the release dir, like every other piece of state.
ORDER_FILE = Path("~/.avctl/library_order.json").expanduser()
QUEUE_FILE = Path("~/.avctl/music_queue.json").expanduser()


def _tunable(name: str, default: int) -> int:
    block = device_config.load_config().get("music") or {}
    value = block.get(name)
    return int(value) if isinstance(value, int) else default


def library_order(force: bool = False) -> list[str]:
    """Persistent IDs newest-added first, from cache when it is fresh.

    Worth being honest about what this buys: the scan itself is only 0.12s.
    The cache exists so the play key never pays even that, and so a stale
    ranking is bounded rather than arbitrary. What actually makes filling a
    queue slow is Music.app's duplicate throughput, which no cache helps.
    """
    ttl = _tunable("order_cache_ttl", 30)
    try:
        cached = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        fresh = time.time() - cached["written_at"] < ttl
        if not force and fresh and cached.get("order"):
            return cached["order"]
    except (OSError, ValueError, KeyError, TypeError):
        pass

    order = _music().library_order()
    try:
        ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=ORDER_FILE.parent, prefix=".liborder-")
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump({"written_at": time.time(), "order": order}, fh)
        os.replace(temp, ORDER_FILE)
    except OSError:
        pass   # a cache that cannot be written is still a working ranking
    return order


# --- the authoritative queue ---------------------------------------------
#
# Music.app still needs a playlist as playback context -- without one its
# Next key is dead -- but that playlist is only a projection now. This
# controller owns the complete logical order, metadata, cursor, shuffle
# preference and projection marker. Every app action mutates this one object;
# the pump only copies its prefix into Music's `avctl` playlist.
#
# The JSON file is outside the release dir like the library caches. A daemon
# restart therefore keeps the queue viewer truthful and lets the pump resume
# an unmaterialized tail instead of forgetting it.


def _queue_identity(item: dict[str, Any]) -> str:
    return str(item.get("pid") or item.get("catalog_id") or "")


def _now_identity(now: dict[str, Any]) -> str:
    return str(now.get("pid") or now.get("catalog_id") or "")


def _media_text(value: Any) -> str:
    """Normalize display metadata for providers without stable now-playing ids."""
    return " ".join(str(value or "").casefold().split())


def _queue_engine(item: dict[str, Any]) -> str:
    try:
        return _music().queue_engine(bool(item.get("catalog_id")))
    except (AttributeError, MusicError, NotImplementedError, RuntimeError,
            ValueError):
        return "catalog" if item.get("catalog_id") else "library"


def _valid_library_id(item_id: str) -> bool:
    try:
        return bool(_music().valid_library_id(item_id))
    except (AttributeError, MusicError, NotImplementedError, RuntimeError,
            ValueError):
        return bool(PID_RE.fullmatch(str(item_id)))


class QueueController:
    """One persisted source of truth for avctl music playback."""

    def __init__(self, path: Path = QUEUE_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.items: list[dict[str, Any]] = []
        self.materialized = 0
        self.current = -1
        self.revision = 0
        self.shuffle = False
        self.pump_revision: int | None = None
        self.resume_attempted = False
        self.rescued_for: tuple[int, int] | None = None
        self.stopped_since: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            items = data.get("items") or []
            if not isinstance(items, list):
                return
            clean = [
                dict(item) for item in items if isinstance(item, dict)
                and (MEDIA_ID_RE.match(str(item.get("pid") or ""))
                     or MEDIA_ID_RE.match(
                         str(item.get("catalog_id") or "")))
            ]
            self.items = clean
            self.materialized = max(0, min(int(data.get("materialized") or 0),
                                           len(clean)))
            self.current = max(-1, min(int(data.get("current", -1)),
                                       len(clean) - 1))
            self.revision = max(0, int(data.get("revision") or 0))
            self.shuffle = bool(data.get("shuffle"))
        except (OSError, ValueError, TypeError):
            pass

    def _save_locked(self) -> None:
        data = {
            "version": 2,
            "revision": self.revision,
            "shuffle": self.shuffle,
            "materialized": self.materialized,
            "current": self.current,
            "items": self.items,
        }
        temp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(dir=self.path.parent,
                                            prefix=".music-queue-")
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp, self.path)
        except OSError:
            if temp:
                try:
                    os.unlink(temp)
                except OSError:
                    pass

    def _changed_locked(self) -> int:
        self.revision += 1
        self.rescued_for = None
        self.stopped_since = None
        self._save_locked()
        self.condition.notify_all()
        return self.revision

    def replace_locked(self, items: list[dict[str, Any]], materialized: int) -> int:
        self.items = list(items)
        self.materialized = max(0, min(materialized, len(items)))
        self.current = -1
        return self._changed_locked()

    def append_locked(self, items: list[dict[str, Any]], materialized: int) -> int:
        self.items.extend(items)
        self.materialized += max(0, min(materialized, len(items)))
        return self._changed_locked()

    def clear_locked(self) -> int:
        self.items = []
        self.materialized = 0
        self.current = -1
        return self._changed_locked()

    def set_shuffle(self, on: bool) -> int:
        with self.lock:
            if self.shuffle == on:
                return self.revision
            self.shuffle = on
            return self._changed_locked()

    def _index_locked(self, identity: str) -> int | None:
        """Locate the most plausible occurrence of the playing item."""
        matches = [index for index, item in enumerate(
            self.items[:self.materialized])
                   if _queue_identity(item) == identity]
        if not matches:
            return None
        if self.current < 0:
            # With no prior cursor, err late: it keeps the projection pump
            # moving and matches the queue's established duplicate rule.
            return matches[-1]
        return min(matches, key=lambda index: abs(index - self.current))

    def _identity_locked(self, now: dict[str, Any]) -> str:
        """Resolve provider state to an id in the authoritative queue.

        Music.app and the MusicKit helper return the exact id that avctl
        queued. Roon's public now-playing projection only carries display
        metadata and issues a new ephemeral id on every state revision. Match
        that metadata against the materialized logical queue so Next, the
        queue cursor, and the background pump remain provider-neutral.
        """
        direct = _now_identity(now)
        if direct and self._index_locked(direct) is not None:
            return direct

        title = _media_text(now.get("track") or now.get("name"))
        if not title:
            return direct
        artist = _media_text(now.get("artist"))
        album = _media_text(now.get("album"))
        matches: list[int] = []
        for index, item in enumerate(self.items[:self.materialized]):
            if _media_text(item.get("name")) != title:
                continue
            if artist and _media_text(item.get("artist")) != artist:
                continue
            if album and item.get("album") and _media_text(item.get("album")) != album:
                continue
            matches.append(index)
        if not matches:
            return direct
        if self.current < 0:
            at = matches[0]
        else:
            # Prefer the current/next occurrence. This keeps duplicate tracks
            # moving forward instead of jumping back to an earlier copy.
            forward = [index for index in matches if index >= self.current]
            at = min(forward or matches, key=lambda index: abs(index - self.current))
        return _queue_identity(self.items[at])

    def playback_identity(self, now: dict[str, Any]) -> str:
        with self.lock:
            return self._identity_locked(now)

    def playback_ids(self, now: dict[str, Any]) -> tuple[str | None, str | None]:
        """Return the queue's local/service id for the playing provider item."""
        with self.lock:
            identity = self._identity_locked(now)
            at = self._index_locked(identity)
            if at is None:
                return (now.get("pid"), now.get("catalog_id"))
            item = self.items[at]
            return (item.get("pid"), item.get("catalog_id"))

    def observe(self, now: dict[str, Any], *, rescue_delay: float = 0.0,
                clock: Callable[[], float] = time.monotonic
                ) -> list[dict[str, Any]] | None:
        """Advance the cursor; return a stranded tail that must be replayed."""
        state = now.get("state")
        with self.lock:
            identity = self._identity_locked(now)
            if state in ("playing", "paused") and identity:
                self.stopped_since = None
                at = self._index_locked(identity)
                if at is not None:
                    if at != self.current:
                        self.current = at
                        # Playing/Next changed in the public Focus view.
                        self._changed_locked()
                    self.rescued_for = None
                return None
            if state != "stopped" or self.current < 0:
                self.stopped_since = None
                return None
            if self.current >= len(self.items) - 1:
                # The last logical item finished. Music's playlist still has
                # dead rows, but avctl's queue is genuinely empty.
                self.clear_locked()
                return None
            observed_at = clock()
            if self.stopped_since is None:
                self.stopped_since = observed_at
            if observed_at - self.stopped_since < max(0.0, rescue_delay):
                return None
            token = (self.revision, self.current)
            if self.rescued_for == token:
                return None
            self.rescued_for = token
            return [dict(item) for item in self.items[self.current + 1:]]

    def remaining(self, now: dict[str, Any]) -> int:
        with self.lock:
            identity = self._identity_locked(now)
            if now.get("state") in ("playing", "paused") and identity:
                at = self._index_locked(identity)
                if at is not None:
                    return max(0, len(self.items) - at - 1)
            if self.current >= 0:
                return max(0, len(self.items) - self.current - 1)
            return len(self.items)

    def details(self) -> dict[str, Any]:
        with self.lock:
            start = self.current + 1 if self.current >= 0 else 0
            upcoming = [self._public(item) for item in self.items[start:]]
            playing = (self._public(self.items[self.current])
                       if 0 <= self.current < len(self.items) else None)
            return {
                "revision": self.revision,
                "shuffle": self.shuffle,
                "count": len(upcoming),
                "playing": playing,
                "items": upcoming,
            }

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        public = {key: item.get(key) for key in
                  ("id", "pid", "name", "artist", "album", "duration")}
        if item.get("catalog_id"):
            public["catalog_id"] = item["catalog_id"]
        if item.get("art"):
            public["art"] = item["art"]
        return public


_QUEUE = QueueController()
_DISPATCH_LOCK = _QUEUE.lock       # compatibility name; one lock, one owner

# 19ms per track, measured on this mini against an 864-track library. Config
# can override it, but the default is a measurement rather than a guess.
_PLACE_MS = 19.0


def order_queue_batch(tracks: list[Any]) -> list[Any]:
    """Copy a new user-requested batch and honor avctl's shuffle state once.

    Internal rescue and cross-engine handoff paths deliberately do not call
    this helper: an already shuffled logical tail must never be shuffled a
    second time when Next crosses between Music.app and catalog playback.
    """
    ordered = list(tracks)
    if _QUEUE.shuffle and len(ordered) > 1:
        random.shuffle(ordered)
    return ordered


def _lead() -> int:
    """How many tracks may be placed before the caller is answered."""
    budget = _tunable("dispatch_budget_ms", 200)
    per = _tunable("place_ms", int(_PLACE_MS)) or 1
    calculated = max(1, int(budget // per))
    try:
        provider_limit = _music().placement_batch_limit()
    except (AttributeError, MusicError, NotImplementedError, RuntimeError,
            ValueError):
        provider_limit = None
    return (min(calculated, max(1, int(provider_limit)))
            if provider_limit is not None else calculated)


def _drip_chunk() -> int:
    configured = max(1, _tunable("drip_chunk", 15))
    try:
        provider_limit = _music().placement_batch_limit()
    except (AttributeError, MusicError, NotImplementedError, RuntimeError,
            ValueError):
        provider_limit = None
    return (min(configured, max(1, int(provider_limit)))
            if provider_limit is not None else configured)


def _queue_items(tracks: list[Any], source: dict[str, Any] | None = None
                 ) -> list[dict[str, Any]]:
    """Normalize track facts once, at the queue boundary."""
    batch = (source or {}).get("batch") or uuid.uuid4().hex
    start = int((source or {}).get("start") or 1)
    items = []
    for offset, raw in enumerate(tracks):
        track = dict(raw) if isinstance(raw, dict) else {"pid": str(raw)}
        pid = str(track.get("pid") or "")
        catalog_id = str(track.get("catalog_id") or track.get("catalogId") or "")
        if bool(_valid_library_id(pid)) == bool(MEDIA_ID_RE.fullmatch(catalog_id)):
            raise ValueError("a queue item needs exactly one valid local or catalog id")
        own_source = track.get("_source")
        if not isinstance(own_source, dict):
            own_source = dict(source or {
                "kind": "catalog" if catalog_id else "library"})
            own_source["batch"] = batch
            if own_source.get("kind") == "playlist":
                own_source["position"] = start + offset
        items.append({
            "id": str(track.get("id") or uuid.uuid4().hex),
            "pid": pid or None,
            "catalog_id": catalog_id or None,
            "name": str(track.get("name") or ""),
            "artist": str(track.get("artist") or ""),
            "album": str(track.get("album") or ""),
            "duration": track.get("duration"),
            "art": track.get("art"),
            "_source": own_source,
        })
    return items


def _start_pump_locked(revision: int) -> None:
    _QUEUE.resume_attempted = True
    if _QUEUE.materialized >= len(_QUEUE.items):
        return
    if _QUEUE.pump_revision is not None:
        _QUEUE.pump_revision = revision
        _QUEUE.condition.notify_all()
        return
    _QUEUE.pump_revision = revision
    threading.Thread(target=_pump, args=(_QUEUE,), daemon=True,
                     name="avctl-queue-pump").start()


def _play_head_locked(music: Any, items: list[dict[str, Any]], lead: int) -> int:
    """Start a logical queue through the source operation its head needs."""
    source = items[0].get("_source") or {}
    if items[0].get("catalog_id"):
        end = 0
        while (end < len(items) and end < lead
               and items[end].get("catalog_id")):
            end += 1
        music.play_catalog([
            str(item["catalog_id"]) for item in items[:end]
        ])
        return end
    if source.get("kind") == "playlist":
        batch = source.get("batch")
        end = 0
        while end < len(items):
            other = items[end].get("_source") or {}
            if other.get("kind") != "playlist" or other.get("batch") != batch:
                break
            end += 1
        music.queue_playlist_bulk(str(source["pid"]),
                                  int(source.get("position") or 1),
                                  replace=True, play=True)
        return end

    # A rescued tail can cross into a cloud-playlist batch. Stop before that
    # boundary so the pump can preserve it with the required bulk operation.
    end = 0
    while end < len(items) and end < lead:
        candidate = items[end].get("_source") or {}
        if (items[end].get("catalog_id")
                or candidate.get("kind") == "playlist"):
            break
        end += 1
    music.play_tracks([str(item["pid"]) for item in items[:end]])
    return end


def _dispatch(tracks: list[Any], replace: bool, play: bool,
              source: dict[str, Any] | None = None, *,
              expected_revision: int | None = None) -> int:
    """Mutate the logical queue, then materialize only its cheap lead."""
    if replace and not play:
        raise ValueError("dispatch cannot replace without playing")
    items = _queue_items(tracks, source)
    if not items:
        raise ValueError("nothing to queue")
    music = _music()
    lead = _lead()
    with _QUEUE.lock:
        if expected_revision is not None and _QUEUE.revision != expected_revision:
            return 0
        if replace or not _QUEUE.items:
            materialized = _play_head_locked(music, items, lead)
            revision = _QUEUE.replace_locked(items, materialized)
        else:
            # Logical tail means tail. If an older tail is still waiting for
            # projection, do not jump this add ahead of it in Music's
            # playlist -- the old dual-list implementation did exactly that.
            room_at_tail = _QUEUE.materialized == len(_QUEUE.items)
            same_engine = (
                not _QUEUE.items
                or _queue_engine(_QUEUE.items[-1]) == _queue_engine(items[0])
            )
            head = items[:lead] if room_at_tail and same_engine else []
            if head:
                engine = _queue_engine(head[0])
                service_item = bool(head[0].get("catalog_id"))
                end = next((index for index, item in enumerate(head)
                            if (_queue_engine(item) != engine
                                or bool(item.get("catalog_id")) != service_item
                                or (item.get("_source") or {}).get("kind")
                                == "playlist")), len(head))
                head = head[:end]
            if head:
                if head[0].get("catalog_id"):
                    music.queue_catalog([
                        str(item["catalog_id"]) for item in head
                    ])
                else:
                    music.queue_append([str(item["pid"]) for item in head])
            revision = _QUEUE.append_locked(items, len(head))
        _start_pump_locked(revision)
    return len(items)


def _void_dispatch(action) -> None:
    """Run a physical clear atomically with the authoritative clear."""
    with _QUEUE.lock:
        action()
        _QUEUE.clear_locked()


def _needle_ahead(placed: list[str], pid: str | None) -> int:
    """How many placed tracks still sit past the needle.

    The LAST occurrence, not the first: the same track placed twice (queued
    twice, or in an album and a later add) would otherwise put the needle
    too early, inflate this number, and stall the drip (#55). Guessing late
    errs toward placing more, which is the cheap direction to be wrong in.
    A needle that is not ours at all counts everything as unplayed.
    """
    if pid and pid in placed:
        at = len(placed) - 1 - placed[::-1].index(pid)
        return len(placed) - at - 1
    return len(placed)


def _materialize_locked(queue: QueueController, chunk: int) -> bool:
    """Project the next logical segment without changing its order."""
    at = queue.materialized
    first = queue.items[at]
    if at and _queue_engine(queue.items[at - 1]) != _queue_engine(first):
        # Music.app and ApplicationMusicPlayer cannot share one physical
        # queue. Leave this engine boundary logical; observe() starts the next
        # segment when the current player stops.
        return False
    source = first.get("_source") or {}
    if first.get("catalog_id"):
        end = at
        while (end < len(queue.items) and end < at + chunk
               and queue.items[end].get("catalog_id")):
            end += 1
        _music().queue_catalog([
            str(item["catalog_id"]) for item in queue.items[at:end]
        ])
        queue.materialized = end
    elif source.get("kind") == "playlist":
        batch = source.get("batch")
        end = at
        while end < len(queue.items):
            other = queue.items[end].get("_source") or {}
            if other.get("kind") != "playlist" or other.get("batch") != batch:
                break
            end += 1
        _music().queue_playlist_bulk(str(source["pid"]),
                                     int(source.get("position") or 1),
                                     replace=False, play=False)
        queue.materialized = end
    else:
        end = at
        # Never cross into a cloud-playlist segment: that segment must use
        # its bulk source operation or Music silently drops cloud tracks.
        while end < len(queue.items) and end < at + chunk:
            candidate = queue.items[end].get("_source") or {}
            if (queue.items[end].get("catalog_id")
                    or candidate.get("kind") == "playlist"):
                break
            end += 1
        batch = queue.items[at:end]
        _music().queue_append([str(item["pid"]) for item in batch])
        queue.materialized = end
    queue._save_locked()
    return True


def _pump(queue: QueueController) -> None:
    """Keep a small physical window ahead of the logical cursor."""
    music = _music()
    tick = max(1.0, float(_tunable("drip_tick", 5)))
    window = max(1, _tunable("queue_window", 20))
    chunk = _drip_chunk()
    # If the needle never turns up in our list -- the user started something
    # in Music.app directly -- this loop is feeding a queue nobody is
    # listening to. Bail after this many fruitless ticks instead of spinning
    # until the next dispatch (#55); a fresh dispatch starts a fresh drip.
    max_idle = max(1, _tunable("drip_max_idle_ticks", 60))
    idle = 0
    # Which structural request this worker has accepted. `_start_pump_locked`
    # updates pump_revision when work arrives for a live pump; if that lands
    # after this worker has decided to return, finally hands it to a successor
    # instead of silently stranding the new tail.
    accepted_revision = queue.pump_revision
    try:
        while True:
            with queue.condition:
                if queue.materialized >= len(queue.items):
                    return
                queue.condition.wait(timeout=tick)
                if queue.materialized >= len(queue.items):
                    return
                accepted_revision = queue.pump_revision
                revision = queue.revision
                placed = [_queue_identity(item)
                          for item in queue.items[:queue.materialized]]
            # The needle read happens OUTSIDE the lock: a snapshot must not
            # hold up someone pressing clear. The poller usually has it
            # already -- riding its cache means a live drip adds no Apple
            # Event traffic of its own (#55) -- and only a stale or absent
            # cache costs a now_playing() of our own.
            pid = _cached_needle(max_age=tick)
            if pid is _NO_NEEDLE:
                try:
                    pid = queue.playback_identity(music.now_playing())
                except (MusicError, ValueError):
                    # A hiccup reading the player is no reason to stop -- but
                    # a player that can NEVER be read is (#99): count it.
                    with queue.lock:
                        if revision != queue.revision:
                            continue
                        idle += 1
                        if idle >= max_idle:
                            return
                    continue
            # `idle` counts ticks where the needle is genuinely not ours --
            # the user started something in Music.app directly. It must NOT
            # count the healthy steady state where our queue is simply topped
            # up past the window: that state lasts several track-lengths, and
            # counting it used to expire max_idle mid-album and strand the
            # whole logical tail (#99).
            with queue.lock:
                if revision != queue.revision:
                    continue
                if pid and pid in placed:
                    idle = 0
                else:
                    idle += 1
                    if idle >= max_idle:
                        return  # nobody is playing our queue; stop feeding it
                if _needle_ahead(placed, pid) > window:
                    continue    # plenty in hand; place nothing
                if queue.materialized >= len(queue.items):
                    return
                try:
                    if not _materialize_locked(queue, chunk):
                        return
                except (MusicError, ValueError):
                    return
    finally:
        with queue.lock:
            restart = _finish_pump_locked(queue, accepted_revision)
            if restart and queue is _QUEUE:
                _start_pump_locked(queue.revision)


def _finish_pump_locked(queue: QueueController,
                        accepted_revision: int | None) -> bool:
    """Release pump ownership and report work that needs a successor.

    The caller holds queue.lock. Separating this tiny handoff makes the
    exit-race deterministic to test: an append can update pump_revision
    after a worker's final fullness check but before this cleanup runs.
    """
    requested_revision = queue.pump_revision
    queue.pump_revision = None
    return (
        queue.materialized < len(queue.items)
        and requested_revision is not None
        and requested_revision != accepted_revision
    )


def _watch_queue(now: dict[str, Any], *,
                 expected_revision: int | None = None) -> None:
    """Re-light Music when its closed Up Next strands a logical tail."""
    try:
        rescue_delay = max(0.0, float(_music().stopped_queue_rescue_delay()))
    except (AttributeError, MusicError, RuntimeError, TypeError, ValueError):
        rescue_delay = 0.0
    with _QUEUE.lock:
        # A state read can finish after a newer Play request. It must not
        # advance or rescue that request using the previous player's state.
        if expected_revision is not None and _QUEUE.revision != expected_revision:
            return
        rest = _QUEUE.observe(now, rescue_delay=rescue_delay)
        revision = _QUEUE.revision
        if not _QUEUE.resume_attempted:
            _start_pump_locked(_QUEUE.revision)
    if not rest:
        return
    try:
        # A user can replace or clear the queue after observe releases its
        # lock. Recheck the revision atomically with physical playback.
        _dispatch(rest, replace=True, play=True, expected_revision=revision)
    except Exception:  # noqa: BLE001 - a failed rescue is a skipped beat
        with _QUEUE.lock:
            if _QUEUE.revision == revision:
                _QUEUE.rescued_for = None   # retry on the next poll beat


def queue_remaining(now: dict[str, Any]) -> Any:
    """Unplayed logical items; Music's dead playlist rows are irrelevant."""
    return _QUEUE.remaining(now)


def queue_details() -> dict[str, Any]:
    """The Focus viewer's stable, metadata-rich queue snapshot."""
    return _QUEUE.details()


# Sentinel: "the cache could not answer", as distinct from "nothing playing"
# (a real answer the drip must respect rather than re-ask).
_NO_NEEDLE = object()


def _cached_needle(max_age: float):
    """The playing local/catalog id from the poller's fresh snapshot."""
    try:
        from . import state
        snap, _, age = state.latest()
    except Exception:  # noqa: BLE001 - the cache is an optimisation, never a dependency
        return _NO_NEEDLE
    if snap is None or age > max_age:
        return _NO_NEEDLE
    fields = ((snap.get("devices") or {}).get("music") or {}).get("fields")
    if not isinstance(fields, dict) or not (
            "pid" in fields or "catalog_id" in fields):
        return _NO_NEEDLE
    return fields.get("pid") or fields.get("catalog_id")


def _start_stopped_music(music: MusicSource) -> dict[str, Any]:
    """Start the logical queue, or build the default one when it is empty."""
    with _QUEUE.lock:
        has_queue = bool(_QUEUE.items)
    if has_queue:
        music.play_queue()      # stopped with a queue: playpause has no context
        return {}

    # Build one metadata-rich logical queue. Shuffle is applied here, by
    # avctl, so later appends remain part of the same playback context.
    songs = recent_songs()
    order: list[Any] = songs or library_order()
    depth = _tunable("autoplay_depth", 0)
    if depth > 0:
        order = order[:depth]
    if not order:
        raise MusicError("the library came back empty")
    shuffled = _QUEUE.shuffle
    if shuffled:
        random.shuffle(order)
    _dispatch(order, replace=True, play=True)
    mode = "shuffled" if shuffled else "newest first"
    return {"message": f"playing your library, {mode} "
                       f"({len(order)} tracks)"}


def play_pause(args: dict[str, Any]) -> dict[str, Any]:
    """Toggle an active player; from a standing start, begin the library."""
    music = _music()
    if music.now_playing().get("state") in ("playing", "paused"):
        music.play_pause()
        return {}
    return _start_stopped_music(music)


def play(args: dict[str, Any]) -> dict[str, Any]:
    """Idempotently resume, retaining the empty-queue autoplay behavior."""
    music = _music()
    playback = music.now_playing().get("state")
    if playback == "playing":
        return {"message": "music already playing", "acted": False}
    if playback == "paused":
        music.play()
        return {"message": "music playing"}
    return _start_stopped_music(music)


def pause(args: dict[str, Any]) -> dict[str, Any]:
    """Idempotently pause without a read-then-toggle race."""
    music = _music()
    playback = music.now_playing().get("state")
    if playback in {"paused", "stopped", "not_running"}:
        return {"message": "music already paused", "acted": False}
    music.pause()
    return {"message": "music paused"}


def next_track(args: dict[str, Any]) -> dict[str, Any]:
    music = _music()

    # The logical queue can be longer than Music's projected playlist: the
    # pump deliberately fills only a small window so a Play/queue gesture
    # returns quickly. If the needle reaches that physical edge before the
    # next pump beat, Music's `next track` is a silent no-op even though the
    # Focus queue truthfully shows more songs. Put the next logical item into
    # Music first, under the queue lock, then advance normally so Music keeps
    # its playlist context and Previous still has its history.
    # Always reconcile once. This matters especially for the catalog helper:
    # its queue has a different transport from Music.app, and a freshly
    # started queue may not have met the state poller yet.
    before = music.now_playing()
    stranded = _QUEUE.observe(before)
    if stranded:
        # The active physical player already stopped (including a helper that
        # lost its queue). In that state there is nothing useful to skip;
        # start the authoritative logical tail instead.
        _dispatch(stranded, replace=True, play=True)
        return {"message": "playing next track"}

    handoff: list[dict[str, Any]] | None = None
    verify_tail: list[dict[str, Any]] | None = None
    verify_revision: int | None = None
    before_identity = _QUEUE.playback_identity(before)
    with _QUEUE.lock:
        current = _QUEUE.current
        target = current + 1
        if 0 <= current < len(_QUEUE.items) and target < len(_QUEUE.items):
            if (_queue_engine(_QUEUE.items[current])
                    != _queue_engine(_QUEUE.items[target])):
                # Music.app and ApplicationMusicPlayer cannot share a
                # physical queue. A plain Next sent to the old engine is a
                # silent no-op at this boundary, so start the logical tail on
                # its owning engine immediately.
                handoff = [dict(item) for item in _QUEUE.items[target:]]
            elif target >= _QUEUE.materialized:
                _materialize_locked(_QUEUE, max(1, _lead()))
            # Music.app can report a successful append while its running Up
            # Next snapshot remains sealed at the old last track. In that
            # state `next track` is a silent no-op even though `materialized`
            # truthfully says the playlist row exists. Keep a revision-bound
            # recovery tail and verify the transport actually advanced.
            target_identity = _queue_identity(_QUEUE.items[target])
            if target_identity != before_identity:
                verify_tail = [dict(item) for item in _QUEUE.items[target:]]
                verify_revision = _QUEUE.revision

    if handoff:
        _dispatch(handoff, replace=True, play=True)
        return {"message": "playing next track"}

    # AppleMusic.next_track routes this to whichever engine is currently
    # active: Music.app for library tracks or the MusicKit helper for catalog
    # tracks.
    music.next_track()
    if verify_tail and before_identity:
        # AppleScript is synchronous in the normal case, while Roon and the
        # MusicKit bridge can publish their new state a beat later. Two short
        # readbacks avoid mistaking propagation lag for a failed Next without
        # making the successful path slower.
        after = music.now_playing()
        for delay in (0.08, 0.18):
            after_identity = _QUEUE.playback_identity(after)
            if after_identity and after_identity != before_identity:
                _QUEUE.observe(after)
                return {}
            if after.get("state") == "stopped":
                break
            time.sleep(delay)
            after = music.now_playing()
        after_identity = _QUEUE.playback_identity(after)
        if not after_identity or after_identity == before_identity:
            recovered = _dispatch(
                verify_tail, replace=True, play=True,
                expected_revision=verify_revision,
            )
            if recovered:
                return {"message": "playing next track"}
    return {}


def prev_track(args: dict[str, Any]) -> dict[str, Any]:
    _music().previous_track()
    return {}


def toggle_shuffle(args: dict[str, Any]) -> dict[str, Any]:
    on = not _QUEUE.shuffle
    _QUEUE.set_shuffle(on)
    return {"message": f"shuffle {'on for the next queue' if on else 'off'}"}


def set_shuffle(
    on: bool,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return an idempotent queue-shuffle handler for language controls."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        _QUEUE.set_shuffle(on)
        return {
            "message": f"shuffle {'on for the next queue' if on else 'off'}",
        }
    return handler


def toggle_repeat_one(args: dict[str, Any]) -> dict[str, Any]:
    music = _music()
    on = music.now_playing().get("repeat") != "one"
    music.set_repeat_one(on)
    return {"message": "repeating this song" if on else "repeat off"}


def set_repeat_one(
    on: bool,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return an idempotent one/off repeat handler for language controls."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        _music().set_repeat_one(on)
        return {"message": "repeating this song" if on else "repeat off"}
    return handler


def _pids(tracks: list[dict[str, Any]]) -> list[str]:
    return [str(track["pid"]) for track in tracks]


def playlists() -> list[dict[str, Any]]:
    """The user's playlists for the library's second shelf."""
    return _music().playlists()


def playlist_tracks(pid: str) -> dict[str, Any]:
    """One playlist with its tracks, in playlist order."""
    if not _valid_library_id(pid):
        raise ValueError(f"not a playlist id: {pid!r}")
    data = _music().playlist_tracks(pid)
    if data is None:
        raise ValueError("no such playlist -- it may have been deleted")
    return data


def _playlist_dispatch(playlist_pid: str, tracks: list[dict[str, Any]],
                       start: int, replace: bool, play: bool) -> int:
    """Queue cloud-safe playlist tracks without creating a second truth."""
    source = {"kind": "playlist", "pid": playlist_pid, "start": start,
              "batch": uuid.uuid4().hex}
    items = _queue_items(tracks, source)
    music = _music()
    with _QUEUE.lock:
        if replace or not _QUEUE.items:
            music.queue_playlist_bulk(playlist_pid, start,
                                      replace=True, play=True)
            revision = _QUEUE.replace_locked(items, len(items))
        else:
            at_physical_tail = _QUEUE.materialized == len(_QUEUE.items)
            if at_physical_tail:
                music.queue_playlist_bulk(playlist_pid, start,
                                          replace=False, play=False)
            revision = _QUEUE.append_locked(
                items, len(items) if at_physical_tail else 0)
        _start_pump_locked(revision)
    return len(items)


def play_playlist(args: dict[str, Any]) -> dict[str, Any]:
    """Play a playlist -- whole, or from a tapped song onward.

    NOT through the pid-by-pid dispatch: a subscription playlist's cloud
    tracks are not library members, and library-pid duplication silently
    drops them (New Music would lose 23 of 25, measured 2026-08-08). One
    bulk duplicate from the source playlist carries everything, in order,
    in a single Apple Event.
    """
    pid = str(args.get("pid") or "")
    tracks = playlist_tracks(pid).get("tracks") or []
    pids = _pids(tracks)
    if not pids:
        raise ValueError("that playlist is empty")
    start = str(args.get("from") or "")
    index = pids.index(start) if start in pids else 0
    _playlist_dispatch(pid, tracks[index:], index + 1,
                       replace=True, play=True)
    remaining = len(pids) - index
    noun = "1 song" if remaining == 1 else f"{remaining} songs"
    return {"message": f"playing {noun}"}


def play_album(args: dict[str, Any]) -> dict[str, Any]:
    album = str(args.get("album", ""))
    tracks = album_tracks(album, str(args.get("artist", "")))
    if not tracks:
        raise ValueError(f"no tracks found for {album!r}")
    # Through dispatch like everything else: a twelve-track album is already
    # over the budget, and a box set is well over it.
    _dispatch(order_queue_batch(tracks), replace=True, play=True)
    return {"message": f"playing {album}"}


def play_track(args: dict[str, Any]) -> dict[str, Any]:
    """The tapped song, then the rest of its album -- the queue playlist is
    rebuilt with the whole album and playback starts at the song."""
    pid = str(args.get("pid", ""))
    tracks = album_tracks(str(args.get("album", "")), str(args.get("artist", "")))
    index = next((i for i, t in enumerate(tracks) if str(t["pid"]) == pid), None)
    if index is None:
        raise ValueError("that song is not in the album's track list")
    _dispatch(tracks[index:], replace=True, play=True)
    return {"message": f"playing {tracks[index].get('name') or 'track'}"}


def play_recent(args: dict[str, Any]) -> dict[str, Any]:
    """A tap in the recently-added SONG view: play that song, then --

    shuffle off: keep walking the list, newest toward oldest, the same
    "from here onward" a mid-album tap means; shuffle on: the tapped song
    first and the rest of the library behind it PRE-SHUFFLED server-side.
    Never Music's own shuffle: that freezes Up Next at the play-time
    snapshot and later queue-adds fall on deaf ears (#137). A pre-shuffled
    order is indistinguishable to the ear and stays extendable.
    """
    pid = str(args.get("pid") or "")
    songs = recent_songs()
    pids = [str(track["pid"]) for track in songs]
    try:
        index = pids.index(pid)
    except ValueError:
        raise ValueError("that song is not in the library list -- refresh?")
    shuffled = _QUEUE.shuffle
    if shuffled:
        rest = songs[:index] + songs[index + 1:]
        random.shuffle(rest)
        order = [songs[index]] + rest
    else:
        order = songs[index:]
    _dispatch(order, replace=True, play=True)
    name = songs[index].get("name") or "track"
    tail = " -- shuffling the rest" if shuffled else ""
    return {"message": f"playing {name}{tail}"}


def _tracks_added_in(period: str) -> tuple[str, list[dict[str, Any]]]:
    """Resolve a Music.app dateAdded period to valid local track rows."""
    period = str(period or "today").strip().lower().replace(" ", "_")
    zone = ZoneInfo("America/Los_Angeles")
    today = datetime.now(zone).date()
    if period == "today":
        first, last = today, today + timedelta(days=1)
    elif period == "yesterday":
        first, last = today - timedelta(days=1), today
    elif period == "this_week":
        first = today - timedelta(days=today.weekday())
        last = today + timedelta(days=1)
    else:
        try:
            first = datetime.strptime(period, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(
                "period must be today, yesterday, this_week, or YYYY-MM-DD"
            ) from None
        last = first + timedelta(days=1)

    tracks = []
    for track in recent_songs():
        if not isinstance(track, dict):
            continue
        try:
            added = datetime.fromtimestamp(
                float(track.get("added") or 0) / 1000, zone).date()
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        if (first <= added < last
                and _valid_library_id(str(track.get("pid") or ""))):
            tracks.append(track)
    return period, tracks


def play_added(args: dict[str, Any]) -> dict[str, Any]:
    """Play or queue tracks by their Music.app dateAdded calendar date.

    Relative dates are resolved here, not by the language model. That makes
    "the music we added today" mean the same thing at 11:59pm and after a
    server restart, and keeps release dates out of the decision entirely.
    """
    mode = str(args.get("mode") or "replace").strip().lower()
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be replace or append")
    period, tracks = _tracks_added_in(str(args.get("period") or "today"))
    if not tracks:
        return {"message": f"no library music was added "
                           f"{period.replace('_', ' ')}",
                "acted": False}
    tracks = order_queue_batch(tracks)
    _dispatch(tracks, replace=mode == "replace", play=mode == "replace")
    noun = "1 track" if len(tracks) == 1 else f"{len(tracks)} tracks"
    verb = "playing" if mode == "replace" else "queued"
    names = ", ".join(str(track.get("name") or "unknown track")
                      for track in tracks[:8])
    remaining = len(tracks) - 8
    if remaining > 0:
        names += f", +{remaining} more"
    return {"message": f"{verb} {noun} added {period.replace('_', ' ')}"
                       f" — {names}"}


def add_added_to_playlist(args: dict[str, Any]) -> dict[str, Any]:
    """Put date-selected local tracks in a real Music.app user playlist.

    This deliberately bypasses the centralized playback queue. It neither
    plays nor imports catalog items; it only organizes tracks already present
    in the local library.
    """
    period, tracks = _tracks_added_in(str(args.get("period") or "today"))
    if not tracks:
        return {"message": f"no library music was added "
                           f"{period.replace('_', ' ')}",
                "acted": False}
    default_names = {
        "today": "Recently Added — Today",
        "yesterday": "Recently Added — Yesterday",
        "this_week": "Recently Added — This Week",
    }
    name = str(args.get("playlist") or "").strip()
    if not name:
        name = default_names.get(period, f"Recently Added — {period}")
    result = _music().add_tracks_to_playlist(name, _pids(tracks))
    added = max(0, int(result.get("added") or 0))
    total = max(0, int(result.get("total") or 0))
    created = bool(result.get("created"))
    if not added and not created:
        return {"message": f"{name} already contains all {len(tracks)} "
                           f"matching tracks ({total} total)",
                "acted": False}
    verb = "created" if created else "updated"
    noun = "track" if added == 1 else "tracks"
    names = ", ".join(str(track.get("name") or "unknown track")
                      for track in tracks[:5])
    remaining = len(tracks) - 5
    if remaining > 0:
        names += f", +{remaining} more"
    return {"message": f"{verb} {name} and added {added} {noun} "
                       f"({total} total) — {names}"}


def add_tracks_to_playlist(name: str,
                           tracks: list[dict[str, Any]]) -> dict[str, Any]:
    """Save already-verified tracks without touching playback."""
    playlist = str(name or "").strip()
    if not playlist or len(playlist) > 100:
        raise ValueError("playlist name must contain 1 to 100 characters")
    clean = []
    seen: set[str] = set()
    for track in tracks:
        if not isinstance(track, dict):
            continue
        pid = str(track.get("pid") or "")
        catalog_id = str(track.get("catalog_id") or track.get("id") or "")
        identity = pid or catalog_id
        if ((_valid_library_id(pid) if pid else
             bool(MEDIA_ID_RE.fullmatch(catalog_id)))
                and identity not in seen):
            clean.append(track)
            seen.add(identity)
    if not clean:
        return {"message": "no verified songs were available",
                "acted": False}
    source = _music()
    if hasattr(source, "add_items_to_playlist"):
        result = source.add_items_to_playlist(playlist, clean)
    else:  # compatibility for small third-party/test doubles
        result = source.add_tracks_to_playlist(playlist, _pids(clean))
    added = max(0, int(result.get("added") or 0))
    total = max(0, int(result.get("total") or 0))
    created = bool(result.get("created"))
    if not added and not created:
        return {"message": f"{playlist} already contains all "
                           f"{len(clean)} selected songs ({total} total)",
                "acted": False}
    verb = "created" if created else "updated"
    noun = "song" if added == 1 else "songs"
    names = ", ".join(str(track.get("name") or "unknown track")
                      for track in clean[:5])
    if len(clean) > 5:
        names += f", +{len(clean) - 5} more"
    imported = max(0, int(result.get("imported") or 0))
    imported_note = (f"; saved {imported} from "
                     f"{service_info().get('name') or 'the music service'}"
                     if imported else "")
    return {"message": f"{verb} {playlist} and added {added} {noun} "
                       f"({total} total{imported_note}) — {names}",
            "acted": True}


def queue_add(args: dict[str, Any]) -> dict[str, Any]:
    """Append to avctl's logical queue, starting it when it was empty."""
    pid = str(args.get("pid") or "")
    playlist = str(args.get("playlist") or "")
    if playlist:
        # Bulk from the source playlist, like play_playlist and for the
        # same reason: cloud tracks survive, library lookups drop them.
        tracks = playlist_tracks(playlist).get("tracks") or []
        pids = _pids(tracks)
        if not pids:
            raise ValueError("that playlist is empty")
        _playlist_dispatch(playlist, tracks, 1, replace=False, play=False)
        noun = "1 song" if len(pids) == 1 else f"{len(pids)} songs"
        return {"message": f"added {noun} to the "
                           f"{_music().queue_playlist} queue"}
    if pid:
        tracks = [{"pid": pid,
                   "name": str(args.get("name") or ""),
                   "artist": str(args.get("artist") or ""),
                   "album": str(args.get("album") or "")}]
    else:
        tracks = album_tracks(str(args.get("album", "")),
                              str(args.get("artist", "")))
        if not tracks:
            raise ValueError("nothing to queue")
        tracks = order_queue_batch(tracks)
    pids = _pids(tracks)
    # Append to the logical tail. The projection pump preserves that order
    # even when some older tracks have not reached Music's playlist yet.
    _dispatch(tracks, replace=False, play=False)
    noun = "1 song" if len(pids) == 1 else f"{len(pids)} songs"
    return {"message": f"added {noun} to the {_music().queue_playlist} queue"}


def clear_queue(args: dict[str, Any]) -> dict[str, Any]:
    """Stop, and empty the queue: one act, because that is what is wanted.

    Music would otherwise play the current track out over an empty queue --
    it keeps the track open after it leaves the playlist -- and a clear that
    leaves music playing is not a clear.
    """
    dropped = 0
    def stop_and_empty() -> None:
        nonlocal dropped
        dropped = _music().clear_queue()
    # Voids any drip: a clear must beat tracks still on their way in.
    _void_dispatch(stop_and_empty)
    if not dropped:
        return {"message": "stopped -- the queue was already empty"}
    noun = "1 song" if dropped == 1 else f"{dropped} songs"
    return {"message": f"stopped and cleared {noun}"}


def _vol_nudge(direction: int) -> dict[str, Any]:
    """One keyboard-sized volume step: the mac's volume keys walk 0..100 in
    sixteenths, so the knob walks the same lattice -- round(i*100/16) for
    i 0..16, which round-trips exactly through AppleScript (verified
    2026-08-02: 0, 6, 12, 19, 25, ... 94, 100). A volume set by other means
    snaps to the nearest sixteenth on the first tap, as the keys do."""
    music = _music()
    current = music.now_playing().get("volume")
    if current is None:
        raise MusicError("could not read the mini's output volume")
    step = max(0, min(16, round(int(current) * 16 / 100) + direction))
    target = min(max_volume(), round(step * 100 / 16))
    music.set_system_volume(target)
    return {"message": f"mini volume {target}%"}


def max_volume() -> int:
    """The Mac mini safety ceiling shared by every control surface."""
    value = device_config.load_config().get("music", {}).get("max_volume", 80)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise RuntimeError("music.max_volume must be an integer 0-100")
    return value


def vol_set(args: dict[str, Any]) -> dict[str, Any]:
    level = args.get("level")
    if not isinstance(level, (int, float)) or isinstance(level, bool):
        raise ValueError("args.level must be a number 0-100")
    requested = int(level)
    target = max(0, min(max_volume(), requested))
    _music().set_system_volume(target)
    suffix = f" (limited to {max_volume()})" if target != requested else ""
    return {"message": f"mini volume {target}%{suffix}"}


def vol_up(args: dict[str, Any]) -> dict[str, Any]:
    return _vol_nudge(1)


def vol_down(args: dict[str, Any]) -> dict[str, Any]:
    return _vol_nudge(-1)


def mute_toggle(args: dict[str, Any]) -> dict[str, Any]:
    music = _music()
    muted = not bool(music.now_playing().get("muted"))
    music.set_system_muted(muted)
    return {"message": "mini muted" if muted else "mini unmuted"}


def set_muted(
    muted: bool,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Build an idempotent system-mute handler for intent-bearing callers."""
    def handler(args: dict[str, Any]) -> dict[str, Any]:
        _music().set_system_muted(muted)
        return {"message": "mini muted" if muted else "mini unmuted"}
    return handler


def scene_volume(args: dict[str, Any]) -> dict[str, Any]:
    """The music scene's mini step: system output to the scene's level.

    The number lives in config (music.scene_volume) so retuning the room
    is a config edit, not a code change. System volume is meaningful with
    Music not running, so no running check is needed here.
    """
    config = device_config.load_config()
    level = (config.get("music") or {}).get("scene_volume")
    if not isinstance(level, int):
        # Where it lived before 3.0's config cleanup.
        level = (config.get("scenes", {}).get("music", {})
                 .get("macos", {}).get("volume"))
    if not isinstance(level, int):
        raise RuntimeError("music.scene_volume is not set in config.yaml")
    requested = level
    level = min(max_volume(), level)
    _music().set_system_volume(level)
    suffix = f" (limited to {max_volume()})" if level != requested else ""
    return {"message": f"mini volume {level}%{suffix}"}


def show_player(args: dict[str, Any]) -> dict[str, Any]:
    """The music scene's screen step: the big Now Playing view on the TV,
    display woken, lyrics on, fullscreen. Idempotent -- see the device."""
    _music().show_player()
    return {"message": "big player on the TV"}


def quiesce(args: dict[str, Any]) -> dict[str, Any]:
    """The everything-off scene's music step: pause, and forget the queue.

    Checked against the running state first so a quit Music stays quit --
    and the phone is told what actually happened, not a generic "done".

    Through _void_dispatch, like clear_queue: this empties the queue
    wholesale, and a drip still in flight would otherwise refill it after
    the room went dark (#54). Off must beat tracks still on their way in.
    """
    music = _music()
    now = music.now_playing()
    _watch_queue(now)
    if now.get("state") == "not_running":
        return {"message": "Music was not running"}
    was_playing = now.get("state") == "playing"
    cleared = 0
    def pause_and_empty() -> None:
        nonlocal cleared
        cleared = music.quiesce()
    _void_dispatch(pause_and_empty)
    return {"message": ", ".join([
        "Music paused" if was_playing else "Music already quiet",
        f"queue cleared ({cleared} song{'s' if cleared != 1 else ''})"
        if cleared else "queue already empty",
    ])}


def refresh(args: dict[str, Any]) -> dict[str, Any]:
    global _recent_cache, _recent_songs_cache, _explore_cache
    with _LOCK:
        _recent_cache = None
        _recent_songs_cache = None
    with _EXPLORE_LOCK:
        _explore_cache = None
    try:
        SONGS_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    return {"message": "re-scanning recently added"}


# --- configured discovery service ----------------------------------------


def search(term: str) -> dict[str, Any]:
    rows = _music().search_service(term, ["albums", "songs"], 12)
    return {
        "albums": [row for row in rows if row.get("kind") == "album"],
        "songs": [row for row in rows if row.get("kind") == "song"],
    }


def search_albums(term: str, limit: int = 50) -> list[dict[str, Any]]:
    """Catalog search, albums only -- the simplified search's whole answer."""
    return _music().search_service_albums(term, limit)


def search_catalog(term: str, kinds: list[str],
                   limit: int = 8) -> list[dict[str, Any]]:
    """Service songs, albums, and editorial/user-facing playlists."""
    return _music().search_service(term, kinds, limit)


def search_catalog_many(terms: list[str], kinds: list[str],
                        limit: int = 8) -> list[list[dict[str, Any]]]:
    """Resolve independent terms through one provider operation when able."""
    return _music().search_service_many(terms, kinds, limit)


def catalog_album(album_id: str) -> dict[str, Any]:
    """One catalog album with its tracks, for the add-or-cherry-pick view."""
    return _music().service_album(album_id)


def catalog_tracks(kind: str, catalog_id: str) -> list[dict[str, Any]]:
    """Resolve one catalog container into directly playable song IDs."""
    return _music().service_tracks(kind, catalog_id)


def explore(limit: int = 10) -> dict[str, Any]:
    """Local listening affinity plus real service recommendations/charts."""
    global _explore_cache
    cap = min(20, max(1, int(limit)))
    songs = [row for row in recent_songs()
             if isinstance(row, dict) and _valid_library_id(
                 str(row.get("pid") or ""))]

    def number(row: dict[str, Any], key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    songs.sort(key=lambda row: (
        row.get("favorited") is True, number(row, "plays"),
        number(row, "lastPlayed"), number(row, "added")), reverse=True)
    local = [{
        "source": "library", "kind": "song", "pid": row.get("pid"),
        "name": row.get("name"), "artist": row.get("artist"),
        "album": row.get("album"), "art": None,
    } for row in songs[:cap]]
    sections: list[dict[str, Any]] = []
    if local:
        sections.append({
            "id": "library-rotation", "title": "Your Rotation",
            "kind": "library", "items": local,
        })

    selected = _music()
    info = selected.service_info(user_token())
    if not info.get("available"):
        return {"sections": sections, "authorized": False,
                "personalized": False, "catalog_available": False,
                "service": info}
    token = user_token()
    authorized = bool(info.get("authorized"))
    now = time.monotonic()
    with _EXPLORE_LOCK:
        cached = _explore_cache
        if cached and cached[0] > now and cached[1] == authorized:
            catalog = cached[2]
        else:
            try:
                # Compatibility for older isolated tests that replaced the
                # former MusicKit seam. Production always dispatches through
                # the configured MusicSource implementation.
                if _musickit is not _ORIGINAL_MUSICKIT:
                    legacy = _musickit()
                    if legacy is None:
                        raise MusicError("music service unavailable")
                    catalog = legacy.explore(token, limit=20)
                else:
                    catalog = selected.explore_service(token, limit=20)
            except MusicError:
                return {"sections": sections, "authorized": authorized,
                        "personalized": False, "personal_error": None,
                        "catalog_available": False, "service": info}
            _explore_cache = (
                time.monotonic() + _EXPLORE_TTL, authorized, catalog)
    catalog_sections = []
    service_source = str(info.get("source") or "service")
    for section in catalog.get("sections") or []:
        if not isinstance(section, dict):
            continue
        catalog_sections.append({
            **section,
            "items": [{**item, "source": service_source}
                      for item in list(section.get("items") or [])[:cap]
                      if isinstance(item, dict)],
        })
    return {
        "sections": sections + catalog_sections,
        "authorized": authorized,
        "personalized": bool(catalog.get("personalized")),
        "personal_error": catalog.get("personal_error"),
        "catalog_available": True,
        "service": info,
    }


def _query_variants(query: str) -> list[str]:
    """The query, plus its other Chinese.

    This library is tagged in traditional script (周杰倫); a phone keyboard
    often types simplified (周杰伦), and substring matching knows nothing of
    the difference. opencc converts both ways when installed
    (requirements.txt); without it the original query stands alone.
    """
    variants = [query]
    try:
        from opencc import OpenCC
        for table in ("s2t", "t2s"):
            converted = OpenCC(table).convert(query)
            if converted not in variants:
                variants.append(converted)
    except Exception:  # noqa: BLE001 - conversion is an upgrade, never a gate
        pass
    return variants


def _search_all_variants(query: str) -> list[dict[str, Any]]:
    """JXA hits for the query in every script it might be written in."""
    seen: set[str] = set()
    hits: list[dict[str, Any]] = []
    for variant in _query_variants(query):
        for track in _music().search_library(variant):
            pid = str(track.get("pid"))
            if pid in seen:
                continue
            seen.add(pid)
            hits.append(track)
    return hits


def local_album_search(query: str) -> list[dict[str, Any]]:
    """Library search, grouped to albums and ranked by how hard they match.

    The JXA search returns matching SONGS; an album whose name matches puts
    every one of its tracks in that list, so ranking groups by hit count
    floats name-matches and heavily-matching albums to the top for free.
    The representative pid is any member track's -- the artwork route
    resolves either kind.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for track in _search_all_variants(query):
        # Group by the ALBUM's artist where one is set: a soundtrack is one
        # album by Various Artists, not fifteen one-song albums by whoever
        # sang each track -- and the tile's artist must be a name that
        # album_tracks() can match, or the opened album arrives empty
        # (the Beauty-and-the-Beast bug, 2026-08-08).
        owner = track.get("albumArtist") or track.get("artist") or ""
        key = ((track.get("album") or "").lower(), owner.lower())
        group = groups.setdefault(key, {
            "album": track.get("album") or "unknown album",
            "artist": owner,
            "pid": track.get("pid"),
            "hits": 0,
        })
        group["hits"] += 1
    ranked = sorted(groups.values(), key=lambda g: -g["hits"])
    return ranked[:50]


def dev_token() -> str:
    return _music().service_developer_token()


def user_token() -> str | None:
    try:
        token = USER_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def save_user_token(token: str) -> None:
    global _explore_cache
    USER_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(USER_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    with _EXPLORE_LOCK:
        _explore_cache = None


def local_search(query: str) -> list[dict[str, Any]]:
    return _music().search_library(query)


def play_song(args: dict[str, Any]) -> dict[str, Any]:
    """Double-tap on a local search hit: the queue becomes just this song.

    Through dispatch like every other wholesale replacement: this was the
    one path that replaced the queue WITHOUT bumping the generation, and a
    drip in flight would refill the replaced queue with the old album right
    after the chosen song (#53).
    """
    pid = str(args.get("pid", ""))
    _dispatch([{"pid": pid, "name": str(args.get("name") or "")}],
              replace=True, play=True)
    return {"message": f"playing {args.get('name') or 'song'}"}


def _service_action(args: dict[str, Any], *, replace: bool) -> dict[str, Any]:
    kind = str(args.get("kind") or "song")
    item_id = str(args.get("id") or "")
    if kind not in {"song", "album", "playlist"}:
        raise ValueError("service item must be a song, album, or playlist")
    if not MEDIA_ID_RE.match(item_id):
        raise ValueError(f"not a music service id: {item_id!r}")
    if kind == "song":
        tracks = [{
            "catalog_id": item_id,
            "name": str(args.get("name") or ""),
            "artist": str(args.get("artist") or ""),
            "album": str(args.get("album") or ""),
            "art": str(args.get("art") or ""),
        }]
    else:
        tracks = catalog_tracks(kind, item_id)
    if not tracks:
        raise MusicError(f"{_music().service_name} returned no playable tracks")
    _dispatch(tracks, replace=replace, play=replace)
    verb = "playing" if replace else "queued"
    label = str(args.get("name") or args.get("album") or kind)
    return {"message": f"{verb} {label} ({len(tracks)} "
                       f"{'song' if len(tracks) == 1 else 'songs'})"}


def play_service(args: dict[str, Any]) -> dict[str, Any]:
    return _service_action(args, replace=True)


def queue_service(args: dict[str, Any]) -> dict[str, Any]:
    return _service_action(args, replace=False)


def add(args: dict[str, Any]) -> dict[str, Any]:
    token = user_token()
    selected = _music()
    selected.add_service_item(
        str(args.get("kind", "albums")), str(args.get("id", "")), token,
        metadata=args,
    )
    info = selected.service_info(token)
    if info.get("library_kind") == "avctl virtual library":
        refresh({})
        return {"message": "saved to the avctl library"}
    return {"message": f"added to {selected.service_name} library; refresh "
                       "when the provider finishes syncing"}


def add_many(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Save verified service items, isolating failures and refreshing once."""
    token = user_token()
    selected = _music()
    added: list[str] = []
    failed: list[str] = []
    for raw in items:
        item = dict(raw) if isinstance(raw, dict) else {}
        item_id = str(item.get("id") or "")
        try:
            selected.add_service_item(
                str(item.get("kind", "albums")), item_id, token,
                metadata=item,
            )
            added.append(item_id)
        except (MusicError, OSError, RuntimeError, TypeError, ValueError):
            failed.append(item_id)
    info = selected.service_info(token)
    if added and info.get("library_kind") == "avctl virtual library":
        refresh({})
        message = "saved to the avctl library"
    else:
        message = (f"added to {selected.service_name} library; refresh "
                   "when the provider finishes syncing")
    return {
        "message": message,
        "added": len(added), "failed": len(failed),
        "added_ids": added, "failed_ids": failed,
    }
