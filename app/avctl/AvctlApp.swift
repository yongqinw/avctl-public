import SwiftUI
import UIKit

@main
struct AvctlApp: App {
    @StateObject private var driver = ActivityDriver()
    @Environment(\.scenePhase) private var phase

    init() {
        PushRegistrar.start()
        BackgroundRefresh.register()
    }

    var body: some Scene {
        WindowGroup {
            // No pinned color scheme: the panel carries its own light and
            // dark palettes behind prefers-color-scheme, and pinning dark
            // here was exactly what kept the light one unreachable.
            ContentView()
                .onChange(of: phase) { _, now in
                    // Stage 2: the app feeds the island itself while it is
                    // on screen. Stage 4 hands this job to the server's
                    // pushes, and this becomes a fast-path, not the only path.
                    if now == .active {
                        driver.connect()
                        // Every foreground is a chance to warm the cover
                        // cache -- the island can only draw what the phone
                        // already holds.
                        CoverPrefetcher.shared.kick()
                    } else {
                        driver.disconnect()
                        // Ask for a background window while leaving; iOS
                        // grants them on its own schedule (#126).
                        BackgroundRefresh.schedule()
                    }
                }
        }
    }
}

struct ContentView: View {
    @Environment(\.scenePhase) private var phase
    @State private var musicLayout = ServerConfig.musicLayout
    @State private var panelColorScheme = ServerConfig.panelColorScheme
    @State private var connectionRevision = 0
    @State private var paired = ServerConfig.isPaired
    @StateObject private var coreDiscovery = CoreDiscovery()

    var body: some View {
        Group {
            if paired {
                PanelView(musicLayout: musicLayout,
                          panelColorScheme: panelColorScheme,
                          connectionRevision: connectionRevision,
                          active: phase == .active)
                    .ignoresSafeArea()
            } else {
                CoreSetupView(discovery: coreDiscovery,
                              colorScheme: panelColorScheme) {
                    paired = true
                    connectionRevision += 1
                }
            }
        }
            // The web panel owns both safe areas for tap-to-top. Native setup
            // keeps SwiftUI's safe areas so its first field never sits under
            // the Dynamic Island or home indicator.
            .background {
                Color(uiColor: panelColorScheme.nativePanelBackground)
                    .ignoresSafeArea()
            }
            .onReceive(NotificationCenter.default
                .publisher(for: .avctlAppearanceChanged)) { note in
                    guard let values = note.object as? [String: String],
                          let layoutValue = values["layout"],
                          let schemeValue = values["theme"],
                          let layout = ServerConfig.MusicLayout(
                            rawValue: layoutValue),
                          let scheme = ServerConfig.PanelColorScheme(
                            rawValue: schemeValue)
                    else { return }
                    musicLayout = layout
                    panelColorScheme = scheme
                }
            .onReceive(NotificationCenter.default
                .publisher(for: .avctlConnectionChanged)) { _ in
                    paired = ServerConfig.isPaired
                    connectionRevision += 1
                }
    }
}

extension ServerConfig.PanelColorScheme {
    /// Matches app.css's `--card`, which is the panel surface touching the
    /// native top safe area. Keep these six closed values beside the native
    /// appearance bridge so a newly-added palette cannot silently stay black.
    var nativePanelBackground: UIColor {
        func rgb(_ red: Int, _ green: Int, _ blue: Int) -> UIColor {
            UIColor(red: CGFloat(red) / 255,
                    green: CGFloat(green) / 255,
                    blue: CGFloat(blue) / 255,
                    alpha: 1)
        }
        switch self {
        case .glass:
            return UIColor { traits in
                traits.userInterfaceStyle == .dark
                    ? rgb(28, 28, 30) : .white
            }
        case .mcintosh: return rgb(16, 20, 17)
        case .porcelain: return rgb(255, 252, 245)
        case .midnight: return rgb(13, 26, 42)
        case .warm: return rgb(36, 25, 20)
        case .contrast: return .black
        }
    }
}

extension Notification.Name {
    static let avctlAppearanceChanged = Notification.Name(
        "avctlAppearanceChanged")
    static let avctlConnectionChanged = Notification.Name(
        "avctlConnectionChanged")
}
