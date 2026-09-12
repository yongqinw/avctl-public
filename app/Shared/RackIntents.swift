import ActivityKit
import AppIntents
import WidgetKit

/// The island's buttons. `LiveActivityIntent` runs in the app's own process,
/// so these share ServerConfig and the URLSession with the panel -- and the
/// phone's Tailscale tunnel, without which /api/cmd is unreachable.
///
/// Failure is reflected into the island (`failed: true`) rather than thrown:
/// a Live Activity button has no error UI of its own, and silence would read
/// as success.
enum RackButton {
    /// `optimistic` rewrites the island's content the instant the button is
    /// pressed -- a play glyph that waits out the server round trip (poll ->
    /// APNs -> island, seconds) reads as a dead button. The server's next
    /// push is the truth and overwrites the guess either way.
    static func press(
        _ id: String,
        args: [String: String] = [:],
        background: Bool = false,
        optimistic: ((inout NowPlayingAttributes.ContentState) -> Void)? = nil
    ) async {
        if let optimistic {
            await rewrite { state in
                optimistic(&state)
                state.failed = false
            }
            // The home-screen widget reads PlaybackStore, not the activity:
            // give it the same guess and repaint it NOW, before the network
            // round trip -- a button that waits reads as dead there too.
            if var stored = PlaybackStore.load() {
                optimistic(&stored)
                stored.failed = false
                PlaybackStore.save(stored)
                WidgetCenter.shared.reloadTimelines(ofKind: HomeWidgetKind.kind)
            }
        }
        do {
            try await AvctlClient.cmd(id, args: args, background: background)
            await rewrite { $0.failed = false }
            // A 202 means the scene is still running (up to ~45s): the
            // reload below will fetch the PRE-command rack. The stamp keeps
            // the widget's next refresh close instead of 20 minutes out (#115).
            if background { ServerConfig.notePendingCommand() }
        } catch {
            await rewrite { $0.failed = true }
        }
        // The home-screen widget shows the same state; a press anywhere is
        // its cue to re-fetch rather than wait out the timeline budget.
        WidgetCenter.shared.reloadTimelines(ofKind: HomeWidgetKind.kind)
    }

    private static func rewrite(
        _ change: (inout NowPlayingAttributes.ContentState) -> Void
    ) async {
        for activity in Activity<NowPlayingAttributes>.activities {
            var state = activity.content.state
            change(&state)
            guard state != activity.content.state else { continue }
            await activity.update(
                ActivityContent(state: state,
                                staleDate: activity.content.staleDate))
        }
    }
}

struct PlayPauseIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Play or pause"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.play_pause") { state in
            state.playState = state.playing ? "paused" : "playing"
            ServerConfig.noteGuess(playState: state.playState)
        }
        return .result()
    }
}

struct NextTrackIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Next track"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.next")
        return .result()
    }
}

struct PrevTrackIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Previous track"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.prev")
        return .result()
    }
}

/// The mini walks its output volume in sixteenths (musiclink._vol_nudge:
/// 0, 6, 12, 19, ... 100). The optimistic guess walks the same lattice --
/// with the SAME rounding: Python's round() is banker's (12.5 -> 12), so
/// .toNearestOrEven here, or the guess and the confirming push disagree at
/// exactly the ties and the island flickers.
private func sixteenthNudge(_ volume: Int, _ direction: Int) -> Int {
    let step = max(0, min(16,
        Int((Double(volume) * 16 / 100).rounded(.toNearestOrEven)) + direction))
    return Int((Double(step) * 100 / 16).rounded(.toNearestOrEven))
}

struct MiniVolUpIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Mini volume up"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.vol.up") { state in
            if let volume = state.volume {
                state.volume = sixteenthNudge(volume, 1)
                ServerConfig.noteGuess(volume: state.volume)
            }
        }
        return .result()
    }
}

