# avctl

avctl is an open-source controller for a home audio/video system. A FastAPI Core
on a Mac connects Apple Music or Roon playback with optional amplifier, DAC,
TV, and Mac controls. Browsers and the native iPhone/iPad app share the same
web interface and Core state.

## Download 6.2

- [Mac installer: Avctl-Server-6.2.pkg](https://github.com/yongqinw/avctl-public/releases/download/6.2/Avctl-Server-6.2.pkg)
- [SHA-256 checksum](https://github.com/yongqinw/avctl-public/releases/download/6.2/Avctl-Server-6.2.pkg.sha256)
- [Release notes and all assets](https://github.com/yongqinw/avctl-public/releases/tag/6.2)
- [Illustrated setup guide](docs/friend-setup.md)
- [Setup guide PDF](https://github.com/yongqinw/avctl-public/releases/download/6.2/Avctl-6.2-Setup-Guide.pdf)
- [Standalone HTML guide](https://github.com/yongqinw/avctl-public/releases/download/6.2/Avctl-6.2-Setup-Guide.html)

Version 6.2 is unsigned and not notarized. Read the installation steps in the
guide before opening the package.

The packaged Core requires an Apple-silicon Mac running macOS 14 or newer, a
working Apple Music or Roon setup, and internet access for online services.
It includes its Python runtime; installation does not require Homebrew, Xcode,
or a source checkout.

The optional native app requires iOS/iPadOS 26 or newer. The publisher's current
build also requires device registration and Developer Mode; obtain its install
link from the publisher. Connect your phone and Core through your own Tailscale
network. Developers can build and sign the app with their own Apple account.

## Features

- Apple Music library and catalog playback, or Roon library and linked services
  such as Qobuz, through a shared music panel and logical queue.
- Optional LG webOS TV, McIntosh serial amplifier, Topping D900 infrared DAC,
  and Mac remote controls; capability-based Roon output controls.
- Configurable panels, volume limits, and scenes, with a browser setup wizard.
- Natural-language Ask through Fireworks, with optional local voice
  transcription and a DeepSeek v4.1 Flash profile.
- An iPhone/iPad shell, widgets, and Live Activities backed by the same Core
  API. Managed Apple services require enrollment with a separate broker.

Device support depends on the selected driver and hardware capabilities.
See the [setup guide](docs/friend-setup.md) for optional components and the
[Roon capability report](docs/roon-capability-report.md) for backend limitations.

## Develop from source

Python 3.12 is recommended. The Core dependencies require Python 3.11 or newer;
macOS integrations and local MLX transcription require the supported Mac host.
From a checkout:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
AVCTL_TEST_PYTHON=.venv/bin/python ./scripts/test_installer_acceptance.sh --focused
```

Use `--full` for the complete Python suite. Automated tests use synthetic
credentials and device fakes. These commands run tests; they do not install or
restart a Core service.

To run Core from source, provide a machine-specific configuration overlay and
choose a port that is available on the development host:

```bash
AVCTL_CONFIG_FILE=/absolute/path/to/your/config.yaml \
AVCTL_PORT=8100 .venv/bin/python -m api
```

Start from [configs/defaults.yaml](configs/defaults.yaml) and keep addresses,
pairing data, and credentials outside Git. A source Core controls the devices
selected in its configuration; use a separate test installation when a packaged
Core already owns those devices. Source runs use the authentication described
in [remote access](docs/remote-access.md); the installer's automatic local
bootstrap belongs to packaged installations.

## Code map

| Path | Responsibility |
|---|---|
| [api/](api/) | FastAPI routes, command validation, shared state, setup, Ask, and queue coordination |
| [api/ui/](api/ui/) | Browser interface used by desktop and native app clients |
| [devices/](devices/) | Device drivers, registry, and Apple Music/Roon provider contracts |
| [configs/defaults.yaml](configs/defaults.yaml) | Generic defaults; owner configuration overlays them |
| [app/](app/) | SwiftUI app, widgets, Live Activities, and MusicKit bridge |
| [mac/InputHelper/](mac/InputHelper/) | Optional macOS input helper |
| [installer/](installer/) | Frozen runtime, per-user service setup, and macOS package build |
| [tests/](tests/) | Regression tests and synthetic device/provider fixtures |
| [scripts/](scripts/) | Build support and diagnostic/acceptance tools |

Use the [Core installer build guide](docs/core-installer.md) to build a package
and the [native app guide](docs/phone-app.md) to generate and build the Xcode
project. Configuration and enrollment contracts are described in the
[setup wizard](docs/ui-setup-wizard.md) and
[managed Apple services](docs/managed-apple-services.md) docs.

## Contribute and license

Submit changes through pull requests to this public repository. Read the
[contribution workflow](docs/public-contributions.md) before starting a change.

avctl is licensed under **GNU GPL version 3 only** (`GPL-3.0-only`); see
[LICENSE](LICENSE). External services and dependencies retain their own terms
and licenses.
