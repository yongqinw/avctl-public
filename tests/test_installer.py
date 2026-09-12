from __future__ import annotations

import argparse
import os
import plistlib
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from api import settings, setup
from api.main import app
from devices import config as device_config
from installer import entrypoint


def test_service_installer_writes_user_launchagents_and_keeps_data(tmp_path):
    application = tmp_path / "Applications" / "Avctl Server.app"
    runtime = application / "Contents/Resources/runtime/avctl-core"
    helper = application / "Contents/Resources/bin/avctl-input-helper"
    runtime.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    runtime.write_text("runtime")
    helper.write_text("helper")
    runtime.chmod(0o755)
    helper.chmod(0o755)
    home = tmp_path / "home"
    args = argparse.Namespace(
        app=str(application), user=None, home=str(home), no_launch=True)

    assert entrypoint.install_service(args) == 0

    core_file = home / "Library/LaunchAgents/com.avctl.api.plist"
    helper_file = home / "Library/LaunchAgents/com.avctl.input-helper.plist"
    core = plistlib.loads(core_file.read_bytes())
    assert core["ProgramArguments"] == [str(runtime.resolve()), "serve"]
    assert core["EnvironmentVariables"]["AVCTL_INSTALL_KIND"] == "package"
    assert core["EnvironmentVariables"]["AVCTL_INSTALL_HOME"] == str(home)
    assert core["EnvironmentVariables"]["AVCTL_BIND"] == "127.0.0.1"
    assert "AVCTL_PORT" not in core["EnvironmentVariables"]
    assert not helper_file.exists()

    enabled = entrypoint.configure_input_helper(
        True, helper=helper, home=home, no_launch=True)
    helper_config = plistlib.loads(helper_file.read_bytes())
    assert enabled["enabled"] is True
    assert helper_config["ProgramArguments"] == [str(helper.resolve())]
    assert helper_file.stat().st_mode & 0o777 == 0o600
    assert core_file.stat().st_mode & 0o777 == 0o600
    assert (home / ".avctl").stat().st_mode & 0o777 == 0o700

    disabled = entrypoint.configure_input_helper(
        False, helper=helper, home=home, no_launch=True)
    assert disabled["enabled"] is False
    assert not helper_file.exists()

    entrypoint.configure_input_helper(
        True, helper=helper, home=home, no_launch=True)

    support = home / "Library/Application Support/avctl"
    (support / "config.yaml").write_text("kept: true\n")
    remove = argparse.Namespace(
        user=None, home=str(home), no_launch=True, delete_data=False)
    assert entrypoint.uninstall_service(remove) == 0
    assert not core_file.exists() and not helper_file.exists()
    assert (support / "config.yaml").exists()


def test_service_installer_preserves_configured_core_port(tmp_path):
    application = tmp_path / "Applications" / "Avctl Server.app"
    runtime = application / "Contents/Resources/runtime/avctl-core"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("runtime")
    runtime.chmod(0o755)
    home = tmp_path / "home"
    support = home / "Library/Application Support/avctl"
    support.mkdir(parents=True)
    (support / "config.yaml").write_text("server: {port: 9123}\n")
    args = argparse.Namespace(
        app=str(application), user=None, home=str(home), no_launch=True)

    assert entrypoint.install_service(args) == 0
    core = plistlib.loads((home / "Library/LaunchAgents/com.avctl.api.plist").read_bytes())
    assert "AVCTL_PORT" not in core["EnvironmentVariables"]
    assert entrypoint._configured_port(support) == 9123


