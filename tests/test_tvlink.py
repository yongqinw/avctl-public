"""Issue #22, the classifiable half: the device's own "link dropped between
check and call" must map to a 502 like every other downstream failure.

The _drop-vs-_ensure race itself is serialized by sharing the connect lock;
that is asserted structurally here (same lock object guards both paths) and
live on the rack, since faking an SSAP websocket teaches nothing.
"""

from __future__ import annotations

import asyncio
import stat

import pytest
from aiowebostv.exceptions import WebOsTvPairError, WebOsTvResponseTypeError

from api import tvlink
from devices.tv import NotConnectedError, PairingRequired, PowerOffTimeout
from devices.tv.lg_webos import AvctlWebOsClient, LGTelevision


def test_not_connected_is_downstream():
    assert NotConnectedError in tvlink._DOWNSTREAM


def test_run_folds_downstream_into_runtimeerror_for_the_route():
    async def dies():
        raise NotConnectedError("call connect() first")

    with pytest.raises(RuntimeError, match="call connect"):
        tvlink._LINK._run(dies(), timeout=5)


def test_webos_registration_uses_avctl_identity_without_blacklisted_signature():
    async def registration():
        return AvctlWebOsClient("192.0.2.1", None).registration_msg()

    message = asyncio.run(registration())
    manifest = message["payload"]["manifest"]

    assert "client-key" not in message["payload"]
    assert "signatures" not in manifest
    assert manifest["signed"]["appId"] == "io.avctl.remote"
    assert manifest["signed"]["serial"] == "avctl-webos-2026"


def test_webos_registration_builds_signed_identity_when_upstream_omits_it(
        monkeypatch):
    monkeypatch.setattr(
        "devices.tv.lg_webos._UpstreamWebOsClient.registration_msg",
        lambda _client: {"payload": {"manifest": {}}},
    )

    async def registration():
        return AvctlWebOsClient("192.0.2.1", None).registration_msg()

    message = asyncio.run(registration())

    assert message["payload"]["manifest"]["signed"] == {
        "created": "20260825",
        "appId": "io.avctl.remote",
        "localizedAppNames": {"": "avctl"},
        "localizedVendorNames": {"": "avctl"},
        "serial": "avctl-webos-2026",
    }


def test_webos_registration_keeps_existing_pairing_credential():
    async def registration():
        return AvctlWebOsClient(
            "192.0.2.1", "paired-key").registration_msg()

    assert asyncio.run(registration())["payload"]["client-key"] == "paired-key"


def test_power_off_bypasses_the_intentionally_empty_upstream_cache(monkeypatch):
    calls = []

    async def shut_down():
        client = AvctlWebOsClient("192.0.2.1", "paired-key")

        async def command(message_type, endpoint):
            calls.append((message_type, endpoint))

        monkeypatch.setattr(client, "command", command)

        # The cache remains at its default false value because avctl disables
        # the subscription that needs permissions this TV refuses. It must not
        # prevent a shutdown after LGTelevision confirmed the live state.
        assert not client.tv_state.is_on
        await client.power_off()

    asyncio.run(shut_down())

    assert calls == [("request", "system/turnOff")]


def test_tv_power_off_waits_for_confirmed_standby(monkeypatch, tmp_path):
    class FakeClient:
        def __init__(self):
            self.states = iter(("Active", "Active Standby"))
            self.shutdowns = 0

        async def power_off(self):
            self.shutdowns += 1

        async def get_power_state(self):
            return {"state": next(self.states)}

    monkeypatch.setattr("devices.tv.lg_webos.POWER_POLL_INTERVAL", 0)
    television = LGTelevision(
        "192.0.2.1", "paired-key", "00:11:22:33:44:55",
        client_key_file=tmp_path / "tv-client-key",
    )
    client = FakeClient()
    television._client = client

    asyncio.run(television.power_off(timeout=1))

    assert client.shutdowns == 1


