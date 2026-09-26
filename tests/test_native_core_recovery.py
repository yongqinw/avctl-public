from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_distributable_app_has_no_personal_core_default():
    config = (ROOT / "app/Shared/ServerConfig.swift").read_text()

    assert 'static let defaultServer = ""' in config
    assert "yongqins-mac-mini" not in config


def test_failed_web_navigation_has_native_core_recovery():
    panel = (ROOT / "app/avctl/PanelView.swift").read_text()
    setup = (ROOT / "app/avctl/CoreSetupView.swift").read_text()

    assert 'change.setTitle("Change Core"' in panel
    assert 'change.accessibilityIdentifier = "change-core"' in panel
    assert "ServerConfig.isPaired = false" in panel
    assert "name: .avctlConnectionChanged" in panel
    assert "_address = State(initialValue: ServerConfig.server)" in setup
    assert "_token = State(initialValue: ServerConfig.token)" in setup