def test_packaged_app_opens_setup_on_configured_core_port(monkeypatch, tmp_path):
    home = tmp_path / "home"
    support = home / "Library/Application Support/avctl"
    support.mkdir(parents=True)
    (support / "config.yaml").write_text("server: {port: 9123}\n")
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.avctl.api.plist").write_bytes(plistlib.dumps({
        "ProgramArguments": [
            "/Applications/Avctl Server.app/Contents/Resources/runtime/avctl-core",
            "serve",
        ],
    }))
    calls = []
    monkeypatch.setattr(
        entrypoint.subprocess, "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    args = argparse.Namespace(user=None, home=str(home))

    assert entrypoint.open_setup(args) == 0
    assert calls == [([
        "/usr/bin/open", "http://127.0.0.1:9123/bootstrap",
    ], {"check": False})]


def test_fresh_install_selects_free_bootstrap_port(monkeypatch, tmp_path):
    support = tmp_path / "Library/Application Support/avctl"
    support.mkdir(parents=True)
    monkeypatch.setattr(entrypoint, "_port_available",
                        lambda port: port == 8003)

    assert entrypoint._fresh_install_port(support, 501, 20) == 8003
    assert yaml.safe_load((support / "config.yaml").read_text()) == {
        "server": {"port": 8003},
    }


def test_install_does_not_replace_running_configured_core(monkeypatch, tmp_path):
    application = tmp_path / "Applications" / "Avctl Server.app"
    runtime = application / "Contents/Resources/runtime/avctl-core"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("runtime")
    runtime.chmod(0o755)
    home = tmp_path / "home"
    support = home / "Library/Application Support/avctl"
    support.mkdir(parents=True)
    (support / "config.yaml").write_text("server: {port: 8000}\n")
    monkeypatch.setattr(entrypoint, "_port_available", lambda _port: False)
    calls = []
    monkeypatch.setattr(
        entrypoint.subprocess, "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    args = argparse.Namespace(
        app=str(application), user=None, home=str(home), no_launch=False)

    assert entrypoint.install_service(args) == 0
    assert not (home / "Library/LaunchAgents/com.avctl.api.plist").exists()
    assert not (home / ".avctl").exists()
    assert calls == [([
        "/bin/launchctl", "asuser", str(os.getuid()), "/usr/bin/open",
        "http://127.0.0.1:8000/",
    ], {"check": False})]


@pytest.mark.parametrize("healthy", [False, True])
def test_installer_opens_setup_only_after_core_is_healthy(
    monkeypatch, tmp_path, capsys, healthy,
):
    application = tmp_path / "Applications" / "Avctl Server.app"
    runtime = application / "Contents/Resources/runtime/avctl-core"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("runtime")
    home = tmp_path / "home"
    calls = []
    probes = []
    monkeypatch.setattr(entrypoint, "_port_available", lambda _port: True)
    monkeypatch.setattr(entrypoint, "_launchctl", lambda *_args: None)
    monkeypatch.setattr(entrypoint.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(entrypoint.subprocess, "run",
                        lambda argv, **_kwargs: calls.append(argv))

    class HealthyResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def urlopen(url, **_kwargs):
        probes.append(url)
        if healthy and len(probes) == 2:
            return HealthyResponse()
        raise OSError("Core is not running")

    monkeypatch.setattr(entrypoint.urllib.request, "urlopen", urlopen)
    args = argparse.Namespace(
        app=str(application), user=None, home=str(home), no_launch=False)

    if healthy:
        assert entrypoint.install_service(args) == 0
        assert len(probes) == 2
        assert calls == [[
            "/bin/launchctl", "asuser", str(os.getuid()), "/usr/bin/open",
            "http://127.0.0.1:8000/bootstrap",
        ]]
        assert "Installed avctl Core" in capsys.readouterr().out
    else:
        with pytest.raises(RuntimeError, match="did not become healthy") as error:
            entrypoint.install_service(args)
        assert str(home / "Library/Logs/avctl/core.log") in str(error.value)
        assert len(probes) == 60
        assert calls == []
        assert "Installed avctl Core" not in capsys.readouterr().out


def test_packaged_server_bounds_shutdown_with_connected_event_streams(
    monkeypatch,
):
    from api import __main__ as server

    calls = []
    monkeypatch.setattr(server.uvicorn, "run",
                        lambda application, **kwargs:
                        calls.append((application, kwargs)))

    server.main()

    assert calls == [("api.main:app", {
        "host": settings.BIND,
        "port": settings.PORT,
        "timeout_graceful_shutdown": 3,
    })]


def test_frozen_entrypoint_dispatches_multiprocessing_helpers_first(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(entrypoint.multiprocessing, "freeze_support",
                        lambda: calls.append("freeze"))
    monkeypatch.setattr(entrypoint, "main", lambda: calls.append("main") or 0)

    assert entrypoint.frozen_main() == 0
    assert calls == ["freeze", "main"]


def test_service_uninstaller_delete_data_removes_config_and_runtime_state(
    tmp_path,
):
    home = tmp_path / "home"
    support = home / "Library/Application Support/avctl"
    runtime_data = home / ".avctl"
    support.mkdir(parents=True)
    runtime_data.mkdir(parents=True)
    (support / "config.yaml").write_text("music: {}\n")
    (runtime_data / "token").write_text("synthetic-token\n")
    unrelated = home / ".keep-me"
    unrelated.write_text("safe\n")
    remove = argparse.Namespace(
        user=None, home=str(home), no_launch=True, delete_data=True)

    assert entrypoint.uninstall_service(remove) == 0

    assert not support.exists()
    assert not runtime_data.exists()
    assert unrelated.read_text() == "safe\n"


@pytest.mark.parametrize("existing_state", [False, True])
def test_root_installer_owns_runtime_state_before_placing_signed_catalog_bridge(
    monkeypatch, tmp_path, existing_state,
):
    application = tmp_path / "Applications" / "Avctl Server.app"
    runtime = application / "Contents/Resources/runtime/avctl-core"
    bridge = application / (
        "Contents/Resources/apple/AvctlMusicBridge.app/Contents/MacOS/"
        "AvctlMusicBridge")
    runtime.parent.mkdir(parents=True)
    bridge.parent.mkdir(parents=True)
    runtime.write_text("runtime")
    bridge.write_text("signed-helper")
    runtime.chmod(0o755)
    bridge.chmod(0o755)
    home = tmp_path / "home"
    runtime_data = home / ".avctl"
    ownership = {}
    if existing_state:
        runtime_data.mkdir(parents=True)
        runtime_data.chmod(0o755)
        (runtime_data / "token").write_text("synthetic-token\n")
        ownership[runtime_data] = (0, 20)
    monkeypatch.setattr(entrypoint, "_account",
                        lambda *_args: ("owner", 501, 20, home))
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint.os, "chown",
                        lambda path, uid, gid:
                        ownership.__setitem__(Path(path), (uid, gid)))
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        assert argv[0] == "/usr/bin/ditto"
        assert ownership.get(runtime_data) == (501, 20)
        assert runtime_data.stat().st_mode & 0o777 == 0o700
        shutil.copytree(argv[1], argv[2], dirs_exist_ok=True)

    monkeypatch.setattr(entrypoint.subprocess, "run", run)
    args = argparse.Namespace(
        app=str(application), user=None, home=str(home), no_launch=True)

    assert entrypoint.install_service(args) == 0

    installed = home / (
        ".avctl/bin/AvctlMusicBridge.app/Contents/MacOS/AvctlMusicBridge")
    assert installed.read_text() == "signed-helper"
    assert ownership[installed] == (501, 20)
    assert calls[0][0] == "/usr/bin/ditto"
    if existing_state:
        assert (runtime_data / "token").read_text() == "synthetic-token\n"


def test_root_installer_owns_runtime_state_without_catalog_bridge(
    monkeypatch, tmp_path,
):
    application = tmp_path / "Applications" / "Avctl Server.app"
    runtime = application / "Contents/Resources/runtime/avctl-core"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("runtime")
    home = tmp_path / "home"
    ownership = {}
    monkeypatch.setattr(entrypoint, "_account",
                        lambda *_args: ("owner", 501, 20, home))
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint.os, "chown",
                        lambda path, uid, gid:
                        ownership.__setitem__(Path(path), (uid, gid)))
    args = argparse.Namespace(
        app=str(application), user=None, home=str(home), no_launch=True)

    assert entrypoint.install_service(args) == 0

    runtime_data = home / ".avctl"
    assert runtime_data.stat().st_mode & 0o777 == 0o700
    assert ownership[runtime_data] == (501, 20)


@pytest.mark.parametrize("path", ["/bootstrap", "/setup"])
def test_packaged_local_bootstrap_sets_cookie_without_url_token(monkeypatch, path):
    monkeypatch.setattr(settings, "INSTALL_KIND", "package")
    with TestClient(app, base_url="http://127.0.0.1",
                    client=("127.0.0.1", 54321)) as client:
        answer = client.get(path, follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/?setup=1"
    assert "avctl_token=" in answer.headers["set-cookie"]
    assert "token=" not in answer.headers["location"]


def test_native_webview_unlock_posts_token_without_multipart_dependency():
    with TestClient(app) as client:
        answer = client.post(
            "/",
            content="token=test-token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

        assert answer.status_code == 303
        assert answer.headers["location"] == "/"
        assert "avctl_token=" in answer.headers["set-cookie"]
        assert client.get("/").status_code == 200


def test_native_webview_unlock_rejects_bad_or_oversized_body():
    with TestClient(app) as client:
        bad = client.post(
            "/", content="token=wrong",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        oversized = client.post(
            "/", content="token=" + "x" * 4_100,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert bad.status_code == 401
    assert oversized.status_code == 401


@pytest.mark.parametrize("path", ["/bootstrap", "/setup"])
@pytest.mark.parametrize("install_kind, base_url, peer, headers", [
    ("package", "http://core.example", "127.0.0.1", {}),
    ("package", "http://127.0.0.1", "192.0.2.1", {}),
    ("package", "http://127.0.0.1", "127.0.0.1", {"x-forwarded-for": "192.0.2.1"}),
    ("source", "http://127.0.0.1", "127.0.0.1", {}),
])
def test_bootstrap_is_not_exposed_outside_local_package(
    monkeypatch, path, install_kind, base_url, peer, headers,
):
    monkeypatch.setattr(settings, "INSTALL_KIND", install_kind)
    with TestClient(app, base_url=base_url, client=(peer, 54321)) as client:
        assert client.get(path, headers=headers).status_code == 404


def test_installation_manifest_distinguishes_optional_capabilities(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "INSTALL_KIND", "package")
    monkeypatch.setattr(settings, "APP_DIST", tmp_path / "app-dist")
    monkeypatch.setenv("AVCTL_INPUT_HELPER", str(tmp_path / "missing-helper"))
    monkeypatch.setattr(device_config, "load_config", lambda: {
        "music": {"AppleMusic": {
            "catalog_player": str(tmp_path / "missing-bridge"),
        }},
    })
    result = setup.installation_capabilities()

    assert result["install_kind"] == "package"
    components = {item["id"]: item for item in result["components"]}
    assert components["core"]["available"] is True
    assert components["mini"]["available"] is False
    assert components["apple_music_catalog"]["available"] is False
    assert components["phone_app"]["available"] is False
    assert "UDID" in components["phone_app"]["detail"]


def test_installer_build_contract_is_self_contained_and_loopback_only():
    root = Path(__file__).parents[1]
    build = (root / "installer/build-pkg.sh").read_text()
    requirements = (root / "requirements.txt").read_text()
    entry = (root / "installer/entrypoint.py").read_text()
    postinstall = (root / "installer/postinstall").read_text()

    assert "PyInstaller" in build
    assert "Avctl Server.app" in build
    assert 'avctl-core" open-setup' in build
    assert "127.0.0.1:8000/bootstrap" not in build
    assert "pkgbuild" in build
    assert '"AVCTL_BIND": "127.0.0.1"' in entry
    assert '"AVCTL_INSTALL_KIND": "package"' in entry
    assert "install-service" in postinstall
    assert "curl" not in postinstall and "git clone" not in postinstall
    assert "AvctlMusicBridge.app" in build
    assert "--collect-submodules devices" in build
    assert 'find_spec("mlx") and find_spec("mlx_whisper") and find_spec("scipy")' in build
    assert 'version("mlx") == "0.23.2"' in build
    assert '--collect-all mlx ' not in build
    assert '--add-data "$MLX_DIR:mlx"' in build
    assert '--add-data "$MLX_DIR/lib/mlx.metallib:."' in build
    assert 'mlx_whisper/assets' in build
    assert 'scipy/_external' in build
    assert '--exclude-module mlx_whisper.torch_whisper' in build
    assert 'CLANG_MODULE_CACHE_PATH' in build
    assert 'mlx==0.23.2; sys_platform == "darwin"' in requirements
