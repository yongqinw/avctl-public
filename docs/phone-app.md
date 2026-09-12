# Native iPhone and iPad app

The native app embeds Core's web panel in a `WKWebView` and adds widgets,
Live Activities, push registration, and device discovery. The current project
targets iOS/iPadOS 26 or newer. For installing the publisher's existing build,
follow the [illustrated setup guide](friend-setup.md).

## Shared Core API

Every client uses the same command and state contracts:

- `GET /api/state` reads the current snapshot.
- `GET /api/events` streams state changes while a client is connected.
- `POST /api/cmd` executes a command from the validated command table.
- `POST /api/phone/register` registers notification tokens for that Core.

The web panel and native intents do not implement separate device-control
paths. A Core UI update appears in both the browser and app web view; changing
Swift code requires rebuilding and reinstalling the native app.

The app stores its Core address in shared configuration used by the app,
widgets, and intents. Tailscale Serve provides authenticated HTTPS access to
the listener's own Core. APNs can deliver activity updates when the app is
suspended, but a button press still needs network access to Core. See
[remote access](remote-access.md) and
[managed Apple services](managed-apple-services.md).

## Source layout

| Path | Purpose |
|---|---|
| `app/project.yml` | XcodeGen project definition and deployment targets |
| `app/avctl/` | SwiftUI shell, setup, discovery, web view, and notification lifecycle |
| `app/Shared/` | Core client, server configuration, state models, artwork cache, and intents |
| `app/AvctlIsland/` | Live Activity and home-screen widget views |
| `app/AvctlMusicBridge/` | macOS MusicKit helper |
| `api/phonelink.py` | Core-side notification and Live Activity coordination |

`NowPlayingActivity.ContentState` and the Python-generated ActivityKit payload
must agree on field names and types. Keep changes to that contract synchronized
across Core and app. The shared artwork cache supplies images that widgets and
Live Activities can render without a network fetch during rendering.

Live Activity delivery also depends on lifecycle rules: push timestamps must
increase, bursty changes are coalesced, and stale activities need replacement.
Transport intents update the local presentation optimistically; later Core
state confirms the result. Maintain these behaviors when changing the native
controls or push sender.

## Generate and compile

Install Xcode with an iOS 26 SDK and XcodeGen. From the repository root:

```bash
cd app
xcodegen generate
xcodebuild -scheme avctl -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO build
```

This checks compilation without producing a device-installable signed app.
The generated `.xcodeproj`, Info.plists, and entitlements are build output;
edit `project.yml` instead.

## Sign your own build

Set your Apple development team, app bundle identifier, and App Group when
building. The project exposes `DEVELOPMENT_TEAM`, `AVCTL_APP_BUNDLE_ID`, and
`AVCTL_APP_GROUP`; use identifiers provisioned by your own account. The host
app and widget extension must share the App Group and compatible capabilities.

Register test devices and enable Developer Mode when required by your signing
method. Provision Push Notifications and MusicKit capabilities for the targets
that use them. Signing keys and export configuration belong outside source
control. `app/ExportOptions.example.plist` illustrates export settings; adjust
the team and distribution method to match your provisioning.

A signed app's APNs environment and bundle topic must match its notification
provider. An independently signed build cannot assume access to the publisher's
broker grants or Apple signing identity.

## Publish an installation page

Core can serve a signed IPA and its installation manifest from `AVCTL_APP_DIST`
(default `~/avctl-app-dist`). That directory is external release state, not a
source directory. The supported filenames are:

```text
manifest.plist
avctl.ipa
version         # optional display label
```

`/app` renders an installation link; `/app/manifest.plist` and `/app/avctl.ipa`
serve those artifacts. These routes require the same authentication as the
other Core endpoints. Configure the manifest with the final HTTPS asset URL,
and ensure the intended device can fetch both files through the chosen access
method. Publishing files does not remove Apple's device registration, signing,
or provisioning requirements.

The Mac Core installer does not contain an iPhone/iPad IPA. Users of an existing
publisher build must obtain that publisher's installation link; an example
address in documentation is not a download service.
