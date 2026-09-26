import SwiftUI
import UIKit

struct CoreSetupView: View {
    @ObservedObject var discovery: CoreDiscovery
    let colorScheme: ServerConfig.PanelColorScheme
    let onPaired: () -> Void

    @State private var address = ""
    @State private var token = ""
    @State private var checking = false
    @State private var message = "Discover a nearby Core or enter its Tailscale address."
    @State private var failed = false

    init(discovery: CoreDiscovery,
         colorScheme: ServerConfig.PanelColorScheme,
         onPaired: @escaping () -> Void) {
        self.discovery = discovery
        self.colorScheme = colorScheme
        self.onPaired = onPaired
        // A fresh install starts empty. Recovery from an unreachable or
        // mistyped Core starts with the saved values so one character can be
        // corrected without destroying the last known configuration first.
        _address = State(initialValue: ServerConfig.server)
        _token = State(initialValue: ServerConfig.token)
    }

    var body: some View {
        GeometryReader { geometry in
            Group {
                if geometry.size.width >= 700 {
                    HStack(spacing: 0) {
                        setupRail.frame(width: 245)
                        Divider()
                        setupForm.frame(maxWidth: 720)
                            .frame(maxWidth: .infinity)
                    }
                } else {
                    setupForm
                }
            }
            .background(Color(uiColor: colorScheme.nativePanelBackground))
        }
        .task { discovery.start() }
        .onDisappear { discovery.stop() }
    }

    private var setupRail: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text("AVCTL").font(.caption.weight(.bold)).tracking(5)
            Label("Connect Core", systemImage: "checkmark.circle.fill")
                .foregroundStyle(accent)
            Label("Choose services", systemImage: "circle")
            Label("Configure panels", systemImage: "circle")
            Label("Verify access", systemImage: "circle")
            Spacer()
            Text("This rail remains on the left on iPad. Setup continues in the themed panel after pairing.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(30)
        .background(Color.primary.opacity(0.035))
    }

    private var setupForm: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Connect your Core")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                Text("Your Core owns device discovery and configuration. Tailscale is the private transport; avctl never asks for or stores your Tailscale account key.")
                    .foregroundStyle(.secondary)

                if !discovery.cores.isEmpty {
                    section("Nearby") {
                        ForEach(discovery.cores) { core in
                            Button {
                                address = core.url.absoluteString
                            } label: {
                                HStack {
                                    VStack(alignment: .leading) {
                                        Text(core.name).fontWeight(.semibold)
                                        Text(core.url.absoluteString)
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Image(systemName: address == core.url.absoluteString
                                          ? "checkmark.circle.fill" : "circle")
                                }
                                .padding(14)
                                .background(accent.opacity(0.09), in: RoundedRectangle(cornerRadius: 14))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }

                section("Core address") {
                    TextField("https://your-core.tailnet.ts.net", text: $address)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .padding(14)
                        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14))
                    SecureField("Bearer token (optional with Tailscale)", text: $token)
                        .textInputAutocapitalization(.never)
                        .padding(14)
                        .background(Color.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 14))
                }

                Text(message)
                    .font(.callout)
                    .foregroundStyle(failed ? Color.red : Color.secondary)

                HStack {
                    Button(discovery.searching ? "Searching…" : "Search again") {
                        discovery.stop(); discovery.start()
                    }
                    .buttonStyle(.bordered)
                    Spacer()
                    Button(checking ? "Checking…" : "Pair with Core") {
                        Task { await pair() }
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(accent)
                    .disabled(checking || address.trimmingCharacters(in: .whitespaces).isEmpty)
                }

                Link("Install or open Tailscale", destination: URL(string: "https://tailscale.com/download/ios")!)
                    .font(.footnote.weight(.semibold))
            }
            .padding(geometryPadding)
        }
    }

    private var geometryPadding: CGFloat { 28 }
    private var accent: Color { Color(uiColor: colorScheme.nativeAccent) }

    private func section<Content: View>(_ title: String,
                                        @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased()).font(.caption2.weight(.bold))
                .tracking(1.5).foregroundStyle(.secondary)
            content()
        }
    }

    @MainActor
    private func pair() async {
        checking = true; failed = false
        defer { checking = false }
        var raw = address.trimmingCharacters(in: .whitespacesAndNewlines)
        if !raw.contains("://") { raw = "https://" + raw }
        guard let base = URL(string: raw), base.host != nil,
              ["http", "https"].contains(base.scheme?.lowercased() ?? "") else {
            failed = true; message = "Enter a complete Core address."; return
        }
        do {
            var health = URLRequest(url: base.appendingPathComponent("healthz"))
            health.timeoutInterval = 8
            let (_, healthResponse) = try await URLSession.shared.data(for: health)
            guard (healthResponse as? HTTPURLResponse)?.statusCode == 200 else {
                throw PairError("That address answered, but it is not a healthy avctl Core.")
            }
            var who = URLRequest(url: base.appendingPathComponent("whoami"))
            who.timeoutInterval = 8
            if !token.isEmpty { who.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
            let (data, response) = try await URLSession.shared.data(for: who)
            guard (response as? HTTPURLResponse)?.statusCode == 200,
                  let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  json["authenticated"] as? Bool == true else {
                throw PairError("Core is reachable but did not authenticate this device. Join the same tailnet or enter the Core token.")
            }
            ServerConfig.server = raw
            ServerConfig.token = token
            ServerConfig.isPaired = true
            ServerConfig.needsSetupWizard = true
            message = "Paired. Continue with services and panels."
            onPaired()
        } catch {
            failed = true
            message = error.localizedDescription
        }
    }

    private struct PairError: LocalizedError {
        let text: String
        init(_ text: String) { self.text = text }
        var errorDescription: String? { text }
    }
}

extension ServerConfig.PanelColorScheme {
    var nativeAccent: UIColor {
        switch self {
        case .glass: return .systemGreen
        case .mcintosh: return UIColor(red: 0.36, green: 0.91, blue: 0.55, alpha: 1)
        case .porcelain: return UIColor(red: 0.56, green: 0.18, blue: 0.27, alpha: 1)
        case .midnight: return UIColor(red: 0.33, green: 0.78, blue: 1, alpha: 1)
        case .warm: return UIColor(red: 0.90, green: 0.59, blue: 0.35, alpha: 1)
        case .contrast: return .systemYellow
        }
    }
}
