from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api import musiclink, views
from api.auth import Identity
from api.main import app
from tests.conftest import AUTH


def test_settings_is_an_alternate_workspace_not_a_sixth_tab():
    html = views.remote(Identity("caller", "token", display="Rack Owner"))

    assert "id='settings-toggle'" in html
    assert "id='settings-workspace'" in html
    assert "data-settings-page='appearance'" in html
    assert "data-settings-page='setup'" in html
    assert "data-settings-page='panels'" in html
    assert "data-settings-page='ask'" in html
    assert "id='music-backends'" in html
    assert "id='music-backend-apply'" in html
    assert "data-tab='settings'" not in html
    assert "Rack Owner" not in html
    assert "tailnet" not in html


def test_setup_ui_offers_no_code_managed_apple_pairing():
    html = views.remote(Identity("caller", "token"))
    script = (Path(__file__).parents[1] / "api/ui/app.js").read_text()

    assert "id='setup-root'" in html
    assert "Pair Apple services" in script
    assert "One-time passphrase" in script
    assert "/api/setup/apple-services" in script


def test_settings_contains_every_existing_appearance_choice():
    html = views.remote(Identity("caller", "token"))

    for layout in ("docked", "stage", "cover-flow", "split-deck"):
        assert f"data-layout='{layout}'" in html
    for theme in ("glass", "mcintosh", "porcelain", "midnight", "warm",
                  "contrast"):
        assert f"data-theme='{theme}'" in html


def test_settings_header_keeps_only_settings_and_refresh_actions():
    html = views.remote(Identity("caller", "token", display="Private Name"))
    brand = html.split("<div class='brand'>", 1)[1].split("</div>", 1)[0]

    assert "settings-toggle" in brand
    assert "brand-refresh" in brand
    assert "class='who'" not in brand
    assert "Private Name" not in brand


def test_panel_settings_api_reorders_and_hides_registered_panels():
    with TestClient(app) as client:
        denied = client.get("/api/settings/panels")
        changed = client.post(
            "/api/settings/panels", headers=AUTH,
            json={
                "order": ["home", "agent", "music", "mini", "amp", "tv"],
                "enabled": ["home", "agent", "music"],
            },
        )
        after = client.get("/api/settings/panels", headers=AUTH)

    assert denied.status_code == 401
    assert changed.status_code == 200
    assert changed.json()["reload_required"] is True
    assert views.panel_order() == ["home", "agent", "music"]
    assert [panel["id"] for panel in after.json()["panels"]] == [
        "home", "agent", "music", "mini", "amp", "tv"]


def test_panel_settings_api_rejects_hiding_home():
    with TestClient(app) as client:
        response = client.post(
            "/api/settings/panels", headers=AUTH,
            json={
                "order": ["home", "music", "agent", "mini", "tv", "amp"],
                "enabled": ["music", "agent"],
            },
        )

    assert response.status_code == 400
    assert "include home" in response.json()["detail"]


def test_music_backend_settings_api_lists_and_switches_sources(monkeypatch):
    payload = {
        "active_backend": "apple_music", "managed": False,
        "backends": [{"id": "apple_music", "label": "Apple Music"},
                     {"id": "roon", "label": "Roon + Qobuz"}],
    }
    selected = []
    monkeypatch.setattr(musiclink, "music_backend_settings", lambda: payload)
    monkeypatch.setattr(
        musiclink, "set_music_backend",
        lambda backend: (selected.append(backend) or {
            **payload, "active_backend": backend}))

    with TestClient(app) as client:
        denied = client.get("/api/settings/music")
        before = client.get("/api/settings/music", headers=AUTH)
        changed = client.post("/api/settings/music", headers=AUTH,
                              json={"backend": "roon"})

    assert denied.status_code == 401
    assert before.json()["active_backend"] == "apple_music"
    assert changed.json()["active_backend"] == "roon"
    assert changed.json()["reload_required"] is True
    assert selected == ["roon"]


def test_remote_without_music_does_not_reserve_the_ipad_music_column():
    views.set_panel_settings(
        ["home", "music", "agent", "mini", "tv", "amp"],
        ["home", "agent", "mini", "tv", "amp"],
    )

    html = views.remote(Identity("caller", "token"))

    assert "class='app no-music-panel'" in html
    assert "id='page-music'" not in html
    assert "data-tab='music'" not in html


def test_remote_without_amp_keeps_music_and_navigation_rendered():
    views.set_panel_settings(
        ["home", "music", "agent", "mini", "tv", "amp"],
        ["home", "music", "agent", "mini", "tv"],
    )

    html = views.remote(Identity("caller", "token"))

    assert "id='page-amp'" not in html
    assert "id='amp-slider'" not in html
    assert "id='page-music'" in html
    assert "data-tab='music'" in html
    assert "data-tab='tv'" in html


def test_optional_panel_startup_hooks_are_guarded():
    """Absent Amp/Music markup must not abort the rest of app.js startup."""
    script = (Path(__file__).parents[1] / "api/ui/app.js").read_text()

    assert "if (slider) {" in script
    assert "$('#dac-resync')?.addEventListener" in script
    assert "if (moreAlbums) {" in script
    assert "if (moreSongs) {" in script
    assert ").observe($('#m-more'))" not in script
    assert ").observe($('#m-songs-more'))" not in script


def test_ask_ui_directive_can_open_visible_panel_and_music_explore():
    script = (Path(__file__).parents[1] / "api/ui/app.js").read_text()

    assert "function applyAgentUI(directive)" in script
    assert "applyAgentUI(data.ui)" in script
    assert "applyAgentUI(event.ui)" in script
    assert "showPage(panel, true)" in script
    assert "musicView === 'explore'" in script
