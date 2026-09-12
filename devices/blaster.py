"""The Global Caché iTach IP2IR provides wired IR for supported AV devices.

IR supports devices without a usable network-control interface and can
wake televisions that stop responding to Wake-on-LAN in deep standby.
Configure the iTach's network address and learned commands in the local
device configuration.

Protocol facts, from the iTach API spec v1.5 (globalcache.com API-iTach.pdf)
and verified against the unit (firmware 710-1005-05):

* Plain ASCII over TCP port 4998, CR-terminated. `sendir,1:P,ID,<code>`
  answers `completeir,1:P,ID` when the IR has left the emitter.
* `<code>` is `<freq>,<repeat>,<offset>,<on1>,<off1>,...` -- on/off values
  are CARRIER CYCLES, not microseconds.
* `set_NET` does not exist on this firmware (ERR 001) -- configure network
  settings through its web page or a DHCP reservation.
* The IR learner is a hole below-right of the power connector; `get_IRL`
  answers `IR Learner Enabled` and then streams one uncompressed sendir
  line per captured button press, to this connection only. Any command
  (or `stop_IRL`) disables it.

Codes in config.yaml come in two spellings, resolved here:

    nec:20DF10EF     -- a 32-bit NEC code (LG's family); the timing is
                        constructed, not stored, because NEC is exactly
                        specified and the hex is what databases publish
    gc:38000,1,1,... -- a raw Global Caché timing string, exactly what the
                        learner emits; used for everything learned (D900)
"""

from __future__ import annotations

import socket
import threading

GC_PORT = 4998

# Error code -> meaning, from the spec's section 6 table (the ones a code
# path here can actually trigger).
_ERRORS = {
    "001": "invalid command (does this firmware support it?)",
    "002": "no such module",
    "003": "no such connector",
    "007": "sendir offset must be odd",
    "010": "sendir needs equal on/off counts",
    "023": "device is busy",
}


class BlasterError(RuntimeError):
    """The iTach was unreachable, refused the command, or timed out."""


def nec_sendir(value: int) -> str:
    """A 32-bit NEC frame as Global Caché timing, in 38kHz cycle counts.

    NEC is 9ms/4.5ms preamble, then 32 bits LSB-first per byte (bit 0 =
    562us/562us, bit 1 = 562us/1687us), a stop mark, and silence out to the
    108ms frame. 21 cycles = 553us at 38kHz -- close enough that every NEC
    receiver accepts it (verified live: completeir + the TV responding).
    """
    seq = [342, 171]
    for byte in value.to_bytes(4, "big"):
        for bit in range(8):
            seq += [21, 64 if (byte >> bit) & 1 else 21]
    seq += [21, 1517]
    return "38000,1,1," + ",".join(str(n) for n in seq)


def resolve_code(spec: str) -> str:
    """Turn a config.yaml code spelling into raw iTach timing."""
    spec = str(spec).strip()
    if spec.lower().startswith("nec:"):
        try:
            return nec_sendir(int(spec[4:], 16))
        except ValueError:
            raise ValueError(f"not a 32-bit hex NEC code: {spec!r}")
    if spec.lower().startswith("gc:"):
        return spec[3:]
    raise ValueError(f"IR codes are 'nec:<hex8>' or 'gc:<timings>', not {spec!r}")


