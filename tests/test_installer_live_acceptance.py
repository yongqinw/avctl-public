from __future__ import annotations

import pytest

from scripts import installer_live_acceptance as acceptance


class FakeCore:
    def __init__(self, *, fail_setup: bool = False,
                 fail_music_zero: bool = False) -> None:
        self.fail_setup = fail_setup
        self.fail_music_zero = fail_music_zero
        self.volumes = {"amp": 42, "music": 28}
        self.commands: list[tuple[str, int]] = []
        self.discoveries: list[str] = []

    def request(self, path, payload=None):
        if path == "/api/state":
            return {
                "implemented": ["amp.vol.set", "music.vol.set"],
                "devices": {
                    key: {"fields": {"volume": value}}
                    for key, value in self.volumes.items()
                },
            }
        if path == "/api/cmd":
            command = payload["cmd"]
            device = command.split(".", 1)[0]
            level = payload["args"]["level"]
            if self.fail_music_zero and device == "music" and level == 0:
                raise acceptance.SmokeFailure("synthetic music failure")
            self.volumes[device] = level
            self.commands.append((command, level))
            return {"status": "ok"}
        if path == "/api/setup":
            if self.fail_setup:
                raise acceptance.SmokeFailure("synthetic setup failure")
            return {"music": ["apple_music", "roon"], "devices": ["amp"]}
        if path == "/api/setup/discover":
            self.discoveries.append(payload["kind"])
            return {"kind": payload["kind"], "candidates": []}
        if path == "/api/settings/music":
            return {"active_backend": "apple_music"}
        if path.startswith("/api/music/search?"):
            return {"albums": [{"id": "album.1", "album": "Kind of Blue"}]}
        if path == "/api/agent":
            assert "do not play" in payload["message"]
            return {"status": "ok", "reply": "Kind of Blue", "acted": False}
        raise AssertionError(f"unexpected request: {path}")


def test_live_acceptance_zeroes_then_restores_outputs(capsys):
    client = FakeCore()

    acceptance.run(client, "Miles Davis", include_network=True)

    assert client.volumes == {"amp": 42, "music": 28}
    assert client.commands == [
        ("amp.vol.set", 0),
        ("music.vol.set", 0),
        ("music.vol.set", 28),
        ("amp.vol.set", 42),
    ]
    assert client.discoveries == ["music", "serial", "access", "network"]
    assert '"status": "ok"' in capsys.readouterr().out


def test_live_acceptance_restores_outputs_when_a_check_fails():
    client = FakeCore(fail_setup=True)

    with pytest.raises(acceptance.SmokeFailure, match="synthetic setup failure"):
        acceptance.run(client, "Miles Davis", include_network=False)

    assert client.volumes == {"amp": 42, "music": 28}
    assert client.commands[-2:] == [
        ("music.vol.set", 28),
        ("amp.vol.set", 42),
    ]


def test_live_acceptance_restores_first_output_when_zeroing_second_fails():
    client = FakeCore(fail_music_zero=True)

    with pytest.raises(acceptance.SmokeFailure, match="synthetic music failure"):
        acceptance.run(client, "Miles Davis", include_network=False)

    assert client.volumes == {"amp": 42, "music": 28}
    assert client.commands == [("amp.vol.set", 0), ("amp.vol.set", 42)]