struct MiniVolDownIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Mini volume down"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.vol.down") { state in
            if let volume = state.volume {
                state.volume = sixteenthNudge(volume, -1)
                ServerConfig.noteGuess(volume: state.volume)
            }
        }
        return .result()
    }
}

/// The amp walks in twos (amplink.VOL_STEP). Same optimistic contract as
/// the mini: flip at the tap, let the confirming push agree.
struct AmpVolUpIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Amp volume up"
    func perform() async throws -> some IntentResult {
        await RackButton.press("amp.vol.up") { state in
            if let volume = state.ampVolume {
                state.ampVolume = min(100, volume + 2)
                ServerConfig.noteGuess(ampVolume: state.ampVolume)
            }
        }
        return .result()
    }
}

struct AmpVolDownIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Amp volume down"
    func perform() async throws -> some IntentResult {
        await RackButton.press("amp.vol.down") { state in
            if let volume = state.ampVolume {
                state.ampVolume = max(0, volume - 2)
                ServerConfig.noteGuess(ampVolume: state.ampVolume)
            }
        }
        return .result()
    }
}

/// The player pills on the large widget: verbs about THIS music, not the
/// rack -- they sit with the player, the deck below is rack-only.
struct StopClearIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Stop and clear the queue"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.clear")
        return .result()
    }
}

struct ShuffleIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Shuffle"
    func perform() async throws -> some IntentResult {
        await RackButton.press("music.shuffle")
        return .result()
    }
}

/// The widget's kind string, shared so the app and intents can nudge it.
enum HomeWidgetKind {
    static let kind = "avctl-nowplaying"
}

/// The quiet-rack widget's one button: wake the room. scene.music runs up
/// to 45s of TV wake, so background like AllOff.
struct MusicSceneIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Music mode"
    func perform() async throws -> some IntentResult {
        await RackButton.press("scene.music", background: true)
        return .result()
    }
}

/// A shelf cover tapped on the widget: play that album on the rack. The
/// parameters ride inside the intent instance the widget baked at render
/// time -- no configuration UI, the button IS the configuration.
struct PlayAlbumIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Play album"

    @Parameter(title: "Album") var album: String
    @Parameter(title: "Artist") var artist: String

    init() {}
    init(album: String, artist: String) {
        self.album = album
        self.artist = artist
    }

    func perform() async throws -> some IntentResult {
        // Dispatching a whole album can outlive an intent's budget the same
        // way a scene can.
        await RackButton.press("music.play_album",
                               args: ["album": album, "artist": artist],
                               background: true)
        return .result()
    }
}

/// The widget's rack row: the Home panel's verbs, one intent each. TV-on
/// rides Wake-on-LAN (up to 45s) so it backgrounds like a scene.
struct AmpOnIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Amp on"
    func perform() async throws -> some IntentResult {
        await RackButton.press("amp.power.on")
        return .result()
    }
}

struct AmpOffIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Amp off"
    func perform() async throws -> some IntentResult {
        await RackButton.press("amp.power.off")
        return .result()
    }
}

struct TVOnIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "TV on"
    func perform() async throws -> some IntentResult {
        await RackButton.press("tv.power.on", background: true)
        return .result()
    }
}

struct TVOffIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "TV off"
    func perform() async throws -> some IntentResult {
        await RackButton.press("tv.power.off")
        return .result()
    }
}

struct TVToMacIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "TV to the Mac"
    func perform() async throws -> some IntentResult {
        await RackButton.press("tv.input.hdmi2")
        return .result()
    }
}

struct TVToDiscIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "TV to the disc player"
    func perform() async throws -> some IntentResult {
        await RackButton.press("tv.input.hdmi1")
        return .result()
    }
}

struct AllOffIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Everything off"
    func perform() async throws -> some IntentResult {
        // A scene can hold the rack for 45s; background asks the server to
        // answer 202 and keep going, so the intent stays inside its budget.
        await RackButton.press("scene.off", background: true)
        return .result()
    }
}
