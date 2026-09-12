# UI setup wizard

The installer is split at the trust boundary instead of pretending an iPhone
can silently configure a Mac or a Tailscale account.

## Install and configure Core

1. Open the signed/notarized `Avctl-Server-<version>.pkg`. It installs a
   self-contained runtime and per-user LaunchAgents; the receiving Mac needs no
   Python, Homebrew, Xcode, or repository checkout.
2. Installer waits for Core health and opens the loopback-only `/bootstrap`.
   This grants the local browser an owner cookie and opens Setup without
   exposing the recovery token in a URL.
3. **Check** reports the Mac/architecture and every bundled or optional
   capability. Unavailable services are labeled before panels are chosen.
4. Continue through Music, Panels, Devices, Access, and Review. Saving writes
   the private overlay atomically; packaged installs restart once to activate
   the selected drivers.
5. In Access, sign in to Tailscale and explicitly enable Serve. Setup shows the
   final HTTPS address to enter in the phone app.

## First launch on iPhone or iPad

1. Install the separately signed app. The device UDID must already be in the
   publisher's provisioning profile; the Core installer cannot bypass this.
2. Enter the Core's Tailscale Serve URL manually. Bonjour discovery remains a
   convenience, not a pairing or identity protocol.
3. The app checks `/healthz`, then `/whoami`. It saves the address only after
   Tailscale confirms an authenticated identity. The bearer token remains an
   advanced local bootstrap/recovery path.

The iPad keeps a 245-point setup rail on the left. The iPhone uses the same
steps in one column. The permanent panel rail is not changed or crowded by a
Setup tab.

## Core wizard

The six stages are:

1. **Check:** show the packaged runtime, host support, input helper, voice
   runtime, Apple Music, Roon driver, and signed phone-app availability.
2. **Music:** choose Apple Music or Roon + Qobuz. Discovery checks Music.app
   locally and uses Roon's own discovery protocol for reachable Cores. When
   several Cores answer, the wizard asks instead of guessing.
3. **Panels:** Home is required. Music, Ask, Mac mini, TV, and DAC/Amp can all
   be included or skipped; the existing panel-order Settings remains available
   afterwards.
4. **Devices:** enter or discover an LG webOS TV, serial amplifier, and iTach
   IR bridge, or project DAC/Amp control through a Roon output. Network and
   serial discovery is read-only and starts only when the user taps Scan.
   Roon authorization happens here through Roon's Extensions screen; its token
   is written privately on the Core and only friendly zone/output choices are
   returned to the UI. Scene levels and hard volume caps are configured here.
   If Ask is enabled, select its provider, save a credential into an
   owner-only local file, and run a real structured tool-call probe before
   continuing. Credential contents are never returned by the API.
5. **Access:** report whether Tailscale is installed, online, and serving the
   Core. The user signs in through Tailscale itself; avctl never receives a
   reusable Tailscale auth key.
6. **Review:** validate a closed setup schema, generate a Home scene containing
   only the devices that were actually configured, write the user override
   atomically, save panel visibility, and request one Core restart.

Wizard configuration lives at:

```text
~/Library/Application Support/avctl/config.yaml
```

It is owner-readable only and overlays bundled defaults. No API keys, Roon
tokens, Apple Music private keys, or TV pairing credentials are accepted by
the setup endpoint. Those remain references to their own protected stores.

Source installations use the same contract: keep the complete overlay in
owner-only machine state and install it at this path before Core starts. The
public repository ships `configs/defaults.yaml`; it never ships a working
rack's addresses, identifiers, learned IR payloads, or signing configuration.

Apple services have three explicit modes. `local` is the single-owner
deployment and reads external private-key paths on that trusted Core.
`managed` fills in the publisher's broker HTTPS URL from
`apple_services.broker.url` and lets friends enter their one-time passphrase;
the address remains editable and existing enrollments keep their paired URL.
The passphrase is never bundled or prefilled. Core creates its own signing
identity and never receives either Apple private key. `disabled` keeps both
capabilities off. The broker pairing
controls live in Access and follow the active color scheme on iPhone and in the
wider left Settings workspace on iPad. See
[managed-apple-services.md](managed-apple-services.md).

## Bonjour advertisement

Core advertises only a stable random Core ID, schema version, and reachable
URL. Set `AVCTL_PUBLIC_URL` to the Tailscale Serve URL when automatic
Tailscale detection is not available. `AVCTL_ADVERTISE=0` disables Bonjour.

The iOS target declares local-network usage and `_avctl._tcp` in its generated
Info.plist, so the permission prompt describes the actual discovery behavior.

## Deliberate boundary

The installer never enrolls a user into a tailnet or stores a Tailscale auth
key. The user signs in through Tailscale and explicitly enables Serve. Phone
access then uses Tailscale identity; there is intentionally no second avctl
pairing database. See `core-installer.md` for packaging and signing details.
