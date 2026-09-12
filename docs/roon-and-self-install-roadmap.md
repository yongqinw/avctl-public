# Roon support and self-installing avctl

Historical implementation plan, written 2026-08-23. Milestone numbers and
acceptance gates below describe the original plan, not the current release
status. For the supported installation flow, see the
[setup guide](friend-setup.md) and [project README](../README.md).

This document is deliberately implementation-oriented. It records the target
architecture, the order of work, the unknowns that must be measured before
committing to an API, the pull-request boundaries, and the acceptance gates.
It is intended to be picked up later and executed one PR at a time.

The two milestones are:

1. **6.0 — Roon support:** Roon can replace Apple Music as the playback
   backend and can optionally replace native rack drivers for Roon-compatible
   devices, without forking the UI or Ask.
2. **7.0 — self-installing avctl:** a friend can install a signed macOS Core,
   discover and configure a rack, pair an iPhone/iPad, receive signed updates,
   and install/update the iOS app through registered-device OTA distribution.

## Product decisions already made

- **No App Store.** There is no App Store submission, review, listing, or
  App Store-specific product work in this plan.
- **Friend-only iOS/iPadOS distribution.** Devices are registered in the
  developer account and included in an Ad Hoc/release-testing provisioning
  profile. Installation and updates use the existing OTA style where current
  iOS permits it, with a cable-based Apple Configurator fallback.
- **Self-contained release tooling.** Release construction and publishing
  belong in this repository. A receiving installation must not depend on an
  external deployment project.
- **Roon is an optional backend, not a fork of avctl.** Apple Music remains a
  supported backend. The panel, transport, queue, Ask tools, scenes, and state
  stream consume common contracts.
- **Qobuz stays behind Roon.** avctl does not collect or persist a friend's
  Qobuz credentials. When Roon is selected, Roon's browse tree, search,
  favorites, playlists, and playback actions are the authority for both the
  local Roon library and linked services such as Qobuz. Result provenance is
  preserved for display, but queueing a Qobuz result must not add it to the
  Roon library unless the user explicitly asks for that mutation.
- **Roon may own both media and hardware.** A friend with Roon Ready hardware
  can let Roon control playback, volume, power, grouping, and source selection.
  A mixed-hardware rack can use Roon for playback while retaining
  native IR/serial drivers for the DAC and amplifier.
- **Signed releases, not `git pull`, are the normal update channel.** Tracking
  `main` may remain an explicit developer option, but friends receive versioned,
  signed artifacts with rollback metadata.
- **One Core remains the authority for a home.** Every phone, iPad, browser,
  widget, Live Activity, and Ask session observes and commands the same Core.

## What the current code gives us

This is not a greenfield rewrite. Useful seams already exist:

- `devices/music/__init__.py` defines `MusicSource`, including browsing,
  transport, queues, playlists, artwork, and optional machine volume.
- `api/musiclink.py` owns the public music routes and the centralized queue
  presentation. That is the correct public boundary even when Roon owns the
  physical queue.
- `devices/registry.py` resolves configured drivers at boot and caches one
  instance per category.
- `api/state.py` and `/api/events` already make the Core the state authority
  for multiple clients.
- `api/agentlink.py` exposes validated tools to Ask and already separates the
  model provider from tool execution.
- `api/main.py` already serves an authenticated `/app` page, manifest, and IPA.
- The native app already supports iPhone and iPad, embeds the web UI, stores
  server configuration in an App Group, and supplies widgets/Live Activities.
- The test suite has synthetic device, queue, Ask, voice, SSE, APNs, UI-config,
  and OTA coverage that can be extended rather than replaced.

The current constraints that 6.0/7.0 must remove are equally concrete:

- Music IDs and queue validation still assume Apple Music persistent IDs or
  Apple catalog IDs.
- `MusicSource` combines several independent capabilities, so a source that
  can browse but not edit playlists has to fail through `NotImplementedError`.
- The registry has four fixed singleton categories: DAC, amp, TV, and music.
  It cannot yet express one Roon output providing several rack capabilities.
- Earlier app configuration coupled builds to one server and signing identity.
- Earlier Core configuration and service files contained machine-specific
  settings inside the checkout.
- OTA publication depended on external deployment tooling.
- APNs provider credentials currently live beside one Core. That cannot be
  copied onto friends' computers.

## Target architecture

```text
 iPhone / iPad / browser / widgets / Ask
                  |
           versioned Core API
                  |
     +------------+-------------+
     |                          |
 Media service              Rack graph
     |                          |
 +---+---------+          +-----+-------------------+
 |             |          |            |            |
Apple Music   Roon       Roon output  native amp   native DAC/TV
 adapter      adapter     controls     driver       driver
                 |
     `roonapi` Python worker
                 |
    (official Node bridge only if a
       required capability is missing)
                 |
              Roon Server
```

The first production candidate is the third-party Python `roonapi` package
(pyRoon). It connects directly to Roon Server's extension WebSocket API and is
already used by Home Assistant for discovery, zones, subscriptions, browsing,
artwork, playback, volume, grouping, shuffle, repeat, and standby. Keeping the
Roon adapter in Python removes a runtime, process boundary, and packaging
failure mode from avctl.

`roonapi` is not Roon's official SDK and its latest PyPI release is 0.1.6 from
December 2023. Current Home Assistant still pins that release, which is useful
evidence but not a substitute for the Phase 0 trial. Roon's official extension
SDK is JavaScript and remains the fallback when a required queue operation or
provider capability is absent or unreliable in pyRoon. Both implementations
must sit behind the same Python adapter contract; UI, policy, Ask, and queue
ownership never depend on which implementation was selected.

Three configurations must work from the same code:

| Configuration | Playback | Volume/power/input |
|---|---|---|
| Mixed-hardware rack | Roon or Apple Music | Native D900 IR + McIntosh serial + LG |
| Friend, all Roon | Roon | Roon output/source controls |
| Friend, hybrid | Roon | Any mixture of Roon and native drivers |

