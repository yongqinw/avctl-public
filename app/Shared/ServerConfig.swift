import Foundation

/// Where the rack lives. Kept in the App Group so an intent fired from the
/// island (a separate process) reaches the same server the panel uses.
enum ServerConfig {
    enum MusicLayout: String, CaseIterable, Identifiable {
        case docked
        case stage
        case coverFlow = "cover-flow"
        case splitDeck = "split-deck"

        var id: String { rawValue }

        var title: String {
            switch self {
            case .docked: "Docked"
            case .stage: "Stage"
            case .coverFlow: "Cover Flow"
            case .splitDeck: "Split Deck"
            }
        }

        var detail: String {
            switch self {
            case .docked: "Library with the full transport below"
            case .stage: "Now Playing leads the Music panel"
            case .coverFlow: "Swipe through recently added albums"
            case .splitDeck: "Library and transport queue share the stage"
            }
        }
    }

    enum PanelColorScheme: String, CaseIterable, Identifiable {
        case glass
        case mcintosh
        case porcelain
        case midnight
        case warm
        case contrast

        var id: String { rawValue }

        var title: String {
            switch self {
            case .glass: "Glass"
            case .mcintosh: "McIntosh"
            case .porcelain: "Porcelain"
            case .midnight: "Midnight"
            case .warm: "Warm Hi-Fi"
            case .contrast: "High Contrast"
            }
        }
    }

    static let appGroup = Bundle.main.object(
        forInfoDictionaryKey: "AVCTLAppGroup"
    ) as? String ?? "group.org.avctl.remote"
    // A distributable build must never send a friend's app to the developer's
    // rack. First launch discovers or asks for the owner's Core instead.
    static let defaultServer = ""

    private static var defaults: UserDefaults {
        UserDefaults(suiteName: appGroup) ?? .standard
    }

    static var server: String {
        get { defaults.string(forKey: "server") ?? defaultServer }
        set { defaults.set(newValue, forKey: "server") }
    }

    static var isPaired: Bool {
        // Existing installs already persisted a server before corePaired was
        // introduced. Treat that valid saved address as the migration signal
        // so an update does not strand the owner's working controller.
        get {
            guard baseURL != nil else { return false }
            if defaults.object(forKey: "corePaired") == nil { return true }
            return defaults.bool(forKey: "corePaired")
        }
        set { defaults.set(newValue, forKey: "corePaired") }
    }

    static var needsSetupWizard: Bool {
        get { defaults.bool(forKey: "needsSetupWizard") }
        set { defaults.set(newValue, forKey: "needsSetupWizard") }
    }

    /// The bearer-token fallback from api/auth.py -- empty when the tailnet
    /// is doing the authenticating, which is the normal state.
    static var token: String {
        get { defaults.string(forKey: "token") ?? "" }
        set { defaults.set(newValue, forKey: "token") }
    }

    static var musicLayout: MusicLayout {
        get {
            guard let raw = defaults.string(forKey: "musicLayout") else {
                return .docked
            }
            return MusicLayout(rawValue: raw) ?? .docked
        }
        set { defaults.set(newValue.rawValue, forKey: "musicLayout") }
    }

    static var panelColorScheme: PanelColorScheme {
        get {
            guard let raw = defaults.string(forKey: "panelColorScheme") else {
                return .glass
            }
            return PanelColorScheme(rawValue: raw) ?? .glass
        }
        set { defaults.set(newValue.rawValue, forKey: "panelColorScheme") }
    }

    static var baseURL: URL? {
        guard let components = URLComponents(string: server),
              ["http", "https"].contains(components.scheme?.lowercased()),
              components.host != nil else { return nil }
        return components.url
    }

    // --- optimistic-guess handshake -------------------------------------
    // An intent's guess and the server's echo race: a state readout that
    // arrives moments after a tap may predate the command it answers, and
    // repainting it would snap the island backwards under the finger. The
    // intent records its guess here (App Group: intents and app are
    // different moments, sometimes different processes); for a few seconds
    // a contradicting echo defers to the guess, then truth wins again.

    static let guessWindow: TimeInterval = 4

    /// Each field carries its own timestamp (#113): the old shared stamp
    /// meant any new guess refreshed EVERY stored field, so a volume tapped
    /// an hour ago came back from the dead whenever play/pause was pressed
    /// -- exactly the backwards snap this mechanism exists to prevent. Two
    /// guesses inside one window (amp volume then mini volume) stay live
    /// independently.
    static func noteGuess(volume: Int? = nil, ampVolume: Int? = nil,
                          playState: String? = nil) {
        let now = Date().timeIntervalSince1970
        if let volume {
            defaults.set(volume, forKey: "guessVolume")
            defaults.set(now, forKey: "guessVolumeAt")
        }
        if let ampVolume {
            defaults.set(ampVolume, forKey: "guessAmpVolume")
            defaults.set(now, forKey: "guessAmpVolumeAt")
        }
        if let playState {
            defaults.set(playState, forKey: "guessPlayState")
            defaults.set(now, forKey: "guessPlayStateAt")
        }
        // "Some guess just happened", for guessAge's fetch-suppression only.
        defaults.set(now, forKey: "guessAt")
    }

    private static func freshly(_ stampKey: String, at now: TimeInterval) -> Bool {
        let at = defaults.double(forKey: stampKey)
        return at > 0 && now - at < guessWindow
    }

    static var recentGuess: (volume: Int?, ampVolume: Int?, playState: String?)? {
        let now = Date().timeIntervalSince1970
        let volume = freshly("guessVolumeAt", at: now)
            ? defaults.object(forKey: "guessVolume") as? Int : nil
        let ampVolume = freshly("guessAmpVolumeAt", at: now)
            ? defaults.object(forKey: "guessAmpVolume") as? Int : nil
        let playState = freshly("guessPlayStateAt", at: now)
            ? defaults.string(forKey: "guessPlayState") : nil
        if volume == nil, ampVolume == nil, playState == nil { return nil }
        return (volume, ampVolume, playState)
    }

    // --- background-command handshake ------------------------------------
    // A 202 answer means "the scene is RUNNING", for up to ~45s. The widget
    // refetching the instant the POST returns sees the pre-command rack and
    // would then sleep 20 minutes on a "quiet" snapshot (#115). This stamp
    // tells its provider to keep a short leash until the rack has had time
    // to land where the command sent it.

    static let pendingCommandWindow: TimeInterval = 90

    static func notePendingCommand() {
        defaults.set(Date().timeIntervalSince1970, forKey: "pendingCmdAt")
    }

    /// Seconds since the last background command, or nil if none/expired.
    static var pendingCommandAge: TimeInterval? {
        let at = defaults.double(forKey: "pendingCmdAt")
        guard at > 0 else { return nil }
        let age = Date().timeIntervalSince1970 - at
        return age < pendingCommandWindow ? age : nil
    }

    /// Seconds since the last guess OF ANY FIELD, or nil if none/expired.
    /// The widget's provider uses this to skip a network fetch that would
    /// race the very command the guess belongs to -- an any-guess gate, so
    /// it deliberately keeps the shared stamp.
    static var guessAge: TimeInterval? {
        let at = defaults.double(forKey: "guessAt")
        guard at > 0 else { return nil }
        let age = Date().timeIntervalSince1970 - at
        return age < guessWindow ? age : nil
    }
}
