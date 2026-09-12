"""McIntosh MAC7200 over RS-232.

Speaks the dialect MEASURED 2026-08-03 against this unit (fw 2.10) -- see the
`measured:` block in configs/mac7200_protocol.yaml. The transcribed PDF
dialect (zones, PON/POF/VUP/VST/HLP) is rejected wholesale by this firmware;
what works is the status tokens used as commands: (QRY), (PWR 0|1),
(VOL 0-100), and by extension (MUT 0|1) and (INP n).

Shape of the conversation, and why this file has a reader thread:

* The amp narrates. Every state change -- ours, the front panel's, the IR
  remote's -- is emitted as an unsolicited `(TOKEN value)` report, and a
  volume set is answered with one report per step as the amp slews to the
  target. So there is no request/response to pair up; instead one thread owns
  the read side forever, folds every report into a state dict, and command
  methods wait until the state says the thing happened.
* Sets that change nothing return SILENCE (measured), so "no reply" is not
  failure -- but unknown commands do get an explicit (ERROR ...) frame, which
  the reader records and the waiting command surfaces.
* NEVER send '?' as a parameter: this firmware parses it as 0. (VOL ?) slewed
  the volume to zero and (PWR ?) powered the amp off. (QRY) is the only query.
  `_send` refuses anything but bare upper-case words and integers.

Exactly one process can hold the serial device -- while the api service is
running, scripts must go through it rather than opening the port themselves.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

import serial

from devices.amp import Amp, AmpError

_FRAME = re.compile(rb"\(([^()]*)\)")
# The whole command vocabulary this file is allowed to emit. Deliberately a
# whitelist: the '?' incident is the kind of thing that only has to happen
# once.
_SAFE = re.compile(r"^\((QRY|PWR [01]|VOL \d{1,3}|MUT [01]|INP \d{1,2})\)$")

# Tokens whose value is a plain int in reports. Identity frames ("MAC7200",
# "Serial Number: ...", "ERROR - ...") are handled separately.
_INT_ARG = re.compile(r"^([A-Z][A-Z0-9]{2}) (-?\d+)$")


class McIntoshMAC7200(Amp):
    """One serial port, one reader thread, one narrated state dict."""

    def __init__(self, port: str, baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        # One read-check-send-await TRANSACTION at a time. _write_lock only
        # keeps two frames from interleaving on the wire; it does nothing for
        # two step_volume calls that both read VOL 40 and both send
        # (VOL 42) -- which is exactly what the phone's hold-repeat produces.
        # Reentrant because step_volume runs set_volume inside its own turn.
        self._txn_lock = threading.RLock()
        self._cond = threading.Condition()
        # Token -> last reported int value. Only ever written by the reader.
        self._state: dict[str, int] = {}
        # Token -> monotonic time it was last reported. Per token rather
        # than one dump-level stamp: the identity frame proved a bad proxy
        # for "dump parsed" (#109).
        self._token_at: dict[str, float] = {}
        self._error: str | None = None
        self._error_seq = 0

    # -- plumbing ---------------------------------------------------------

    def _ensure(self) -> serial.Serial:
        with self._write_lock:
            if self._serial is not None and self._serial.is_open:
                return self._serial
            if self._serial is not None:
                # A handle that is not open should hold no fd, but close is
                # cheap and a leak here costs the port itself (#109).
                try:
                    self._serial.close()
                except (serial.SerialException, OSError):
                    pass
            try:
                self._serial = serial.Serial(
                    self.port, self.baud, bytesize=8, parity="N",
                    stopbits=1, timeout=0.2,
                )
            except (serial.SerialException, OSError) as exc:
                raise AmpError(
                    f"cannot open {self.port}: {exc} -- unplugged, or another "
                    "process holds the port"
                )
            self._reader = threading.Thread(
                target=self._read_forever, args=(self._serial,),
                name="amp-reader", daemon=True,
            )
            self._reader.start()
            return self._serial

    def _read_forever(self, ser: serial.Serial) -> None:
        """Owns the read side until the port dies. Never raises."""
        buffer = b""
        while True:
            try:
                chunk = ser.read(512)
            except (serial.SerialException, OSError):
                # Unplugged; the next command reopens. Close the dead handle
                # NOW -- pyserial has no __del__, so a dropped handle leaks
                # its fd (and abort pipes) for the process's life, and a
                # stale fd on a re-enumerated USB device can make the reopen
                # fail with "another process holds the port" (#109).
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    pass
                break
            if not chunk:
                continue
            buffer += chunk
            consumed = 0
            for match in _FRAME.finditer(buffer):
                self._ingest(match.group(1).decode("ascii", errors="replace"))
                consumed = match.end()
            # Keep a partial trailing frame; drop inter-frame noise (the amp
            # pads QRY dumps with stray 0x00 bytes).
            buffer = buffer[consumed:]
            if b"(" not in buffer:
                buffer = b""
        with self._cond:
            if self._serial is ser:
                self._serial = None
            self._cond.notify_all()

    def _ingest(self, frame: str) -> None:
        with self._cond:
            if frame.startswith("ERROR"):
                self._error = frame
                self._error_seq += 1
            else:
                # Identity lines (MAC7200, FW Version, ...) carry no int arg
                # and fall through unrecorded on purpose: the identity frame
                # is the FIRST frame of a dump, and stamping freshness off it
                # let query() return the pre-dump state (#109).
                match = _INT_ARG.match(frame)
                if match:
                    token = match.group(1)
                    self._state[token] = int(match.group(2))
                    self._token_at[token] = time.monotonic()
            self._cond.notify_all()

    def _send(self, command: str) -> int:
        """Write one frame; returns the error sequence before the write."""
        if not _SAFE.match(command):
            raise ValueError(f"refusing to send {command!r}")
        ser = self._ensure()
        with self._cond:
            seq = self._error_seq
        with self._write_lock:
            try:
                ser.write(command.encode("ascii") + b"\r")
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                raise AmpError(f"serial write failed: {exc}")
        return seq

    def _await(self, seq: int, done: Callable[[dict[str, int]], bool],
               timeout: float, doing: str) -> None:
        """Wait until the narrated state satisfies `done`.

        An ERROR frame arriving after our write aborts the wait: with one
        writer (us) and reports for everything else, an error in that window
        is about the command we just sent.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._error_seq != seq:
                    raise AmpError(f"amp rejected {doing}: {self._error}")
                if done(dict(self._state)):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AmpError(
                        f"no confirmation of {doing} within {timeout:.0f}s -- "
                        "amp in standby, or cable unplugged?"
                    )
                self._cond.wait(remaining)

    # -- reads ------------------------------------------------------------

    def query(self, timeout: float = 4.0) -> dict[str, int]:
        """(QRY): refresh and return the full narrated state.

        Freshness is judged per token (#109): PWR must have been re-reported
        after our send -- it is in every dump, standby included -- and when
        the amp is on, so must the working tokens the callers act on. Waking
        on the dump's first frame could return the previous state, or {} on
        first use, and turn into a spurious "amp is in standby".
        """
        sent = time.monotonic()
        seq = self._send("(QRY)")

        def fresh(state: dict[str, int]) -> bool:
            at = self._token_at            # read under _cond, via _await
            if at.get("PWR", 0.0) < sent:
                return False
            if state.get("PWR") != 1:
                return True                # standby dump: identity + PWR 0
            # Only tokens the amp has ever narrated are required: a powered
            # dump re-reports all of them, but demanding one the firmware
            # never mentions would turn every query into a timeout.
            return all(at.get(token, 0.0) >= sent
                       for token in ("VOL", "MUT", "INP") if token in state)

        self._await(seq, fresh, timeout, "the query")
        with self._cond:
            return dict(self._state)

    def state(self) -> dict[str, int]:
        """Last narrated state, no wire traffic. May be empty before a query."""
        with self._cond:
            return dict(self._state)

    # -- commands ---------------------------------------------------------
    # Each waits for the report that says it happened; the no-change case
    # (silence, measured) is handled by checking state first.

    def power_on(self, timeout: float = 15.0) -> bool:
        """(PWR 1). Single send suffices (measured -- no PON-twice quirk);
        the amp answers with a full state dump at once, though the audio path
        keeps settling for ~10s after. Returns False if it was already on."""
        with self._txn_lock:
            if self.query().get("PWR") == 1:
                return False
            seq = self._send("(PWR 1)")
            self._await(seq, lambda s: s.get("PWR") == 1, timeout, "power on")
            return True

    def power_off(self, timeout: float = 10.0) -> bool:
        with self._txn_lock:
            if self.query().get("PWR") != 1:
                return False
            seq = self._send("(PWR 0)")
            self._await(seq, lambda s: s.get("PWR") == 0, timeout, "power off")
            return True

    def set_volume(self, level: int, timeout: float = 20.0) -> int:
        """(VOL n). The amp slews, narrating every step; generous timeout
        because 0 to 100 is a hundred reports, not one."""
        level = int(level)
        if not 0 <= level <= 100:
            raise ValueError(f"volume {level} not in 0-100")
        with self._txn_lock:
            current = self._on_state("set the volume")
            if current.get("VOL") == level:
                return level
            seq = self._send(f"(VOL {level})")
            self._await(seq, lambda s: s.get("VOL") == level, timeout,
                        "the volume")
            return level

    def step_volume(self, delta: int) -> int:
        with self._txn_lock:
            current = self._on_state("step the volume")
            if "VOL" not in current:
                raise AmpError("volume unknown -- query failed?")
            return self.set_volume(max(0, min(100, current["VOL"] + delta)))

    def set_mute(self, muted: bool, timeout: float = 5.0) -> bool:
        target = 1 if muted else 0
        with self._txn_lock:
            if self._on_state("mute").get("MUT") == target:
                return bool(target)
            seq = self._send(f"(MUT {target})")
            self._await(seq, lambda s: s.get("MUT") == target, timeout, "mute")
            return bool(target)

    def toggle_mute(self) -> bool:
        with self._txn_lock:
            return self.set_mute(self._on_state("mute").get("MUT") != 1)

    def set_input(self, input_id: int, timeout: float = 8.0) -> bool:
        """(INP n). Returns False if already there."""
        input_id = int(input_id)
        if not 1 <= input_id <= 9:
            raise ValueError(f"input {input_id} not in 1-9")
        with self._txn_lock:
            if self._on_state("switch input").get("INP") == input_id:
                return False
            seq = self._send(f"(INP {input_id})")
            self._await(seq, lambda s: s.get("INP") == input_id, timeout,
                        "the input switch")
            return True

    def _on_state(self, doing: str) -> dict[str, int]:
        """Fresh state, insisting the amp is on -- volume, mute and input all
        answer with silence from standby, which would read as a dead cable."""
        state = self.query()
        if state.get("PWR") != 1:
            raise AmpError(f"amp is in standby -- switch it on to {doing}")
        return state

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "McIntoshMAC7200":
        from devices import config as device_config
        mine = device_config.driver_block(config, "amp", "McIntoshMAC7200")
        port = str(mine.get("port", ""))
        if not port or port == "TODO":
            raise ValueError("amp.port is not configured")
        return cls(port, int(mine.get("baud", 115200)))