## Shared engineering rules

1. **Capabilities, not device names.** UI and Ask request `transport.next`,
   `volume.set`, or `power.standby`; the rack graph resolves the provider.
2. **Stable IDs, not display names.** Persist Roon `core_id`, `zone_id`,
   `output_id`, and source-control key. Names are labels and may change.
3. **The backend owns physical truth.** When Roon is active, its zone and queue
   subscriptions are authoritative. avctl exposes them but does not shadow
   them in a competing queue.
4. **The Core owns client truth.** All clients consume the same merged Core
   snapshot/SSE revision. Client-local polling must not create divergent state.
5. **Unsupported operations are capabilities, not surprises.** The API says
   whether queue clear/reorder, absolute volume, playlist editing, grouping,
   or standby exists before a button or tool is offered.
6. **Safety is enforced below Ask and UI.** Volume caps and destructive-action
   policy are checked in the command layer after intent resolution.
7. **No silent library mutation.** Browsing, streaming, and queueing never add
   an item to a user's library. Only an explicit library/playlist command may
   mutate it.
8. **A release can fail honestly.** Missing Roon authorization, a dead Roon
   adapter/fallback bridge, an expired iOS profile, or an unreachable device
   must produce a precise status and recovery action.

---

# Milestone 6.0 — Roon support

## 6.0 success definition

6.0 is complete when:

- A clean config can select Apple Music or Roon without changing UI code.
- Roon pairing, re-pairing, core selection, zone selection, and output
  selection survive restarts and renames.
- The Music panel browses/searches the selected backend, renders artwork,
  shows now playing, and controls transport.
- The transport queue reflects the real Roon zone queue and stays in sync on
  iPhone and iPad.
- Queue operations exposed by avctl are proven against the public Roon API;
  unsupported operations are not simulated or falsely reported as successful.
- A Roon-compatible friend's device can be controlled through Roon where the
  SDK exposes the capability.
- A mixed-hardware rack can use Roon for music while retaining its existing native
  DAC, amp, and TV drivers.
- Ask can browse, search, play, queue, inspect the queue, select a zone, and
  control rack capabilities without knowing which backend implements them.
- All existing Apple Music workflows and safety limits continue to pass.

## Phase 0 — trial spike before production design

Do this during the 14-day Roon trial. The output is a checked-in capability
report and captured **synthetic/sanitized** fixtures, not production code.

### Test setup

1. Install Roon Server on a development Mac.
2. Sign Roon into Qobuz and verify that the same browse/search APIs expose
   both the Roon library and Qobuz catalog. Record the result source and the
   available actions; do not infer them from English labels.
3. Use the Mac's built-in output and the current USB DAC as Roon outputs. A
   Roon Ready endpoint is not required to test the extension API or USB audio.
4. If possible, spend one session on the friend's actual Roon-compatible
   endpoint. This is required before declaring hardware-control support done.
5. Create the first disposable probe with `roonapi==0.1.6`. Exercise it under
   avctl's actual Python version, not only the versions listed on PyPI.
6. Pin the wheel hash and record the upstream commit/license. Run its socket and
   callbacks behind a dedicated worker so its synchronous waits never block
   FastAPI or the state event loop.
7. For every missing or unreliable must-have operation, create the smallest
   equivalent probe using the official `node-roon-api`,
   `node-roon-api-transport`, `node-roon-api-browse`, and
   `node-roon-api-image` packages. Do not build the complete Node bridge unless
   the comparison proves it is needed.
8. Record exact Node Git commits/package-lock if the fallback is exercised.
   Roon's official API is marked beta, so floating dependencies are
   unacceptable.

The first local preflight on 2026-08-23 already established two useful facts:
`roonapi==0.1.6` installs and imports under avctl's Python 3.13.2 environment,
and SOOD discovery finds the Roon Server installed on the Mac mini. Pairing,
authorization, browse behavior, Qobuz actions, and device controls remain
unproven until the probe is authorized inside Roon.

### Synthetic Roon topology for driver and UI work

Check in a deterministic fake Core before binding any panel to live hardware.
It must use the same normalized adapter contract as pyRoon, not imitate
pyRoon's mutable dictionaries directly:

- Core `mock-core`, with a selected `living-room` zone.
- A variable-volume Roon Ready output with confirmed numeric volume, mute,
  standby, and convenience-switch capabilities.
- A fixed-volume USB DAC output with no volume or input controls.
- An incremental-only output to prove that avctl refuses unsafe absolute
  volume and does not render a misleading percentage slider.
- Library, Qobuz, album, track, playlist, and image-key browse fixtures.
- A queue long enough to test pagination, play-from-here, shuffle, and
  simultaneous iPhone/iPad revisions.
- Disconnect, authorization-needed, output removal, zone rename, grouping,
  and out-of-order callback scenarios.

The full-capability mock is a UI development instrument, not a promise that
every Roon endpoint is a controllable DAC or amplifier. Real controls are
enabled only from the capabilities Roon reports for the selected output.

### Questions the spike must answer empirically

- Pairing lifecycle: first authorization, stored token, Roon Server restart,
  adapter restart, authorization revocation, and switching between two cores.
- Zone lifecycle: subscribe, rename, group/ungroup, transfer, output removal,
  sleep/wake, and a zone disappearing during a command.
- Transport: play, pause, next, previous, seek, shuffle, loop, stop, standby,
  and source selection.
- Volume types: numeric, dB, incremental, fixed, mute, step size, min/max, and
  grouped-zone per-output behavior.
- Browse: hierarchy, `item_key` lifetime, action lists, pagination, search,
  library, playlists, genres, albums, performers, and image keys.
- Linked services: search and browse Qobuz, distinguish Qobuz from library
  results, play and queue catalog tracks without importing them, and perform
  an explicit favorite/library mutation only when requested.
- Queue: subscription data, pagination, `play_from_here`, append, play next,
  replace, clear, remove, reorder, shuffle, and building a 30-track mixed list.
