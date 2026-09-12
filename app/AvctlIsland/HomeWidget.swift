import AppIntents
import SwiftUI
import WidgetKit

/// The island, at home-screen scale. Same contract, same intents, two new
/// freedoms and one constraint: a widget's provider MAY fetch the network
/// (it pulls /api/state itself, over the tailnet), its canvas follows the
/// system's light/dark instead of the island's always-black, but it
/// refreshes on a system budget, not a stream. The progress bar still
/// advances live between refreshes, and every button press re-fetches --
/// the budget only governs how fast other rooms' changes land.
///
/// The large family is three bands (mocked and approved 2026-08-08):
/// the rack's status strip, the player, and a two-row deck of the Home
/// panel's verbs -- audio story first, video story second.
struct HomeNowPlayingWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: HomeWidgetKind.kind,
                            provider: NowPlayingProvider()) { entry in
            HomeWidgetView(entry: entry)
                .containerBackground(for: .widget) {
                    Color(uiColor: .systemBackground)
                }
        }
        .configurationDisplayName("The rack")
        .description("Status, player, and the controls that matter.")
        .supportedFamilies([.systemMedium, .systemLarge])
        // The widget's own push channel (#126): the mini nudges WidgetKit
        // when something the widget shows changes, so it stays honest while
        // the app is suspended -- the same trick the island has always used,
        // finally available to widgets.
        .pushHandler(WidgetPushRegistrar.self)
    }
}

/// The widget twin of PushRegistrar: hand every minted widget token to the
/// mini, which is the only party that knows when a reload is worth budget.
struct WidgetPushRegistrar: WidgetPushHandler {
    init() {}

    func pushTokenDidChange(_ pushInfo: WidgetPushInfo,
                            widgets: [WidgetInfo]) {
        let token = pushInfo.token.map { String(format: "%02x", $0) }.joined()
        Task {
            guard var request = AvctlClient.request(path: "/api/phone/register",
                                                    timeout: 10) else { return }
            request.httpMethod = "POST"
            request.setValue("application/json",
                             forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(
                withJSONObject: ["kind": "widget", "token": token])
            // Best effort, like every token: the system re-mints on
            // relevant changes, so a miss heals itself.
            _ = try? await URLSession.shared.data(for: request)
        }
    }
}

struct NowPlayingEntry: TimelineEntry {
    let date: Date
    let state: NowPlayingAttributes.ContentState?
    let rack: RackStatus?   // nil = the fetch failed; the strip hides
    let fresh: Bool         // false = tailnet unreachable, player is held
}

struct NowPlayingProvider: TimelineProvider {
    func placeholder(in context: Context) -> NowPlayingEntry {
        NowPlayingEntry(date: .now, state: PlaybackStore.load(),
                        rack: nil, fresh: false)
    }

    func getSnapshot(in context: Context,
                     completion: @escaping (NowPlayingEntry) -> Void) {
        completion(placeholder(in: context))
    }

