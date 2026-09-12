# avctl roadmap

Historical design notes written 2026-08-07, starting from version 2.0.
Feature status, estimates, and version targets below are archival, not current
release guarantees. Start with the [project README](../README.md) and
[setup guide](friend-setup.md) for current usage.

The rule this project has run on since the beginning applies to everything
below: **only ship what actually works, and say plainly what does not.** Every
version here has a way to fail honestly.

---

## Where 2.0 leaves us

The early architecture polled device state separately for every client,
coupling device I/O to HTTP request rate. Synchronous routes ran on Starlette
worker threads, while scenes ran steps through a `ThreadPoolExecutor`. The
following proposals addressed that latency and concurrency model.

Two things stand out. The phone learns about the rack on a timer rather than
when something happens. And the work of asking the rack is paid per client
per poll, rather than once.

---

## 2.1.0 — The disc player

The last box with no controls. Everything needed is already proven: status
reads over HTTP today (REVIEW/PST, unauthenticated), and IR now works —
2.0 solved aiming and code capture on the D900.

- Aim an emitter at the UB820's IR window; record the port in `blaster.ports.disc`
- Learn power, transport, nav and menu codes with `scripts/learn_ir.py`
  (which now rejects payload-free captures, the trap that cost a day)
- Restore the Disc panel and the `disc.*` commands removed in the 2.0 slimming
  — `devices/bluray.py` and `api/disclink.py` were deliberately kept as the record
- Panasonic uses Kaseikyo, not NEC, so codes come from the learner rather than
  a constructed hex value

**Risk:** low. The only unknown is whether a single emitter can reach the
player's window given where it sits.

---

## 2.2.0 — Thread safety, and the tests that prove it

The system genuinely runs concurrently: sync routes execute on Starlette's
threadpool, scenes fan out across a `ThreadPoolExecutor`, the TV owns a
background asyncio loop, the amp has a reader thread, and the state poll runs
against all of it. Most of it is careful. Not all of it has been checked.

**The audit is done, and it found live bugs rather than theory.** Ordered by
whether they leave *durable, user-visible wrongness* (you walk to the rack and
fix something) versus a spurious error a retry clears.

**Durable — fix first:**

1. **Nothing stops two scenes running at once.** `POST /api/cmd` never
   serialises, and a Music scene can occupy 45–70 s in the TV wake. Press Off
   during it and the wake loop keeps sending magic packets: *the TV ends up on
   after you asked for off*. The amp's 10 s settle collides with a power-off
   and throws. Both DAC steps race. The client makes this likely, not
   theoretical — `fetch` has no timeout, so a 70 s IR-fallback wake outlasts
   the user's patience and they press again. **Fix: one lock at the route,
   answering 409 rather than queueing. ~30 lines.**
2. **The D900's walk is an unlocked read-modify-write held for up to 5.6 s.**
   `select()` reads the file, presses, re-reads, writes — per press. Two
   concurrent walks both read `usb`, both press (the DAC advances *two*), both
   write `opt1`. Silently off by one: exactly the failure the module exists to
   prevent, and the one that makes you walk over and Resync. There is also a
   **latent crash** — `CYCLE.index(self.current)` is unguarded against `None`,
   so a truncated state file raises mid-walk after some presses have already
   gone out. **Fix: an RLock around whole walks, and guard that line.**
3. **The DAC state files are not written atomically.** `Path.write_text()`
   truncates in place, so a crash or power cut leaves a zero-length file — and
   `devices/dac.py`'s own docstring promises this state survives power cuts.
   The irony is that `_persist()` in `state.py` already does it correctly with
   mkstemp + `os.replace`; the files that matter more got the weaker
   treatment. **Fix: ~10 lines, copy the pattern that already exists.**

**Spurious — fix after:**

4. **Amp hold-repeat issues genuinely concurrent RS-232 transactions.** Holding
   volume fires every 120–420 ms without awaiting. Bytes are safe, but
   *transactions* are not: two threads read 58 and both target 60 (steps get
   lost), the readback predicate is satisfied by the amp slewing *through* a
   value en route to somewhere else, and an error provoked by one thread
   surfaces on another — including on the state poll, which then paints the
   amp offline.
