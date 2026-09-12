"""Frozen Core entry point and per-user LaunchAgent installer."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import plistlib
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

CORE_LABEL = "com.avctl.api"
HELPER_LABEL = "com.avctl.input-helper"
CATALOG_BRIDGE = Path(
    ".avctl/bin/AvctlMusicBridge.app"
)


def _configured_port(support: Path) -> int:
    """Read an existing installer-owned port, falling back safely on first run."""
    try:
        value = yaml.safe_load((support / "config.yaml").read_text()) or {}
        port = int((value.get("server") or {}).get("port") or 8000)
    except (FileNotFoundError, TypeError, ValueError, yaml.YAMLError):
        return 8000
    return port if 1024 <= port <= 65535 else 8000


def _private_plist(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".launchagent-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "wb") as output:
            plistlib.dump(value, output, fmt=plistlib.FMT_XML, sort_keys=False)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _private_yaml(path: Path, value: dict, uid: int, gid: int) -> None:
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            yaml.safe_dump(value, output, sort_keys=False)
        if os.geteuid() == 0:
            os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(handle)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _port_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True


def _packaged_agent(path: Path) -> bool:
    try:
        value = plistlib.loads(path.read_bytes())
        executable = str((value.get("ProgramArguments") or [""])[0])
    except (FileNotFoundError, IndexError, plistlib.InvalidFileException):
        return False
    return executable.endswith(
        "/Avctl Server.app/Contents/Resources/runtime/avctl-core")


def _fresh_install_port(support: Path, uid: int, gid: int) -> int:
    """Choose a reachable bootstrap port before the web chooser exists."""
    for port in range(8000, 8100):
        if _port_available(port):
            if port != 8000:
                _private_yaml(support / "config.yaml", {"server": {"port": port}},
                              uid, gid)
            return port
    raise RuntimeError("avctl needs one available local port from 8000 through 8099")


def _account(name: str | None, home: str | None) -> tuple[str, int, int, Path]:
    account = pwd.getpwnam(name) if name else pwd.getpwuid(os.getuid())
    return account.pw_name, account.pw_uid, account.pw_gid, Path(home or account.pw_dir)


def _launchctl(uid: int, action: str, plist: Path) -> None:
    domain = f"gui/{uid}"
    if action == "bootstrap":
        subprocess.run(["/bin/launchctl", "bootout", domain, str(plist)],
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)],
                       check=True)
    else:
        subprocess.run(["/bin/launchctl", "bootout", domain, str(plist)],
                       check=False)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if os.geteuid() != 0:
        return
    for root, directories, files in os.walk(path):
        os.chown(root, uid, gid)
        for name in directories:
            os.chown(Path(root) / name, uid, gid)
        for name in files:
            os.chown(Path(root) / name, uid, gid)


def install_service(args: argparse.Namespace) -> int:
    app = Path(args.app).resolve()
    runtime = app / "Contents/Resources/runtime/avctl-core"
    helper = app / "Contents/Resources/bin/avctl-input-helper"
    packaged_bridge = app / "Contents/Resources/apple/AvctlMusicBridge.app"
    if app.name != "Avctl Server.app" or not runtime.is_file():
        raise SystemExit("Avctl Server.app does not contain its Core runtime")
    name, uid, gid, home = _account(args.user, args.home)
    support = home / "Library/Application Support/avctl"
    logs = home / "Library/Logs/avctl"
    agents = home / "Library/LaunchAgents"
    for directory in (support, logs, agents):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(directory, uid, gid)
    config_file = support / "config.yaml"
    core_plist = agents / f"{CORE_LABEL}.plist"
    package_upgrade = _packaged_agent(core_plist)
    port = _configured_port(support)
    if not args.no_launch and not package_upgrade and not _port_available(port):
        if config_file.exists():
            # A source deployment or another configured Core already owns this
            # user's state and port. Installing a second copy would share its
            # token, queue, and devices, so leave that Core entirely alone.
            subprocess.run([
                "/bin/launchctl", "asuser", str(uid), "/usr/bin/open",
                f"http://127.0.0.1:{port}/",
            ], check=False)
            print(
                f"Existing avctl Core already uses port {port}; "
                "installed the app without replacing that service."
            )
            return 0
        port = _fresh_install_port(support, uid, gid)
    # The package installer runs as root; Core runs as the selected user.
    # Create (or repair) its state directory explicitly before bridge mkdir
    # can create .avctl as a root-owned intermediate directory.
    runtime_data = home / ".avctl"
    runtime_data.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_data.chmod(0o700)
    if os.geteuid() == 0:
        os.chown(runtime_data, uid, gid)
    if packaged_bridge.is_dir():
        bridge = home / CATALOG_BRIDGE
        bridge.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # ditto preserves the nested app's signatures and extended attributes.
        subprocess.run(["/usr/bin/ditto", str(packaged_bridge), str(bridge)],
                       check=True)
        _chown_tree(bridge.parent, uid, gid)
    _private_plist(core_plist, {
        "Label": CORE_LABEL,
        "ProgramArguments": [str(runtime), "serve"],
        "WorkingDirectory": str(app / "Contents/Resources"),
        "EnvironmentVariables": {
            "HOME": str(home), "AVCTL_BIND": "127.0.0.1",
            "AVCTL_INSTALL_KIND": "package",
            "AVCTL_INSTALL_HOME": str(home),
            "AVCTL_INPUT_HELPER": str(helper),
        },
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "core.log"),
        "StandardErrorPath": str(logs / "core.log"),
    })
    # The helper is bundled but intentionally dormant. The setup wizard only
    # installs and launches it when the owner chooses the Mac mini panel, so a
    # music-only installation never asks for Accessibility permission.
    plists = [core_plist]
    if os.geteuid() == 0:
        for plist in plists:
            os.chown(plist, uid, gid)
    if not args.no_launch:
        for plist in reversed(plists):
            _launchctl(uid, "bootstrap", plist)
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"avctl Core did not become healthy on port {port}; "
                f"check {logs / 'core.log'}"
            )
        subprocess.run([
            "/bin/launchctl", "asuser", str(uid), "/usr/bin/open",
            f"http://127.0.0.1:{port}/bootstrap",
        ], check=False)
    print(f"Installed avctl Core for {name}; setup: http://127.0.0.1:{port}/bootstrap")
    return 0


def configure_input_helper(
    enabled: bool, *, helper: str | Path | None = None,
    home: str | Path | None = None, no_launch: bool = False,
) -> dict[str, object]:
    """Install/remove the optional per-user Mac remote-input LaunchAgent."""
    _, uid, gid, account_home = _account(None, str(home) if home else None)
    executable = Path(helper or os.environ.get("AVCTL_INPUT_HELPER", ""))
    agents = account_home / "Library/LaunchAgents"
    logs = account_home / "Library/Logs/avctl"
    plist = agents / f"{HELPER_LABEL}.plist"
    if not enabled:
        if not no_launch and plist.exists():
            _launchctl(uid, "bootout", plist)
        plist.unlink(missing_ok=True)
        return {"enabled": False, "plist": str(plist)}
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("the packaged input helper is unavailable")
    for directory in (agents, logs):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    _private_plist(plist, {
        "Label": HELPER_LABEL, "ProgramArguments": [str(executable)],
        "EnvironmentVariables": {"HOME": str(account_home)},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 3,
        "StandardOutPath": str(logs / "input-helper.log"),
        "StandardErrorPath": str(logs / "input-helper.log"),
    })
    if os.geteuid() == 0:
        os.chown(plist, uid, gid)
    if not no_launch:
        _launchctl(uid, "bootstrap", plist)
    return {"enabled": True, "plist": str(plist)}


def open_setup(args: argparse.Namespace) -> int:
    """Open Setup on the port stored by this user's packaged Core."""
    _, _, _, home = _account(args.user, args.home)
    support = home / "Library/Application Support/avctl"
    port = _configured_port(support)
    suffix = "/bootstrap" if _packaged_agent(
        home / "Library/LaunchAgents" / f"{CORE_LABEL}.plist") else "/"
    subprocess.run(
        ["/usr/bin/open", f"http://127.0.0.1:{port}{suffix}"],
        check=False,
    )
    return 0