- Whether browse actions can perform queue changes that transport itself does
  not expose, and how success is confirmed by a subsequent queue revision.
- Whether a Roon-controlled device exposes volume, standby, source controls,
  or only playback.
- Python 3.13 compatibility, callback thread behavior, simultaneous requests,
  shutdown, bounded command timeout, reconnect latency, and whether state
  dictionaries can be copied safely while callbacks update them.
- Whether pyRoon's `register_volume_control` is sufficient for the current
  native amplifier, and which source/standby provider functions would still
  require the official Node SDK.

### Required spike report

Add `docs/roon-capability-report.md` with one row per operation:

| Operation | Implementation/API path | Tested device | Result | Confirmation event | Product decision |
|---|---|---|---|---|---|

The queue is the most important unknown. pyRoon exposes queue subscription and
browse actions such as Play Now, Add Next, and Queue, but it has no clean public
method for every granular queue mutation. Roon's official transport wrapper
adds `play_from_here`, yet it also lacks a simple universal
append/remove/reorder surface. Do not call pyRoon's private `_request` from
production merely to make the matrix look complete, and do not design a fake
client queue to paper over a gap. If an operation cannot be performed through
a public, tested action, mark it unavailable in Roon mode.

Phase 0 ends with an explicit implementation decision:

- **Choose pyRoon** if it satisfies required playback, state, browse, queue,
  volume, and reconnect behavior. The Server package then contains no Node
  runtime.
- **Choose the official Node fallback** only if a must-have capability is
  unavailable or materially unreliable in pyRoon. Record the exact failing
  test that justifies the extra process and runtime.

**PR 6.0.0:** spike tool, package lock, sanitized fixtures, and report only.

## Phase 1 — source-neutral media contracts

Split the current broad `MusicSource` into composable protocols while keeping
an adapter that satisfies the old interface during migration.

### Common identifiers

```python
@dataclass(frozen=True)
class MediaRef:
    provider: str       # "apple_music" or "roon"
    kind: str           # track, album, artist, playlist, browse_item
    id: str             # opaque provider ID
    scope: str | None   # library, catalog, core/zone, or provider-defined
```

Rules:

- The Core never parses a Roon `item_key` or assumes an Apple persistent-ID
  shape outside its provider adapter.
- API responses carry `provider`, `kind`, `id`, and `capabilities`.
- Artwork keys remain opaque Core keys, so the app keeps calling one artwork
  endpoint regardless of source.
- Old PID/catalog routes remain as a v1 compatibility projection until the UI,
  Ask, and tests have migrated.

### Capability protocols

- `MediaBrowser`: roots, browse children, pagination, actions.
- `MediaSearch`: scoped search with typed results.
- `LibraryManager`: add/remove only when explicitly supported.
- `PlaylistManager`: list/read/create/append/remove when supported.
- `QueueController`: inspect, append, next, replace, clear, remove, reorder,
  play-from-here; each operation separately advertised.
- `TransportController`: now playing, play/pause/stop, next/previous, seek,
  shuffle, repeat.
- `ArtworkProvider`: resolve image key to bytes and cache metadata.
- `VolumeController`, `PowerController`, `InputController`: rack capabilities,
  separate from media browsing.

### Versioned capability response

Add `/api/meta` and include at least:

```json
{
  "core_version": "6.0",
  "api_version": 2,
  "config_schema": 2,
  "providers": ["apple_music", "roon"],
  "capabilities": {},
  "min_app_version": "6.0"
}
```

The app dims or omits unsupported controls from this response. It never learns
support by sending a command and waiting for `NotImplementedError`.

**PR 6.0.1:** contracts, Apple adapter, API v2 shapes, compatibility routes,
contract tests. No visible behavior change.

## Phase 2 — Python Roon adapter with a Node escape hatch

Implement `devices/roon/` against `roonapi` first.

### Python worker boundary

- One dedicated worker owns the `RoonApi` instance and all synchronous calls.
  FastAPI handlers submit bounded commands; they never run pyRoon waits on the
  event loop.
- pyRoon callbacks copy the relevant zone/output/queue data under an adapter
  lock, then enter the Core through `loop.call_soon_threadsafe`. Core code never
  iterates pyRoon's mutable public dictionaries directly.
- Every command carries an ID, deadline, operation, arguments, selected core,
  and expected source revision where applicable.
- Events receive monotonically increasing adapter revisions for cores, zones,
  outputs, queue, now playing, and authorization.
- Backpressure is bounded. A slow Core receives a fresh snapshot after a gap,
  not an unbounded replay of stale volume events.
- Authorization tokens are persisted through avctl's secret/state layer, not
  files owned implicitly by pyRoon.
- Startup and shutdown are bounded even when Roon Server is absent. avctl never
  relies on pyRoon's blocking constructor from the main thread.

### Adapter surface

```text
adapter.status
core.list / core.select / core.forget
zone.list / zone.subscribe / zone.select
output.list
transport.control / transport.seek / transport.set_shuffle
volume.change / mute.set / standby.set / source.select
queue.get / queue.subscribe / queue.play_from_here
browse.root / browse.children / browse.action / search
image.get
```

Only queue operations proven in Phase 0 are added. Browse action titles are
treated as Roon-provided capabilities, not assumed English strings.

### Optional official Node fallback

If Phase 0 selects Node, create a narrow `bridges/roon/` process using the
official SDK. It implements the identical adapter surface over a versioned
newline-delimited JSON-RPC Unix socket under Application Support. Core
launches/supervises it, checks a version/capability handshake, and continues to
own all public state and policy. The Node process never gains UI, Ask logic, a
second queue, or unrelated secrets.

### Reliability behavior

- Roon absent: adapter reports `disconnected`, retries with capped jitter, and
  never blocks Core startup.
- Authorization needed: setup UI shows the Roon authorization instruction and
  waits; normal panel reports `needs_authorization`.