5. **`tvlink._drop()` runs with no lock** and can disconnect a websocket
   another coroutine is mid-call on. The resulting `NotConnectedError` is not
   in `_DOWNSTREAM`, so it escapes normalisation and reaches the phone as a
   502 reading "call connect() first".
6. **Artwork extraction writes straight to its final path**, so a concurrent
   reader can be handed a zero-length file — which is then served with
   `immutable` and cached by the browser for a week. Same failure with no
   concurrency at all if osascript times out mid-write: the empty file is
   returned forever until the cache directory is cleared.
7. **The iTach opens a fresh connection per send** with nothing serialising the
   physically-single IR module; its `023 device is busy` error aborts a DAC
   walk partway, leaving the device on an unknown input.

**Deliberately not on this list:** the lazy singletons are all correct
(check-then-act *inside* the lock), `_persist()` is correct, the websocket
multiplexes safely, and there are no lock-ordering deadlocks. `tvlink`'s
`_tv` assignment looks unsafe but is safe because no `await` sits between the
check and the assignment — an invariant worth a comment, since adding an
`await` there later would break it silently.

**And the part that makes it real: a test suite.** There is none today, and
5,279 lines is past the point where that is comfortable — especially with 3.0
being a refactor of everything. The good news is that it is cheap, because the
architecture already has the seams:

- every driver takes plain values and an injected transport
  (`ToppingD900(blaster=...)`)
- there is exactly **one chokepoint per protocol** to stub:
  `AppleMusic._osascript`, `ITachIR._exchange`, `PanasonicUB820._post`,
  the amp's serial port
- `views.key()` already enforces that no button can exist without a command —
  a single test that renders every page catches an entire class of drift

Target: pytest, fakes for all four transports, and a concurrency test that
fails on the races the audit finds before it passes.

---

## 2.3.0 — The phone stops asking

In the earlier design, the phone asked "what is happening?" every 15 seconds,
and every client repeated the device reads. Pressing a physical remote could
leave the app displaying stale state until its next poll.

**Server-Sent Events**, not WebSockets. This traffic is entirely one-way
(server → phone; commands already go over `POST /api/cmd`), SSE is plain HTTP
so it passes through `tailscale serve` with no upgrade negotiation, and
`EventSource` reconnects on its own — which matters for a phone that sleeps,
loses signal, and wakes in a pocket. WebSockets would buy bidirectionality
this app does not need and cost reconnection logic it would have to write.

The change worth making is bigger than the transport, though:

**Decouple device polling from HTTP.** One background task polls the rack on
its own schedule, keeps the last snapshot in memory, and pushes to every
connected client. Then:

- device I/O cost stops scaling with client count *or* poll rate
- the phone can learn about changes in **under a second** instead of 15
- a command's own result can push immediately, so the UI settles at the speed
  of the device rather than the next tick
- per-device intervals become possible: the amp can be asked every 2 s, the
  Music library scan far less often

**Fallbacks stay honest.** If `EventSource` fails or the stream drops for
good, the client falls back to the existing 15 s poll. The LED already
communicates staleness; that stays.

**Risk:** moderate. A long-lived stream plus a background poller is exactly
where the thread-safety work must land first — hence the ordering.

---

## 3.0.0 — The rack described, not coded

Today, adding a device means editing `devices/`, `api/<thing>link.py`,
`api/commands.py`, `api/state.py` and `api/views.py`. The goal for 3.0 is that
it means editing **YAML**.

### The shape

```yaml
devices:
  amp:
    driver: devices.amp:McIntoshMAC7200      # module:Class, resolved at boot
    transport: {port: /dev/cu.usbserial-EXAMPLE, baud: 115200}
    capabilities: [power, volume, mute, input]

panels:
  - id: home
    title: Home
    sections:
      - title: DAC / Amp
        layout: c4
        keys:
          - {cmd: dac.power,        label: DAC power, span: 2}
          - {cmd: amp.power.toggle, label: Amp power, span: 2, class: accent}
```

