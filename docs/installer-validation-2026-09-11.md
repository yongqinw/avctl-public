# Installer validation — 2026-09-11

Historical report for an installer development build. The results below
record the original validation; they do not certify the current release.
For the 6.2 installation steps and signing caveat, see the
[setup guide](friend-setup.md).

## Result

The self-contained macOS Core installer, setup wizard, managed Apple service
enrollment, optional Voice runtime, optional Mac remote input, and iOS setup
flow passed isolated end-to-end validation against a development package.

## Tested artifacts

- frozen package: `Avctl-Server-5.0.180-freeze.pkg`
- iPhone and iPad simulator builds generated from the app source at validation time

All credentials used by automated validation were synthetic. No production
Fireworks key was inspected or printed.

## Installer and setup matrix

The package was expanded and run with an isolated home directory, configuration
directory, ports, service label, and model cache. Validation covered:

- first-run authentication and setup state;
- owner-selected loopback listening port, including validation, restart, and
  Tailscale Serve targeting;
- Apple Music plus Ask activation;
- managed Apple broker enrollment with a one-use invitation;
- panel selection and ordering;
- zero-volume preservation through setup and restart;
- Voice dependency availability, model preparation, and real local inference;
- optional Mac remote-input enable and disable without touching the live helper;
- configuration persistence after Core restart; and
- package operation without relying on the source checkout or its virtualenv.

The final frozen Voice status was:

```text
supported=true runtime=true prepared=true ready=true
model=mlx-community/whisper-large-v3-turbo
```

## App validation

The tested iOS app compiled successfully and its setup UI test passed on both:

- iPhone simulator: 1 passed;
- iPad simulator: 1 passed, including the persistent left setup rail.

The native web container now shows a Connecting state instead of a blank screen,
retries failed navigation with bounded exponential backoff, retries immediately
after foregrounding, recovers from WebContent process termination, and uses a
navigation watchdog for black-holed VPN routes.

## Automated test results

- installer/backend focused matrix: 183 passed;
- full Python suite with local socket access: 881 passed;
- post-freeze-entrypoint focused regression set: 49 passed.

The remaining Python warning is Starlette's TestClient/httpx deprecation warning;
it does not affect installer behavior.

## Defects found and fixed during validation

1. PyInstaller attempted to import MLX/Metal during a headless package build.
   MLX data is now collected statically from the installed wheel.
2. The frozen runtime omitted MLX helper modules, the root `mlx.metallib`, and
   SciPy external modules. Package collection now includes them explicitly.
3. Frozen multiprocessing children were incorrectly routed into avctl's CLI
   parser. The entrypoint now invokes `multiprocessing.freeze_support()` before
   application dispatch.
4. Isolated optional-Mini setup could target the real user's LaunchAgent. The
   installer service now carries an explicit install home and setup propagates it
   to helper operations.
5. Graceful server shutdown could hang too long. Uvicorn now has a bounded
   graceful-shutdown timeout.
6. Zero is a valid configured volume but was previously vulnerable to truthy
   fallback logic. Setup and restart tests now protect it.
7. A disconnected iPhone could remain as a blank web view. The app now exposes
   connection state and automatically recovers when the Core route returns.
8. Swift 6 emitted NetService delegate isolation warnings. The delegate
   conformances now explicitly support the pre-concurrency API.
9. The packaged Core port was fixed at 8000. Setup now accepts an available
   unprivileged port, persists it in owner configuration, restarts onto it,
   preserves it across package upgrades, and points Tailscale Serve at it.

## Broker security and scope

A separate broker integration test verified one-use enrollment, signed requests, replay
protection, encrypted device storage, APNs relay, MusicKit token minting, and
`0600` permissions for local sensitive files. A device receives broker-issued
capability credentials; it does not receive the private signing key. The broker
server and that external integration environment are not included in this
repository; local tests cover the Core client with synthetic responses.

The current broker covers Apple Music/MusicKit and APNs. Ask provider credentials
(including Fireworks) remain a separately configured local provider credential;
the broker does not proxy Fireworks API requests.

## Distribution caveats

The locally built unsigned package is validated for development installation.
Developer ID signing and notarization are the intended distribution build path.
The published 6.2 package is unsigned and not notarized, as its setup guide states.
The iOS app must also be signed with provisioning that includes each device UDID
unless another permitted distribution method is selected.

No production device discovery result was accepted automatically, no audible
playback was initiated, and no production deployment configuration was replaced
during this validation.