def uninstall_service(args: argparse.Namespace) -> int:
    _, uid, _, home = _account(args.user, args.home)
    agents = home / "Library/LaunchAgents"
    for label in (HELPER_LABEL, CORE_LABEL):
        plist = agents / f"{label}.plist"
        if not args.no_launch and plist.exists():
            _launchctl(uid, "bootout", plist)
        plist.unlink(missing_ok=True)
    if args.delete_data:
        support = home / "Library/Application Support/avctl"
        if support.name == "avctl" and support.parent.name == "Application Support":
            shutil.rmtree(support, ignore_errors=True)
        runtime_data = home / ".avctl"
        if runtime_data.name == ".avctl" and runtime_data.parent == home:
            shutil.rmtree(runtime_data, ignore_errors=True)
    print("Removed avctl services" + (" and data" if args.delete_data else "; kept data"))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="avctl-core")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    open_command = commands.add_parser("open-setup")
    open_command.add_argument("--user")
    open_command.add_argument("--home")
    for name in ("install-service", "uninstall-service"):
        command = commands.add_parser(name)
        command.add_argument("--user")
        command.add_argument("--home")
        command.add_argument("--no-launch", action="store_true")
        if name == "install-service":
            command.add_argument("--app", required=True)
        else:
            command.add_argument("--delete-data", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "serve":
        from api.__main__ import main as serve
        serve()
        return 0
    if args.command == "install-service":
        return install_service(args)
    if args.command == "uninstall-service":
        return uninstall_service(args)
    return open_setup(args)


def frozen_main() -> int:
    """Let PyInstaller dispatch multiprocessing helper invocations first."""
    multiprocessing.freeze_support()
    return main()


if __name__ == "__main__":
    raise SystemExit(frozen_main())