Three registries fall out of that: **drivers** (resolved by `importlib` from a
`module:Class` string), **capabilities** (what commands a driver offers, which
generates the command table instead of it being hand-written), and **layout**
(panels and keys, which generates the markup).

### What must not be lost

This is the risk, and it is worth naming loudly. The current code's real value
is not its structure — it is that **every device's weirdness is written down
next to the code that copes with it**: the LG lying about being Active on its
way down, the amp's undocumented dialect, the D900's payload-free repeat
frames, the exact reason a press interval is 0.8 s. A generic configuration
layer that dissolves those into uniform YAML would be a downgrade dressed as
an abstraction.

So: drivers stay hand-written Python, with their comments intact. Only
*wiring* becomes declarative.

Two more invariants to carry over:

- **`views.key()`'s guarantee** — a button that cannot be dispatched cannot be
  rendered. In a config world this becomes validation at load: unknown `cmd`
  in a panel is a startup failure, not a 404 at 2am.
- **Honest degradation** — a driver that cannot reach its device must still
  produce a snapshot entry saying so, exactly as `safe_state()` does now.

### The music source becomes a driver too

This is the part 4.0 depends on, so it belongs in 3.0's design rather than
being discovered later. Today the Music panel is welded to Apple Music: it
calls `musiclink`, which calls AppleScript, which talks to Music.app. But the
panel's *shape* — a library grid with artwork, an album view, search, a
transport bar with now-playing — is not specific to Apple Music at all. It is
the shape of any music source.

So the same driver treatment applies: define what a music source must offer

    now_playing()      track, artist, album, art, position, state
    library_recent()   albums, newest first
    album_tracks()     a specific album
    search()           within the source
    play/pause/next/previous/seek
    volume             where the source owns it

...and let config say which driver provides it. Apple Music via AppleScript is
one implementation. Roon is the next.

Doing this in 3.0 means 4.0 is a new driver plus a config line, rather than a
second music panel or a rewrite of the first.

### Prerequisites

Tests (2.2.0), and pydantic for config validation so a typo produces a
readable error at boot rather than an `AttributeError` mid-scene.

**Risk:** high — this is a rewrite of the wiring of a working system. Ordering
it after tests is not optional.

---

## 2.4.0 — Finish the PWA (one hour, most of "an app")

Slotted here rather than in 5.0 because it is an hour of work for the majority
of what "make it a real app" means day to day. `_page()` already emits
`apple-mobile-web-app-capable` and a viewport with `viewport-fit=cover`, so
standalone display and safe areas already work. Missing:

- a **web app manifest** (`display: standalone`) — there is none at all
- an **`apple-touch-icon`** — the home screen icon is currently a screenshot
- `apple-mobile-web-app-status-bar-style`
- startup images

That gets a proper icon, a real chrome-free launch, and — should it ever be
wanted — the two preconditions for **Web Push**, which iOS has supported since
16.4 for home-screen web apps specifically, no developer account required.

---

## 4.0.0 — Roon — **blocked on a decision that is not engineering**

The research turned up one fact that governs everything else, so it goes first.

> **Roon does not integrate Apple Music.** It supports TIDAL, Qobuz, KKBOX,
> nugs, internet radio and local files. Apple is absent, and structurally so —
> there is no Apple equivalent of the partner API TIDAL and Qobuz expose. This
> is not on anyone's roadmap.

So "the Music panel controls Roon" is not an *addition* to what exists. It is a
**replacement** of `devices/music.py`, `api/musiclink.py`, the JXA library
scans, the `raw data` artwork extraction, the `avctl` playlist standing in for
Up Next, the MusicKit developer token and the Music User Token — and it only
works if the *library* also moves to Qobuz or TIDAL (another subscription) or
to local files Roon can index.

Running both side by side means two music systems on one mini, contending for
the same USB DAC, and a now-playing strip at the top of every panel with two
possible answers. That is a real regression in the one thing the phone is
pulled out to answer.

**The engineering, for when the decision is made, is genuinely fine:**

