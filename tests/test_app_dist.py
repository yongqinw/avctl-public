"""The OTA update doors: /app (the Install page) and /app/<artifact>.

The contract that matters: the routes serve exactly what the deployer
published -- newest build or a rollback alike -- and answer 404, never a
traceback, while nothing is published yet.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api import main, settings
def test_unpublished_is_a_404_not_a_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "APP_DIST", tmp_path)
    with TestClient(main.app) as client:
        assert client.get("/app").status_code == 404
        assert client.get("/app/manifest.plist").status_code == 404
        assert client.get("/app/avctl.ipa").status_code == 404
        assert client.head("/app/avctl.ipa").status_code == 404


def test_published_build_is_served_with_its_version(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "APP_DIST", tmp_path)
    (tmp_path / "manifest.plist").write_text("<plist/>", encoding="utf-8")
    (tmp_path / "avctl.ipa").write_bytes(b"ipa-bytes")
    (tmp_path / "version").write_text("321\n", encoding="utf-8")

    with TestClient(main.app) as client:
        page = client.get("/app")
        assert page.status_code == 200
        assert "build 321" in page.text
        assert "itms-services://" in page.text
        assert client.get("/app/manifest.plist").text == "<plist/>"
        assert client.get("/app/avctl.ipa").content == b"ipa-bytes"
        probe = client.head("/app/avctl.ipa")
        assert probe.status_code == 200
        assert probe.content == b""
        assert probe.headers["content-length"] == str(len(b"ipa-bytes"))


def test_only_named_artifacts_are_served(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "APP_DIST", tmp_path)
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    with TestClient(main.app) as client:
        assert client.get("/app/secret.txt").status_code == 404
        assert client.head("/app/secret.txt").status_code == 404
