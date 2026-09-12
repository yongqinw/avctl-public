# Managed Apple services

## Current boundary

avctl currently supports **owner-hosted mode**: the Python Apple Music driver
and APNs sender read private-key paths from the Core's owner configuration.
That is suitable for a trusted owner-operated Core. It is not a secure way to
give third-party Core machines access to one developer account.

The public setup wizard therefore never accepts an Apple `.p8` upload and the
bundled defaults contain no working developer identity. A friend can use Roon
without these keys. Managed Apple Music catalog access and background APNs
delivery use the broker contract below. This repository contains the Core
client, not a broker server. Broker operators keep service configuration and
signing keys outside source control.

## Broker design

The broker is a small HTTPS service controlled by the app publisher. It keeps
the MusicKit and APNs private keys in its secret store and exposes two narrow
capabilities, not the keys themselves:

```text
publisher broker
  ├─ MusicKit private key ──> short-lived developer token ──> paired Core
  └─ APNs private key ──────> APNs request ─────────────────> Apple
                                  ^
paired app/Core ── per-installation signing identity + revocable grant
```

### Enrollment

1. Core creates a random installation ID and an Ed25519 key pair locally.
2. The publisher creates a one-time, expiring passphrase. Setup fills in the
   broker HTTPS URL from `apple_services.broker.url` in bundled defaults; the
   listener enters only their passphrase, or edits the address for another
   broker. Existing enrollments keep their saved broker address. The passphrase
   field always starts empty; installers and defaults never contain invites.
3. Broker binds that public key to the installation. The private half stays in
   an owner-only Core file and the passphrase is never persisted.
4. Every request includes a nonce, timestamp, body digest, and installation
   signature. Broker rejects replays and revoked installations.

### MusicKit

`POST /v1/musickit/token` returns a short-lived Apple Music developer token
after installation authentication and rate-limit checks. The Apple Music
private key never leaves the broker. If a web origin uses the token, the token
should carry the matching origin restriction.

The listener still grants MusicKit access to their own Apple Music account.
Their Music User Token is subscriber-specific and remains on their Core; it is
not shared with the publisher or other installations.

Longer term, catalog operations can move into the signed macOS MusicKit bridge
and use MusicKit's automatic token management. That removes the Python
driver's need for a developer token for those operations, but still requires
the listener's Music authorization.

### APNs

`POST /v1/push` accepts a closed, size-bounded avctl activity payload and a
registered device-token reference. Broker checks that the installation owns
that reference, applies per-installation quotas, then sends the request to
APNs. It never returns the APNs provider JWT or signing key.

APNs registration is scoped to the environment and bundle topic of the signed
app. A friend may use the publisher's distributed build and notification
provider, but each physical device still has its own APNs device token.

### Operations

- Keep MusicKit and APNs signing keys separate and encrypted at rest.
- Log installation ID, operation, status, latency, and usage; never log tokens.
- Rate-limit catalog requests and pushes per installation and globally.
- Support immediate installation revocation and Apple-key rotation.
- Validate an allowlisted schema; never proxy arbitrary URLs, APNs topics, or
  arbitrary notification payloads.
- Return explicit capability status so the installer labels managed Apple
  services unavailable until broker enrollment actually succeeds.

## Deployment boundary

The broker binds only to loopback. A public HTTPS reverse proxy is required so
a friend's Core can reach it without joining the publisher's tailnet. On the
broker host, Tailscale Funnel can expose only the broker port while Core and
administrative services remain private. The broker still
rejects every request without a valid enrolled-Core signature.

The broker contract includes one-use invites, revocation, signed request
bodies, timestamp and nonce replay protection, per-installation quotas,
short-lived MusicKit developer tokens, encrypted APNs device-token storage,
server-owned topics/environments, and a closed Live Activity/widget payload
schema. Key rotation and persistent/distributed rate limits remain operational
follow-ups before supporting more than a small trusted friend group.

Apple requires ES256 developer/provider tokens and limits APNs provider token
refresh cadence; a broker implementation should cache valid APNs JWTs and
reuse its HTTP/2 client. See [Apple Music developer tokens](https://developer.apple.com/documentation/AppleMusicAPI/generating-developer-tokens),
[APNs token authentication](https://developer.apple.com/documentation/UserNotifications/establishing-a-token-based-connection-to-apns), and
[Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel).