    func getTimeline(in context: Context,
                     completion: @escaping (Timeline<NowPlayingEntry>) -> Void) {
        Task {
            // A reload arriving moments after a button press is the press's
            // own repaint: the stored state already wears the optimistic
            // guess, and a network fetch now would race the very command
            // the guess belongs to. Paint from the store instantly and
            // reconcile on a quick follow-up refresh instead.
            if let age = ServerConfig.guessAge, age < 3 {
                completion(Timeline(
                    entries: [NowPlayingEntry(date: .now,
                                              state: PlaybackStore.load(),
                                              // Held, not nil: the strip
                                              // used to blink out for 15s
                                              // after every press (#135).
                                              rack: RackStore.load(),
                                              fresh: true)],
                    policy: .after(.now + 15)))
                return
            }
            var state = PlaybackStore.load()
            // The last strip anyone saw survives a failed fetch; a fresh
            // one below overwrites it.
            var rack = RackStore.load()
            var fresh = false
            // A snapshot whose music state is nil is a readout the mini
            // could NOT get (one flaky osascript) -- treat it like an
            // unreachable tailnet and hold what we had, rather than
            // painting a quiet rack that is actually mid-album (#114).
            if let snap = await Self.fetch(),
               snap.devices.music.fields.state != nil {
                state = snap.contentState()
                rack = snap.rackStatus()
                RackStore.save(snap.rackStatus())
                fresh = true
                // Inside the guess window a fetched readout may still
                // predate the command -- the guess wins, same rule as the
                // island's SSE driver.
                if let guess = ServerConfig.recentGuess {
                    if let guessed = guess.volume { state?.volume = guessed }
                    if let guessed = guess.ampVolume {
                        state?.ampVolume = guessed
                    }
                    if let guessed = guess.playState {
                        state?.playState = guessed
                    }
                }
                if let held = state {
                    PlaybackStore.save(held)
                } else {
                    // A fresh stopped readout is authoritative. Keeping the
                    // old store made later placeholders resurrect a ghost
                    // player until its 30-minute hold expired.
                    PlaybackStore.clear()
                }
                if let key = state?.artworkKey {
                    // The provider may touch the network; the views may
                    // not. This is the moment covers get fetched.
                    await ArtworkStore.ensure(key)
                }
            }
            // A held "playing" from hours ago is a ghost: dead progress
            // bar, often a pruned cover, standing where the quiet rack
            // should be. Fresh readouts say what is true; a stale hold
            // decays into the honest quiet band (#135).
            if !fresh, let heldAge = PlaybackStore.age, heldAge > 30 * 60 {
                state = nil
            }
            let entry = NowPlayingEntry(date: .now, state: state,
                                        rack: rack, fresh: fresh)
            // iOS budgets widget refreshes (~40-70/day) and quietly defers
            // blind polling -- which is how the title froze on the last
            // song while the bar kept moving. So AIM instead of poll: the
            // state knows when the current track ends; ask to refresh a
            // beat after that exact moment. One aimed refresh per track
            // spends budget where the pixels actually change. (Clamped:
            // never sooner than 45s, never later than 15min; paused or
            // quiet racks poll slow.)
            var refreshAt: Date
            if state?.playState == "playing", let end = state?.trackEnd {
                refreshAt = min(max(end.addingTimeInterval(4),
                                    .now + 45),
                                .now + 15 * 60)
            } else if state?.playState == "paused" {
                refreshAt = .now + 10 * 60
            } else {
                refreshAt = .now + 20 * 60
            }
            // A background command (scene.music, tv.power.on) answered 202
            // and is still running -- up to ~45s. The immediate post-press
            // refetch saw the PRE-command rack, and the quiet-rack branch
            // above would park the next look 20 minutes out (#115). Keep a
            // short leash until the scene has had time to land.
            if ServerConfig.pendingCommandAge != nil {
                refreshAt = min(refreshAt, .now + 60)
            }
            completion(Timeline(entries: [entry], policy: .after(refreshAt)))
        }
    }

    private static func fetch() async -> Snapshot? {
        guard let request = AvctlClient.request(path: "/api/state", timeout: 8),
              let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200
        else { return nil }
        return try? JSONDecoder().decode(Snapshot.self, from: data)
    }
}

struct HomeWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: NowPlayingEntry

    private var playing: Bool {
        let s = entry.state?.playState
        return s == "playing" || s == "paused"
    }

    var body: some View {
        if family == .systemLarge {
            // Top-anchored, deck pinned to the bottom edge: the old centered
            // stack split its slack into dead margins above and below. The
            // one flexible region is between the player and the deck.
            VStack(spacing: 12) {
                if let rack = entry.rack {
                    // Held readouts dim rather than vanish: a strip that
                    // blinks out reads as breakage, not honesty (#135).
                    StatusStrip(rack: rack).opacity(entry.fresh ? 1 : 0.55)
                }
                if let state = entry.state, playing {
                    PlayerBand(entry: entry, state: state)
                    Spacer(minLength: 0)
                } else {
                    QuietBand()
                }
                Deck()
            }
            .foregroundStyle(.primary)
        } else if let state = entry.state, playing {
            MediumNowPlaying(state: state)
        } else {
            MediumQuiet()
        }
    }
}

/// Band one: the rack at a glance -- the panel's status screen distilled.
/// Read-only truth; the deck below is how it changes.
private struct StatusStrip: View {
    let rack: RackStatus

