# Ask + Music Backend Reliability Audit — 2026-08-26

This is a historical engineering audit. Recorded test counts describe the
original audit runs; they are not a fresh validation of a public release.

## Scope

This audit exercises the same Ask planning and action handlers against both
music implementations:

- Apple Music: Music.app library rows, MusicKit catalog results, direct catalog
  playback, synchronized-library mutation, and the centralized avctl queue.
- Roon: native Roon library rows, the avctl virtual library, Qobuz discovery,
  stable virtual-library IDs, Roon playback IDs, and the same centralized queue.

The regression scenarios cover mixed-source curation, classics/library adding,
live versions, text-only model answers, large batches, and recently-added
playback. The audit used non-mutating checks and synthetic regression tests.

## Failure cases

### Explicit commands could be accepted as text-only success

At least one complex mixed-source request received model scratch/reasoning but
no tool call. Because the response contained text, Ask treated the turn as a
successful chat answer even though nothing played or entered Q.

The underlying problem was not missing intent keywords. Ask had no terminal
invariant that an explicit play, queue, transport, clear-Q, or library-add
request must result in either a tool action or a deliberate clarification.

### Roon library adding selected existing live recordings too eagerly

For `add_only`, the local-match shortcut used the same loose title/artist score
as ordinary playback. A local live recording could therefore satisfy a
request for the studio recording before Qobuz was searched, even when later
catalog ranking would have preferred studio results.

### One failed item could spoil a large library-add operation

Ask previously called the single-item add path once per result. That caused
three problems:

1. one provider failure interrupted the remaining verified items;
2. Roon refreshed the virtual library after every item;
3. Roon could repeat its expensive album-healing Browse flow for every loose
   Qobuz track even when the verified search result already contained an album.

### Partial failures produced misleading receipts

The old receipt counted intended additions, not successful additions. A batch
could say that all songs were added even when a provider write failed. The
selection follow-up path had the same issue and then polled synchronization for
items that had never been accepted by the provider.

### A generic selection path still assumed Apple-style IDs

When acting on a previous result, local rows were revalidated with the shared
Apple-compatible regular expression instead of the selected driver's
`valid_library_id()` contract. Roon's current IDs happen to fit that expression,
but the code violated the generic backend boundary and could discard future
provider IDs.

## Fixes

### 1. Fireworks remains the planner, but skipped actions are retried once

Ask now recognizes only the presence of an explicit action requirement. It does
not choose the tool, candidate, source, order, or target locally. If Fireworks
returns ordinary text without a tool for an explicit music action, Ask feeds the
planning failure back to Fireworks and gives it one recovery round.

This preserves the intended "all decisions through Fireworks" design while
preventing silent no-ops. Truly ambiguous follow-ups such as `play it` may still
end in a clarification after that bounded retry.

### 2. `add_only` uses strict recording matching

Local candidates for library adding now use recording-aware matching. Title,
primary artist, and album/version signals are evaluated before an existing
library row can suppress service discovery. Live, concert, karaoke, tribute,
and other non-studio variants no longer count as the requested studio recording
merely because title and artist are similar.

Ordinary playback keeps the looser matching behavior, where an already-owned
playable version is often useful.

### 3. Added a backend-generic batch save operation

`musiclink.add_many()` now:

- saves every verified item independently;
- records successful and failed provider IDs;
- continues after an isolated item failure;
- refreshes the Roon virtual library once after the batch;
- leaves Apple Music's asynchronous synchronization behavior intact.

Both curation (`curate_music` with `add_only`) and result follow-ups
(`add_and_play` / `add_and_queue`) use this operation. Polling and playback now
cover only provider-accepted items.

### 4. Reused verified Roon metadata safely

The generic `MusicSource.add_service_item()` contract now accepts optional
verified metadata. Roon re-resolves the provider ID, then accepts the album hint
only when normalized title and primary artist agree with that resolved item.
This prevents arbitrary metadata injection while avoiding repeated Roon Browse
album searches for the exact verified Qobuz result.

When the hint is absent or does not agree, Roon retains the slower album-healing
fallback. Apple Music accepts the generic signature but continues to let
MusicKit perform the library mutation.

### 5. Receipts reflect actual writes

Ask now reports the number and names of successful additions. Failed or
unverified candidates are explicitly counted as left out. It uses correct
singular/plural wording, does not claim playback started when every import
failed, and reports partial failures alongside successful follow-up actions.

### 6. Driver-owned ID validation is used in selection follow-ups

