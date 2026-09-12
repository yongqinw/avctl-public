"""The scoped-down issue #32: scenes and panel order come from config.

Not the whole UI -- the panels themselves stay code. Config declares
which panels exist and in what order, and 1-4 start scenes composed from
the named step registry. Everything validates at load with the menu of
what actually exists; Everything-off is not configuration.
"""

from __future__ import annotations

import json

import pytest

from api import commands, views
from devices import config as device_config


def with_ui(monkeypatch, ui: dict) -> None:
    monkeypatch.setattr(device_config, "load_config", lambda: {"ui": ui})


@pytest.fixture
def rebuilt():
    yield
    commands.rebuild()   # back to the real config's table


def test_default_is_exactly_the_app_we_have(monkeypatch):
    with_ui(monkeypatch, {})
    assert views.panel_order() == [
        "home", "music", "agent", "mini", "tv", "amp"]
    (scene,) = commands.scenes()
    assert scene["id"] == "music"
    assert scene["steps"][0] == "tv.to_mac"


def test_two_scenes_become_two_commands_and_two_keys(monkeypatch, rebuilt):
    with_ui(monkeypatch, {"scenes": [
        {"id": "music", "label": "Music", "steps": ["amp.music_level"]},
        {"id": "late", "label": "Late night", "note": "amp only, quiet",
         "steps": ["amp.on", "mini.volume"]},
    ]})
    commands.rebuild()
    ids = commands.implemented()
    assert "scene.music" in ids and "scene.late" in ids
    home = views._home()
    assert "data-cmd='scene.late'" in home
    assert "data-cmd='scene.off'" in home          # Off is not configuration
    # A generated note says what the scene runs when config gave none.
    assert "runs: amp.music_level" in commands.get("scene.music").note


def test_unknown_step_stops_the_boot_with_the_menu(monkeypatch):
    with_ui(monkeypatch, {"scenes": [
        {"id": "bad", "steps": ["amp.on", "disco.lights"]}]})
    with pytest.raises(ValueError) as caught:
        commands.scenes()
    assert "disco.lights" in str(caught.value)
    assert "amp.music_level" in str(caught.value)   # the menu of real steps


def test_more_than_four_scenes_is_refused(monkeypatch):
    with_ui(monkeypatch, {"scenes": [
        {"id": f"s{n}", "steps": ["amp.on"]} for n in range(5)]})
    with pytest.raises(ValueError, match="1 to 4"):
        commands.scenes()


def test_panels_pick_presence_and_order(monkeypatch):
    with_ui(monkeypatch, {"panels": ["home", "amp"]})
    assert views.panel_order() == ["home", "amp"]


def test_panels_cannot_invent_or_drop_home(monkeypatch):
    with_ui(monkeypatch, {"panels": ["home", "disc"]})
    with pytest.raises(ValueError, match="panels that exist"):
        views.panel_order()
    with_ui(monkeypatch, {"panels": ["music", "amp"]})
    with pytest.raises(ValueError, match="must include home"):
        views.panel_order()


def test_runtime_panel_settings_preserve_hidden_positions(monkeypatch):
    with_ui(monkeypatch, {"panels": [
        "home", "music", "agent", "mini", "tv", "amp"]})

    result = views.set_panel_settings(
        ["home", "agent", "music", "mini", "amp", "tv"],
        ["home", "agent", "amp"],
    )

    assert views.panel_order() == ["home", "agent", "amp"]
    assert [panel["id"] for panel in result["panels"]] == [
        "home", "agent", "music", "mini", "amp", "tv"]
    assert [panel["id"] for panel in result["panels"] if panel["enabled"]] == [
        "home", "agent", "amp"]
    saved = json.loads(views.PANEL_SETTINGS_FILE.read_text())
    assert saved["order"] == [
        "home", "agent", "music", "mini", "amp", "tv"]
    assert views.PANEL_SETTINGS_FILE.stat().st_mode & 0o777 == 0o600


def test_newly_registered_panel_is_appended_to_existing_settings(monkeypatch):
    views.set_panel_settings(list(views._PANELS), list(views._PANELS))
    monkeypatch.setitem(views._PANELS, "dvd", ("DVD", "D", lambda: ""))

    payload = views.panel_settings()

    assert payload["panels"][-1]["id"] == "dvd"
    assert payload["panels"][-1]["enabled"] is False


def test_environment_panel_order_is_visible_but_read_only(monkeypatch):
    monkeypatch.setenv("AVCTL_UI_PANELS", "home,agent,music")

    payload = views.panel_settings()

    assert payload["managed"] is True
    assert views.panel_order() == ["home", "agent", "music"]
    with pytest.raises(ValueError, match="managed"):
        views.set_panel_settings(list(views._PANELS), list(views._PANELS))


# --- version skew degrades to "coming soon", never to an error ------------


def test_unknown_key_renders_dimmed_instead_of_crashing():
    html = views.key("disc.play", "Play")
    assert "data-cmd='disc.play'" in html
    assert "soon" in html


def test_unknown_command_answers_coming_soon_not_404():
    from fastapi.testclient import TestClient

    from api.main import app
    from tests.conftest import AUTH

    with TestClient(app) as client:
        answer = client.post("/api/cmd", json={"cmd": "disc.play"},
                             headers=AUTH)
    assert answer.status_code == 501
    body = answer.json()
    assert body["status"] == "coming_soon"
    assert "does not know" in body["note"]


def test_not_implemented_driver_verb_answers_coming_soon(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from tests.conftest import AUTH

    def missing(args):
        raise NotImplementedError("this DAC has no input-cycle button")
    row = commands.COMMANDS["dac.input.next"]
    monkeypatch.setitem(commands.COMMANDS, "dac.input.next",
                        type(row)(id=row.id, device=row.device,
                                  label=row.label, handler=missing,
                                  note=row.note))
    with TestClient(app) as client:
        answer = client.post("/api/cmd", json={"cmd": "dac.input.next"},
                             headers=AUTH)
    assert answer.status_code == 501
    assert answer.json()["status"] == "coming_soon"
    assert "no input-cycle button" in answer.json()["note"]
