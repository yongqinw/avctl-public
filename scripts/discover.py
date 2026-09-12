#!/usr/bin/env python3
"""Find the AV devices on this LAN.

Stdlib only, so it runs on the Mac mini's system Python 3.9 as-is.
Scans the local /24 for the control ports each device listens on.

    python3 discover.py            # scan the interface with the default route
    python3 discover.py 192.168.1  # or force a /24 prefix
"""

import concurrent.futures
import socket
import subprocess
import sys
import urllib.error
import urllib.request

# port -> what listening there implies
SIGNATURES = {
    3000: "LG webOS (SSAP, unencrypted)",
    3001: "LG webOS (SSAP, TLS)",
    8080: "HTTP control (Panasonic?)",
    80: "HTTP",
}

CONNECT_TIMEOUT = 0.4


def local_prefix() -> str:
    """The /24 of whichever interface holds the default route."""
    route = subprocess.run(
        ["route", "-n", "get", "default"], capture_output=True, text=True
    ).stdout
    iface = ""
    for line in route.splitlines():
        if "interface:" in line:
            iface = line.split(":")[1].strip()
    if not iface:
        sys.exit("no default route found; pass a /24 prefix explicitly")
    ip = subprocess.run(
        ["ipconfig", "getifaddr", iface], capture_output=True, text=True
    ).stdout.strip()
    if not ip:
        sys.exit(f"no IPv4 address on {iface}; pass a /24 prefix explicitly")
    return ip.rsplit(".", 1)[0]


def probe(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(CONNECT_TIMEOUT)
        return s.connect_ex((host, port)) == 0


def is_panasonic_bluray(host: str) -> bool:
    """Panasonic players expose a remote-control CGI. Probing it identifies them
    without sending a keypress -- a bare GET is rejected, but a 404 vs a refused
    connection still tells us the endpoint exists."""
    url = f"http://{host}/nas/dvdr/dvdr_ctrl.cgi"
    try:
        urllib.request.urlopen(url, timeout=1.0)
        return True
    except urllib.error.HTTPError:
        return True  # endpoint is there, it just dislikes an empty GET
    except Exception:
        return False


def scan_host(prefix: str, n: int):
    host = f"{prefix}.{n}"
    open_ports = [p for p in SIGNATURES if probe(host, p)]
    if not open_ports:
        return None
    tags = [SIGNATURES[p] for p in open_ports]
    if 80 in open_ports and is_panasonic_bluray(host):
        tags.append(">>> Panasonic Blu-ray control CGI present")
    return host, open_ports, tags


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else local_prefix()
    print(f"scanning {prefix}.1-254 ...\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
        futures = [pool.submit(scan_host, prefix, n) for n in range(1, 255)]
        for f in concurrent.futures.as_completed(futures):
            hit = f.result()
            if hit:
                host, ports, tags = hit
                print(f"{host:16} {str(ports):16} {' | '.join(tags)}")
    print("\ndone. LG TV = the host with 3000/3001 open.")


if __name__ == "__main__":
    main()
