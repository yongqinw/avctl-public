# Avctl Server installer

For a walkthrough of installation, every wizard step, optional components,
credentials, and the iPhone/iPad app, start with the
[setup guide for friends](friend-setup.md).

The distributable Core is `Avctl Server.app` inside a macOS installer package.
It targets Apple-silicon Macs on macOS 14 or newer and does not depend on a
system Python, Homebrew, Xcode, or a Git checkout on the receiving Mac. The
release builder freezes Python and the declared dependencies with PyInstaller.

## Friend installation

1. Download `Avctl-Server-<version>.pkg` and follow its release guide before
   opening it. Release 6.4 is unsigned and not notarized.
2. The package installs `/Applications/Avctl Server.app`, creates a per-user
   LaunchAgent for Core, starts it, waits for `/healthz`, and opens Setup on
   the configured local Core port (8000 on a fresh installation). The optional
   Mac-input helper is enabled later when its panel is selected.
3. The loopback-only bootstrap grants the local browser an owner cookie without
   placing the Core token in browser history or process arguments. It opens the
   Setup workspace automatically.
4. Setup reports which optional capabilities are actually present. Choose a
   music backend and panels, discover devices, set volume caps, authorize Roon
   or managed Apple services, and save. The package includes the signed
   MusicKit bridge; the Apple Music step verifies both bridge consent and
   read-only Music.app automation before calling that backend ready. If Ask is
   selected, Setup stores the user's provider credential in a private local
   file and verifies structured tool calling. A
   packaged Core restarts itself once to activate the validated configuration.
5. In Access, install/sign in to Tailscale on the Mac and press **Enable
   Tailscale Serve**. Setup displays the resulting HTTPS URL.
6. Install the separately signed iPhone/iPad app and enter that Tailscale URL.
   Tailscale identity authenticates it; no second phone-pairing protocol is
   required.

The Access step also lets the owner choose the loopback Core port (1024–65535).
The choice is stored in the owner configuration and takes effect on restart;
Tailscale Serve is pointed at the same port, while Core remains bound only to
`127.0.0.1`. Fresh installations start setup on port 8000 when it is free, or
the first free port through 8099 when another application already owns 8000.
If this macOS user already has a configured avctl Core running, the package
opens that Core and does not replace it or start a second instance. Test two
independent Cores with a separate macOS user or VM so they do not share state.

The setup report is intentionally honest about the iOS build. A friend device
must have its UDID registered in the publisher's provisioning profile before
the signed app can be installed. The Core installer cannot bypass that Apple
requirement.

## Build

Create a Python 3.12 virtual environment, install `requirements.txt` and
`installer/requirements.txt`, then run:

```bash
AVCTL_BUILD_PYTHON=.installer-venv/bin/python \
AVCTL_VERSION=6.4 \
AVCTL_APPLICATION_SIGN_IDENTITY="Developer ID Application: …" \
AVCTL_INSTALLER_SIGN_IDENTITY="Developer ID Installer: …" \
./installer/build-pkg.sh
```

## Local acceptance

Run the focused, hardware-free installer matrix before packaging:

```bash
AVCTL_TEST_PYTHON=/path/to/venv/bin/python \
  ./scripts/test_installer_acceptance.sh --focused
```

Use `--full` for the whole repository suite. Both modes use synthetic
credentials and drivers; they cannot touch the current rack or music library.
They cover service installation, private/atomic configuration, device
discovery contracts, zero-volume round trips, both Apple Music and Roon queue
implementations, and Ask plans against both backends.

An already-installed Core has a separate live smoke test. It temporarily lowers
every reachable output to zero before discovery, search, or Ask; it never
starts playback, changes the queue/library, switches backends, enables
Tailscale, or saves setup, and it restores the previous volumes afterward:

```bash
./venv/bin/python scripts/installer_live_acceptance.py
```

Pass `--network` only when a read-only local `/24` scan is wanted. The script
uses `AVCTL_TEST_TOKEN` or `~/.avctl/token` internally and never prints it.

With no identities, the script makes a local ad-hoc/unsigned package for
testing. Developer ID signing, Apple notarization, stapling, and a generated
SHA-256 file are the intended distribution path. The published 6.4 package
is unsigned and not notarized; its setup guide documents that caveat.

PyInstaller explicitly collects every API/device driver because the registry
loads drivers dynamically. The receiving Mac therefore has Apple Music, Roon,
LG webOS, serial amp, IR DAC, Ask, and discovery implementations in the frozen
runtime; choosing a different backend does not require Python packages later.

Opening `Avctl Server.app` later reads the saved Core port and returns to local
Setup. The bundled
`Uninstall avctl.command` unloads the LaunchAgents and keeps configuration by
default. `--delete-data` is an explicit destructive choice that also removes
the user configuration and the Core's `~/.avctl` runtime state, including its
local bearer token and broker identity; unrelated files in the home directory
are left alone.

Ask setup includes the local MLX transcription runtime. Choosing Ask + voice
prepares the configured Whisper model on the Mac and must pass before setup
can finish; choosing text-only Ask skips the model. The Mac remote-input
helper is also opt-in: it is installed and launched only when the Mac mini
panel is selected, so other installations do not request Accessibility.
