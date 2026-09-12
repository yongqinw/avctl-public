"""Issue #29: the remote installs like an app.

Everything here is served unauthenticated (branding, not data), so the
tests double as a check that none of it accidentally grew an auth
dependency -- iOS fetches icons and manifests outside the page session.
"""

from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

from api import pwa
from api.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_manifest_is_a_standalone_app(client):
    answer = client.get("/manifest.webmanifest")
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith(
        "application/manifest+json")
    m = answer.json()
    assert m["display"] == "standalone"
    assert m["id"] == "/" and m["scope"] == "/" and m["start_url"] == "/"
    # Every icon the manifest names must actually be servable.
    for icon in m["icons"]:
        fetched = client.get(icon["src"])
        assert fetched.status_code == 200
        w, h = png_size(fetched.content)
        assert f"{w}x{h}" == icon["sizes"]


def test_touch_icon_renders_at_apple_size(client):
    answer = client.get("/static/icon-180.png")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/png"
    assert png_size(answer.content) == (180, 180)


def test_unlisted_icon_size_is_404(client):
    assert client.get("/static/icon-57.png").status_code == 404


def test_splash_serves_only_listed_sizes(client):
    width, height = sorted(pwa.splash_sizes())[0]   # smallest: cheap render
    answer = client.get(f"/static/splash-{width}x{height}.png")
    assert answer.status_code == 200
    assert png_size(answer.content) == (width, height)
    assert client.get("/static/splash-123x456.png").status_code == 404


def test_page_head_wires_it_all_up(client):
    """The gate page shares _page() with the remote, and needs no auth."""
    answer = client.get("/")
    assert answer.status_code == 401           # locked, but fully formed
    head = answer.text
    assert "<link rel='manifest' href='/manifest.webmanifest'>" in head
    assert "/static/icon-180.png" in head
    assert "apple-mobile-web-app-status-bar-style" in head
    assert "apple-touch-startup-image" in head
    # Every splash the head references is in the route's whitelist.
    for w, h, r in pwa.SPLASH_POINTS:
        assert f"/static/splash-{w * r}x{h * r}.png" in head
        assert (w * r, h * r) in pwa.splash_sizes()


def test_icon_cache_renders_once():
    first = pwa.icon_png(192)
    assert pwa.icon_png(192) is first          # cached, not re-rendered