- `roonapi` (pyroon) is the only real Python option — frozen at 0.1.6 since
  Dec 2023, but **Home Assistant ships exactly that version today**, and the
  protocol has been stable. Transport, zones, volume, browse, live state
  callbacks and album art (a plain URL, proxyable like Music.app's) are all
  present.
- **Auth is one persisted token**, the same shape as the TV's pairing key.
  Discovery is automatic over UDP multicast — no IP to configure.
- **Roon Core can run on this mini** (macOS 12+, SSD). The D900 on USB appears
  as a local output; no Roon Bridge needed.
- The one integration cost: pyroon is a synchronous thread on
  `websocket-client`, so it needs a bridge into the async world — a different
  shape from every other module in `devices/`.
- Cost: **$14.99/mo, $149.88/yr, or $829.99 lifetime.** 14-day trial.

**The more interesting integration nobody asks for.** Roon's API runs both
ways: an extension can register as a **volume control and source control**, so
*Roon* drives *your* hardware — the MAC7200's real volume in Roon's remote,
and the amp powering on when you hit play. That is additive rather than
replacing anything, and it works while you stay on Apple Music. The catch:
pyroon implements volume control but **not** source control, so it means a
fork or the Node bridge.

**Recommendation:** take the 14-day trial *before writing any code*, because
the question is whether you want to leave Apple Music — a music decision. If
the answer is no, the Roon-shaped want (richer metadata, better now-playing)
is cheaper to satisfy by deepening the MusicKit integration you already have a
paid key for.

---

## 5.0.0 — The phone app — **you do not want the App Store**

The goal as stated, "in the app store just for myself," turns out to be the
single most expensive way to get the thing actually wanted.

**Why the App Store fails, and it is not the guideline you would expect.**
Everyone assumes 4.2 (minimum functionality, no repackaged websites). A real
native shell with widgets and Control Center controls would likely clear that.
The blocker is **2.1(a) App Completeness**: a reviewer in Cupertino cannot
reach your Mac mini over your tailnet. They will see a spinner and a connection
error and reject it as non-functional. The guideline's own escape hatch is a
built-in demo mode — meaning **a fake rack, built and maintained forever, so
that a stranger can verify an app only you will ever install.** Unlisted
distribution does not help: it hides the app from search but still requires
passing full review, and the conversion is reportedly one-way.

**What you should do instead — you already own the answer.** The paid
Apple Developer Program you bought for MusicKit removes every expiry that
makes personal development painful: **no expiration limits on App IDs,
devices, or provisioning profiles.** Build a SwiftUI app in Xcode, install it
on your phone, and it lives there on a **one-year** profile. No App Review, no
demo mode, no 4.2 argument, and **every native API available**. If tethering to
Xcode annoys you, **internal TestFlight** is the alternative: no beta review
for internal testers, builds live 90 days.

**What native actually buys** — and it is not the main UI, which is already
fast and tuned for one hand in a dark room. It is everything *outside* the app:

- **Control Center controls** (iOS 18+) — "Everything off" as a button you
  reach without unlocking. Apple's own framing for the API is thermostats and
  cars; this is exactly that.
- **Live Activities / Dynamic Island** — now-playing on the lock screen.
- **Widgets** — current scene, amp volume, what is playing.
- **App Intents / Siri** — "Hey Siri, music scene."
- **Lock screen transport controls** via `MPRemoteCommandCenter`.

All of them can call the existing `POST /api/cmd` directly, because
`api/commands.py` is already a validated command table — the native side needs
no new backend.

**Ordering:** 2.4.0's manifest work first (an hour, most of the daily feel),
then a native shell only if Control Center and Siri are genuinely wanted. The
App Store is not a step on this path.

---

## 6.0.0 — The panel you talk to

A text box that turns "put something on" into commands the rack already has.

### The command table is already the tool schema

This is the whole reason the feature is small. `api/commands.py` is ~70 ids,
each with a human label and a note recording the device's weirdness — which is
exactly the shape a model's tool definition wants. `implemented()` already
separates what is real from what is drawn but inert.

So the model is handed **only** `implemented()` ids, as a JSON-schema `enum`,
under strict validation. It then cannot emit a command that does not exist or
one that answers 501. That is `views.key()`'s guarantee carried one layer out:
a button that cannot be dispatched cannot be rendered, and now a command that
cannot be dispatched cannot be produced. Same invariant, no new mechanism.

The `note` fields are the most valuable part of the prompt. "The remote's power
is a toggle, so this flips a tracked belief" is precisely what stops a model
confidently asserting `dac.power` when nothing knows the current state.

Three tools rather than one, split by argument shape — most commands take
nothing, and a free-form `args` object cannot be strictly validated:

    run(steps: [<enum of implemented ids>])
    set_volume(target: "amp" | "mini", level: int)
    play(album?, artist?, track?)

### A route, not a command

`POST /api/ask` answers `{say, steps, confidence}`. It returns a **plan**; it
does not execute. The phone renders the plan and dispatches the steps through
the existing `POST /api/cmd`. That keeps one place where a device is ever
touched, and keeps the interpreter out of the command table entirely — talking
is not something the rack does.

Execution policy follows the honesty the rest of the app already has: a single
low-risk step (volume, pause, input) fires at once and the panel says what it
did; a multi-step or scene-level plan is shown with a Run button. Nobody wants
"everything off" fired on a maybe, halfway through a film.

### The local parse earns its place, and not mainly for speed

Order is: normalise, then a small phrase table, then the model.

1. **Offline honesty.** The rack is on a local subnet. Making "everything off"
   depend on a third-party API being reachable is a regression against the one
   thing this project has been careful about since 1.0. The phrase table means
   the ten things actually said still work when the internet does not.
2. **Zero latency on the common path.** "off", "music", "louder" is most of
   daily use, and a 1–2 s round trip there would feel worse than the buttons.

Then log every phrase that falls through to the model. Anything that recurs is
promoted into the table by hand — measure first, then decide, as everywhere
else here.

### State goes in the user turn

The model needs `state.snapshot()` to resolve "turn it down", "pause that",
"what is on". Two consequences:

- The tool list and system prompt stay byte-frozen and cacheable; the snapshot
  is appended per request, after them. Interpolating the snapshot into the
  system prompt would invalidate the cache on every single ask.
- Re-reading devices for every `snapshot()` adds avoidable latency. The
  proposed background poller lets Ask attach the cached state without
  issuing another set of device reads.

### The provider decision is smaller than it looks

At this volume the cost difference between vendors is noise: roughly 3–4k input
tokens (cached, so a tenth of that) and ~100 output per ask, which is fractions
of a cent — single-digit dollars a year. So the choice is about tool-call
reliability and latency, not price.

What matters structurally is that it sits behind one module, `api/brain.py`,
with one function — `interpret(text, snapshot) -> Plan` — so the vendor is one
file. Worth knowing: DeepSeek and Kimi both speak the OpenAI wire format, so
those two plus OpenAI are a single implementation, not three.

**Say this plainly, because the project has been careful about it elsewhere:**
what is typed, plus the rack snapshot including what is playing, leaves the
tailnet. The phrase table is the part that does not.

**Dependency:** 2.2.0's route lock, not 3.0. A model emitting a five-step plan
while a Music scene is 45 s into a TV wake is exactly the double-scene race the
audit found — and this feature makes issuing concurrent plans easy rather than
accidental. Given that, it could reasonably ship as 2.5.0; nothing in 3.0
through 5.0 is a prerequisite.

**Free voice:** iOS keyboard dictation on a text field gives voice input with
no Whisper, no audio pipeline and no second API. Worth knowing before anyone
designs a recording flow.

**Risk:** low, and unusually so for a feature that sounds large. The strict
enum means the failure mode is "it did not understand", not "it did the wrong
thing to the amp".

---

## 7.0.0 — Siri — there is no API, and that settles the design

Researched 2026-08-07. One fact governs everything else, so it goes first.

> **Siri cannot be handed an API.** Apple announced the new Siri on
> 2026-06-08 — in developer testing on iOS/iPadOS/macOS/visionOS 27, user beta
> later in 2026, English first, **not initially available in the EU**, and
> unavailable in China pending regulatory approval. It is built on onscreen
> awareness, personal context and App Intents, and works as a router that
> chains intents across apps. But there is no chat-completions endpoint, and a
> remote service cannot be registered as a Siri intermediary. Integration
> happens only through App Intents inside an **installed app**.

So the framing inverts. This is not "open the avctl API to Siri" — it is
making an app legible to the operating system. Which means **Siri depends on
5.0**, and 5.0's chosen path already clears the way: a personal provisioning
profile or internal TestFlight avoids App Review entirely, and App Intents work
fine there. The 2.1(a) demo-mode problem never arises.

### Three paths, and the cheapest is not Siri

**1. Shortcuts — works today, no app, no developer account.** A Shortcut with
`Get Contents of URL` posting to `/api/cmd` is voice-triggerable now. One
documented trap: shortcuts invoked *by voice* — and any created in the Home
app — can fail local-network resolution with "A server with the specified
hostname could not be found", while the same shortcut works when tapped. This
app is reached over `tailscale serve` at a MagicDNS name rather than a LAN
hostname, so whether that bites here is **a measurement, not an assumption**.
Cheapest possible experiment: one Shortcut, one `scene.off`, say it out loud.

**2. A HomeKit bridge — ranked first, despite being last to be thought of.**
`HAP-python` implements the accessory protocol in Python and ships a TV
accessory; the Homebridge AVR plugins expose a receiver as Power, Input, Volume
and Remote. This project is already Python with a driver layer that takes plain
values and an injected transport, so the bridge is a thin adapter over what
exists. It buys Siri, the Home app, Control Center, the lock screen and
automations at once — with no App Store, no developer account, no App Intents
and no cloud. Homebridge 2.0 (2026-05-04) added Matter, so the same bridge
could reach other ecosystems if that ever mattered.

The catch, named honestly: HomeKit models accessories with fixed
characteristics, not arbitrary commands. `scene.music` maps to a switch
cleanly; `dac.resync` maps to nothing. And the D900's open-loop tracked state
becomes **visible in the Home app**, which will confidently display a value
nobody read back. A bridge therefore exposes a deliberate subset, and the
subset boundary is where the tracked-vs-read distinction falls.

**3. App Intents — the actual 7.0.** Assistant Schemas are pre-shaped intents
Siri is trained on: 100+ actions across roughly twelve domains (media, browser,
camera, mail, photos, system and so on). There is no AV-rack domain, so this
would ship mostly custom intents — which still work; schemas only map natural
language better and add build-time validation. Usefully, **schemas can be built
and tested in the Shortcuts app today**, and Apple's stated position is that
the same intents later work with Siri automatically. So the work is verifiable
before Siri is even available.

And again the command table is the source of truth: an App Intent per scene,
plus parameterised intents for volume and input, all dispatching to
`POST /api/cmd`. No second backend, same as the 5.0 widgets.

### Watch item, not a plan

Apple began testing MCP support in iOS/iPadOS/macOS (spotted in the macOS 26.1
beta, 2025-09-22), explicitly laying groundwork to bring MCP to App Intents;
Xcode 27 shipped an MCP bridge, though that is developer tooling rather than a
system-wide client. iOS 27 also lets users choose the model behind Apple
Intelligence. If App Intents does gain MCP, 6.0's tool schema and this path
converge into one surface with two consumers — the phone's own panel, and Siri.
That would be a genuinely good outcome. It is also speculation, and is recorded
here as something to watch rather than something to build toward.

**Risk:** low for paths 1 and 2, and both are answerable this month. High
uncertainty on path 3 — not technically, but because its availability, region
support and quality are all outside this project's control.

---

## The through-line

Each version buys the next one's freedom. Tests (2.2.0) make the 3.0 refactor
survivable. The driver architecture (3.0) turns a music source into a config
line, which is the only thing that makes a Roon switch cheap. And the command
table that has been enforced since 1.0 — no button without a command — is what
lets a native widget in 5.0 call the rack without inventing a second API.

That last point keeps paying. The same table is what a model is allowed to
choose from in 6.0, and what an App Intent dispatches to in 7.0. Three
consumers, one list, one place where a device is touched — and in every case
the interesting design work was already done by refusing to let a control exist
without a command behind it.

Nothing here is urgent. 2.0 already does what the project set out to do.