def test_tv_power_off_rejects_an_unconfirmed_shutdown(monkeypatch, tmp_path):
    class FakeClient:
        async def power_off(self):
            pass

        async def get_power_state(self):
            return {"state": "Active"}

    monkeypatch.setattr("devices.tv.lg_webos.POWER_POLL_INTERVAL", 0)
    television = LGTelevision(
        "192.0.2.1", "paired-key", "00:11:22:33:44:55",
        client_key_file=tmp_path / "tv-client-key",
    )
    television._client = FakeClient()

    with pytest.raises(PowerOffTimeout, match="still reports Active"):
        asyncio.run(television.power_off(timeout=0.001))


def test_rejected_tv_key_repairs_and_persists_replacement(monkeypatch,
                                                          tmp_path):
    attempts = []

    class FakeClient:
        def __init__(self, host, key):
            self.host = host
            self.client_key = key
            self.live = False
            attempts.append(key)

        async def connect(self):
            if self.client_key == "revoked-key":
                raise WebOsTvResponseTypeError(
                    {"type": "error",
                     "error": "401 insufficient permissions"})
            self.client_key = "replacement-key"
            self.live = True

        async def disconnect(self):
            self.live = False

        def is_connected(self):
            return self.live

    monkeypatch.setattr("devices.tv.lg_webos.AvctlWebOsClient", FakeClient)
    key_file = tmp_path / "private" / "tv-client-key"
    television = LGTelevision(
        "192.0.2.1", "revoked-key", "00:11:22:33:44:55",
        client_key_file=key_file,
    )

    asyncio.run(television.connect())

    assert attempts == ["revoked-key", None]
    assert television.client_key == "replacement-key"
    assert television.connected
    assert key_file.read_text(encoding="utf-8").strip() == "replacement-key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    asyncio.run(television.disconnect())


def test_valid_config_key_is_migrated_to_private_key_file(monkeypatch,
                                                          tmp_path):
    attempts = []

    class FakeClient:
        def __init__(self, host, key):
            self.client_key = key
            self.live = False
            attempts.append(key)

        async def connect(self):
            self.live = True

        async def disconnect(self):
            self.live = False

        def is_connected(self):
            return self.live

    monkeypatch.setattr("devices.tv.lg_webos.AvctlWebOsClient", FakeClient)
    key_file = tmp_path / "tv-client-key"
    television = LGTelevision(
        "192.0.2.1", "working-key", "00:11:22:33:44:55",
        client_key_file=key_file,
    )

    asyncio.run(television.connect())

    assert attempts == ["working-key"]
    assert key_file.read_text(encoding="utf-8").strip() == "working-key"
    asyncio.run(television.disconnect())


def test_unaccepted_tv_pairing_is_an_explicit_state(monkeypatch, tmp_path):
    class RefusedClient:
        def __init__(self, host, key):
            self.client_key = key

        async def connect(self):
            raise WebOsTvPairError("pairing declined")

        async def disconnect(self):
            pass

    monkeypatch.setattr(
        "devices.tv.lg_webos.AvctlWebOsClient", RefusedClient)
    television = LGTelevision(
        "192.0.2.1", "", "00:11:22:33:44:55",
        client_key_file=tmp_path / "tv-client-key",
    )

    with pytest.raises(PairingRequired, match="on-screen prompt"):
        asyncio.run(television.connect())


def test_tv_state_reports_pairing_required_instead_of_standby(monkeypatch):
    link = tvlink._Link()
    dropped = False

    async def pairing_required():
        raise PairingRequired("accept prompt")

    async def drop():
        nonlocal dropped
        dropped = True

    async def unexpected_probe():
        raise AssertionError("pairing failure must not be labeled standby")

    monkeypatch.setattr(link, "_ensure", pairing_required)
    monkeypatch.setattr(link, "_drop", drop)
    monkeypatch.setattr(link, "_ssap_port_accepts", unexpected_probe)

    state = asyncio.run(link._state())

    assert dropped
    assert state == {
        "online": True,
        "power": None,
        "input": None,
        "sound": None,
        "detail": "pairing required",
    }
