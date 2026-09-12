from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api import bonjour, minilink, settings, setup, voicelink
from api.main import app
from devices import config as device_config
from tests.conftest import AUTH


def test_user_config_recursively_overlays_bundled_defaults(monkeypatch, tmp_path):
    bundled = tmp_path / "bundled.yaml"
    user = tmp_path / "user.yaml"
    bundled.write_text("music:\n  driver: AppleMusic\n  max_volume: 80\ntv:\n  host: old\n")
    user.write_text("music:\n  driver: RoonMusic\ntv:\n  host: new\n")
    monkeypatch.setattr(device_config, "CONFIG_FILE", bundled)
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", user)

    value = device_config.load_config()

    assert value["music"] == {"driver": "RoonMusic", "max_volume": 80}
    assert value["tv"]["host"] == "new"


def test_save_user_config_is_private_and_atomic(monkeypatch, tmp_path):
    destination = tmp_path / "state" / "config.yaml"
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", destination)

    assert device_config.save_user_config({"schema_version": 1}) == destination

    assert yaml.safe_load(destination.read_text()) == {"schema_version": 1}
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(destination.parent.glob(".config-*.yaml"))


def test_manifest_round_trips_only_safe_installer_fields(monkeypatch, tmp_path):
    destination = tmp_path / "config.yaml"
    destination.write_text("""
music: {driver: RoonMusic, max_volume: 70, scene_volume: 45}
roon: {host: roon.local, port: 9330, zone_id: zone-1, token: never-return}
tv:
  driver: LGTelevision
  host: tv.local
  mac: '00:11:22:33:44:55'
  inputs: {macmini: HDMI_3}
  LGTelevision: {client_key: never-return}
ui: {panels: [home, music]}
""")
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", destination)

    current = setup.manifest()["current"]

    assert current["music"] == "roon"
    assert current["roon"] == {
        "host": "roon.local", "port": 9330, "zone_id": "zone-1"}
    assert current["tv"]["mac_input"] == "HDMI_3"
    assert "never-return" not in repr(current)


@pytest.mark.parametrize("configured", [False, True])
def test_setup_prefills_default_broker_without_an_invite_or_enrollment(
    monkeypatch, tmp_path, configured,
):
    config = {"apple_services": {"broker": {
        "url": "https://default-broker.example",
        "state_file": str(tmp_path / "broker.json"),
        "invite": "never-return-this-passphrase",
    }}}
    monkeypatch.setattr(device_config, "load_config", lambda: config)
    monkeypatch.setattr(device_config, "load_user_config", lambda:
                        {"music": {"driver": "AppleMusic"}} if configured else {})

    manifest = setup.manifest()
    expected = {"enrolled": False, "url": "https://default-broker.example",
                "installation_id": None}

    assert manifest["configured"] is configured
    assert manifest["apple_broker"] == expected
    if configured:
        assert manifest["current"]["apple_services"] == {"mode": "local", **expected}
    else:
        assert manifest["current"] is None
    assert "never-return" not in repr(manifest)
    assert setup.apple_services({"action": "status"}) == {
        **expected, "capabilities": {},
    }
    with pytest.raises(setup.SetupError, match="broker invite"):
        setup.apple_services({"action": "enroll", "url": expected["url"]})


def test_compile_setup_supports_roon_and_itach_without_secret_values():
    value = setup.compile_override({
        "server": {"port": 9123},
        "music": "roon",
        "panels": ["home", "music", "tv", "amp"],
        "roon": {"host": "roon.local", "port": 9330, "zone_id": "zone-1",
                 "output_id": "output-1"},
        "tv": {"host": "192.168.1.20", "mac": "AA:BB:CC:DD:EE:FF",
               "mac_input": "HDMI_3"},
        "amp": {"mode": "roon"},
        "dac": {"mode": "itach", "host": "192.168.1.30", "ir_port": 2},
        "max_volume": {"amp": 65, "music": 75},
    })

    assert value["music"]["driver"] == "RoonMusic"
    assert value["server"] == {"port": 9123}
    assert value["roon"]["zone_id"] == "zone-1"
    assert value["amp"]["driver"] == "RoonAmp"
    assert value["dac"]["driver"] == "ToppingD900"
    assert value["blaster"]["ports"]["dac"] == 2
    assert value["tv"]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert value["tv"]["inputs"]["macmini"] == "HDMI_3"
    assert value["music"]["scene_volume"] == 56
    assert value["amp"]["presets"]["music"] == 60
    assert value["ui"]["panels"] == ["home", "music", "tv", "amp"]
    assert value["ui"]["scenes"][0]["steps"] == [
        "tv.to_mac", "dac.to_usb", "amp.music_level",
        "mini.volume", "mini.player"]
    assert "token" not in repr(value).lower()
    assert value["apple_services"] == {"mode": "local"}