Previous-result actions now call the active music driver's ID validator rather
than assuming Apple Music persistent-ID syntax.

## New backend test matrix

| Scenario | Apple Music | Roon/Qobuz | Invariant checked |
| --- | --- | --- | --- |
| 20 local + service happy songs, mixed and appended to Q | Yes | Yes | Both sources survive, shuffle happens before placement, no implicit library mutation |
| New/unheard service songs, replace and play | Yes | Covered by shared curation tests | Direct service playback does not add to library |
| New + old jazz, mixed, shuffled, play now | Covered by shared curation tests | Yes | One logical queue replacement; stateful provider search is batched |
| Jay Chou + JJ Lin classics, add to library | Yes | Yes | Exact primary artists; successful batch receipt |
| Studio classics with live versions present | Shared strict-match test | Yes | Only studio Qobuz IDs are saved |
| One provider write fails in a batch | Yes | Yes | Later items still save; partial failure is truthful |
| Recently added this week, shuffle and play | Yes | Yes | Calendar filtering, shuffle intent, centralized queue, stable virtual IDs |
| Explicit command gets text but no tool | Backend-independent | Backend-independent | Fireworks receives one recovery round |
| Ambiguous `play it` still lacks a target | Backend-independent | Backend-independent | Bounded retry may still clarify; no guessed mutation |

The Apple integration fixture subclasses the production `AppleMusic` driver
and keeps only Music.app writes in memory. The Roon fixture uses the production
`RoonMusic`, `VirtualMusicLibrary`, and centralized queue with a deterministic
Roon Core fake. The scripted model controls only Fireworks output; all action
validation, matching, discovery, saving, queueing, and receipts are production
code.

## Performance impact

The primary Roon improvement is removal of avoidable repeated stateful work:

- service candidates are already discovered in batches;
- verified album metadata is carried into batch saving;
- virtual-library refresh happens once, not once per track;
- one bad item no longer causes the model/user to repeat the whole request.

This does not eliminate unavoidable live Roon Core latency, Qobuz latency, or
the fallback album-healing path for incomplete search results. It substantially
reduces amplification inside avctl for large `add_only` requests.

## Verification

- Focused Apple/Roon Ask, agent reliability, driver, and virtual-library suite:
  **507 passed**.
- Full repository suite, including localhost socket integration tests:
  **786 passed** in 55.92 seconds.
- Diff whitespace validation: clean.

The only emitted warning is the existing Starlette `TestClient` / `httpx`
deprecation notice; it is unrelated to these changes.

## Remaining risks and next tests

This pass intentionally used deterministic backends and did not spend Fireworks
credits or mutate the live Roon/Apple queues while unattended. The following
remain suitable for a supervised device pass:

1. measure cold and warm live Roon Core latency for 20–30 Qobuz candidates;
2. force a real Qobuz item to disappear between search and save and verify the
   partial receipt in the app;
3. verify Apple Music synchronization delay after a 20-item add batch;
4. interrupt a large Roon queue with Next, Clear Q, and a backend switch;
5. compare the same vague bilingual prompt on DeepSeek and Kimi after a model
   switch and a new session;
6. verify live/concert rejection against provider metadata that omits album
   names, which exercises the slower healing fallback.

These are external-state and latency checks rather than known deterministic
correctness failures. The repository behavior covered by this audit is green.

## Follow-up: translated Roon/Qobuz metadata

Translated catalog titles exposed a separate matching problem: a grouped
request could reject valid recordings, then repeat expensive exact-title
searches without resolving the mismatch. Equivalent title examples used in
regression coverage include:

- `晴天` appeared as `Sunny Day — Jay Chou`;
- `青花瓷` appeared as `Blue and White Porcelain`;
- `稻香` appeared as `Rice Field`;
- `告白气球` appeared as `Love Confession`.

Roon search rows can also combine writer and performer credits in the artist
field and omit the album. These shapes make strict title equality and a
"first credit is the performer" rule unreliable. Live recordings and covers
still need independent rejection; relaxing every match would recreate the
unwanted-library problem.

The initial follow-up implementation added a constrained semantic-review round:

1. avctl performs one real grouped provider search;
2. deterministic matching accepts obvious exact studio recordings;
3. unresolved translated titles are returned to Fireworks as numbered requests
   and numbered real options, without provider IDs;
4. Fireworks may pair only high-confidence semantic equivalents;
5. the server resolves those indexes back to cached IDs and independently
   rejects prohibited recording versions, duplicate choices, expired reviews,
   and cross-request reuse;
