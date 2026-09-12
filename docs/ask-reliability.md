# Ask planning and execution contract

The model selected by the active provider profile is the sole semantic planner
for every valid Ask utterance. There is
no local regex fast path, intent veto, command-word gate, query substitution,
or queue-mode rewrite. The same model path handles short controls, angry or
profane speech, ambiguous follow-ups, Explore, library management, and compound
rack commands.

The server is deliberately mechanical after planning. It validates tool names,
JSON arguments, enums and bounds; resolves server-owned result identifiers;
executes the allowlisted handlers in model-provided order; preserves hardware
limits; and reports the real outcome. It does not reinterpret whether the
caller meant the action. When meanings would produce materially different
actions, the selected model asks one concise clarification.

Information tools such as `search_music`, `explore_music`, and `curate_music`
return real data to the selected model for another planning round. Up to four rounds let
the model search or explore, inspect the results, and then choose a verified
selection action. Terminal controls return their verified local receipt
immediately after execution.

## Verified selections

An information-only `search_music` or `curate_music` result is copied into a
bounded ledger keyed by authenticated caller and conversation. Its active state
is cached in memory and persisted on the Mac mini so a service restart does not
break “play those” follow-ups.
The model and phone receive an opaque `selection_id` plus display metadata; local
Music persistent IDs and Apple catalog IDs remain server-side.

Only the latest selection can be acted on. `act_on_selection` supports:

- replacing or appending the centralized queue with verified local and Apple
  Music catalog tracks;
- explicitly importing catalog songs before playing or queueing them;
- adding verified local songs to a Music.app playlist.

Before either mutation, the stored persistent IDs are checked against the
current local-library snapshot. Missing tracks are dropped; if the library
cannot be checked, the request is rejected before anything changes.

Catalog-only matches stream directly through the centralized queue without a
library mutation. Apple Music albums and playlists are expanded to catalog
tracks. Starting a new conversation archives the old active state. The opaque
session id is kept in the app's local storage, so an app/WebView reload restores
the same conversation without storing message text on the phone.

## Private local history

Completed text and voice turns are stored in
`~/.avctl/ask_history.sqlite3`. The containing directory is owner-only and the
database mode is `0600`. Authenticated caller identities are SHA-256 keyed; API
keys, authorization headers, audio, and raw driver exceptions are never stored.
Set `AVCTL_ASK_HISTORY_FILE` to relocate the database.

The active prompt remains bounded to six exchanges. The complete local archive
is for UI restoration and offline analysis, not an unbounded prompt append. Use:

```sh
./venv/bin/python scripts/analyze_ask_history.py --days 30
```

The report stays local and highlights rejected/unknown actions, voice failures,
repeated corrections, tool failure counts, and provider latency. This evidence
is used to improve tools, grounding, and transcription without adding local
natural-language control rules.

Stable cross-conversation context is also model-owned. The selected model may call
`manage_memory` when the caller expresses a lasting preference, routine,
personal naming convention, or recurring correction. These short memories are
stored caller-scoped in the same private database and appended as inert context
to later prompts. The model can forget an exact `memory_id`; avctl does not use
regexes to infer or rewrite these memories. One-off commands, current device
state, credentials, secrets, and raw tool results must never become memories.
For an explicit reference to an older conversation that is outside the active
six exchanges, the model can call `recall_history`. The server performs only a
caller-scoped database retrieval; the model interprets the returned historical
records and decides what they mean now.

## Provider profiles

Ask uses one provider for an entire turn. A Settings switch affects the next
turn only, so a multi-tool command can never change models after an earlier
tool has already changed the rack. Profiles live under `agent.profiles` in
the owner overlay above `configs/defaults.yaml`; the selected name is persisted in
`~/.avctl/agent_profile`. `AVCTL_AGENT_PROFILE` can lock the selection for a
managed deployment.

`fireworks` is a specialization for priority tier, prompt-cache affinity and
provider pricing. `openai-compatible` is the common driver for vLLM, SGLang,
Ollama and compatible hosted APIs. Provider code transports messages and
normalizes errors only; prompt construction, tool validation, sequential
execution, history and hardware safety remain in `api/agentlink.py`.

The Settings connection test asks the selected model for one synthetic,
side-effect-free tool call. A successful HTTP response without a valid tool
call does not pass. Credential contents are loaded on the server and are never
returned by the settings API.

## Idempotent delivery

Typed and voice clients attach a fresh `request_id`. The server binds it to the
caller, conversation, and exact utterance, then caches the completed result. A
duplicate delivery waits for the owner or replays the same receipt instead of
running tools twice. Reusing an ID for different text is rejected.

The cache is bounded to 256 completed/in-flight records and lives only for the
current service process. Clients without a request ID retain the previous API
shape and behavior.

Responses carrying a request ID include:

- `action_status`: `succeeded`, `no_op`, `rejected`, `partial`, or `unknown`;
- `outcomes`: one status per tool call;
- `trace`: provider, tool, total timing, model rounds, and tool-call count;
- `replayed`: whether this response came from the idempotency cache.

An `unknown` outcome means the driver may have changed state before its reply
failed. It must not be retried under a new request ID without checking state.

## Regression harness

`tests/ask_harness.py` supplies deterministic provider replies without network
or credentials. `tests/test_agent_reliability.py` asserts end state rather than
model prose: every utterance reaching the planner, angry Mandarin playback,
multi-round Explore, exact dispatched persistent/catalog IDs, playlist
mutations, zero duplicate actions, selection scope isolation, reset behavior,
and client/API request-ID propagation. Add new failure modes there as replayable
scenarios.
