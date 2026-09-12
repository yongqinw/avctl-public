# Remote access

avctl Core binds to loopback and is intended to sit behind an authenticated
private-network proxy. Tailscale Serve is the supported default because it
provides an HTTPS origin without opening Core to the public Internet.

## Recommended topology

```text
iPhone / iPad ── private tailnet ── Mac running avctl Core ── AV devices
```

Install Tailscale on the Core host and the controller devices, then publish
Core's loopback port:

```bash
tailscale serve --bg 8000
```

The resulting URL has the form `https://<host>.<tailnet>.ts.net`. Enter it in
the app's first-launch Core screen or set `AVCTL_PUBLIC_URL` so Bonjour can
advertise it. Never use `tailscale funnel`; Funnel makes the service public.

For an unattended Core, disable key expiry for that one machine or provision
it with an appropriately scoped tagged auth key. Keep enrollment inside the
Tailscale client or administrator workflow: the avctl setup API does not
accept, store, or distribute Tailscale account keys.

## Optional AV subnet routing

Some racks place TVs and bridges on a separate subnet behind the Core host.
That routing, DHCP, NAT, interface names, reservations, and firewall policy
are host infrastructure and therefore installation-specific. They do not
belong in the public avctl repository.

If the Core needs to expose that subnet to other tailnet devices, configure IP
forwarding and advertise the route using the host's deployment tooling, then
approve it in the Tailscale admin console. The UI installer only discovers
devices reachable from Core; it never rewrites host network configuration.

## Trust boundary

- Core remains on `127.0.0.1`; Tailscale Serve terminates TLS.
- Tailnet identity headers are trusted only on a loopback-bound request.
- The local bearer token exists for bootstrap and recovery, not as a shared
  multi-user identity.
- A compromised enrolled device is still a trusted network peer. Use ACLs,
  device revocation, and per-Core pairing to limit that blast radius.
- Personal topology, MAC addresses, pairing keys, and host service files stay
  in the owner's deployment state outside Git.
