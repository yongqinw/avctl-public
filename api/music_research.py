"""Bounded, read-only public music research for Ask.

The model can call functions but cannot browse public references by itself.
This module exposes two fixed music-reference services instead of an arbitrary
URL fetcher: Wikipedia for descriptive context and MusicBrainz for structured
recording credits.
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import threading
import time
from typing import Any, Callable

import requests


_WIKIPEDIA_EN = "https://en.wikipedia.org/w/api.php"
_WIKIPEDIA_ZH = "https://zh.wikipedia.org/w/api.php"
_MUSICBRAINZ = "https://musicbrainz.org/ws/2/recording"
_USER_AGENT = "avctl/6.0 (https://github.com/yongqinw/avctl-public)"
_CACHE_TTL = 900.0
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CJK = re.compile(r"[\u3400-\u9fff]")


def _clean(value: Any, limit: int) -> str:
    return _CONTROL.sub("", str(value or "")).strip()[:limit]


def _wikipedia(query: str, limit: int) -> list[dict[str, str]]:
    endpoint = _WIKIPEDIA_ZH if _CJK.search(query) else _WIKIPEDIA_EN
    response = requests.get(
        endpoint,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": limit,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "inprop": "url",
            "format": "json",
            "formatversion": 2,
            "redirects": 1,
        },
        headers={"User-Agent": _USER_AGENT},
        timeout=(2.0, 4.0),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Wikipedia returned a non-object response")
    query_payload = payload.get("query")
    pages = (query_payload.get("pages") or []
             if isinstance(query_payload, dict) else [])
    return [
        {
            "source": "Wikipedia",
            "title": _clean(page.get("title"), 200),
            "summary": _clean(page.get("extract"), 900),
            "url": _clean(page.get("fullurl"), 500),
        }
        for page in pages[:limit]
        if isinstance(page, dict) and page.get("title")
    ]


def _artist_credit(recording: dict[str, Any]) -> str:
    names = []
    for credit in recording.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        name = credit.get("name")
        if not name and isinstance(credit.get("artist"), dict):
            name = credit["artist"].get("name")
        if name:
            names.append(_clean(name, 160))
    return ", ".join(dict.fromkeys(names))[:300]


def _musicbrainz(query: str, limit: int) -> list[dict[str, str]]:
    response = requests.get(
        _MUSICBRAINZ,
        params={"query": query, "fmt": "json", "limit": limit},
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        timeout=(2.0, 4.0),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("MusicBrainz returned a non-object response")
    evidence = []
    for row in (payload.get("recordings") or [])[:limit]:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        releases = row.get("releases") or []
        album = next((release.get("title") for release in releases
                      if isinstance(release, dict) and release.get("title")), "")
        recording_id = _clean(row.get("id"), 80)
        evidence.append({
            "source": "MusicBrainz",
            "title": _clean(row.get("title"), 200),
            "artist": _artist_credit(row),
            "album": _clean(album, 200),
            "date": _clean(row.get("first-release-date"), 20),
            "url": (f"https://musicbrainz.org/recording/{recording_id}"
                    if recording_id else ""),
        })
    return evidence


def research(query: str, limit: int = 6) -> dict[str, Any]:
    """Return bounded evidence from fixed public reference providers."""
    query = _clean(query, 300)
    if not query:
        raise ValueError("music research query is empty")
    limit = max(1, min(int(limit), 8))
    key = (query.casefold(), limit)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            result = copy.deepcopy(cached[1])
            result["cached"] = True
            return result

    providers: tuple[
        tuple[str, Callable[[str, int], list[dict[str, str]]]], ...
    ] = (
        ("Wikipedia", _wikipedia),
        ("MusicBrainz", _musicbrainz),
    )
    evidence: list[dict[str, str]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=2,
                            thread_name_prefix="avctl-music-research") as pool:
        futures = {pool.submit(function, query, limit): name
                   for name, function in providers}
        for future in as_completed(futures):
            name = futures[future]
            try:
                evidence.extend(future.result())
            except Exception:
                # Each provider is optional. Malformed JSON or an unexpected
                # client-library failure must not discard valid evidence from
                # the other fixed source, and exception details stay out of
                # model context and Ask history.
                errors.append(f"{name} unavailable")

    result: dict[str, Any] = {
        "message": (f"Found {len(evidence)} public music-reference results "
                    f"for {query}." if evidence else
                    f"Public music research is unavailable for {query}."),
        "acted": False,
        "query": query,
        "evidence": evidence[:limit * 2],
        "errors": sorted(errors),
        "cached": False,
    }
    with _CACHE_LOCK:
        ttl = _CACHE_TTL if evidence and not errors else 60.0
        _CACHE[key] = (now + ttl, copy.deepcopy(result))
        if len(_CACHE) > 128:
            oldest = min(_CACHE, key=lambda item: _CACHE[item][0])
            _CACHE.pop(oldest, None)
    return result