@pytest.mark.parametrize("payload, message", [
    ({"music": "spotify"}, "music must"),
    ({"music": "roon", "roon": {}}, "zone or output"),
    ({"music": "apple_music", "tv": {"host": "tv", "mac": "bad"}},
     "TV MAC"),
    ({"music": "apple_music", "max_volume": {"amp": 71}}, "0-70"),
    ({"music": "apple_music", "panels": ["music"]}, "include home"),
    ({"music": "apple_music", "scene_volume": {"music": 81}}, "0-80"),
    ({"music": "apple_music", "dac": {"mode": "itach", "host": "x",
                                         "ir_port": 9}}, "1, 2, or 3"),
    ({"music": "apple_music", "server": {"port": 80}}, "1024-65535"),
    ({"music": "apple_music", "server": {"port": 70000}}, "1-65535"),
])
def test_compile_setup_rejects_unsafe_or_incomplete_drafts(payload, message):
    with pytest.raises(setup.SetupError, match=message):
        setup.compile_override(payload)


def test_setup_endpoints_require_auth_and_activate(monkeypatch, tmp_path):
    destination = tmp_path / "config.yaml"
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", destination)
    monkeypatch.setattr(setup, "discover_serial", lambda: [
        {"path": "/dev/cu.test", "name": "Test", "manufacturer": None,
         "serial": None, "vid": None, "pid": None}
    ])

    with TestClient(app) as client:
        denied = client.get("/api/setup")
        manifest = client.get("/api/setup", headers=AUTH)
        discovery = client.post("/api/setup/discover", headers=AUTH,
                                json={"kind": "serial"})
        saved = client.post("/api/setup/activate", headers=AUTH, json={
            "music": "apple_music", "max_volume": {"music": 80},
        })

    assert denied.status_code == 401
    assert manifest.json()["steps"] == [
        "check", "music", "panels", "devices", "access", "review"]
    assert discovery.json()["candidates"][0]["path"] == "/dev/cu.test"
    assert saved.json()["restart_required"] is True
    assert yaml.safe_load(destination.read_text())["music"]["driver"] == "AppleMusic"


def test_setup_voice_prepares_bundled_runtime_without_credentials(monkeypatch):
    ready = {
        "supported": True, "runtime": True, "prepared": True,
        "ready": True, "enabled": False, "model": "synthetic/model",
        "detail": "Transcription model is ready",
    }
    monkeypatch.setattr(voicelink, "prepare", lambda: dict(ready))
    monkeypatch.setattr(voicelink, "status", lambda: {**ready, "ready": False})

    with TestClient(app) as client:
        denied = client.post("/api/setup/voice", json={"action": "prepare"})
        status = client.post("/api/setup/voice", headers=AUTH,
                             json={"action": "status"})
        prepared = client.post("/api/setup/voice", headers=AUTH,
                               json={"action": "prepare"})

    assert denied.status_code == 401
    assert status.json()["ready"] is False
    assert prepared.json()["ready"] is True
    assert "credential" not in repr(prepared.json()).lower()


def test_setup_mini_only_enables_optional_helper_on_request(monkeypatch):
    calls = []

    async def unavailable():
        raise minilink.MiniUnavailable("waiting for Accessibility")

    monkeypatch.setattr(setup, "configure_input_helper",
                        lambda enabled: calls.append(enabled) or {"enabled": enabled})
    monkeypatch.setattr(minilink, "open_session", unavailable)

    with TestClient(app) as client:
        denied = client.post("/api/setup/mini", json={"action": "enable"})
        enabled = client.post("/api/setup/mini", headers=AUTH,
                              json={"action": "enable"})
        disabled = client.post("/api/setup/mini", headers=AUTH,
                               json={"action": "disable"})

    assert denied.status_code == 401
    assert enabled.json()["helper"]["permission"] is False
    assert disabled.json()["enabled"] is False
    assert calls == [True, False]


