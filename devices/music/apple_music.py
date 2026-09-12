"""Music.app on the Mac mini itself, driven over AppleScript.

The mini is the source in the music scene (USB -> D900 -> amp), so "control
the music" means controlling the copy of Music.app already running on this
machine -- there is no network protocol, just Apple Events via osascript.

Verified on the actual library (2026-08-02, 857 tracks), and each fact shaped
the code:

* **JXA for reads, classic AppleScript for writes.** `osascript -l JavaScript`
  can JSON.stringify its answer, so track names full of quotes and CJK come
  back parseable instead of delimiter-mangled. More importantly it fetches
  whole columns in one Apple Event (`tracks.name()`), which is what makes the
  full-library scan 0.12s rather than one event per track. Classic AS keeps
  the two write paths -- `write raw data to file` and `duplicate ... to
  playlist` -- because those are its well-trodden ground.

* **`raw data`, not `data`, for artwork.** `data` re-encodes to TIFF; `raw
  data` is the stored bytes (measured: 1200x1200 JPEG, 320KB, 0.15s).

* **Albums have no persistent ID in the scripting model.** Identity is
  (album artist or artist, album name); a representative *track* persistent ID
  stands in as the artwork cache key. Track persistent IDs are 16 hex chars
  and stable for the life of the library entry.

* **Up Next is not scriptable.** No API reads it, appends to it, or inserts
  into it. The `avctl` user playlist stands in as the queue: replace-and-play
  for "play this now", append for "play later". "Play next" cannot be built --
  AppleScript can neither insert at a playlist position nor reorder tracks.
  Deleting tracks from the playlist does not touch the library.

* Untrusted strings (album/artist names) enter JXA only via json.dumps -- a
  JSON string literal is a valid JS string literal. Persistent IDs are
  validated against ^[0-9A-F]{16}$ before touching classic AppleScript, where
  there is no such safe literal form.

Phase 2 (catalog search and add-to-library) speaks the Apple Music web API
instead: MusicKit class below. It needs a developer token (an ES256 JWT from
a paid-account MusicKit key) and, for library writes, a Music User Token that
only MusicKit JS in a browser can mint -- see api/musiclink.py for where that
lands on disk.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import requests

from devices.music import MusicError, MusicSource, PID_RE
from devices.music.catalog_player import CatalogPlayer

CATALOG_URL = "https://api.music.apple.com/v1/catalog/{storefront}/search"
SEARCH_SUGGESTIONS_URL = (
    "https://api.music.apple.com/v1/catalog/{storefront}/search/suggestions"
)
ALBUM_URL = "https://api.music.apple.com/v1/catalog/{storefront}/albums/{id}"
PLAYLIST_TRACKS_URL = (
    "https://api.music.apple.com/v1/catalog/{storefront}/playlists/{id}/tracks"
)
CHARTS_URL = "https://api.music.apple.com/v1/catalog/{storefront}/charts"
RECOMMENDATIONS_URL = "https://api.music.apple.com/v1/me/recommendations"
LIBRARY_ADD_URL = "https://api.music.apple.com/v1/me/library"

# --- the scripts ----------------------------------------------------------
#
# Raw strings, because \u0000 below must reach JavaScript as an escape
# sequence, not as an actual NUL in the source handed to osascript.

# System output volume rides along in the same call: the phone's volume knob
# turns the mini's own output (what the user actually reaches for), and it is
# meaningful whether or not Music is running.
_JXA_NOW_PLAYING = r"""(() => {
  const sys = Application.currentApplication();
  sys.includeStandardAdditions = true;
  const vs = sys.getVolumeSettings();
  const out = {volume: vs.outputVolume, muted: vs.outputMuted};
  const m = Application('Music');
  if (!m.running()) { out.state = 'not_running'; return JSON.stringify(out); }
  // How deep the queue playlist is. Rides along here rather than costing a
  // second osascript, because the play key needs it on every press.
  try {
    const q = m.userPlaylists.byName(QUEUE_JSON);
    out.queued = q.exists() ? q.tracks.length : 0;
  } catch (e) { out.queued = 0; }
  out.state = m.playerState();
  out.shuffle = m.shuffleEnabled();
  out.repeat = m.songRepeat();
  try {
    const t = m.currentTrack;
    out.track = t.name(); out.artist = t.artist(); out.album = t.album();
    out.pid = t.persistentID(); out.duration = t.duration();
    out.position = m.playerPosition();
  } catch (e) {}  // stopped: currentTrack raises
  return JSON.stringify(out);
})()"""

# The whole library in five Apple Events, grouped into albums newest-first --
# ALL of them, not a top-N: the grid pages through to the very end of the
# library the way Music's own Recently Added does, so slicing is the API
# layer's job, not this scan's.
# \u0000 as the join char because it is the one thing a tag cannot contain.
_JXA_RECENT = r"""(() => {
  const t = Application('Music').libraryPlaylists[0].tracks;
  const albums = t.album(), aArtists = t.albumArtist(), artists = t.artist(),
        added = t.dateAdded(), pids = t.persistentID();
  const byAlbum = {};
  for (let i = 0; i < pids.length; i++) {
    const artist = aArtists[i] || artists[i] || '';
    const key = artist + '\u0000' + (albums[i] || '');
    const ts = added[i] ? added[i].getTime() : 0;
    const e = byAlbum[key];
    if (!e) byAlbum[key] = {album: albums[i] || '', artist: artist,
                            pid: pids[i], added: ts, tracks: 1};
    else { e.tracks++; if (ts > e.added) e.added = ts; }
  }
  return JSON.stringify(Object.values(byAlbum)
    .sort((a, b) => b.added - a.added));
})()"""

# One `whose` event narrowed by album name; the artist check happens in JS
# because `whose` cannot express "albumArtist or artist equals X". The length
# guard matters: fetching a column off a zero-match specifier throws -1728
# rather than returning [] (measured 2026-08-02).
# The user's playlists, minus the avctl queue (internal plumbing) and
# folders (containers, not playable). One first-track pid rides along as
# the tile's artwork key -- playlists rarely carry art of their own.
# Kept as SONGS instead of being folded into albums -- the song view is the
# library's other grain. Personal affinity comes from Music's own favorited
# flag and play history; this is local listening data, not catalog popularity.
# Sorted newest-added first here so the API layer only ever slices.
_JXA_RECENT_SONGS = r"""(() => {
  const t = Application('Music').libraryPlaylists[0].tracks;
  const names = t.name(), artists = t.artist(), aArtists = t.albumArtist(),
        albums = t.album(), plays = t.playedCount(), played = t.playedDate(),
        favorites = t.favorited(),
        added = t.dateAdded(), pids = t.persistentID();
  const out = [];
  for (let i = 0; i < pids.length; i++) {
    out.push({name: names[i] || '', artist: artists[i] || '',
              albumArtist: aArtists[i] || '', album: albums[i] || '',
              pid: pids[i], plays: plays[i] || 0,
              favorited: Boolean(favorites[i]),
              lastPlayed: played[i] ? played[i].getTime() : 0,
              added: added[i] ? added[i].getTime() : 0});
  }
  out.sort((a, b) => b.added - a.added);
  return JSON.stringify(out);
})()"""

_JXA_PLAYLISTS = r"""(() => {
  // The FULL playlists collection, not userPlaylists(): Apple's
  // subscription playlists (Replay 2025, New Music) live only in the
  // former -- userPlaylists() silently omits them, which is how the
  // special playlists went missing twice (measured 2026-08-08 on this
  // Music.app). specialKind 'none' is then exactly the shelf: it admits
  // user lists, smart lists and subscription lists alike, and excludes
  // the Library/Music masters and folders.
  const ps = Application('Music').playlists();
  const out = [];
  for (const p of ps) {
    let name, pid;
    try { name = p.name(); pid = p.persistentID(); } catch (e) { continue; }
    if (name === QUEUE_JSON) continue;
    let kind = null;
    try { kind = p.specialKind(); } catch (e) {}
    if (kind !== 'none') continue;
    let count = 0, art = null;
    try { count = p.tracks.length; } catch (e) {}
    if (count > 0) { try { art = p.tracks[0].persistentID(); } catch (e) {} }
    out.push({name: name, pid: pid, count: count, art: art});
  }
  return JSON.stringify(out);
})()"""

_JXA_PLAYLIST_TRACKS = r"""(() => {
  const ps = Application('Music').playlists.whose({persistentID: PID_JSON});
  if (ps.length === 0) return JSON.stringify(null);
  const p = ps[0], tr = p.tracks;
  if (tr.length === 0) return JSON.stringify({name: p.name(), tracks: []});
  const names = tr.name(), artists = tr.artist(), albums = tr.album(),
        durs = tr.duration(), pids = tr.persistentID();
  const out = [];
  for (let i = 0; i < pids.length; i++) {
    out.push({name: names[i], artist: artists[i], album: albums[i],
              duration: durs[i], pid: pids[i]});
  }
  return JSON.stringify({name: p.name(), tracks: out});
})()"""

_JXA_ALBUM_TRACKS = r"""(() => {
  const tr = Application('Music').libraryPlaylists[0].tracks
    .whose({album: ALBUM_JSON});
  if (tr.length === 0) return JSON.stringify([]);
  const names = tr.name(), artists = tr.artist(), aArtists = tr.albumArtist(),
        discs = tr.discNumber(), nums = tr.trackNumber(),
        durs = tr.duration(), pids = tr.persistentID();
  const out = [];
  for (let i = 0; i < pids.length; i++) {
    // Either identity may be the one the caller knows: a compilation's
    // tile says "Various Artists" (the album artist) while its tracks
    // carry their own names -- and a per-artist tile says the reverse.
    // Insisting on one field emptied soundtrack albums (2026-08-08).
    if (ARTIST_JSON && (aArtists[i] || '') !== ARTIST_JSON
        && (artists[i] || '') !== ARTIST_JSON) continue;
    out.push({name: names[i], artist: artists[i], disc: discs[i] || 1,
              num: nums[i] || 0, duration: durs[i], pid: pids[i]});
  }
  out.sort((a, b) => a.disc - b.disc || a.num - b.num);
  return JSON.stringify(out);
})()"""

# Library search: one bulk fetch, substring match across name/artist/album
# in JS (case-insensitive -- `whose` cannot say "contains, any field, any
# case" and three separate whose events would each cost a round trip).
# Same shape as the recently-added scan, so the cost is the same 0.1s.
_JXA_SEARCH_LIBRARY = r"""(() => {
  // Every term must land somewhere in the track's text -- "chou fantasy"
  // finds Fantasy by Jay Chou even though no single field holds both.
  const terms = Q_JSON.toLowerCase().split(/\s+/).filter((t) => t);
  const t = Application('Music').libraryPlaylists[0].tracks;
  const names = t.name(), artists = t.artist(), albums = t.album(),
        aArtists = t.albumArtist(), durs = t.duration(),
        pids = t.persistentID();
  const out = [];
  for (let i = 0; i < pids.length && out.length < 250; i++) {
    const hay = ((names[i] || '') + ' ' + (artists[i] || '') + ' '
      + (albums[i] || '') + ' ' + (aArtists[i] || '')).toLowerCase();
    let ok = true;
    for (const term of terms) {
      if (hay.indexOf(term) === -1) { ok = false; break; }
    }
    if (!ok) continue;
    out.push({name: names[i], artist: artists[i], album: albums[i],
              albumArtist: aArtists[i], duration: durs[i], pid: pids[i]});
  }
  return JSON.stringify(out);
})()"""

# The big player on the TV: Window -> Now Playing (art left, lyrics right),
# lyrics on, fullscreen. UI scripting, because none of it is in Music's
# scripting dictionary -- needs osascript in Accessibility (granted
# 2026-08-05, verified live). Every step is guarded so the scene is
# idempotent: the Now Playing item wears a checkmark while active (clicking
# again would toggle it OFF), the lyrics item reads Show/Hide by state, and
# Enter Full Screen only exists while windowed.
_AS_SHOW_PLAYER = """\
tell application "Music"
\treopen
\tactivate
end tell
delay 0.6
tell application "System Events" to tell process "Music"
\tset nowPlaying to menu item "Now Playing" of menu "Window" of \
menu bar item "Window" of menu bar 1
\tif value of attribute "AXMenuItemMarkChar" of nowPlaying is missing value then
\t\tclick nowPlaying
\t\tdelay 1
\tend if
\tset viewNames to name of every menu item of menu "View" of \
menu bar item "View" of menu bar 1
\tif viewNames contains "Show Lyrics" then
\t\tclick menu item "Show Lyrics" of menu "View" of menu bar item "View" of menu bar 1
\tend if
\tif viewNames contains "Enter Full Screen" then
\t\tclick menu item "Enter Full Screen" of menu "View" of menu bar item "View" of menu bar 1
\tend if
end tell"""

# The library ranked newest-added first. Only the persistent IDs come back;
# the caller caches them, because Music.app's duplicate throughput -- not this
# sort -- is what makes filling a queue slow (measured 2026-08-07: this is
# 0.12s, while duplicating 864 tracks in order is 17s).
_JXA_LIBRARY_ORDER = r"""(() => {
  const t = Application('Music').libraryPlaylists[0].tracks;
  const pids = t.persistentID(), added = t.dateAdded();
  return JSON.stringify(pids
    .map((p, i) => [p, added[i] ? added[i].getTime() : 0])
    .sort((a, b) => b[1] - a[1])
    .map(r => r[0]));
})()"""

# Everything, in one duplicate. Order is Music's internal order, NOT date
# order (measured) -- which is exactly right when shuffle is on and wrong
# otherwise, hence the two paths in musiclink.
_AS_QUEUE_WHOLE_LIBRARY = """\
tell application "Music"
\tif not (exists user playlist "QUEUE") then
\t\tmake new user playlist with properties {name:"QUEUE"}
\tend if
\tset q to user playlist "QUEUE"
\tdelete every track of q
\tduplicate (every track of library playlist 1) to q
\tplay q
end tell"""

# Stop first, then empty: clearing means the room goes quiet, not that the
# last track plays out over an empty queue. Both in one script so it is one
# round trip. Counts before it deletes so the toast can say what it dropped.
# One bulk delete, not an Apple Event per track: `delete every track` empties
# 864 in 0.12s (MEASURED). A missing playlist is no error -- nothing queued
# is nothing to clear, and the stop still happened.
_AS_CLEAR_QUEUE = """\
tell application "Music"
\tstop
\tif not (exists user playlist "QUEUE") then return 0
\tset q to user playlist "QUEUE"
\tset n to count of tracks of q
\tdelete every track of q
\treturn n
end tell"""

_JXA_PLAY_PAUSE = r"""(() => {
  const m = Application('Music');
  if (!m.running()) m.activate();
  m.playpause();
  return 'ok';
})()"""

_JXA_PLAY = r"""(() => {
  const m = Application('Music');
  if (!m.running()) m.activate();
  m.play();
  return 'ok';
})()"""

_JXA_PAUSE = r"""(() => {
  const m = Application('Music');
  if (m.running()) m.pause();
  return 'ok';
})()"""

_AS_ARTWORK = """\
tell application "Music"
\tset t to first track of library playlist 1 whose persistent ID is "PID"
\tif (count of artworks of t) is 0 then error "no artwork"
\tset d to raw data of artwork 1 of t
end tell
set f to open for access POSIX file "DEST" with write permission
set eof f to 0
write d to f
close access f"""


class AppleMusic(MusicSource):
    service_name = "Apple Music"
    service_source = "apple_music"

    def __init__(
        self,
        queue_playlist: str = "avctl",
        timeout: float = 20.0,
        state_timeout: float = 5.0,
        max_system_volume: int = 80,
        catalog_player: str | Path | None = None,
        catalog: "MusicKit | None" = None,
    ):
        # The playlist name lands inside classic AppleScript string literals,
        # where there is no safe quoting -- so refuse names that would need it.
        if '"' in queue_playlist or "\\" in queue_playlist:
            raise ValueError("queue_playlist must not contain quotes or backslashes")
        self.queue_playlist = queue_playlist
        self.timeout = timeout
        # Separate, shorter: now_playing() runs inside every /api/state poll,
        # and a hung Music.app must not stall the whole screen for 20s.
        self.state_timeout = state_timeout
        if not 0 <= int(max_system_volume) <= 100:
            raise ValueError("max_system_volume must be in 0-100")
        self.max_system_volume = int(max_system_volume)
        self.catalog = catalog
        self.catalog_player = CatalogPlayer(
            catalog_player,
            developer_token_provider=self.service_developer_token,
        )

    @classmethod
    def from_config(cls, config: dict) -> "AppleMusic":
        """Unlike the networked devices there is no host that can be missing,
        so an absent music block still yields a working instance."""
        from devices.config import driver_block
        mine = driver_block(config, "music", "AppleMusic")
        return cls(
            queue_playlist=mine.get("queue_playlist") or "avctl",
            max_system_volume=mine.get("max_volume", 80),
            catalog_player=mine.get("catalog_player"),
            catalog=MusicKit.from_config(config),
        )

    # -- raw protocol ----------------------------------------------------

    def _osascript(self, script: str, lang: str = "JavaScript",
                   timeout: float | None = None) -> str:
        """The one place Apple Events are actually sent.

        If the LaunchDaemon context ever cannot reach Music.app (TCC -1743 /
        session -600), the fix -- whatever shape it takes -- goes here, and
        the rest of this class never knows.
        """
        try:
            proc = subprocess.run(
                ["/usr/bin/osascript", "-l", lang, "-e", script],
                capture_output=True, text=True,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise MusicError("Music.app did not answer in time")
        except OSError as exc:
            raise MusicError(f"could not run osascript: {exc}")
        if proc.returncode != 0:
            # stderr verbatim: for TCC failures it carries the error number
            # that decides what to do next, and paraphrasing it helps nobody.
            raise MusicError(proc.stderr.strip() or "osascript failed")
        return proc.stdout.strip()

    def _jxa_json(self, script: str, timeout: float | None = None) -> Any:
        out = self._osascript(script, timeout=timeout)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise MusicError(f"unparseable answer from Music.app: {out[:120]}")

    @staticmethod
    def _check_pids(pids: list[str]) -> list[str]:
        for pid in pids:
            if not PID_RE.match(str(pid)):
                raise ValueError(f"not a track persistent ID: {pid!r}")
        return [str(pid) for pid in pids]

    # -- reads -----------------------------------------------------------

    def now_playing(self) -> dict[str, Any]:
        """Player state, current track, shuffle, repeat and queue depth."""
        script = _JXA_NOW_PLAYING.replace("QUEUE_JSON",
                                          json.dumps(self.queue_playlist))
        catalog = self.catalog_player.state_if_active()
        local = self._jxa_json(script, timeout=self.state_timeout)
        if catalog is None:
            return local
        return {
            "state": catalog.get("state"),
            "track": catalog.get("track"),
            "artist": catalog.get("artist"),
            "album": None,
            "pid": None,
            "catalog_id": catalog.get("catalog_id"),
            "art": catalog.get("artwork"),
            "duration": catalog.get("duration"),
            "position": catalog.get("position"),
            "shuffle": catalog.get("shuffle"),
            "repeat": "one" if catalog.get("repeat_one") else "off",
            "volume": local.get("volume"),
            "muted": local.get("muted"),
        }

    def library_order(self) -> list[str]:
        """Every track's persistent ID, newest-added first."""
        return self._jxa_json(_JXA_LIBRARY_ORDER)

    def play_queue(self) -> None:
        """Play the queue playlist from the top.

        Needed because `playpause` on a STOPPED player has no context to
        resume -- the queue may be full and the button still do nothing.
        """
        if self.catalog_player.active:
            self.catalog_player.command("play")
            return
        self._osascript(
            f'tell application "Music" to play user playlist "{self.queue_playlist}"',
            lang="AppleScript")

    def clear_queue(self) -> int:
        """Stop playback and empty the queue. Returns the tracks dropped.

        The stop is not incidental. Music holds the current track open even
        after it leaves the playlist (MEASURED -- it plays on to the end),
        so without it "clear" would leave a song still going with nothing
        behind it. Clearing means the room goes quiet.
        """
        catalog = 1 if self.catalog_player.active else 0
        if catalog:
            self.catalog_player.command("clear")
        out = self._osascript(
            _AS_CLEAR_QUEUE.replace("QUEUE", self.queue_playlist),
            lang="AppleScript")
        try:
            return max(catalog, int(out.strip()))
        except ValueError:      # a count we cannot read is not a failure
            return 0

    def play_whole_library(self) -> None:
        """Replace the queue with the entire library and play.

        One duplicate for the lot -- about a second, versus seventeen to
        place 864 tracks in a chosen order. The order that comes back is
        Music's own, so this is only correct with shuffle on; ordered play
        goes through play_tracks with a slice of library_order().
        """
        self.catalog_player.deactivate()
        self._osascript(
            _AS_QUEUE_WHOLE_LIBRARY.replace("QUEUE", self.queue_playlist),
            lang="AppleScript", timeout=60.0)

    def recently_added(self) -> list[dict[str, Any]]:
        """Every album newest-first, like Music's Recently Added shelf.

        `added` is JS epoch milliseconds; `pid` is a representative track's
        persistent ID, which is what the artwork route keys on. The full list
        on purpose -- pagination happens against the cached copy upstream.
        """
        return self._jxa_json(_JXA_RECENT)

    def recently_added_songs(self) -> list[dict[str, Any]]:
        """Every track newest-first -- the song-grain twin of
        recently_added(). `added` is JS epoch milliseconds; pagination
        happens against the cached copy upstream."""
        return self._jxa_json(_JXA_RECENT_SONGS)

    def album_tracks(self, album: str, artist: str = "") -> list[dict[str, Any]]:
        script = (_JXA_ALBUM_TRACKS
                  .replace("ALBUM_JSON", json.dumps(album))
                  .replace("ARTIST_JSON", json.dumps(artist)))
        return self._jxa_json(script)

    def playlists(self) -> list[dict[str, Any]]:
        """User playlists, in Music's own order; the avctl queue excluded."""
        script = _JXA_PLAYLISTS.replace(
            "QUEUE_JSON", json.dumps(self.queue_playlist))
        return self._jxa_json(script)

    def playlist_tracks(self, pid: str) -> dict[str, Any] | None:
        """One playlist's name and tracks, in playlist order; None if gone."""
        script = _JXA_PLAYLIST_TRACKS.replace("PID_JSON", json.dumps(pid))
        return self._jxa_json(script)

    def add_tracks_to_playlist(self, name: str,
                               pids: list[str]) -> dict[str, Any]:
        """Create a Music user playlist when needed and add local tracks.

        This is intentionally separate from the internal queue playlist:
        editing a collection must not alter Up Next or start playback.  The
        whole mutation is one AppleScript invocation, and existing persistent
        IDs are skipped so a repeated voice command is idempotent.
        """
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("playlist name must be 1-100 characters")
        if any(ord(char) < 32 for char in name):
            raise ValueError("playlist name contains control characters")
        if name.casefold() == self.queue_playlist.casefold():
            raise ValueError("the avctl queue is not a user playlist")
        checked = self._check_pids(pids)
        if not checked:
            raise ValueError("nothing to add to the playlist")

        # JSON's quote/backslash escapes are also valid AppleScript string
        # escapes. ensure_ascii=False keeps CJK names literal rather than
        # emitting JSON \u escapes, which AppleScript does not understand.
        destination = json.dumps(name, ensure_ascii=False)
        lines = [
            'tell application "Music"',
            "\tset madePlaylist to false",
            f"\tif not (exists user playlist {destination}) then",
            "\t\tmake new user playlist with properties "
            f"{{name:{destination}}}",
            "\t\tset madePlaylist to true",
            "\tend if",
            f"\tset destinationPlaylist to user playlist {destination}",
            "\tset beforeCount to count of tracks of destinationPlaylist",
        ]
        for pid in checked:
            lines += [
                f'\tif (count of (every track of destinationPlaylist whose '
                f'persistent ID is "{pid}")) is 0 then',
                f'\t\tduplicate (every track of library playlist 1 whose '
                f'persistent ID is "{pid}") to destinationPlaylist',
                "\tend if",
            ]
        lines += [
            "\tset finalCount to count of tracks of destinationPlaylist",
            "\treturn ((finalCount - beforeCount) as string) & "
            "(ASCII character 9) & (finalCount as string) & "
            "(ASCII character 9) & (madePlaylist as string)",
            "end tell",
        ]
        out = self._osascript("\n".join(lines), lang="AppleScript",
                              timeout=60.0)
        parts = out.split("\t")
        if len(parts) != 3:
            raise MusicError("unparseable playlist result from Music.app")
        try:
            added, total = int(parts[0]), int(parts[1])
        except ValueError:
            raise MusicError("unparseable playlist counts from Music.app") from None
        return {
            "name": name,
            "created": parts[2].strip().casefold() == "true",
            "added": added,
            "total": total,
        }

    def search_library(self, query: str) -> list[dict[str, Any]]:
        """Local search: library songs whose name, artist or album contains
        the query, case-insensitively. Capped at 30 -- a phone list."""
        script = _JXA_SEARCH_LIBRARY.replace("Q_JSON", json.dumps(query))
        return self._jxa_json(script)

    # -- transport -------------------------------------------------------

    def play_pause(self) -> None:
        catalog = self.catalog_player.state_if_active()
        if catalog is not None:
            self.catalog_player.command(
                "pause" if catalog.get("state") == "playing" else "play")
            return
        self._osascript(_JXA_PLAY_PAUSE)

    def play(self) -> None:
        if self.catalog_player.active:
            self.catalog_player.command("play")
            return
        self._osascript(_JXA_PLAY)

    def pause(self) -> None:
        if self.catalog_player.active:
            self.catalog_player.command("pause")
            return
        self._osascript(_JXA_PAUSE)

    def next_track(self) -> None:
        if self.catalog_player.active:
            self.catalog_player.command("next")
            return
        self._osascript("Application('Music').nextTrack()")

    def previous_track(self) -> None:
        if self.catalog_player.active:
            self.catalog_player.command("previous")
            return
        self._osascript("Application('Music').previousTrack()")

    # Shuffle and repeat need a two-step dance. MEASURED 2026-08-02 on this
    # Music.app: `shuffle enabled` and `song repeat` accept a set and
    # SILENTLY IGNORE it until Music has played something since it launched;
    # after its first playback the same sets stick fine (verified both ways).
    # Reads work throughout. So: try the property, read it back, and when it
    # did not stick, click the actual Controls menu via System Events -- which
    # requires osascript to be granted Accessibility once (System Settings ->
    # Privacy & Security -> Accessibility -> add /usr/bin/osascript). On a
    # mini where Music plays daily the fallback is nearly never taken.

    def _menu_click(self, submenu: str, item: str) -> None:
        script = (
            'tell application "System Events" to tell process "Music"\n'
            f'\tclick menu item "{item}" of menu "{submenu}" of '
            f'menu item "{submenu}" of menu "Controls" of menu bar 1\n'
            "end tell"
        )
        try:
            self._osascript(script, lang="AppleScript")
        except MusicError as exc:
            if "assistive access" in str(exc) or "-1719" in str(exc):
                raise MusicError(
                    "this macOS ignores the scripted shuffle/repeat switch; "
                    "the menu-click fallback needs Accessibility for "
                    "/usr/bin/osascript (System Settings -> Privacy & "
                    "Security -> Accessibility)")
            raise

    def set_shuffle(self, on: bool) -> None:
        if self.catalog_player.active:
            self.catalog_player.command("shuffle_on" if on else "shuffle_off")
            return
        self._osascript(
            f"Application('Music').shuffleEnabled = {'true' if on else 'false'}")
        state = self._jxa_json(
            "JSON.stringify(Application('Music').shuffleEnabled())",
            timeout=self.state_timeout)
        if bool(state) != on:
            self._menu_click("Shuffle", "On" if on else "Off")

    def set_repeat_one(self, on: bool) -> None:
        if self.catalog_player.active:
            self.catalog_player.command("repeat_one_on" if on else "repeat_off")
            return
        # 'one' or 'off' only: 'all' is not a state this remote offers, and a
        # toggle between three values needs a screen to say where it landed.
        target = "one" if on else "off"
        self._osascript(f"Application('Music').songRepeat = '{target}'")
        state = self._jxa_json(
            "JSON.stringify(Application('Music').songRepeat())",
            timeout=self.state_timeout)
        if state != target:
            self._menu_click("Repeat", "One" if on else "Off")

    def show_player(self) -> None:
        """Put the big Now Playing view, lyrics and all, on the TV.

        Wakes the display first (the mini blanks it after 10 minutes and a
        dark TV looks like a broken scene), then drives Music's menus --
        see _AS_SHOW_PLAYER for why UI scripting and how it stays
        idempotent."""
        subprocess.Popen(
            ["/usr/bin/caffeinate", "-u", "-t", "3"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._osascript(_AS_SHOW_PLAYER, lang="AppleScript")

    # -- the mini's own output volume ------------------------------------
    # macOS system volume, not Music's sound volume: the user's habit is the
    # mini's volume, and Music's own slider stays at 100 feeding the DAC.

    def set_system_volume(self, level: int) -> None:
        level = max(0, min(self.max_system_volume, int(level)))
        # An audible level clears mute too, in the same call: `output volume
        # 56` with `output muted true` still standing is silence wearing a
        # number -- the Music scene set the room's level and the room stayed
        # quiet (found live 2026-08-09 with the mini muted). Matching the
        # hardware volume keys' behavior; zero stays zero and leaves mute
        # alone, since 0 already means silence.
        unmute = " output muted false" if level > 0 else ""
        self._osascript(f"set volume output volume {level}{unmute}",
                        lang="AppleScript", timeout=self.state_timeout)

    def set_system_muted(self, on: bool) -> None:
        self._osascript(
            f"set volume output muted {'true' if on else 'false'}",
            lang="AppleScript", timeout=self.state_timeout)

    # -- the queue playlist ----------------------------------------------

    def _queue_script(self, pids: list[str], replace: bool, play: bool) -> str:
        lines = [
            'tell application "Music"',
            f'\tif not (exists user playlist "{self.queue_playlist}") then',
            f'\t\tmake new user playlist with properties '
            f'{{name:"{self.queue_playlist}"}}',
            "\tend if",
            f'\tset q to user playlist "{self.queue_playlist}"',
        ]
        if replace:
            lines.append("\tdelete every track of q")
        lines += [
            f'\tduplicate (every track of library playlist 1 '
            f'whose persistent ID is "{pid}") to q'
            for pid in self._check_pids(pids)
        ]
        if play:
            # The playlist itself, never `play track N of q`: playing a bare
            # track gives Music NO playing context, and `next track` then
            # does nothing at all (measured 2026-08-02 -- this presented as
            # "the next button is broken"). Playing the playlist is what
            # makes next/previous walk it.
            #
            # Shuffle is lifted for the play (`play q` under shuffle
            # starts at a random track -- measured, an album play opened on
            # track 7) and NOT put back: re-enabling it materialized a
            # shuffled Up Next from the playlist snapshot, and every track
            # appended afterwards -- queue_add, the drip's chunks -- joined
            # the playlist but never the shuffled queue. "Queued music
            # never gets played" (measured live 2026-08-10, shuffle:true
            # mid-queue). An ordered dispatch MEANS this order; the shuffle
            # key on the transport re-enables it deliberately.
            lines += [
                "\tif shuffle enabled then set shuffle enabled to false",
                "\tplay q",
            ]
        lines.append("end tell")
        return "\n".join(lines)

    def queue_playlist_bulk(self, playlist_pid: str, start_pos: int,
                            replace: bool, play: bool) -> None:
        """The queue built from a SOURCE PLAYLIST, by position, in one
        duplicate -- never by library lookup: a subscription playlist's
        cloud tracks are not library members, and pid-by-pid duplication
        silently drops them (measured 2026-08-08: New Music would have
        lost 23 of its 25 tracks). start_pos is 1-based, Apple's way.
        """
        if not PID_RE.match(playlist_pid):
            raise ValueError(f"not a playlist id: {playlist_pid!r}")
        if replace and play:
            self.catalog_player.deactivate()
        src = f'(first playlist whose persistent ID is "{playlist_pid}")'
        span = ("every track of src" if start_pos <= 1
                else f"tracks {int(start_pos)} thru -1 of src")
        lines = [
            'tell application "Music"',
            f'\tif not (exists user playlist "{self.queue_playlist}") then',
            f'\t\tmake new user playlist with properties '
            f'{{name:"{self.queue_playlist}"}}',
            "\tend if",
            f'\tset q to user playlist "{self.queue_playlist}"',
            f"\tset src to {src}",
        ]
        if replace:
            lines.append("\tdelete every track of q")
        lines.append(f"\tduplicate ({span}) to q")
        if play:
            # Shuffle lifted and NOT restored, same reason as _queue_script:
            # the restore froze Up Next at the play-time snapshot and every
            # later append fell on deaf ears (#137).
            lines += [
                "\tif shuffle enabled then set shuffle enabled to false",
                "\tplay q",
            ]
        lines.append("end tell")
        # lang matters: the default is JavaScript, and classic AppleScript
        # fed to the JS parser dies on its second word -- "Unexpected
        # identifier 'application'" -- which is exactly how every playlist
        # tap failed from the day #87 shipped until this line said so.
        self._osascript("\n".join(lines), lang="AppleScript")

    def play_tracks(self, pids: list[str], start: int = 0) -> None:
        """Rebuild the queue playlist from `start` onward and play it.

        "Play this album" is start=0; "play this song" is start=its index,
        which keeps the songs after it and drops the ones before -- the same
        thing tapping mid-album does in Apple Music proper.
        """
        if not pids:
            raise ValueError("nothing to play")
        if not 0 <= start < len(pids):
            raise ValueError(f"start {start} out of range")
        self.catalog_player.deactivate()
        self._osascript(self._queue_script(pids[start:], replace=True, play=True),
                        lang="AppleScript")

    def queue_append(self, pids: list[str]) -> None:
        """Append to the queue playlist -- "play later", the only queue verb
        AppleScript can offer (no insertion, so no "play next")."""
        if not pids:
            raise ValueError("nothing to queue")
        self._osascript(self._queue_script(pids, replace=False, play=False),
                        lang="AppleScript")

    def play_catalog(self, catalog_ids: list[str]) -> None:
        """Stream catalog songs directly without changing Music's library."""
        if not catalog_ids:
            raise ValueError("nothing to play")
        # Settle MusicKit permission before pausing Music.app. If consent is
        # missing, this path is a known no-op rather than a half-player swap.
        self.catalog_player.ensure_authorized()
        self._osascript(_JXA_PAUSE)
        self.catalog_player.replace(catalog_ids)

    def queue_catalog(self, catalog_ids: list[str]) -> None:
        if not catalog_ids:
            raise ValueError("nothing to queue")
        self.catalog_player.append(catalog_ids)

    def quiesce(self) -> int:
        """Pause playback and empty the queue playlist; returns how many
        queued tracks were dropped. One Apple Event round-trip.

        The caller must first check Music is running (now_playing) -- a bare
        `tell` LAUNCHES a quit app, which is the opposite of what the
        everything-off scene doing the calling means.
        """
        script = "\n".join([
            'tell application "Music"',
            "\tpause",  # a no-op when already paused or stopped
            f'\tif not (exists user playlist "{self.queue_playlist}") '
            "then return 0",
            f'\tset q to user playlist "{self.queue_playlist}"',
            "\tset n to count of tracks of q",
            "\tdelete every track of q",
            "\treturn n",
            "end tell",
        ])
        catalog = 1 if self.catalog_player.active else 0
        if catalog:
            self.catalog_player.command("clear")
        out = self._osascript(script, lang="AppleScript")
        try:
            return max(catalog, int(out))
        except ValueError:
            return 0

    # -- artwork ---------------------------------------------------------

    def extract_artwork(self, pid: str, dest: Path) -> None:
        """Write a track's stored artwork bytes to `dest`. Raises MusicError
        when the track has none (the route turns that into a 404).

        The AppleScript writes to a sibling temp path and the rename happens
        here, after the bytes are known to be whole: `dest` existing is the
        cache-hit test upstream AND the route caches it immutable for a week,
        so a crash mid-write must never leave a partial file under the final
        name -- that is a broken cover the phone keeps for seven days.
        """
        (pid,) = self._check_pids([pid])
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Unique per CALL, not per process (#111): two threads extracting the
        # same pid would share a per-pid temp name -- one truncates the
        # other's in-flight write and the finally deletes it out from under
        # the os.replace. Today musiclink's _ARTWORK_GATE serializes callers,
        # but the safety must not depend on an upstream semaphore.
        temp = dest.with_name(
            f".{dest.name}.{os.getpid()}.{threading.get_ident()}.part")
        script = _AS_ARTWORK.replace("PID", pid).replace("DEST", str(temp))
        try:
            self._osascript(script, lang="AppleScript")
            if not temp.exists() or temp.stat().st_size == 0:
                raise MusicError(f"no artwork bytes came back for {pid}")
            os.replace(temp, dest)
        finally:
            temp.unlink(missing_ok=True)

    # -- generic discovery-service contract -----------------------------

    def _catalog(self) -> "MusicKit":
        if self.catalog is None:
            raise MusicError(
                "Apple Music needs music.AppleMusic.team_id / key_id in "
                "config.yaml (an Apple Developer account)")
        return self.catalog

    def service_info(self, credential: str | None = None) -> dict[str, Any]:
        available = self.catalog is not None
        return {
            "name": self.service_name,
            "source": self.service_source,
            "available": available,
            "authorized": available and bool(credential),
            "personalized": available and bool(credential),
            "can_stream_service": available and self.catalog_player.available(),
            "can_add_to_library": available and bool(credential),
            "library_kind": "Music.app library",
            "can_edit_playlists": True,
            "supports_date_added": True,
            "supports_play_history": True,
            "batched_search": False,
            "authorization_url": "/music/auth" if available else None,
        }

    def search_service_albums(self, term: str,
                              limit: int = 50) -> list[dict[str, Any]]:
        return self._catalog().search_albums(term, limit)

    def search_service(self, term: str, kinds: list[str],
                       limit: int = 8) -> list[dict[str, Any]]:
        return self._catalog().search_catalog(term, kinds, limit)

    def service_album(self, item_id: str) -> dict[str, Any]:
        return self._catalog().album_detail(item_id)

    def service_tracks(self, kind: str,
                       item_id: str) -> list[dict[str, Any]]:
        if kind == "album":
            album = self._catalog().album_detail(item_id)
            art = album.get("art")
            name = album.get("album")
            return [{
                **track,
                "catalog_id": track.get("id"),
                "album": name,
                "art": art,
            } for track in album.get("tracks") or [] if track.get("id")]
        if kind == "playlist":
            return self._catalog().playlist_tracks(item_id)
        raise ValueError("service container must be an album or playlist")

    def explore_service(self, credential: str | None = None,
                        limit: int = 10) -> dict[str, Any]:
        return self._catalog().explore(credential, limit)

    def add_service_item(self, kind: str, item_id: str,
                         credential: str | None = None,
                         metadata: dict[str, Any] | None = None) -> None:
        if not credential:
            raise MusicError(
                "no Music User Token yet -- authorize once at /music/auth")
        self._catalog().add_to_library(kind, item_id, credential)

    def service_developer_token(self) -> str:
        return self._catalog().dev_token()


# --- phase 2: the Apple Music web API -------------------------------------


class MusicKit:
    """Catalog search and add-to-library, over api.music.apple.com.

    Two credentials, deliberately kept apart:
      * the developer token -- minted here from the MusicKit .p8 key, proves
        the *app*;
      * the Music User Token -- proves the *account*, and can only be minted
        by MusicKit JS in a browser (served by /music/auth). It arrives as a
        plain string; storage is the caller's problem.
    """

    def __init__(self, team_id: str, key_id: str, key_file: str,
                 storefront: str = "us", timeout: float = 10.0,
                 token_provider: Any = None):
        self.team_id = team_id
        self.key_id = key_id
        self.key_file = Path(key_file).expanduser()
        self.storefront = storefront
        self.timeout = timeout
        self.token_provider = token_provider
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_refresh_margin = 60 if token_provider is not None else 86400

    @classmethod
    def from_config(cls, config: dict) -> "MusicKit | None":
        """None until the developer-account facts are filled in."""
        from devices.config import driver_block
        block = driver_block(config, "music", "AppleMusic")
        apple_services = config.get("apple_services") or {}
        service_mode = str(apple_services.get("mode") or "local")
        if service_mode == "disabled":
            return None
        if service_mode == "managed":
            from devices.apple_broker import ManagedAppleBroker
            broker = ManagedAppleBroker.from_config(config)
            if broker is None:
                return None
            return cls("", "", "", storefront=block.get("storefront") or "us",
                       token_provider=broker.musickit_token)
        team_id = block.get("team_id")
        key_id = block.get("key_id")
        if not team_id or team_id == "TODO" or not key_id or key_id == "TODO":
            return None
        return cls(
            team_id=str(team_id),
            key_id=str(key_id),
            key_file=block.get("key_file") or "~/.avctl/musickit.p8",
            storefront=block.get("storefront") or "us",
        )

    def dev_token(self) -> str:
        """The ES256 JWT Apple calls a developer token. Good for months;
        re-minted a day before it would expire."""
        if self._token and time.time() < self._token_exp - self._token_refresh_margin:
            return self._token
        if self.token_provider is not None:
            try:
                self._token, self._token_exp = self.token_provider()
            except Exception as exc:
                raise MusicError(f"managed MusicKit unavailable: {exc}") from None
            return self._token
        # Imported here, not at module top: phase 1 must run on a mini that
        # has never installed PyJWT/cryptography.
        import jwt

        try:
            key = self.key_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise MusicError(f"cannot read MusicKit key {self.key_file}: {exc}")
        now = time.time()
        self._token_exp = now + 150 * 86400
        self._token = jwt.encode(
            {"iss": self.team_id, "iat": int(now), "exp": int(self._token_exp)},
            key, algorithm="ES256", headers={"kid": self.key_id},
        )
        return self._token

    def search(self, term: str, limit: int = 12) -> dict[str, list[dict]]:
        """Catalog search, reduced to what non-album callers render."""
        try:
            resp = requests.get(
                CATALOG_URL.format(storefront=self.storefront),
                params={"term": term, "types": "albums,songs", "limit": limit},
                headers={"Authorization": f"Bearer {self.dev_token()}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MusicError(f"Apple Music API unreachable: {exc}")
        if resp.status_code == 401:
            raise MusicError("Apple rejected the developer token -- check "
                             "music.team_id / key_id / key_file")
        if not resp.ok:
            raise MusicError(f"catalog search failed: HTTP {resp.status_code}")
        results = resp.json().get("results", {})

        def slim(kind: str) -> list[dict]:
            out = []
            for item in results.get(kind, {}).get("data", []):
                attrs = item.get("attributes", {})
                art = (attrs.get("artwork") or {}).get("url", "")
                out.append({
                    "id": item.get("id"),
                    "name": attrs.get("name"),
                    "artist": attrs.get("artistName"),
                    "album": attrs.get("albumName"),
                    "art": art.replace("{w}", "300").replace("{h}", "300"),
                })
            return out

        return {"albums": slim("albums"), "songs": slim("songs")}

    @staticmethod
    def _album_row(item: dict) -> dict:
        attrs = item.get("attributes") or {}
        art = str((attrs.get("artwork") or {}).get("url") or "")
        return {
            "id": item.get("id"),
            "album": attrs.get("name"),
            "artist": attrs.get("artistName"),
            "art": art.replace("{w}", "300").replace("{h}", "300"),
            "tracks": attrs.get("trackCount"),
            "year": str(attrs.get("releaseDate") or "")[:4],
        }

    @staticmethod
    def _song_album_resource(song: dict) -> dict | None:
        albums = (((song.get("relationships") or {}).get("albums") or {})
                  .get("data") or [])
        if albums and isinstance(albums[0], dict) and albums[0].get("id"):
            # The first relationship is the song's primary release. Do not
            # promote every compilation on which the same recording appears.
            return albums[0]
        return None

    def search_albums(self, term: str, limit: int = 50) -> list[dict]:
        """Apple-ranked results projected onto the album-only search UI.

        Preserve ``topResults`` order for both songs and albums. A song result
        becomes its primary album tile; album results remain album tiles.
        Apple's ordinary album-keyword results follow in their original order.
        """
        cap = max(1, int(limit))
        first_page = min(25, cap)
        headers = {"Authorization": f"Bearer {self.dev_token()}"}
        try:
            suggestions_response = requests.get(
                SEARCH_SUGGESTIONS_URL.format(storefront=self.storefront),
                params={"term": term, "kinds": "topResults",
                        "types": "songs,albums", "include[songs]": "albums",
                        "limit": 10},
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MusicError(f"Apple Music API unreachable: {exc}")
        if suggestions_response.status_code == 401:
            raise MusicError("Apple rejected the developer token -- check "
                             "music.team_id / key_id / key_file")
        related_resources: list[dict] = []
        suggestions = []
        if suggestions_response.ok:
            suggestions = list(
                (suggestions_response.json().get("results") or {})
                .get("suggestions") or [])
        for suggestion in suggestions:
            content = suggestion.get("content") or {}
            if content.get("type") == "albums":
                album = content
            elif content.get("type") == "songs":
                album = self._song_album_resource(content)
            else:
                continue
            if album is None:
                continue
            if album.get("attributes"):
                related_resources.append(album)

        try:
            response = requests.get(
                CATALOG_URL.format(storefront=self.storefront),
                params={"term": term, "types": "albums", "limit": first_page},
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MusicError(f"Apple Music API unreachable: {exc}")
        if response.status_code == 401:
            raise MusicError("Apple rejected the developer token -- check "
                             "music.team_id / key_id / key_file")
        if not response.ok:
            raise MusicError(f"catalog search failed: HTTP {response.status_code}")
        direct_resources = list(
            (response.json().get("results", {}).get("albums") or {})
            .get("data") or [])

        # Preserve the old second page of direct album matches.
        offset = len(direct_resources)
        has_more = offset == first_page
        while len(direct_resources) < cap and has_more:
            page = min(25, cap - len(direct_resources))
            try:
                more = requests.get(
                    CATALOG_URL.format(storefront=self.storefront),
                    params={"term": term, "types": "albums",
                            "limit": page, "offset": offset},
                    headers=headers, timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise MusicError(f"Apple Music API unreachable: {exc}")
            if more.status_code == 401:
                raise MusicError("Apple rejected the developer token -- check "
                                 "music.team_id / key_id / key_file")
            if not more.ok:
                raise MusicError(f"catalog search failed: HTTP {more.status_code}")
            page_resources = list(
                (more.json().get("results", {}).get("albums") or {})
                .get("data") or [])
            direct_resources.extend(page_resources)
            has_more = len(page_resources) == page
            offset += len(page_resources)

        answer: list[dict] = []
        seen: set[str] = set()
        for item in related_resources + direct_resources:
            album_id = str(item.get("id") or "")
            if not album_id or album_id in seen:
                continue
            row = self._album_row(item)
            if not row.get("album"):
                continue
            seen.add(album_id)
            answer.append(row)
            if len(answer) >= cap:
                break
        return answer

    def search_catalog(self, term: str, kinds: list[str],
                       limit: int = 8) -> list[dict]:
        """Reduced song/album/playlist results for language control.

        Editorial Apple Music playlists use curatorName instead of
        artistName. Keeping that distinction here lets the agent say
        "Today's Hits — Apple Music" rather than presenting a blank owner.
        """
        allowed = {"songs", "albums", "playlists"}
        requested = [kind for kind in kinds if kind in allowed]
        if not requested:
            raise ValueError("catalog kinds must include songs, albums, or playlists")
        try:
            resp = requests.get(
                CATALOG_URL.format(storefront=self.storefront),
                params={"term": term, "types": ",".join(requested),
                        "limit": min(25, max(1, int(limit)))},
                headers={"Authorization": f"Bearer {self.dev_token()}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MusicError(f"Apple Music API unreachable: {exc}")
        if not resp.ok:
            raise MusicError(f"catalog search failed: HTTP {resp.status_code}")
        results = resp.json().get("results", {})
        out = []
        for kind in requested:
            singular = kind[:-1] if kind.endswith("s") else kind
            for item in results.get(kind, {}).get("data", []):
                attrs = item.get("attributes", {})
                artwork = attrs.get("artwork") or {}
                art = str(artwork.get("url") or "")
                row = {
                    "kind": singular,
                    "id": item.get("id"),
                    "name": attrs.get("name"),
                    "artist": (attrs.get("artistName")
                               or attrs.get("curatorName") or "Apple Music"),
                    "album": attrs.get("albumName"),
                }
                if art:
                    row["art"] = art.replace("{w}", "400").replace("{h}", "400")
                out.append(row)
        return out

    @staticmethod
    def _explore_item(item: dict) -> dict | None:
        """Reduce any chart/recommendation resource to one UI-safe shape."""
        kind = str(item.get("type") or "")
        singular = kind[:-1] if kind.endswith("s") else kind
        if singular not in {"song", "album", "playlist"} or not item.get("id"):
            return None
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict) or not attrs.get("name"):
            return None
        artwork = attrs.get("artwork") or {}
        art = str(artwork.get("url") or "") if isinstance(artwork, dict) else ""
        return {
            "source": "apple_music",
            "kind": singular,
            "id": item.get("id"),
            "name": attrs.get("name"),
            "artist": (attrs.get("artistName") or attrs.get("curatorName")
                       or "Apple Music"),
            "album": attrs.get("albumName"),
            "art": art.replace("{w}", "400").replace("{h}", "400"),
            "year": str(attrs.get("releaseDate") or "")[:4],
        }

    def explore(self, user_token: str | None = None,
                limit: int = 10) -> dict[str, Any]:
        """Personal recommendations when authorized, plus public charts."""
        cap = min(20, max(1, int(limit)))
        base_headers = {"Authorization": f"Bearer {self.dev_token()}"}
        sections: list[dict[str, Any]] = []
        personalized = False
        personal_error = None
        if user_token:
            try:
                response = requests.get(
                    RECOMMENDATIONS_URL,
                    params={"limit": 8},
                    headers={**base_headers, "Music-User-Token": user_token},
                    timeout=self.timeout,
                )
                if response.status_code == 403:
                    personal_error = "Apple Music authorization expired"
                elif not response.ok:
                    personal_error = (
                        f"recommendations unavailable (HTTP {response.status_code})")
                else:
                    for recommendation in response.json().get("data", [])[:6]:
                        attrs = recommendation.get("attributes") or {}
                        title = attrs.get("title") or {}
                        if isinstance(title, dict):
                            title = title.get("stringForDisplay")
                        contents = (recommendation.get("relationships") or {}).get(
                            "contents", {}).get("data", [])
                        items = [self._explore_item(item) for item in contents]
                        items = [item for item in items if item is not None][:cap]
                        if items:
                            sections.append({
                                "id": f"recommendation-{recommendation.get('id')}",
                                "title": str(title or "For You"),
                                "kind": "recommendation",
                                "items": items,
                            })
                    personalized = bool(sections)
            except (requests.RequestException, TypeError, ValueError):
                personal_error = "recommendations unavailable"

        try:
            response = requests.get(
                CHARTS_URL.format(storefront=self.storefront),
                params={"types": "songs,albums,playlists", "limit": cap},
                headers=base_headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            if not sections:
                raise MusicError(f"Apple Music API unreachable: {exc}") from exc
        else:
            if not response.ok:
                if not sections:
                    raise MusicError(
                        f"catalog charts failed: HTTP {response.status_code}")
            else:
                results = response.json().get("results", {})
                for kind in ("playlists", "albums", "songs"):
                    charts = results.get(kind) or []
                    if not isinstance(charts, list) or not charts:
                        continue
                    chart = charts[0] if isinstance(charts[0], dict) else {}
                    items = [self._explore_item(item)
                             for item in chart.get("data", [])]
                    items = [item for item in items if item is not None][:cap]
                    if items:
                        sections.append({
                            "id": f"chart-{kind}",
                            "title": str(chart.get("name")
                                         or f"Top {kind.title()}"),
                            "kind": "chart",
                            "items": items,
                        })
        return {
            "sections": sections,
            "personalized": personalized,
            "personal_error": personal_error,
        }

    def album_detail(self, album_id: str) -> dict:
        """One album with its track list, for the add-or-cherry-pick view."""
        if not re.match(r"^[0-9a-zA-Z.\-]+$", str(album_id)):
            raise ValueError(f"not a catalog id: {album_id!r}")
        try:
            resp = requests.get(
                ALBUM_URL.format(storefront=self.storefront, id=album_id),
                headers={"Authorization": f"Bearer {self.dev_token()}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MusicError(f"Apple Music API unreachable: {exc}")
        if not resp.ok:
            raise MusicError(f"album lookup failed: HTTP {resp.status_code}")
        data = (resp.json().get("data") or [{}])[0]
        attrs = data.get("attributes", {})
        art = (attrs.get("artwork") or {}).get("url", "")
        tracks = []
        for item in (data.get("relationships", {})
                     .get("tracks", {}).get("data", [])):
            t = item.get("attributes", {})
            tracks.append({
                "id": item.get("id"),
                "name": t.get("name"),
                "artist": t.get("artistName"),
                "duration": (t.get("durationInMillis") or 0) / 1000,
            })
        return {
            "id": data.get("id"),
            "album": attrs.get("name"),
            "artist": attrs.get("artistName"),
            "art": art.replace("{w}", "600").replace("{h}", "600"),
            "year": (attrs.get("releaseDate") or "")[:4],
            "tracks": tracks,
        }

    def playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        """All public catalog tracks in a playlist, preserving its order."""
        if not re.match(r"^[0-9a-zA-Z.\-]+$", str(playlist_id)):
            raise ValueError(f"not a catalog id: {playlist_id!r}")
        url = PLAYLIST_TRACKS_URL.format(
            storefront=self.storefront, id=playlist_id)
        out: list[dict[str, Any]] = []
        while url and len(out) < 500:
            try:
                response = requests.get(
                    url if url.startswith("http")
                    else "https://api.music.apple.com" + url,
                    params={"limit": 100} if not out else None,
                    headers={"Authorization": f"Bearer {self.dev_token()}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise MusicError(f"Apple Music API unreachable: {exc}") from exc
            if not response.ok:
                raise MusicError(
                    f"playlist lookup failed: HTTP {response.status_code}")
            payload = response.json()
            for item in payload.get("data") or []:
                attrs = item.get("attributes") or {}
                artwork = attrs.get("artwork") or {}
                art = str(artwork.get("url") or "")
                if item.get("id") and attrs.get("name"):
                    out.append({
                        "catalog_id": item["id"],
                        "name": attrs["name"],
                        "artist": attrs.get("artistName") or "",
                        "album": attrs.get("albumName") or "",
                        "duration": (attrs.get("durationInMillis") or 0) / 1000,
                        "art": art.replace("{w}", "400").replace("{h}", "400"),
                    })
            url = payload.get("next")
        return out

    def add_to_library(self, kind: str, catalog_id: str, user_token: str) -> None:
        """POST the item into the account's library. It reaches the mini via
        iCloud Sync Library -- seconds to minutes later, not immediately."""
        if kind not in ("albums", "songs", "playlists"):
            raise ValueError(
                f"kind must be albums, songs, or playlists, not {kind!r}")
        if not re.match(r"^[0-9a-zA-Z.\-]+$", str(catalog_id)):
            raise ValueError(f"not a catalog id: {catalog_id!r}")
        try:
            resp = requests.post(
                LIBRARY_ADD_URL,
                params={f"ids[{kind}]": catalog_id},
                headers={"Authorization": f"Bearer {self.dev_token()}",
                         "Music-User-Token": user_token},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise MusicError(f"Apple Music API unreachable: {exc}")
        if resp.status_code == 403:
            raise MusicError("Apple rejected the Music User Token -- "
                             "re-authorize at /music/auth")
        if resp.status_code not in (200, 202):
            raise MusicError(f"add to library failed: HTTP {resp.status_code}")
