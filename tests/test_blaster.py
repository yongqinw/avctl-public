"""Issue #24: the iTach speaks to one command at a time, or not reliably.

A real loopback TCP server plays the iTach: it acks completeir, counts how
many connections are open at once, and remembers every command -- so both
the serialization and the port-0 fix are asserted against actual sockets,
the same path the walk uses.
"""

from __future__ import annotations

import socketserver
import threading
import time

import pytest

from devices import blaster as blaster_module
from devices.blaster import BlasterError, ITachIR


class FakeITach(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.commands: list[str] = []
        self.error_reply: str | None = None   # answer this instead of the ack
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server: FakeITach = self.server
        with server.guard:
            server.active += 1
            server.max_active = max(server.max_active, server.active)
        try:
            data = self.request.recv(4096).decode().strip()
            with server.guard:
                server.commands.append(data)
            time.sleep(0.02)   # the window two unserialized sends collide in
            if server.error_reply:
                self.request.sendall(server.error_reply.encode() + b"\r")
                return
            connector = data.split(",")[1] if "," in data else "1:1"
            ident = data.split(",")[2] if data.count(",") >= 2 else "0"
            self.request.sendall(f"completeir,{connector},{ident}\r".encode())
        finally:
            with server.guard:
                server.active -= 1


@pytest.fixture
def itach(monkeypatch):
    server = FakeITach()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(blaster_module, "GC_PORT", server.server_address[1])
    yield server
    server.shutdown()
    server.server_close()


def test_concurrent_sends_serialize(itach):
    unit = ITachIR("127.0.0.1")
    threads = [threading.Thread(target=unit.send, args=("nec:20DF10EF", 1))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(itach.commands) == 4
    assert itach.max_active == 1


def test_separate_drivers_for_one_itach_still_serialize(itach):
    """The DAC and TV own different driver objects but share one wire."""
    units = [ITachIR("127.0.0.1") for _ in range(4)]
    threads = [threading.Thread(target=unit.send,
                                args=("nec:20DF10EF", 1))
               for unit in units]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(itach.commands) == 4
    assert itach.max_active == 1


def test_port_zero_is_not_all_three(itach):
    unit = ITachIR("127.0.0.1")
    unit.send("nec:20DF10EF", port=0)
    assert len(itach.commands) == 1
    assert itach.commands[0].startswith("sendir,1:0,")


def test_no_port_still_means_every_connector(itach):
    unit = ITachIR("127.0.0.1")
    unit.send("nec:20DF10EF")
    connectors = [c.split(",")[1] for c in itach.commands]
    assert connectors == ["1:1", "1:2", "1:3"]


def test_refusal_raises_with_the_spec_meaning(itach):
    unit = ITachIR("127.0.0.1")
    itach.error_reply = "ERR IR,1:1,023"
    with pytest.raises(BlasterError, match="busy"):
        unit.send("nec:20DF10EF", port=1)