6. the existing batch-save or centralized-queue path performs the action.

For multi-song requests by one artist, reviewable grouped results no longer
trigger an exact Roon Browse search for every title. The regression scenario
for `晴天`, `青花瓷`, and an already exact `明明就` therefore performs one
`Jay Chou` search, two model rounds, and one atomic batch save while excluding
the live and tribute options.

After this follow-up, the full repository suite passes **788 tests** in 55.64
seconds, including the localhost socket integration tests.

## Follow-up: one semantic gate for Apple Music and Roon

The translated-title fix still left an architectural inconsistency: an exact
string match from either service bypassed Fireworks, while only candidates
rejected by local heuristics reached the semantic review. That made the least
ambiguous rows intelligent and the most ambiguous rows rule-driven, and it
also meant a compound `shuffle + curate` plan could stop after changing
shuffle without running the pending review.

The curation action path now uses one provider-independent contract:

1. the configured Apple Music or Roon/Qobuz driver returns real candidates;
2. avctl removes only obviously unrequested alternate versions such as live,
   cover, tribute, karaoke, medley, remix, or instrumental recordings;
3. every remaining service candidate, including exact title matches, is sent
   to Fireworks as a numbered option with title, full credits, album, and
   source but without an actionable provider ID;
4. the system prompt asks Fireworks to resolve translation, transliteration,
   alternate scripts, localized titles, artist credit rolls, album context,
   and same-title ambiguity;
5. Fireworks may select one offered option per requested song or omit any or
   every uncertain request;
6. the server binds the decision to the caller, session, original message,
   expiry window, and cached real IDs, rechecks prohibited recording versions,
   restores original request order, and executes the local plus service result
   as one queue or library batch.

Local-library matches remain deterministic because they already carry stable
library IDs and personal metadata. A review no longer drops those local rows:
mixed Apple/Roon and local queues retain both sources and their original
candidate order before an explicit shuffle is applied. A pending review also
forces the next model round even when another tool in the first batch already
acted, such as `music_transport(shuffle_on)`.

This moves semantic choice to Fireworks while keeping authority and identity
enforcement in avctl. The model cannot invent IDs, reuse an option, cross
sessions, replay an expired review, mutate a library without explicit wording,
or force an unrequested live/cover variant through the final server guard.

Verification after the unified gate and 100k budget: **790 tests passed** in
55.25 seconds;
the only warning remains the pre-existing Starlette/httpx deprecation notice.

## Post-gate Apple/Roon execution review

The backend suites were run independently after the unified semantic gate,
then their assertions were inspected rather than treating one aggregate green
run as proof that both drivers behaved identically.

Apple Music, **5/5 passed**:

- a 20-song local/catalog mix entered the second model round, preserved both
  ID domains, honored explicit shuffle, queued once, and never imported;
- a service-only play request streamed three catalog tracks without touching
  Sync Library;
- an explicit four-song library request performed one reviewed bulk add;
- an ambiguous `晴天` search put a same-title niche credit ahead of the
  translated original, and the review selected `Sunny Day — Jay Chou` while
  the live version remained unavailable to the model;
- recently-added local playback retained its date scope and shuffle behavior.

Roon/Qobuz, **5/5 passed**:

- a 20-song virtual-library/Qobuz mix used one grouped provider search, one
  semantic review, one queue batch, and explicit shuffle;
- a six-song old/new jazz request replaced and started the queue once;
- a four-song classics request saved only studio tracks and rejected every
  live/tour candidate;
- virtual-library tracks added this week kept stable local IDs through shuffle;
- translated `晴天`/`青花瓷` candidates were resolved to `Sunny Day` and
  `Blue and White Porcelain` in a second model round, while live and tribute
  options were excluded and the three selected tracks were saved atomically.

The wider Ask group (planning, prompt rules, provider switching, recovery,
history, Apple, and Roon) passed **495 tests**. These are deterministic
execution-contract tests: they prove candidate visibility, model-round wiring,
authorization, ordering, and side effects, but they do not claim that a live
Fireworks response or provider network will always make the same semantic
choice. The ambiguous Apple and translated Roon cases specifically exercise
the handoff where that live model judgment occurs.

Both shipped Fireworks profiles now request a **100,000-token maximum output
budget**, and the production request-payload test verifies the same value. This
is `max_tokens`, not a way to enlarge a model's fixed context window; the
provider still enforces its own input-plus-output context ceiling.