- Python worker failure: Core reports stale Roon state, rebuilds the adapter
  with a bounded policy, and keeps non-Roon rack controls alive.
- Optional Node process crash: the same behavior applies through its
  supervisor; this path exists only when Phase 0 selected it.
- SDK callback throws: isolate the callback, log operation and safe metadata,
  and request a full resubscription.
- Core changes: invalidate zone/item keys from the old core before accepting
  commands for the new one.

**PR 6.0.2:** pyRoon worker/adapter, fixtures, reconnect/thread-safety tests,
and—only when justified by 6.0.0—the compatible Node bridge/client and protocol
tests. Keep Roon hidden behind an experimental config flag.

## Phase 3 — rack graph instead of fixed categories

The current registry's singleton category model is preserved as a compatibility
layer, but configuration moves to named instances and explicit bindings.

```yaml
racks:
  main:
    media: roon.main_zone
    transport: roon.main_zone
    volume: amp.mcintosh
    power:
      amp: amp.mcintosh
      dac: dac.d900
      display: tv.living_room
    input:
      amp: amp.mcintosh
      dac: dac.d900

devices:
  - id: roon.main_zone
    driver: roon.zone
    config:
      core_id: "..."
      zone_id: "..."
  - id: amp.mcintosh
    driver: mcintosh.mac7200
    config: { ... }
```

### Validation at boot

- IDs are unique and references resolve.
- Required rack roles have a provider.
- Bound devices advertise the required capability.
- No control dependency cycle exists.
- A Roon zone/core exists or is marked `pending_discovery` during setup.
- Physical output ownership is unambiguous. Two volume providers cannot both
  respond to one logical volume command unless an explicit aggregate policy
  defines the order.
- Existing `dac`, `amp`, `tv`, and `music` blocks migrate automatically into
  a generated `main` rack with identical behavior.

### Scene resolution

Scenes target logical roles (`main.power.on`, `main.media.activate`) instead of
driver commands. Their compiled steps are visible in setup before saving.
Device-specific timing and protocol quirks remain in hand-written drivers.

**PR 6.0.3:** instance registry, rack graph, migration loader, validation,
scene compiler, and regression tests.

## Phase 4 — volume and hardware safety model

Roon outputs do not share one volume representation. Normalize state without
pretending every endpoint has absolute volume:

```python
VolumeState(
    mode="number" | "db" | "incremental" | "fixed",
    value=None,
    minimum=None,
    maximum=None,
    step=None,
    muted=None,
    safety_max=None,
    readback="confirmed" | "estimated" | "none",
)
```

Rules:

- Existing amp and Mac safety caps remain hard command-layer limits.
- Numeric/dB Roon outputs use both avctl's configured cap and Roon's own
  configured volume limit where available; the stricter limit wins.
- An incremental-only device cannot be safely capped by avctl without trusted
  absolute readback. Setup must warn and require either a device/Roon-side
  limit or explicit acceptance of no absolute cap.
- Fixed-volume outputs do not render a slider or accept volume tools.
- Grouped zones expose each output's volume and an aggregate command only when
  behavior has been tested. Never flatten unlike dB/number scales into a fake
  percentage.
- Open-loop IR state remains `estimated`; it is not upgraded to `confirmed`
  because Roon can play through the output.

### Providing legacy hardware controls to Roon

The extension API can expose controls for non-Roon Ready hardware. pyRoon
implements registration/update callbacks for volume and mute; the official
Node SDK has the broader source-control surface. Add only the capabilities
proven for the implementation selected in Phase 0, after consuming Roon
controls is stable:

```text
Roon request -> Python callback (or optional Node RPC) -> rack command
             -> safety cap -> physical driver -> readback/estimate
             -> Roon state update
```

Callbacks have timeouts and idempotency keys. A serial amp reports confirmed
readback; an IR-only DAC reports the honest estimated/indeterminate state.

**PR 6.0.4:** normalized volume, safety enforcement, grouped-output handling,
and optionally Roon-provided controls for proven native devices.

## Phase 5 — event-driven merged state

Roon subscriptions feed the existing Core state engine. They do not create a
second SSE endpoint.

- Keep one latest state per provider with source revision and timestamp.
- Merge it into a rack snapshot with one Core revision.
- Push a changed snapshot to every connected client through `/api/events`.
- Preserve v1 fields until current clients age out.
- Mark source fields stale independently: a dead Roon adapter/fallback bridge
  must not paint the native amp offline, and a dead serial port must not erase
  now playing.
- After reconnect, request full Roon snapshots before applying deltas.
- Reject a delayed command response whose source revision predates the current
  state.

This phase is also the multi-client fix: iPhone and iPad must render the same
Core revision for now playing, queue, rack state, and volumes.

**PR 6.0.5:** Roon state reducer, merged revisions, stale-state model, SSE
fixtures, reconnect/out-of-order tests.

## Phase 6 — Music panel, transport, and iPad behavior

The visual structure stays familiar; only data and capabilities change.

- Music panel roots become provider-defined: Library, Search, Playlists, Roon
  browse roots, genres, artists, or services as available.
- Search result rows carry their source and action capabilities.
- Queue remains owned by the transport, not the Music panel.
- On iPad the queue remains a stable left panel in portrait and landscape.
- On iPhone the queue remains a transport-owned sheet/detail surface.
- Long queues keep transport actions pinned outside the scrolling track list.
- Queue is openable for a single playing item and for an empty/stopped state.
- Existing themes and music layouts remain; source changes do not fork CSS.
- Back buttons use the established icon and alignment.
- Artwork continues through the Core cache, including queued Roon items.

The UI must never render append/clear/reorder controls unless the selected
zone advertises them.

**PR 6.0.6:** provider-aware Music UI, queue UI, iPhone/iPad layout tests,
artwork caching, and Apple Music visual regression.

## Phase 7 — Ask integration

Ask receives backend-neutral tools:

