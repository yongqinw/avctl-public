"""Learn IR codes off the physical remotes, via the iTach's on-board learner.

    venv/bin/python scripts/learn_ir.py --check          # port modes, no learning
    venv/bin/python scripts/learn_ir.py dac_input_next   # learn one code

The learner is the small hole below and to the RIGHT of the power connector
-- point the remote at the iTach itself, not at an emitter. Hold it a few
centimetres away and TAP the button once, briefly, only after this script
says ARMED.

Why the fuss about timing: NEC remotes send one data frame and then, while
the button stays down, a stream of contentless "repeat" frames. Arm late or
hold the button and the learner captures repeats only -- which look like a
successful capture and control nothing. That is exactly what happened on
2026-08-05 (three captures, all repeats, hours lost). So this script
classifies every frame it hears and refuses to print a code that carries no
data, decoding real ones to address/command so they can be sanity-checked
against published tables.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from devices import config as device_config
from devices.blaster import ITachIR

GC_PORT = 4998

# NEC timings in carrier cycles at ~38kHz (1 cycle = 26.2us).
NEC_HEAD_MARK = 342      # 9ms
NEC_DATA_SPACE = 171     # 4.5ms -> a real frame with 32 bits behind it
NEC_REPEAT_SPACE = 85    # 2.25ms -> filler, no payload
TOL = 14


def classify(timings: list[int]) -> str:
    if len(timings) < 2:
        return "fragment"
    mark, space = timings[0], timings[1]
    if abs(mark - NEC_HEAD_MARK) > 90:
        return "not-NEC"
    if abs(space - NEC_DATA_SPACE) <= TOL:
        return "data"
    if abs(space - NEC_REPEAT_SPACE) <= TOL:
        return "repeat"
    return "not-NEC"


def decode_nec(timings: list[int]) -> str:
    """Address and command from a data frame, with NEC's inverse checks.

    Bits are LSB-first per byte; a 1 is a long space (~1.7ms), a 0 a short
    one. Classic NEC complements the address; extended NEC uses 16 bits
    straight, so report which one this is rather than calling it corrupt.
    """
    bits = []
    for i in range(2, min(len(timings) - 1, 2 + 64), 2):
        bits.append(1 if timings[i + 1] > 40 else 0)
    if len(bits) < 32:
        return f"only {len(bits)} bits -- truncated"
    octets = [sum(b << i for i, b in enumerate(bits[n:n + 8]))
              for n in range(0, 32, 8)]
    addr, naddr, cmd, ncmd = octets
    if cmd ^ ncmd != 0xFF:
        return "command checksum failed -- capture is noisy, try again"
    if addr ^ naddr == 0xFF:
        return f"NEC address 0x{addr:02X}, command 0x{cmd:02X}"
    return (f"extended NEC address 0x{addr:02X}{naddr:02X}, "
            f"command 0x{cmd:02X}")


def check_ports(host: str) -> None:
    """The diagnostic that was never run: what mode is each connector in?

    A port set to SENSOR still answers sendir politely on some firmware
    while radiating nothing -- indistinguishable from a misplaced emitter
    unless you ask.
    """
    for port in (1, 2, 3):
        sock = socket.create_connection((host, GC_PORT), timeout=5)
        sock.sendall(f"get_IR,1:{port}\r".encode())
        answer = sock.recv(256).decode(errors="replace").strip()
        sock.close()
        mode = answer.rsplit(",", 1)[-1]
        flag = "  <-- not an IR output!" if mode not in ("IR", "IR_BLASTER") else ""
        print(f"  port {port}: {mode}{flag}")


def learn(host: str, name: str, window: float) -> int:
    sock = socket.create_connection((host, GC_PORT), timeout=8)
    sock.sendall(b"get_IRL\r")
    sock.settimeout(8)
    hello = sock.recv(256).decode(errors="replace")
    if "Enabled" not in hello:
        print(f"learner refused: {hello.strip()!r}")
        return 1

    print()
    print(f"  ARMED for {window:.0f}s -- learning {name!r}")
    print("  Point the remote at the small hole beside the iTach's power")
    print("  connector and TAP the button once. Do not hold it.")
    print()

    deadline = time.time() + window
    buffer, best = "", None
    try:
        while time.time() < deadline:
            sock.settimeout(max(1.0, deadline - time.time()))
            chunk = sock.recv(4096).decode(errors="replace")
            if not chunk:
                break
            buffer += chunk
            while "\r" in buffer:
                line, buffer = buffer.split("\r", 1)
                if not line.strip().startswith("sendir"):
                    continue
                fields = line.strip().split(",")
                freq, timings = fields[3], [int(x) for x in fields[6:]]
                kind = classify(timings)
                if kind == "data":
                    code = "gc:" + ",".join(fields[3:])
                    print(f"  heard: DATA frame, {len(timings)} timings")
                    print(f"         {decode_nec(timings)}")
                    best = code
                    raise StopIteration
                print(f"  heard: {kind} frame ({len(timings)} timings) "
                      "-- no payload, press again with a short tap")
    except (socket.timeout, StopIteration):
        pass
    finally:
        try:
            sock.sendall(b"stop_IRL\r")
            sock.close()
        except OSError:
            pass

    if not best:
        print("\n  nothing usable captured. Tap -- do not hold -- and keep the")
        print("  remote a few cm from the learner hole.")
        return 1
    print(f"\n{name}: {best}\n")
    print("Paste into config.yaml under the device's codes: (with a "
          "CONFIRMED date).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", default="code",
                        help="what this code is, echoed back for your notes")
    parser.add_argument("--check", action="store_true",
                        help="report each connector's mode and exit")
    parser.add_argument("--window", type=float, default=60.0,
                        help="seconds to listen (default 60)")
    args = parser.parse_args()

    blaster = ITachIR.from_config(device_config.load_config())
    if blaster is None:
        print("blaster.host is not set in the owner config")
        sys.exit(1)

    if args.check:
        print(f"iTach at {blaster.host}:")
        check_ports(blaster.host)
        sys.exit(0)
    sys.exit(learn(blaster.host, args.name, args.window))


if __name__ == "__main__":
    main()