    var body: some View {
        HStack {
            cell("SCENE", rack.scene?.uppercased() ?? "—",
                 lit: rack.scene != nil)
            cell("TV", rack.tvPower == false ? "OFF"
                 : (rack.tvInput?.uppercased() ?? "ON"))
            cell("DAC", rack.dacInput?.uppercased() ?? "—")
            cell("AMP", rack.ampPower == false ? "OFF"
                 : rack.ampVolume.map { "\($0)%" } ?? "ON")
            cell("MINI", rack.miniVolume.map { "\($0)%" } ?? "—")
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 6)
        .padding(.horizontal, 4)
        .background(RoundedRectangle(cornerRadius: 12)
            .fill(.primary.opacity(0.06)))
    }

    private func cell(_ key: String, _ value: String,
                      lit: Bool = false) -> some View {
        VStack(spacing: 1) {
            Text(key)
                .font(.system(size: 8.5, weight: .heavy))
                .kerning(0.8)
                .foregroundStyle(.tertiary)
            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(lit ? AnyShapeStyle(.green)
                                     : AnyShapeStyle(.primary))
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
    }
}

/// Band two, playing: the player. No volume bar on the right anymore --
/// MINI in the status strip is the readout, the deck's Vol keys are the
/// control -- so the titles take the full width. Music verbs (stop & clear,
/// shuffle) ride WITH the player as a slim pill row: they are about this
/// music, not the rack, and the queue count shows what "clear" would drop.
private struct PlayerBand: View {
    let entry: NowPlayingEntry
    let state: NowPlayingAttributes.ContentState

    var body: some View {
        VStack(spacing: 9) {
            HStack(spacing: 13) {
                BigCover(state: state, size: 84)
                VStack(alignment: .leading, spacing: 4) {
                    TrackTitles(state: state, alignment: .leading,
                                titleFont: .title3.bold(),
                                bylineFont: .footnote)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if !entry.fresh {
                        Text("held — tailnet unreachable")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
            }
            TimedProgress(state: state, ink: .primary)
            TransportRow(state: state, ink: .primary, showPower: false)
            HStack(spacing: 6) {
                SlimPill(intent: StopClearIntent(), icon: "stop.fill",
                         label: "Stop & clear", warm: true)
                SlimPill(intent: ShuffleIntent(), icon: "shuffle",
                         label: "Shuffle")
                if let queued = entry.rack?.queued, queued > 0 {
                    Text("\(queued) queued")
                        .font(.system(size: 10.5, weight: .semibold))
                        .monospacedDigit()
                        .foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity)
                }
            }
        }
    }
}

private struct SlimPill<I: LiveActivityIntent>: View {
    let intent: I
    let icon: String
    let label: String
    var warm = false

    var body: some View {
        Button(intent: intent) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 9, weight: .bold))
                Text(label)
                    .font(.system(size: 10.5, weight: .semibold))
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 7)
            .foregroundStyle(warm ? AnyShapeStyle(.red.opacity(0.85))
                                  : AnyShapeStyle(.primary))
            .background(Capsule().fill(.primary.opacity(0.07)))
        }
        .buttonStyle(.plain)
    }
}

/// Band two, quiet: the speakers keep the cover's seat, the doorbell waits.
private struct QuietBand: View {
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "hifispeaker.2")
                .font(.system(size: 34, weight: .light))
                .foregroundStyle(.secondary)
                .frame(width: 84, height: 84)
                .background(RoundedRectangle(cornerRadius: 14)
                    .fill(.primary.opacity(0.06)))
            VStack(alignment: .leading, spacing: 8) {
                Text("Quiet rack")
                    .font(.title3.weight(.semibold))
                Button(intent: MusicSceneIntent()) {
                    Label("Music mode", systemImage: "music.note")
                        .font(.callout.weight(.semibold))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(Capsule().fill(.primary.opacity(0.1)))
                }
                .buttonStyle(.plain)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxHeight: .infinity)
    }
}