```text
search_media(query, sources, kinds, limit)
browse_media(parent, limit, offset)
play_media(refs, rack, mode, shuffle)
queue_media(refs, rack, position, shuffle)
inspect_queue(rack)
select_zone(rack, zone)
set_volume(rack, target, value/change)
set_power(rack, target, state)
set_input(rack, target, input)
```

Behavioral rules:

- The model decides vague intent; the command layer validates capabilities,
  safety, and explicit-mutation requirements.
- “Play,” including angry or colloquial variants, means execute playback when
  a reasonable target has already been established in the conversation.
- “Find” or “show me” returns choices; it does not play unless the user's
  wording or conversational follow-up asks to play/queue.
- If ambiguity changes the physical result materially—wrong room, two artists
  with the same name, or replace versus append with no context—Ask asks one
  concise clarification.
- “My liked/favorite songs” prefers personal library/play history signals, not
  an artist's generic popular list.
- “Do not add to my library” is maintained through the full tool chain.
- Multi-action prompts produce an ordered plan and execution receipts. A later
  step is skipped if its prerequisite failed.
- Tool results include actual queue/state revisions so Ask cannot claim an
  action succeeded before the backend confirms it.

**PR 6.0.7:** generic tools, Roon capability context, compound-action receipts,
clarification and vague-language prompt tests.

## Phase 8 — 6.0 release gate

### Automated

- Contract tests run against Apple and fake-Roon adapters.
- Recorded fixtures cover authorization, core/zone changes, grouped outputs,
  every volume type, queue revisions, browse pagination, and artwork.
- Sidecar crash/restart, malformed message, timeout, SDK event flood, and
  stale/out-of-order response tests.
- Config migration and capability validation tests.
- Volume caps at every entry point: panel, scene, Ask, App Intent, and Roon
  callback.
- No-library-mutation tests for play, queue, search, and curation.
- Existing Apple Music, voice, Ask, SSE, iPad configuration, and OTA tests pass.

### Hardware

- Mixed-hardware rack: Roon playback into USB DAC; native DAC/amp/TV scenes; next,
  previous, stop, shuffle, queue, and volume safety.
- Friend endpoint: discovery, rename, reboot, volume, mute, standby/source if
  advertised, grouping if used.
- Two clients open: iPhone and iPad agree within one SSE update after physical,
  Roon, UI, and Ask changes.
- 30-track mixed curation either works through a confirmed Roon public action
  or the UI/Ask explicitly explains the unsupported operation.

Do not tag 6.0 until all applicable rows in the Phase 0 capability report have
been converted into either tested support or explicit capability absence.

---

# Milestone 7.0 — self-installing Core and friend-only OTA app

## 7.0 success definition

A friend with a clean Apple-silicon Mac can:

1. Download a signed/notarized `Avctl Server` release from GitHub.
2. Open a setup wizard, authorize Roon or discover native devices, select a
   rack topology, set safety limits, and run non-destructive tests.
3. Install a background service without editing plist files or repository
   paths.
4. Pair an already-registered iPhone/iPad using a one-time code/QR flow.
5. Install or update the signed iOS/iPadOS app from an HTTPS OTA page where
   the current OS accepts the registered-device web flow, or use the documented
   Apple Configurator fallback.
6. Receive Core and app update notices, choose stable/beta, update, and roll
   back without losing configuration.
7. Remove avctl cleanly while choosing whether to keep configuration/backups.

## OTA reality and constraints

Friend-only OTA is workable, but it is not unrestricted public distribution.
These constraints are product requirements:

- The Apple Developer Program permits up to **100 registered iPhones and 100
  registered iPads per membership year** (separate product-family pools).
- Disabling a device during the year does not restore its slot. The list can
  be pruned when the membership year resets.
- Every receiving device's UDID must be registered and included in the build's
  provisioning profile.
- Adding a friend's device means regenerating the profile and publishing a new
  signed IPA before that device can install it.
- Provisioning profiles and signing certificates expire. The release metadata
  and app must warn before expiry; a newly signed build must be installed in
  time.
- The IPA contains no server secret, APNs provider key, model key, or friend
  credential. Signing assets stay in the release environment.
- iOS will not silently self-update an arbitrary OTA app. avctl can detect an
  update and open the installer; the user confirms installation.
- Apple currently documents registered-device export/install through Xcode or
  Apple Configurator. Apple's documented `itms-services` web procedure is
  written for enterprise in-house distribution. The existing avctl Ad Hoc OTA
  behavior therefore gets a per-major-iOS validation gate and Configurator is
  the supported fallback—not a hidden emergency trick.
- OTA manifest and IPA URLs must be HTTPS with a certificate trusted by the
  device. A raw LAN IP or self-signed local certificate is not sufficient.

No App Store or TestFlight work is required by this plan.

## Phase 1 — externalize config, state, and secrets

Move runtime data out of the Git checkout:

```text
~/Library/Application Support/avctl/
  config.yaml
  state/
  cache/
  providers/
  backups/
~/Library/Logs/avctl/
```

- Ship sanitized defaults inside the application bundle.
- Put tokens and service credentials in Keychain or a root-readable service
  secret store as appropriate; config stores references, never secret values.
- Support `AVCTL_CONFIG_DIR` for development/tests only.
- Add a versioned config schema and transactional migrations.
- Before migration, create a timestamped backup; on failure, retain old data
  and boot into recovery/setup mode.
- Import the existing repository config once, showing a redacted summary and
  asking for confirmation before writing it to Application Support.
- Replace hard-coded launchd user, checkout, venv, HOME, and log paths.
- Keep hardware protocol tables bundled and versioned separately from user
  configuration.

**PR 7.0.1:** data directories, Keychain wrapper, schema/migrations, legacy
importer, backup/restore tests.

## Phase 2 — driver catalog and discovery contracts

Each driver supplies a manifest alongside its implementation:

```yaml
id: mcintosh.mac7200
name: McIntosh MAC7200
capabilities: [power, volume.absolute, mute, input]
config_schema: { ... }
discovery: [serial]
permissions: [serial]
readback: {power: confirmed, volume: confirmed, input: confirmed}
safe_tests: [identify, read_status]
```

Discovery providers:

- Roon core/zone/output discovery through the selected Roon adapter.
- Bonjour/SSDP/network discovery for supported network devices.
- Serial-port enumeration with USB vendor/product metadata.
- Guided IR setup: choose blaster, port, learn/test command, confirm observed
  state, and record that readback is estimated.
- Manual host/port/identifier entry as a first-class fallback.

Discovery returns candidates; it never changes hardware by itself. Tests are
classified as read-only, reversible, or potentially disruptive and the wizard
labels them accordingly.

**PR 7.0.2:** driver manifests, discovery API, candidate model, synthetic
discoverers, and current-driver manifests.

## Phase 3 — bootstrap Core and setup API

The Core must run before any rack exists.

- `bootstrap`: no owner and no config; setup endpoints available only from
  localhost until ownership is claimed.
- `setup`: owner exists; discovery and draft config can run.
- `ready`: validated rack is active.
- `recovery`: installed config failed migration/validation; controls remain
  off, logs/backup restore remain available.

Setup changes are written to a draft, validated, compiled into a proposed rack
graph/scenes, and activated atomically. A failed activation rolls back.

**PR 7.0.3:** bootstrap state machine, setup API, draft/commit/rollback,
authorization and recovery tests.

## Phase 4 — setup UI

The setup experience lives in the same themed panel system and remains usable
on Mac, iPad, and iPhone. It is not a sixth permanent bottom panel.

Wizard order:

1. Welcome, installation health, and owner creation.
2. Discover media backends automatically. Probe for a reachable Roon Core and
   for Apple Music on this Mac (Music.app availability, automation permission,
   MusicKit bridge health, library access, and playback capability), then show
   Roon, Apple Music, or both as detected choices. Manual configuration remains
   available when discovery is incomplete.
3. Complete the selected providers' setup: Roon authorization/core/zone
   selection and/or Apple Music automation/MusicKit authorization plus a
   read-only library and playback check.
4. Discover rack devices and assign logical roles.
5. Show the proposed signal/control graph.
6. Configure inputs, power order, scene timing, and volume caps.
7. Run read-only checks, then explicitly approved physical tests.
8. Configure local-only, LAN, and optional Tailscale access.
9. Show the Tailscale Serve URL for manual entry on phones/iPads.
10. Final verification and recovery guidance.

After setup, Settings can re-enter the wizard for one subsystem, reorder or
hide panels, add future panels such as a disc player, switch model providers,
and change themes. Settings is reached by the existing settings button/flip,
not shake detection.

**PR 7.0.4:** setup UI, graph preview, safety confirmations, responsive/iPad
tests, accessibility and theme coverage.

## Phase 5 — private app connection

- Advertise `_avctl._tcp` with Bonjour and a non-secret Core ID/version.
- Add `NSLocalNetworkUsageDescription` and `NSBonjourServices` to the app.
- App's first launch accepts the Core's Tailscale Serve URL manually. Bonjour
  can remain a convenience on the local network, not a pairing protocol.
- The app verifies `/healthz` and `/whoami` before saving the address.
- Tailscale identity is the normal authorization boundary. Removing a device
  or user from the tailnet revokes its access without an avctl pairing store.
- A private bearer token remains only for loopback bootstrap and explicit
  recovery; it is never embedded in an installer or shared URL.

The packaged Core stays on loopback behind Tailscale Serve. Tailscale identity
headers are never trusted on a raw LAN listener.

**PR 7.0.5:** manual Tailscale URL entry, identity verification, recovery
authentication tests, and optional native discovery UI.

## Phase 6 — signed macOS Server application

Package one `Avctl Server.app` containing:

- Python Core, `roonapi` when selected, and all locked dependencies.
- The Node runtime/bridge and locked official Roon SDK dependencies only when
  Phase 0 proved that fallback necessary.
- Setup/status UI and command-line diagnostics.
- A bundled LaunchAgent or helper registered with `SMAppService` on macOS 13+.
- Config migration and uninstaller tools.

Release it as a signed/notarized DMG:

1. Build reproducibly on clean Apple silicon.
2. Sign nested code from the inside out with Developer ID.
3. Enable Hardened Runtime and only required entitlements.
4. Submit for notarization and staple the ticket.
5. Generate checksums and a signed update manifest.
6. Test install with no system Python, Node, Homebrew, Xcode, or repository
   present. If pyRoon won Phase 0, verify that the app bundle contains no Node
   runtime at all.

Initial platform scope is Apple-silicon macOS 14+. The MusicKit catalog player
uses `ApplicationMusicPlayer`, which is unavailable on macOS 13.
Intel/universal builds are added only when a real target requires them.

The menu-bar/server window shows Core health, version, selected rack, service
status, open setup, export diagnostics, check update, restart, and uninstall.
It does not duplicate the control panel.

**PR 7.0.6:** app bundle, service registration, packaging/notarization scripts,
clean-machine smoke test, uninstall/keep-data behavior.

## Phase 7 — Core release and update channel

Use GitHub Releases as the immutable source of versioned artifacts and Sparkle
for the macOS update UX.

- Stable and beta appcasts are signed independently.
- Release metadata includes Core/API/config versions, minimum macOS, selected
  Roon adapter/version (and optional bridge version), checksums, release notes,
  migration notes, and rollback compatibility.
- Download and verify before stopping the running service.
- Keep the previous known-good app until the new version passes a health check.
- Roll back binaries automatically if startup/health fails; never roll back a
  migrated config without its paired backup.
- Friends update only from signed releases. Developer “follow main” mode is
  opt-in, visually marked unsupported, and isolated from stable data.

**PR 7.0.7:** GitHub release workflow, signed Sparkle feeds, stable/beta,
health-checked update and rollback tests.