class ITachIR:
    # Every send gets a fresh ID so completeir acks can never be confused
    # across connections; it only has to differ per command, not persist.
    # Class-level, like the counter: two instances would still be talking to
    # the same physical box.
    _counter = 0
    _counter_lock = threading.Lock()
    # Drivers are cached per device, not per wire: the DAC, TV IR fallback
    # and disc player can each construct a view of the same iTach. The box
    # still accepts only one send at a time, so instances for one host share
    # a lock or their separate TCP connections race into ERR 023.
    _host_locks: dict[str, threading.Lock] = {}
    _host_locks_guard = threading.Lock()

    def __init__(self, host: str, timeout: float = 6.0):
        self.host = host
        self.timeout = timeout
        # One send at a time. The unit answers ERR 023 (busy) or interleaves
        # when two commands arrive on separate connections -- and a DAC walk
        # losing one press to that is a silent desync the user has to notice
        # and repair by hand.
        with ITachIR._host_locks_guard:
            self._send_lock = ITachIR._host_locks.setdefault(
                host, threading.Lock())

    @classmethod
    def from_config(cls, config: dict) -> "ITachIR | None":
        """None until blaster.host is filled in."""
        block = config.get("blaster") or {}
        host = block.get("host")
        if not host or host == "TODO":
            return None
        return cls(host=str(host))

    def _exchange(self, command: str, timeout: float | None = None) -> str:
        sock = None
        try:
            sock = socket.create_connection(
                (self.host, GC_PORT), timeout=timeout or self.timeout)
            sock.sendall((command + "\r").encode())
            # Read until the CR that ends the reply: one recv assumed the
            # whole ack arrived in one segment, and a fragmented completeir
            # read as "no ack" AFTER the IR had already fired -- which walks
            # the tracked DAC one press away from reality (#110).
            raw = b""
            while b"\r" not in raw:
                chunk = sock.recv(2048)
                if not chunk:
                    break   # peer closed; classify whatever arrived
                raw += chunk
            data = raw.decode(errors="replace").strip()
        except OSError as exc:
            raise BlasterError(f"iTach at {self.host} unreachable: {exc}")
        finally:
            # Closed on every path: a recv that dies mid-walk must not leak
            # a connection into a box that only holds a few at once.
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if data.startswith("ERR"):
            code = data.rsplit(",", 1)[-1]
            raise BlasterError(
                f"iTach refused: {_ERRORS.get(code, data)} ({data})")
        return data

    def send(self, code: str, port: int | None = None) -> None:
        """Fire one code spec out one IR port -- or out ALL of them.

        port=None blasts every connector in turn: correct whenever the code
        only means something to one device (each emitter is glued to its own
        device's IR window, and a foreign code is ignored), and it is what
        makes TV wake work before the port map is even filled in.
        """
        timing = resolve_code(code)
        # `is not None`, not truthiness: port 0 is a config mistake, and
        # silently promoting it to "all three" fires codes at devices nobody
        # aimed at. Let it go out as connector 0 and come back as the
        # iTach's own ERR 003.
        ports = [port] if port is not None else [1, 2, 3]
        with self._send_lock:
            for connector in ports:
                with ITachIR._counter_lock:
                    ITachIR._counter = (ITachIR._counter + 1) % 65536
                    counter = ITachIR._counter
                reply = self._exchange(
                    f"sendir,1:{connector},{counter},{timing}")
                if not reply.startswith("completeir"):
                    raise BlasterError(f"no completeir ack: {reply!r}")

    def on_port(self, port: int | None) -> "BoundPort":
        """A view of this blaster that always fires out one connector.

        Device drivers should hold one of these rather than the blaster
        itself: they get a plain `.send(code)` and stay ignorant of which
        emitter is glued to their front panel, which is a config fact.
        """
        return BoundPort(self, port)

    def learn(self, wait: float = 30.0) -> str:
        """Capture one button press via the on-board learner.

        Returns the code as a 'gc:' spec ready for config.yaml. The learner
        is the small hole below-right of the power connector -- hold the
        remote a few centimetres away and press once, briefly.
        """
        sock = None
        try:
            sock = socket.create_connection((self.host, GC_PORT),
                                            timeout=self.timeout)
            sock.sendall(b"get_IRL\r")
            sock.settimeout(wait)
            buffer = ""
            while True:
                chunk = sock.recv(4096).decode(errors="replace")
                if not chunk:
                    raise BlasterError("iTach closed the learner connection")
                buffer += chunk
                if "Unavailable" in buffer:
                    raise BlasterError("IR learner unavailable on this unit")
                if "sendir" in buffer and "\r" in buffer.split("sendir", 1)[1]:
                    break
        except socket.timeout:
            raise BlasterError(f"nothing learned in {wait:.0f}s -- press the "
                               "button closer to the learner hole")
        except OSError as exc:
            raise BlasterError(f"iTach at {self.host} unreachable: {exc}")
        finally:
            # Closed on EVERY path (#110): the box holds only a few TCP
            # connections, and a missed 30s window used to strand one --
            # with the learner still armed -- until GC got around to it.
            if sock is not None:
                try:
                    sock.sendall(b"stop_IRL\r")
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        # The capture is 'sendir,1:1,<id>,<freq>,<repeat>,<offset>,<timings>';
        # keep everything from <freq> on -- port and id belong to the sender.
        line = buffer.split("sendir", 1)[1].split("\r", 1)[0]
        parts = line.lstrip(",").split(",")
        return "gc:" + ",".join(parts[2:])


class BoundPort:
    """One blaster, one connector. See ITachIR.on_port()."""

    def __init__(self, blaster: ITachIR, port: int | None):
        self._blaster = blaster
        self._port = port

    def send(self, code: str) -> None:
        self._blaster.send(code, port=self._port)
