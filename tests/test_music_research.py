from __future__ import annotations

import requests

from api import music_research


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def setup_function():
    with music_research._CACHE_LOCK:  # noqa: SLF001 - isolate module cache
        music_research._CACHE.clear()  # noqa: SLF001


def test_research_uses_only_fixed_reference_hosts_and_caches(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "wikipedia.org" in url:
            return _Response({"query": {"pages": [{
                "title": "不能说的秘密",
                "extract": "A film with music by Jay Chou.\x00",
                "fullurl": "https://zh.wikipedia.org/wiki/example",
            }]}})
        assert url == music_research._MUSICBRAINZ  # noqa: SLF001
        return _Response({"recordings": [{
            "id": "recording-1", "title": "Secret",
            "artist-credit": [{"name": "Jay Chou"}],
            "first-release-date": "2007",
            "releases": [{"title": "Secret OST"}],
        }]})

    monkeypatch.setattr(music_research.requests, "get", get)

    first = music_research.research(
        "不能说的秘密 soundtrack https://untrusted.example", 3)
    second = music_research.research(
        "不能说的秘密 soundtrack https://untrusted.example", 3)

    assert {row["source"] for row in first["evidence"]} == {
        "Wikipedia", "MusicBrainz",
    }
    assert "\x00" not in str(first)
    assert first["acted"] is False
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(calls) == 2
    assert {url for url, _kwargs in calls} == {
        music_research._WIKIPEDIA_ZH,  # noqa: SLF001
        music_research._MUSICBRAINZ,  # noqa: SLF001
    }
    assert all(kwargs["timeout"] == (2.0, 4.0) for _url, kwargs in calls)


def test_research_returns_partial_evidence_when_one_source_fails(monkeypatch):
    def get(url, **_kwargs):
        if "wikipedia.org" in url:
            raise requests.Timeout("slow")
        return _Response({"recordings": [{
            "id": "recording-2", "title": "Blue in Green",
            "artist-credit": [{"artist": {"name": "Miles Davis"}}],
        }]})

    monkeypatch.setattr(music_research.requests, "get", get)

    result = music_research.research("Blue in Green credits", 2)

    assert [row["title"] for row in result["evidence"]] == ["Blue in Green"]
    assert result["errors"] == ["Wikipedia unavailable"]
    assert result["acted"] is False


def test_research_rejects_empty_query_without_network(monkeypatch):
    calls = []
    monkeypatch.setattr(
        music_research.requests, "get",
        lambda *_args, **_kwargs: calls.append(True),
    )

    try:
        music_research.research("\x00\t", 4)
    except ValueError as exc:
        assert str(exc) == "music research query is empty"
    else:
        raise AssertionError("empty research query was accepted")
    assert calls == []