## Phase 8 — registered-device OTA release pipeline

Move the iPhone/iPad release path into `.github/workflows/` plus repository
scripts. Signing credentials live in the protected release environment or a
dedicated local signing Mac, never in Git.

### Device enrollment

1. Friend retrieves the device UDID using Finder or Xcode and sends it through
   an agreed private channel.
2. Account holder registers it with a human-readable owner/device name.
3. Release operator regenerates the Ad Hoc/release-testing profile containing
   the desired iPhones and iPads.
4. The next signed IPA includes those devices.
5. Maintain a private enrollment inventory containing owner, device family,
   UDID, status, and membership-year slot. Do not commit it to this repository.

### Build and validate

- Generate the Xcode project from `app/project.yml`.
- Use a distribution configuration and a dedicated export options file; the
  current `debugging` export remains development-only.
- Build iPhone and iPad support into one IPA.
- Make build number monotonic and map it to the Git commit/tag.
- Validate bundle IDs, application groups, Live Activity/widget extensions,
  embedded profile, permitted devices, expiration, signing certificate,
  entitlements, and APNs environment from the final IPA.
- Install the exact exported IPA on at least one registered iPhone and iPad
  before publishing.
- Run native compile/UI tests and a compatibility check against the minimum
  and current Core API.

### Publish

Publish an OTA bundle:

```text
ios-release.json       signed metadata, versions, expiry, checksums
manifest.plist         software-package URL + bundle/version/title
avctl.ipa              signed registered-device build
install.html           one Install/Update button and troubleshooting
```

All URLs are HTTPS with a publicly trusted certificate. Two supported hosting
topologies are allowed:

1. **Central OTA host (preferred):** a small HTTPS site or public release
   artifact host contains no secrets. This is simplest for friends and does
   not require their Core to have public/trusted TLS.
2. **Per-Core mirror:** the Core downloads and verifies the signed OTA bundle,
   serves the existing `/app` routes, and rewrites only the manifest's absolute
   IPA URL. This is allowed when Tailscale Serve or another trusted HTTPS
   frontend is configured.

Do not point `itms-services` at a private GitHub asset that requires browser
cookies or an authorization header; the system installer cannot inherit them.

### In-app update UX

- Core and native Settings compare current build with signed
  `ios-release.json`.
- Show version, release notes, device eligibility, profile expiry, and whether
  the current Core is compatible.
- “Install update” opens the `itms-services` manifest flow.
- Preserve app data by keeping bundle IDs and App Group identifiers stable;
  tell users to install over the existing app rather than deleting it.
- If web OTA fails on the current OS, show the precise Apple Configurator
  fallback instead of retrying forever.

### Mandatory current-OS validation

For every major iOS/iPadOS release and signing-method change:

- Export a registered-device distribution IPA.
- Install from the HTTPS `itms-services` page on both iPhone and iPad.
- Verify whether Developer Mode is required and document the observed result.
- Verify first launch, local-network permission, App Group data migration,
  APNs token environment, Live Activity, widget, microphone, and update-over-
  existing-build behavior.
- Verify the Configurator fallback from a clean device.

If Apple stops accepting Ad Hoc `itms-services`, the product remains friend-
installable through Configurator; do not silently pivot to an App Store path.

**PR 7.0.8:** signing/export configs, release workflow, metadata/manifest,
Core `/app` sync, native update UI, eligibility/expiry validation, OTA and
Configurator runbook.

## Phase 9 — APNs without distributing the provider key

The APNs provider private key must not live on every friend's Core. Full
server-originated Live Activity and widget push therefore needs a minimal
hosted relay:

```text
Friend Core --outbound authenticated event--> relay --APNs--> friend's device
```

- One APNs signing key under the developer team can serve the owner's and
  friends' installations of the same signed avctl app. The relay sends each
  event to that friend's registered device token and the avctl bundle topic.
- The APNs key is **not friend-scoped**. A team-scoped key may authorize every
  topic in the developer team, and even a topic-specific key is scoped to the
  app topic rather than one person. Merely giving the key to a friend's Core
  would therefore give it broader authority than “only his notifications.”
- Friend isolation is an avctl relay invariant: every device token belongs to
  exactly one paired Core/owner account, and that Core's credential can submit
  only for those token IDs. The caller never supplies an arbitrary raw APNs
  token, topic, environment, or push type.
- Relay stores the APNs provider key; friends never receive it.
- Each Core has a revocable relay credential and may address only its paired
  devices.
- Payload schema is narrow: playback/rack display state and allowed update
  event types. The relay cannot send rack commands or reach the home.
- Encrypt transport, rate-limit by Core/device, retain minimal logs, and allow
  complete device/Core deletion.
- Validate the production/sandbox APNs environment from the actual OTA-signed
  app and route tokens accordingly.
- Reject cross-Core token references before constructing an APNs request and
  test this as a tenant-isolation boundary.
- Treat APNs `BadDeviceToken`, `DeviceTokenNotForTopic`, and unregistered-token
  responses as token lifecycle events; remove or quarantine only that device's
  token without affecting the other friend installations.

Offer a no-relay mode. In that mode the foreground app and SSE work normally,
but pushes that originate while the app is suspended are explicitly disabled.

**PR 7.0.9:** relay protocol/client, per-Core enrollment, production APNs,
no-relay degradation, synthetic APNs tests. Relay deployment is a separate
operations task, not part of a friend's Core installer.

## Phase 10 — migration, diagnostics, and 7.0 release gate

### Migrate an existing installation

1. Export and redact current configuration.
2. Install `Avctl Server.app` alongside the current service without starting
   both drivers concurrently.
3. Import into the new schema and compare compiled rack/scenes.
4. Stop old launchd service, start new service, and run the full hardware suite.
5. Pair existing phone/iPad without losing app preferences/history.
6. Exercise Core update, rollback, OTA app update, and backup restore.
7. Only then remove the previous service registration and deployment dependency.

### Diagnostics bundle