def test_setup_persists_voice_choice_with_ask_panel():
    voice = setup.compile_override({
        "music": "apple_music", "panels": ["home", "agent"],
        "voice": {"enabled": True},
    })
    text_only = setup.compile_override({
        "music": "apple_music", "panels": ["home", "agent"],
        "voice": {"enabled": False},
    })

    assert voice["voice"] == {"enabled": True}
    assert text_only["voice"] == {"enabled": False}


def test_packaged_setup_only_enables_remote_input_for_mini_panel(
    monkeypatch, tmp_path,
):
    from installer import entrypoint

    calls = []
    monkeypatch.setattr(settings, "INSTALL_KIND", "package")
    monkeypatch.setenv("AVCTL_INSTALL_HOME", str(tmp_path / "package-home"))
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", tmp_path / "config.yaml")
    def configure_helper(enabled, **kwargs):
        calls.append((enabled, kwargs))
        return {"enabled": enabled}

    monkeypatch.setattr(entrypoint, "configure_input_helper", configure_helper)

    setup.activate({"music": "apple_music", "panels": ["home", "mini"]})
    setup.activate({"music": "apple_music", "panels": ["home"]})

    assert calls == [
        (True, {"home": str(tmp_path / "package-home")}),
        (False, {"home": str(tmp_path / "package-home")}),
    ]


def test_setup_apple_services_endpoint_enrolls_without_returning_private_key(
    monkeypatch,
):
    monkeypatch.setattr(setup, "apple_services", lambda payload: {
        "enrolled": True, "url": payload["url"],
        "installation_id": "a" * 32,
        "capabilities": {"musickit": True, "apns": True},
    })
    with TestClient(app) as client:
        denied = client.post("/api/setup/apple-services", json={"action": "status"})
        paired = client.post("/api/setup/apple-services", headers=AUTH, json={
            "action": "enroll", "url": "https://broker.example",
            "invite": "one-use-passphrase",
        })

    assert denied.status_code == 401
    assert paired.status_code == 200
    assert paired.json()["capabilities"]["musickit"] is True
    assert "private" not in repr(paired.json()).lower()


def test_network_discovery_rejects_non_local_prefix_shape():
    with pytest.raises(setup.SetupError, match="must look"):
        setup.discover_network("example.com")


def test_enable_tailscale_serve_uses_selected_loopback_core_port(monkeypatch):
    calls = []

    class Answer:
        returncode = 0
        stdout = ""
        stderr = ""

    statuses = iter([
        {"installed": True, "online": True, "serve": False, "url": "https://core.ts.net"},
        {"installed": True, "online": True, "serve": True, "url": "https://core.ts.net"},
    ])
    monkeypatch.setattr(setup, "_tailscale_executable", lambda: "/tailscale")
    monkeypatch.setattr(setup, "tailscale_status", lambda: next(statuses))
    monkeypatch.setattr(setup.subprocess, "run",
                        lambda argv, **_kwargs: calls.append(argv) or Answer())

    result = setup.enable_tailscale_serve(9123)

    assert calls == [["/tailscale", "serve", "--bg", "--https=443", "9123"]]
    assert result["serve"] is True


def test_tailscale_status_ignores_other_served_ports(monkeypatch):
    responses = iter([
        (0, '{"Self":{"DNSName":"core.example.ts.net.","Online":true}}'),
        (0, '{"Web":{"core.example.ts.net:8443":{"Handlers":{"/":'
            '{"Proxy":"http://127.0.0.1:8600"}}}}}'),
    ])

    class Answer:
        def __init__(self):
            self.returncode, self.stdout = next(responses)
            self.stderr = ""

    monkeypatch.setattr(setup, "_tailscale_executable", lambda: "/tailscale")
    monkeypatch.setattr(setup.subprocess, "run", lambda *_args, **_kwargs: Answer())

    result = setup.tailscale_status()

    assert result["online"] is True
    assert result["serve"] is False
    assert result["serve_port"] is None


def test_packaged_restart_retargets_existing_tailscale_serve(monkeypatch):
    events = []
    monkeypatch.setattr(settings, "INSTALL_KIND", "package")
    monkeypatch.setattr(device_config, "load_config",
                        lambda: {"server": {"port": 9123}})
    monkeypatch.setattr(setup, "tailscale_status",
                        lambda: {"serve": True, "serve_port": 8000})
    monkeypatch.setattr(setup, "enable_tailscale_serve",
                        lambda port: events.append(("serve", port)))
    monkeypatch.setattr(setup.time, "sleep",
                        lambda seconds: events.append(("sleep", seconds)))
    monkeypatch.setattr(setup.os, "kill",
                        lambda pid, sig: events.append(("kill", pid, sig)))

    setup.restart_packaged_core()

    assert events[0] == ("serve", 9123)
    assert events[1] == ("sleep", 0.5)
    assert events[2][0] == "kill"


