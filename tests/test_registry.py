"""The registry: config names a class, boot finds it or says why not."""

from __future__ import annotations

import pytest

from devices import config as device_config
from devices import registry
from devices.amp.mcintosh_mac7200 import McIntoshMAC7200
from devices.dac.topping_d900 import ToppingD900
from devices.music.apple_music import AppleMusic
from devices.music.roon import RoonMusic
from devices.tv.lg_webos import LGTelevision


@pytest.fixture(autouse=True)
def fresh_registry():
    registry.reset()
    yield
    registry.reset()


def test_defaults_resolve_without_any_driver_keys(monkeypatch):
    monkeypatch.setattr(device_config, "load_config", lambda: {})
    assert registry.driver_class("dac") is ToppingD900
    assert registry.driver_class("amp") is McIntoshMAC7200
    assert registry.driver_class("tv") is LGTelevision
    assert registry.driver_class("music") is AppleMusic


def test_config_names_the_class(monkeypatch):
    monkeypatch.setattr(device_config, "load_config",
                        lambda: {"dac": {"driver": "ToppingD900"}})
    assert registry.driver_class("dac") is ToppingD900


def test_typo_fails_at_boot_with_the_menu(monkeypatch):
    monkeypatch.setattr(device_config, "load_config",
                        lambda: {"dac": {"driver": "ToppingDL90"}})
    with pytest.raises(registry.RegistryError) as caught:
        registry.driver_class("dac")
    message = str(caught.value)
    assert "ToppingDL90" in message          # what was asked for
    assert "ToppingD900" in message          # what actually exists
    assert "TrackedDac" not in message       # intermediates are not drivers


def test_the_instance_is_shared(monkeypatch, tmp_path):
    monkeypatch.setattr(device_config, "load_config",
                        lambda: {"dac": {"state_file": str(tmp_path / "d")}})
    assert registry.device("dac") is registry.device("dac")


def test_music_reads_the_music_block(monkeypatch):
    monkeypatch.setattr(device_config, "load_config",
                        lambda: {"music": {"driver": "AppleMusic"}})
    assert registry.driver_class("music") is AppleMusic


def test_saved_music_backend_overrides_shipped_default(monkeypatch, tmp_path):
    selected = tmp_path / "music-backend"
    selected.write_text("RoonMusic\n", encoding="utf-8")
    config = {"music": {"driver": "AppleMusic",
                        "backend_state_file": str(selected)}}
    monkeypatch.setattr(device_config, "load_config", lambda: config)

    assert registry.driver_class("music") is RoonMusic


def test_music_backend_save_is_atomic_and_normalizes_friendly_name(tmp_path):
    selected = tmp_path / "settings" / "music-backend"
    config = {"music": {"backend_state_file": str(selected)}}

    assert device_config.save_music_driver(config, "roon") == selected
    assert selected.read_text(encoding="utf-8") == "RoonMusic\n"
    assert device_config.configured_driver(
        config, "music", "AppleMusic") == "RoonMusic"


def test_validate_names_every_category(monkeypatch):
    monkeypatch.setattr(device_config, "load_config", lambda: {})
    lines = "\n".join(registry.validate())
    for name in ("ToppingD900", "McIntoshMAC7200", "LGTelevision",
                 "AppleMusic"):
        assert name in lines


# --- driver_block: generic keys with the driver's own laid over ----------


def test_driver_block_overlays_and_falls_back():
    config = {"music": {"driver": "AppleMusic", "queue_window": 20,
                        "AppleMusic": {"team_id": "T1"},
                        "Roon": {"host": "elsewhere"}}}
    mine = device_config.driver_block(config, "music", "AppleMusic")
    assert mine["team_id"] == "T1"          # own sub-block
    assert mine["queue_window"] == 20       # generic key still visible
    assert "host" not in mine               # another driver's business is not
    theirs = device_config.driver_block(config, "music", "Roon")
    assert theirs["host"] == "elsewhere" and "team_id" not in theirs


def test_driver_block_accepts_the_old_flat_shape():
    flat = {"amp": {"driver": "McIntoshMAC7200", "port": "/dev/x"}}
    assert device_config.driver_block(
        flat, "amp", "McIntoshMAC7200")["port"] == "/dev/x"


def test_shipped_defaults_are_inert_and_resolve_every_driver():
    """Public defaults name capabilities without describing a real rack."""
    config = device_config.load_config()
    from devices.music.apple_music import MusicKit
    assert config["music"]["driver"] == "AppleMusic"
    assert registry.driver_class("music") in {AppleMusic, RoonMusic}
    assert AppleMusic.from_config(config).queue_playlist == "avctl"
    assert MusicKit.from_config(config) is None
    assert config["amp"]["McIntoshMAC7200"]["port"] is None
    with pytest.raises(ValueError, match="tv config is missing"):
        LGTelevision.from_config(config)
    dac = ToppingD900.from_config(config)
    assert dac.blaster is None and dac.code_next == ""


def test_owner_overlay_can_still_configure_hardware(monkeypatch, tmp_path):
    bundled = tmp_path / "defaults.yaml"
    bundled.write_text("""
tv:
  driver: LGTelevision
  host: null
  mac: null
  LGTelevision:
    client_key_file: ~/.avctl/tv-client-key
""", encoding="utf-8")
    owner = tmp_path / "config.yaml"
    owner.write_text("""
tv:
  host: 192.0.2.10
  mac: 00:11:22:33:44:55
  inputs: {macmini: HDMI_2}
""", encoding="utf-8")
    monkeypatch.setattr(device_config, "CONFIG_FILE", bundled)
    monkeypatch.setattr(device_config, "USER_CONFIG_FILE", owner)

    television = LGTelevision.from_config(device_config.load_config())
    assert television.host == "192.0.2.10"
    assert television.inputs["macmini"] == "HDMI_2"


def test_tv_prefers_private_key_file_over_config_credential(tmp_path):
    key_file = tmp_path / "tv-client-key"
    key_file.write_text("durable-key\n", encoding="utf-8")
    config = {
        "tv": {
            "driver": "LGTelevision",
            "host": "192.0.2.1",
            "mac": "00:11:22:33:44:55",
            "client_key": "old-config-key",
            "client_key_file": str(key_file),
        }
    }

    television = LGTelevision.from_config(config)

    assert television.client_key == "durable-key"
    assert television.client_key_file == key_file
