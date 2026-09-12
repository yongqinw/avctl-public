"""Issue #23: a partial artwork file must never land under the final name.

The route serves whatever exists at the cache path with an immutable
one-week Cache-Control, so "exists" has to mean "complete". The fake
osascript writes to whatever path the script names -- whole, partial, or
not at all -- and the assertions are about what ends up on disk.
"""

from __future__ import annotations

import re
import time
import types

import pytest
from fastapi.testclient import TestClient

from api import musiclink
from api.main import app
from devices.music import AppleMusic, MusicError
from tests.conftest import AUTH

PID = "0123456789ABCDEF"
JPEG = b"\xff\xd8" + b"artwork-bytes"
PREFETCH_QUEUE_ARTWORK = musiclink.prefetch_queue_artwork

_DEST = re.compile(r'POSIX file "([^"]+)"')


def scripted(behavior):
    """An AppleMusic whose osascript is `behavior(target_path)`."""
    music = AppleMusic()

    def _osascript(self, script, lang="JavaScript", timeout=None):
        return behavior(_DEST.search(script).group(1))

    music._osascript = types.MethodType(_osascript, music)
    return music


def test_complete_write_lands_under_the_final_name(tmp_path):
    def write_whole(target):
        with open(target, "wb") as fh:
            fh.write(JPEG)
        return ""

    dest = tmp_path / f"{PID}.img"
    scripted(write_whole).extract_artwork(PID, dest)
    assert dest.read_bytes() == JPEG
    assert list(tmp_path.iterdir()) == [dest]


def test_crash_mid_write_leaves_no_file(tmp_path):
    def write_half_and_die(target):
        with open(target, "wb") as fh:
            fh.write(JPEG[:3])
        raise MusicError("osascript killed mid-write")

    dest = tmp_path / f"{PID}.img"
    with pytest.raises(MusicError):
        scripted(write_half_and_die).extract_artwork(PID, dest)
    # Nothing under the final name, and no temp litter either: the next
    # request re-extracts instead of caching three bytes for a week.
    assert list(tmp_path.iterdir()) == []


def test_empty_write_is_an_error_not_a_cache_entry(tmp_path):
    def write_nothing(target):
        open(target, "wb").close()
        return ""

    dest = tmp_path / f"{PID}.img"
    with pytest.raises(MusicError):
        scripted(write_nothing).extract_artwork(PID, dest)
    assert list(tmp_path.iterdir()) == []


def test_cached_artwork_route_serves_an_existing_cover(monkeypatch, tmp_path):
    monkeypatch.setattr(musiclink, "_artwork_dir", lambda: tmp_path)
    path = tmp_path / f"{PID}.img"
    path.write_bytes(JPEG)

    with TestClient(app) as client:
        answer = client.get(
            f"/api/music/artwork/{PID}?cached=1", headers=AUTH,
        )

    assert answer.status_code == 200
    assert answer.content == JPEG
    assert answer.headers["content-type"] == "image/jpeg"


def test_cached_artwork_miss_never_asks_music_to_extract(monkeypatch, tmp_path):
    monkeypatch.setattr(musiclink, "_artwork_dir", lambda: tmp_path)
    monkeypatch.setattr(
        musiclink,
        "_music",
        lambda: (_ for _ in ()).throw(AssertionError("must stay cache-only")),
    )

    with TestClient(app) as client:
        answer = client.get(
            f"/api/music/artwork/{PID}?cached=1", headers=AUTH,
        )

    assert answer.status_code == 404
    assert answer.json()["detail"] == "no artwork"
    assert answer.headers["cache-control"] == "no-store"


def test_queue_artwork_prefetch_is_bounded_deduplicated_and_nonblocking(
        monkeypatch, tmp_path):
    cached = "0000000000000001"
    extractable = "0000000000000002"
    missing = "0000000000000003"
    (tmp_path / f"{cached}.img").write_bytes(JPEG)
    monkeypatch.setattr(musiclink, "_artwork_dir", lambda: tmp_path)

    calls = []

    class ArtworkMusic:
        def extract_artwork(self, pid, path):
            calls.append(pid)
            if pid == missing:
                raise MusicError("track has no artwork")
            path.write_bytes(JPEG)

    monkeypatch.setattr(musiclink, "_music", lambda: ArtworkMusic())
    with musiclink._ARTWORK_WARM_LOCK:
        assert not musiclink._ARTWORK_WARM_RUNNING
        musiclink._ARTWORK_WARM_PENDING.clear()
        musiclink._ARTWORK_WARM_MISSING.clear()

    snapshot = {
        "playing": {"pid": cached},
        "items": [
            {"pid": extractable},
            {"pid": missing},
            {"catalog_id": "catalog-1", "art": "https://example/art.jpg"},
            {"pid": extractable},
        ],
    }
    started = time.monotonic()
    PREFETCH_QUEUE_ARTWORK(snapshot)
    assert time.monotonic() - started < 0.1

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with musiclink._ARTWORK_WARM_LOCK:
            if not musiclink._ARTWORK_WARM_RUNNING:
                break
        time.sleep(0.01)

    assert calls == [extractable, missing]
    assert (tmp_path / f"{extractable}.img").read_bytes() == JPEG

    # Cached covers and definite no-artwork results do not get retried every
    # time the Focus queue polls.
    PREFETCH_QUEUE_ARTWORK(snapshot)
    time.sleep(0.03)
    assert calls == [extractable, missing]