def test_activate_rejects_an_unavailable_changed_core_port(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PORT", 8000)
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", tmp_path / "config.yaml")
    monkeypatch.setattr(setup.socket.socket, "bind",
                        lambda _self, _address: (_ for _ in ()).throw(OSError("busy")))

    with pytest.raises(setup.SetupError, match="9123 is unavailable"):
        setup.activate({"music": "apple_music", "server": {"port": 9123}})

    assert not (tmp_path / "config.yaml").exists()


def test_apple_music_setup_authorizes_bridge_and_checks_music_app(
    monkeypatch,
):
    events = []

    class Player:
        def __init__(self, executable):
            events.append(("bridge", executable))

        def available(self):
            return True

        def ensure_authorized(self):
            events.append(("authorize",))

        def close(self):
            events.append(("close",))

    class Answer:
        returncode = 0
        stdout = "stopped"
        stderr = ""

    monkeypatch.setattr("devices.music.catalog_player.CatalogPlayer", Player)
    monkeypatch.setattr(device_config, "load_config", lambda: {
        "music": {"AppleMusic": {"catalog_player": "/synthetic/bridge"}},
    })
    monkeypatch.setattr(setup.subprocess, "run", lambda argv, **_kwargs: Answer())

    assert setup.authorize_apple_music()["authorized"] is True
    assert events == [("bridge", "/synthetic/bridge"),
                      ("authorize",), ("close",)]


def test_setup_access_and_apple_music_actions_require_owner(monkeypatch):
    monkeypatch.setattr(setup, "enable_tailscale_serve", lambda port=None: {
        "installed": True, "online": True, "serve": True,
        "url": "https://core.example.ts.net", "port": port,
    })
    monkeypatch.setattr(setup, "authorize_apple_music", lambda: {
        "authorized": True, "detail": "ready",
    })
    with TestClient(app) as client:
        denied = client.post("/api/setup/access", json={
            "action": "enable_tailscale_serve"})
        access = client.post("/api/setup/access", headers=AUTH, json={
            "action": "enable_tailscale_serve"})
        music = client.post("/api/setup/apple-music", headers=AUTH, json={
            "action": "authorize"})

    assert denied.status_code == 401
    assert access.json()["serve"] is True
    assert music.json()["authorized"] is True


def test_skipped_rack_builds_a_music_only_home_scene():
    value = setup.compile_override({
        "music": "apple_music", "panels": ["home", "music"],
        "max_volume": {"music": 72}, "scene_volume": {"music": 44},
    })

    assert "tv" not in value
    assert "amp" not in value
    assert "dac" not in value
    assert value["music"]["scene_volume"] == 44
    assert value["ui"]["scenes"][0]["steps"] == [
        "mini.volume", "mini.player"]


def test_roon_authorization_saves_token_privately_and_returns_named_outputs(
    tmp_path,
):
    class FakeRoon:
        ready = True
        token = "synthetic-roon-token"
        core_id = "core-1"
        core_name = "Test Core"
        zones = {"zone-1": {
            "zone_id": "zone-1", "display_name": "Living Room",
            "outputs": [{"output_id": "output-1"}],
        }}
        outputs = {"output-1": {
            "output_id": "output-1", "display_name": "Stereo",
            "zone_id": "zone-1", "volume": {"value": 20},
            "source_controls": [{"control_key": "input"}],
        }}

        def stop(self):
            self.stopped = True

    created = []

    def factory(*args, **kwargs):
        instance = FakeRoon()
        created.append(instance)
        return instance

    token_file = tmp_path / "roon-token"
    try:
        waiting = setup.start_roon_authorization(
            {"host": "roon.local", "port": 9330}, api_factory=factory)
        result = setup.roon_authorization_status(token_file=token_file)
    finally:
        setup.cancel_roon_authorization()

    assert waiting["state"] == "waiting"
    assert result["authorized"] is True
    assert result["zones"][0]["name"] == "Living Room"
    assert result["outputs"][0] == {
        "id": "output-1", "name": "Stereo", "zone_id": "zone-1",
        "volume": True, "source_control": True,
    }
    assert "token" not in result
    assert token_file.read_text().strip() == "synthetic-roon-token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert created[0].stopped is True


def test_roon_setup_endpoint_is_authenticated_and_never_returns_token(monkeypatch):
    monkeypatch.setattr(setup, "start_roon_authorization", lambda payload: {
        "state": "waiting", "authorized": False,
        "detail": "Approve in Roon",
    })

    with TestClient(app) as client:
        denied = client.post("/api/setup/roon", json={"action": "start"})
        started = client.post("/api/setup/roon", headers=AUTH, json={
            "action": "start", "host": "roon.local", "port": 9330,
        })

    assert denied.status_code == 401
    assert started.json()["state"] == "waiting"
    assert "token" not in repr(started.json()).lower()


def test_bonjour_core_id_is_stable_private_and_advertising_can_be_disabled(
    monkeypatch, tmp_path,
):
    identity = tmp_path / "core-id"
    monkeypatch.setattr(settings, "CORE_ID_FILE", identity)
    monkeypatch.setattr(settings, "ADVERTISE", False)

    first = bonjour.CoreAdvertisement._core_id()
    second = bonjour.CoreAdvertisement._core_id()

    assert first == second
    assert identity.stat().st_mode & 0o777 == 0o600
    assert bonjour.CoreAdvertisement().start() is False


def test_bonjour_registration_failure_does_not_abort_core_startup(
    monkeypatch,
):
    events = []

    class BrokenZeroconf:
        def register_service(self, _info):
            raise RuntimeError("mDNS event loop blocked")

        def close(self):
            events.append("closed")

    class ServiceInfo:
        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setitem(sys.modules, "zeroconf", types.SimpleNamespace(
        ServiceInfo=ServiceInfo, Zeroconf=BrokenZeroconf))
    monkeypatch.setattr(settings, "ADVERTISE", True)
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://core.example.ts.net")
    monkeypatch.setattr(bonjour.CoreAdvertisement, "_local_ip",
                        staticmethod(lambda: "192.0.2.10"))

    advertisement = bonjour.CoreAdvertisement()

    assert advertisement.start() is False
    assert events == ["closed"]
    assert advertisement._zeroconf is None


def test_setup_ui_is_themed_workspace_and_not_a_panel():
    from api.auth import Identity
    from api import views

    html = views.remote(Identity("caller", "token"))
    script = (Path(__file__).parents[1] / "api/ui/app.js").read_text()

    assert "data-settings-page='setup'" in html
    assert "id='setup-root'" in html
    assert "data-tab='setup'" not in html
    assert "runSetupDiscovery('network')" in script
    assert "Every other control can be skipped" in script
    assert "Ask + local voice" in script
    assert "Prepare and verify local voice transcription" in script


@pytest.mark.parametrize("music, extra", [
    ("apple_music", {
        "amp": {"mode": "serial", "port": "/dev/cu.synthetic"},
        "dac": {"mode": "itach", "host": "192.0.2.10", "ir_port": 2},
    }),
    ("roon", {"roon": {"host": "roon.local", "port": 9330,
                        "zone_id": "zone-1", "output_id": "output-1"},
              "amp": {"mode": "roon"}, "dac": {"mode": "roon"}}),
])
def test_zero_volume_setup_round_trips_for_every_music_backend(
    monkeypatch, tmp_path, music, extra,
):
    destination = tmp_path / "config.yaml"
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", destination)
    payload = {
        "music": music,
        "panels": ["home", "music", "agent", "amp"],
        "max_volume": {"amp": 0, "music": 0},
        "scene_volume": {"amp": 0, "music": 0},
        **extra,
    }

    saved = setup.activate(payload)
    current = setup.manifest()["current"]

    assert saved["config"]["music"]["max_volume"] == 0
    assert saved["config"]["music"]["scene_volume"] == 0
    assert saved["config"]["amp"]["max_volume"] == 0
    assert saved["config"]["amp"]["presets"]["music"] == 0
    assert current["max_volume"] == {"amp": 0, "music": 0}
    assert current["scene_volume"] == {"amp": 0, "music": 0}


def test_setup_javascript_does_not_treat_zero_as_missing():
    script = (Path(__file__).parents[1] / "api/ui/app.js").read_text()

    assert "input.value = value ?? '';" in script
    assert "return current ?? fallback;" in script
    assert "setupDraft.max_volume.music || '80'" not in script
    assert "setupDraft.scene_volume.music || '56'" not in script
    assert "Core listening port" in script
    assert "server: setupDraft.server" in script
