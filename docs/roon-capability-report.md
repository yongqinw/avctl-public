# Roon capability report

Historical capability investigation, started 2026-08-23. These results record
the original adapter investigation, not a fresh release acceptance run.
Results marked **mock only** validate avctl's normalized
contract and drivers, not Roon or a physical endpoint. Live rows must name the
tested output class and confirmation event before the capability is enabled.

SOOD discovery provides the server address and extension port. Core, zone, and
output IDs are installation-specific setup inputs and are not shared here.
The report also covered installation and import of `roonapi==0.1.6`.

| Operation | Implementation/API path | Tested device | Result | Confirmation event | Product decision |
|---|---|---|---|---|---|
| Server discovery | pyRoon `RoonDiscovery.all()` | macOS host | Pass | SOOD response with extension port | Keep pyRoon discovery candidate |
| Python 3.13 import | `roonapi==0.1.6` | avctl venv | Pass | Import and API introspection | Continue Phase 0 with pinned 0.1.6 |
| Authorization/token reuse | pyRoon registry | Roon Server | Pass | `ready`, token persisted mode 0600, reconnect | Live read-only adapter enabled |
| Zone subscribe/state | pyRoon transport subscription | Idle System Output | Partial | Output snapshot arrived; no active zone snapshot | Synthesize honest stopped zone; retest while playing |
| Library browse | Roon Browse API | Roon library | Pass | Explore → Library → Search/Artists/Albums/Tracks | Replay path; item keys are context-bound |
| Qobuz browse | Roon Browse API | Linked Qobuz service | Pass | Qobuz → New Releases/Playlists/Taste/My Qobuz | Qobuz remains owned by Roon |
| Integrated track search | Browse input under Library/Search | Library + linked service | Pass | Artist search returned typed track action lists | Expose read-only search first |
| Generic provider smoke | `RoonMusic` over live pyRoon | Roon Server + Qobuz | Pass | State, library, playlists, mixed search, five-track album expansion, and three Explore sections completed without mutation | Keep `scripts/roon_smoke.py` as the read-only acceptance check |
| Qobuz search/play | Roon Browse action list | Linked Qobuz service | Adapter complete; live mutation pending | Search result exposes Play Now/Add Next/Queue/Radio; context replay is covered by fake-live tests | Use durable browse recipes, never persist context-bound item keys |
| Queue subscribe | pyRoon Transport v2 callback | Fake-live output | Pass | Queue callback projects title/artist/album and depth; subscription restores after a simulated reconnect | Retest against an actively playing live zone |
| Queue replace | Browse `Play Now`, then `Queue` | Fake-live output | Pass | Ordered action receipt | Live mutation test required |
| Queue append | Browse `Queue`/`Add Next` | Fake-live output | Pass | Ordered action receipt | Live mutation test required |
| Queue clear | Public transport stop + avctl logical mask | Fake-live output | Partial by API design | Stop confirmed; avctl reports an empty queue | Roon's public Transport v2 has no remove/clear request; the next `Play Now` replaces the stopped physical queue |
| Play/pause/next/previous | pyRoon `playback_control` | Mock Roon Ready output | Mock pass | Fake zone revision | Live test required |
| Shuffle/repeat | pyRoon `shuffle` / `repeat` | Mock Roon Ready output | Mock pass | Fake zone revision | Live test required |
| Numeric volume/mute | pyRoon output volume calls | System Output | Readback pass; mutation pending | Numeric range, step, current value, and mute state | Bound the configured volume ceiling by the endpoint's hard and soft limits |
| Fixed output | No volume capability | Mock USB DAC | Mock pass | Command refused | Hide slider and volume tools |
| Incremental volume | Relative step only | Mock incremental endpoint | Mock pass | Absolute command refused | Require Roon/device-side safety limit |
| Standby/wake | pyRoon `standby` / `convenience_switch` | System Output | Unsupported | `supports_standby=false`, status indeterminate | Do not render power control |
| Input/source selection | Roon source controls | System Output | Unsupported as DAC input | One source control is not an input selector | Never infer DAC inputs from source controls |

## Next live session

1. Run `./venv/bin/python scripts/roon_probe.py`, then run the non-mutating
   provider check with `./venv/bin/python scripts/roon_smoke.py`.
2. In Roon, authorize **avctl** under Settings → Extensions. The probe and
   live worker deliberately share one stable extension identity and token.
3. Preserve only sanitized zone/output shapes as fixtures; never commit the
   authorization token or authenticated traffic.
4. Select a disposable Roon zone and exercise Play Now, Queue, Next,
   Previous, shuffle, repeat, and stop/replace while watching the zone queue.
5. Confirm callback behavior when the same queue is changed from a second
   Roon client and after Roon Server restarts.

## Implemented provider boundary

The Music panel and Ask no longer initialize MusicKit directly. They resolve
one configured `MusicSource`, whose two domains are:

- the user's library, albums, songs, playlists, transport, and queue; and
- a discovery service with search, album/playlist expansion, Explore,
  optional library mutation, and direct play/queue.

`AppleMusic` implements those domains with Music.app plus MusicKit.
`RoonMusic` implements them with Roon Library plus Roon's integrated Qobuz
browse tree. Both feed the same avctl logical queue. Roon library and Qobuz
items intentionally share one queue engine because Roon can mix both in one
zone; Apple local and catalog playback retain their two physical engines.

Current honest Roon limitations:

- Roon Browse does not expose a stable date-added field, so the Recently Added
  shelf follows Roon's Library/Albums ordering rather than inventing dates.
- The measured Qobuz action menus did not expose a dependable Add to Library.
  avctl therefore owns a local SQLite virtual library of explicit Qobuz
  bookmarks. It stores stable avctl ids plus metadata, re-resolves stale Roon
  Browse handles after restarts, and never writes on direct Play/Queue.
- Public Transport v2 exposes queue subscription and play-from-here but no
  granular clear/remove operation. avctl stops and logically masks a cleared
  queue until the next replacement.
- pyRoon 0.1.6 can briefly retain `ready=true` while replacing its websocket,
  and can emit its own startup-level “connection is not ready” log. avctl gates
  commands and queue re-subscription on the underlying socket, bounds timeouts,
  and keeps the serialized worker alive after a caller times out.