/// Band three: the deck, icon-first. Row one is the sound story -- the
/// scene, the whole-rack off, the amp, the mini's volume nudges. Row two is
/// the picture story -- TV power and its two destinations. No amp-off key:
/// "All off" covers shutdown, and Music mode wakes the amp anyway.
/// Off-verbs wear the warm tint; the scene glows.
private struct Deck: View {
    var body: some View {
        VStack(spacing: 7) {
            HStack(spacing: 7) {
                RackKey(intent: MusicSceneIntent(), icon: "music.note",
                        label: "Music", tint: .accent)
                RackKey(intent: AllOffIntent(), icon: "power",
                        label: "All off", tint: .warm)
                RackKey(intent: AmpOnIntent(), icon: "hifispeaker.2.fill",
                        label: "Amp on")
                RackKey(intent: MiniVolDownIntent(), icon: "minus",
                        label: "Vol −")
                RackKey(intent: MiniVolUpIntent(), icon: "plus",
                        label: "Vol +")
            }
            HStack(spacing: 7) {
                RackKey(intent: TVOnIntent(), icon: "tv", label: "TV on")
                RackKey(intent: TVOffIntent(), icon: "tv.slash",
                        label: "TV off", tint: .warm)
                RackKey(intent: TVToMacIntent(), icon: "desktopcomputer",
                        label: "Mac")
                RackKey(intent: TVToDiscIntent(), icon: "opticaldisc",
                        label: "Disc")
            }
        }
    }
}

private struct RackKey<I: LiveActivityIntent>: View {
    enum Tint { case plain, accent, warm }

    let intent: I
    let icon: String
    let label: String
    var tint: Tint = .plain

    var body: some View {
        Button(intent: intent) {
            VStack(spacing: 3) {
                Image(systemName: icon)
                    .font(.system(size: 16, weight: .medium))
                    .frame(height: 18)
                // The icon carries the key; the micro-label is insurance for
                // the ambiguous pairs (which volume the -/+ nudge).
                Text(label)
                    .font(.system(size: 8.5, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 7)
            .foregroundStyle(tint == .warm ? AnyShapeStyle(.red.opacity(0.85))
                                           : AnyShapeStyle(.primary))
            .background(RoundedRectangle(cornerRadius: 12)
                .fill(tint == .accent ? AnyShapeStyle(.green.opacity(0.16))
                                      : AnyShapeStyle(.primary.opacity(0.07))))
        }
        .buttonStyle(.plain)
    }
}

/// systemMedium: the island's expanded card, almost literally. Unchanged
/// by the max-widget round -- it has no room for bands.
private struct MediumNowPlaying: View {
    let state: NowPlayingAttributes.ContentState

    var body: some View {
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                BigCover(state: state, size: 84)
                TrackTitles(state: state, alignment: .leading,
                            titleFont: .title3.weight(.semibold),
                            bylineFont: .footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
                VolumeControl(state: state, ink: .primary,
                              width: 46, height: 76)
            }
            TransportRow(state: state, ink: .primary)
        }
        .foregroundStyle(.primary)
    }
}

private struct MediumQuiet: View {
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "hifispeaker.2")
                .font(.system(size: 30, weight: .light))
                .foregroundStyle(.secondary)
                .frame(width: 72, height: 72)
                .background(RoundedRectangle(cornerRadius: 13)
                    .fill(.primary.opacity(0.06)))
            VStack(alignment: .leading, spacing: 8) {
                Text("Quiet rack")
                    .font(.headline)
                Button(intent: MusicSceneIntent()) {
                    Label("Music mode", systemImage: "music.note")
                        .font(.callout.weight(.semibold))
                        .padding(.horizontal, 14)
                        .padding(.vertical, 7)
                        .background(Capsule().fill(.primary.opacity(0.1)))
                }
                .buttonStyle(.plain)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .foregroundStyle(.primary)
    }
}

/// The bounded widget-size copy first -- NEVER the full original: decoding
/// arbitrary source bytes in the widget process risks the ~30MB jetsam
/// ceiling (#115). Thumb as the understudy, glyph as the last resort.
private struct BigCover: View {
    let state: NowPlayingAttributes.ContentState
    let size: CGFloat

    var body: some View {
        if let cover = ArtworkStore.image(for: state.artworkKey, .large)
            ?? ArtworkStore.image(for: state.artworkKey) {
            Image(uiImage: cover)
                .resizable()
                .scaledToFill()
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: size / 6))
        } else {
            Image(systemName: state.playing
                  ? "hifispeaker.2.fill" : "hifispeaker.2")
                .font(.title)
                .foregroundStyle(.primary)
                .frame(width: size, height: size)
        }
    }
}
