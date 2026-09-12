import ActivityKit
import Foundation

/// The island's data contract, shared verbatim between the app (which starts
/// and feeds activities) and the widget extension (which renders them). In
/// stage 4 the server's APNs payloads must encode exactly this shape.
struct NowPlayingAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var track: String
        var artist: String
        var album: String
        var playState: String        // "playing" | "paused"
        /// The mini's system output -- the volume actually ridden during
        /// music, and what music.vol.up/down nudge.
        var volume: Int?
        var muted: Bool
        /// The amp's knob (MAC7200). Not drawn today; optional so payloads
        /// from older servers still decode.
        var ampVolume: Int?
        var ampMuted: Bool?
        /// "c+" (fader, default) or "c" (plain readout) -- config.yaml
        /// phone.island_volume decides, and the style riding the state is
        /// what lets a config edit reskin the island with no app rebuild.
        var volumeStyle: String?
        /// Track progress as wall-clock dates, so the island's bar advances
        /// on the phone's own clock between pushes -- one push per track,
        /// not one per second.
        var trackStart: Date?
        var trackEnd: Date?
        /// Raised by an intent whose POST failed; cleared by the next state
        /// update. An intent has no other voice than the island itself.
        var failed: Bool
        /// Opaque cover key for ArtworkStore (today: the track's Music.app
        /// persistent ID). Optional twice over: a source may not mint keys,
        /// and a key the phone has not cached renders as the glyph.
        var artworkKey: String?

        var playing: Bool { playState == "playing" }
    }

    /// Static for the activity's whole life: which server raised it.
    var server: String
}
