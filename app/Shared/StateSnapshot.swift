import Foundation

/// Just enough of /api/state's shape for the phone. Every field optional:
/// a device readout the poller could not get arrives as null, and version
/// skew must read as "unknown", never as a decode failure. Shared because
/// two things consume the same endpoint: the app's SSE driver and the
/// home-screen widget's timeline provider.
struct Snapshot: Decodable {
    var devices: Devices
    var scene: String?
    var islandVolume: String?

    private enum CodingKeys: String, CodingKey {
        case devices
        case scene
        case islandVolume = "island_volume"
    }

    struct Devices: Decodable {
        var music: Music
        var amp: Amp?
        var tv: Tv?
        var dac: Dac?
    }
    struct Music: Decodable { var fields: MusicFields }
    struct Amp: Decodable {
        var power: Bool?
        var fields: AmpFields
    }
    struct Tv: Decodable {
        var power: Bool?
        var fields: TvFields
    }
    struct Dac: Decodable { var fields: DacFields }

    struct TvFields: Decodable { var input: String? }
    struct DacFields: Decodable { var input: String? }

    struct MusicFields: Decodable {
        var state: String?
        var track: String?
        var artist: String?
        var album: String?
        var position: Double?
        var duration: Double?
        var volume: Int?     // the mini's system output, 0..100
        var muted: Bool?
        var pid: String?     // persistent ID; doubles as the artwork key
        var queued: Int?     // unplayed tracks still ahead (drip-aware)
    }
    struct AmpFields: Decodable {
        var volume: Int?
        var muted: Bool?
    }

    /// The one mapping from wire shape to the island/widget contract.
    /// No optimistic guesses here -- callers that have guesses (the SSE
    /// driver) apply them on top.
    func contentState(at now: Date = Date()) -> NowPlayingAttributes.ContentState? {
        let music = devices.music.fields
        guard let play = music.state,
              play == "playing" || play == "paused" else { return nil }
        var trackStart: Date?
        var trackEnd: Date?
        if let position = music.position, let duration = music.duration,
           duration > 0 {
            trackStart = now.addingTimeInterval(-position)
            trackEnd = trackStart!.addingTimeInterval(duration)
        }
        return NowPlayingAttributes.ContentState(
            track: music.track ?? "—",
            artist: music.artist ?? "",
            album: music.album ?? "",
            playState: play,
            volume: music.volume,
            muted: music.muted ?? false,
            ampVolume: devices.amp?.fields.volume,
            ampMuted: devices.amp?.fields.muted,
            volumeStyle: islandVolume,
            trackStart: trackStart,
            trackEnd: trackEnd,
            failed: false,
            artworkKey: music.pid)
    }
}

/// The rack at a glance, for the widget's status strip: read-only truth
/// from the same /api/state fetch the player rides. Codable so the widget
/// can HOLD the last strip it saw across failed fetches -- a tunnel that
/// is down at refresh time used to blank the strip entirely (#135).
struct RackStatus: Codable {
    var scene: String?
    var tvPower: Bool?
    var tvInput: String?
    var dacInput: String?
    var ampPower: Bool?
    var ampVolume: Int?
    var miniVolume: Int?
    var queued: Int?     // for the player band's read-only queue pill
}

extension Snapshot {
    func rackStatus() -> RackStatus {
        RackStatus(
            scene: scene,
            tvPower: devices.tv?.power,
            tvInput: devices.tv?.fields.input,
            dacInput: devices.dac?.fields.input,
            ampPower: devices.amp?.power,
            ampVolume: devices.amp?.fields.volume,
            miniVolume: devices.music.fields.volume,
            queued: devices.music.fields.queued)
    }
}

/// The last playback state anyone saw, in the App Group: the widget's
/// fallback when the tailnet is unreachable at refresh time, written by
/// whoever learned something newest (the SSE driver, the widget provider).
enum PlaybackStore {
    private static let key = "lastPlayback"
    private static let atKey = "lastPlaybackAt"

    static func save(_ state: NowPlayingAttributes.ContentState) {
        guard let data = try? JSONEncoder().encode(state) else { return }
        let defaults = UserDefaults(suiteName: ServerConfig.appGroup)
        defaults?.set(data, forKey: key)
        defaults?.set(Date().timeIntervalSince1970, forKey: atKey)
    }

    static func load() -> NowPlayingAttributes.ContentState? {
        guard let data = UserDefaults(suiteName: ServerConfig.appGroup)?
            .data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(
            NowPlayingAttributes.ContentState.self, from: data)
    }

    static func clear() {
        let defaults = UserDefaults(suiteName: ServerConfig.appGroup)
        defaults?.removeObject(forKey: key)
        defaults?.removeObject(forKey: atKey)
    }

    /// Seconds since the stored state was written, or nil if never. What
    /// keeps a held "playing" honest: hours-old playback shown as current
    /// is a ghost player with a dead progress bar (#135).
    static var age: TimeInterval? {
        let at = UserDefaults(suiteName: ServerConfig.appGroup)?
            .double(forKey: atKey) ?? 0
        guard at > 0 else { return nil }
        return Date().timeIntervalSince1970 - at
    }
}

/// The strip's twin of PlaybackStore: the last rack readout anyone saw.
/// Held across failed fetches so the strip never blanks -- it dims to
/// "held" honesty instead (#135).
enum RackStore {
    private static let key = "lastRack"

    static func save(_ rack: RackStatus) {
        guard let data = try? JSONEncoder().encode(rack) else { return }
        UserDefaults(suiteName: ServerConfig.appGroup)?.set(data, forKey: key)
    }

    static func load() -> RackStatus? {
        guard let data = UserDefaults(suiteName: ServerConfig.appGroup)?
            .data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(RackStatus.self, from: data)
    }
}