User-exported diagnostics include versions, capability graph, redacted config,
recent structured errors, service status, and discovery summaries. They exclude
tokens, API keys, signing material, full chat content, and personal media
library data by default.

### Clean-machine matrix

| Case | Required result |
|---|---|
| Clean Mac, Roon-only rack | Discover, authorize, configure, survive reboot |
| Clean Mac, native rack | Discover/manual configure, safe tests, survive reboot |
| Hybrid rack | Roon playback + native controls resolve correctly |
| Roon absent/revoked | Setup recovery; native rack remains usable |
| Two clients | iPhone/iPad state and queue revisions remain identical |
| Core stable update | Signed update, migration, health confirmation |
| Broken Core update | Automatic binary rollback and intact config |
| Registered new device | New profile/build installs only after registration |
| Unregistered device | Installer explains ineligibility; no generic failure |
| OTA current iOS/iPadOS | Web install/update verified or Configurator fallback shown |
| Expiring profile | Warning early enough to publish and install renewal |
| No APNs relay | Foreground control works; unavailable background features are clear |
| Uninstall | Service stops; user chooses keep/remove data |

Tag 7.0 only after a friend—not the developer—can complete the documented
clean install without shell commands or access to the source checkout.

---

# Proposed PR sequence

Keep PRs narrow enough to verify and revert independently:

| PR | Deliverable | Depends on |
|---|---|---|
| 6.0.0 | Roon spike and capability report | Roon trial |
| 6.0.1 | Media contracts and Apple compatibility adapter | — |
| 6.0.2 | pyRoon adapter; Node fallback only if justified | 6.0.0, 6.0.1 |
| 6.0.3 | Rack graph and config migration | 6.0.1 |
| 6.0.4 | Volume/safety and hardware controls | 6.0.2, 6.0.3 |
| 6.0.5 | Merged Roon state and SSE revisions | 6.0.2, 6.0.3 |
| 6.0.6 | Music/transport UI and iPad queue | 6.0.5 |
| 6.0.7 | Ask tools and vague/compound intent tests | 6.0.4–6.0.6 |
| 6.0.8 | Hardware qualification and 6.0 release | all 6.0 PRs |
| 7.0.1 | External data, Keychain, migrations | 6.0 rack schema stable |
| 7.0.2 | Driver catalog and discovery | 6.0.3 |
| 7.0.3 | Bootstrap/setup API | 7.0.1, 7.0.2 |
| 7.0.4 | Setup UI | 7.0.3 |
| 7.0.5 | Bonjour and secure pairing | 7.0.3 |
| 7.0.6 | Signed/notarized Server app | 7.0.1–7.0.5 |
| 7.0.7 | GitHub/Sparkle Core updates | 7.0.6 |
| 7.0.8 | Registered-device OTA app releases | stable Core API/pairing |
| 7.0.9 | Optional APNs relay | 7.0.5, 7.0.8 |
| 7.0.10 | Migration, diagnostics, friend qualification | all 7.0 PRs |

# Decisions to make during execution

These are intentionally deferred until evidence exists:

1. Whether pyRoon passes the required Python 3.13, reconnect, queue, and
   hardware-provider tests or a documented gap requires the official Node
   fallback.
2. Which Roon queue mutations are actually possible through public pyRoon or
   official SDK actions.
3. Whether to expose legacy DAC/amp controls back into Roon in 6.0 or a minor
   follow-up after consuming Roon controls is stable.
4. Whether the first friend needs multi-rack/multi-home switching or one Core
   per home is enough for 7.0.
5. Central OTA host versus per-Core Tailscale HTTPS mirror. The artifact format
   supports both; central hosting is the default recommendation.
6. Hosted APNs relay versus accepting foreground-only behavior for friends.
7. Whether current iOS/iPadOS still accepts the exact Ad Hoc
   `itms-services` workflow. This is measured each major OS release.

# Reference material

Roon:

- [pyRoon package](https://pypi.org/project/roonapi/)
- [pyRoon source](https://github.com/pavoni/pyroon)
- [Current Home Assistant pin of `roonapi==0.1.6`](https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/components/roon/manifest.json)
- [Current Home Assistant Roon implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/roon/media_player.py)
- [Official Roon extension API](https://github.com/RoonLabs/node-roon-api)
- [Roon transport API](https://roonlabs.github.io/node-roon-api/RoonApiTransport.html)
- [Roon output model](https://roonlabs.github.io/node-roon-api/Output.html)
- [Roon zone model](https://roonlabs.github.io/node-roon-api/Zone.html)
- [Roon architecture](https://help.roonlabs.com/portal/en/kb/articles/architecture)
- [Roon-supported audio outputs](https://help.roonlabs.com/portal/en/kb/articles/faq-what-audio-outputs-or-devices-are-supported-by-roon)
- [Roon volume limits](https://help.roonlabs.com/portal/en/kb/articles/volume-limits)

Apple and release infrastructure:

- [Apple: distributing to registered devices](https://developer.apple.com/documentation/xcode/distributing-your-app-to-registered-devices)
- [Apple: create an Ad Hoc provisioning profile](https://developer.apple.com/help/account/provisioning-profiles/create-an-ad-hoc-provisioning-profile)
- [Apple: registered-device limits](https://developer.apple.com/help/account/devices/devices-overview)
- [Apple: wireless manifest/IPA requirements](https://support.apple.com/guide/deployment/depce7cefc4d/web)
- [Apple: APNs token-based connections and key scopes](https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns)
- [Apple: sending APNs requests to device tokens](https://developer.apple.com/documentation/usernotifications/sending-notification-requests-to-apns)
- [Apple: local-network privacy and Bonjour](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)
- [Apple: `SMAppService`](https://developer.apple.com/documentation/servicemanagement/smappservice)
- [Apple: notarizing macOS software](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
- [Sparkle documentation](https://sparkle-project.org/documentation/)
- [GitHub Releases documentation](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)
