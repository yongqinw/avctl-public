import SwiftUI
import UIKit
import WebKit

/// The panel is the web panel -- one UI, two doors. This view is the app's
/// door; the tailnet URL in Safari stays the other. Native code begins where
/// the web cannot go: the island, the intents, the pushes.
struct PanelView: UIViewRepresentable {
    let musicLayout: ServerConfig.MusicLayout
    let panelColorScheme: ServerConfig.PanelColorScheme
    let connectionRevision: Int
    let active: Bool

    func makeCoordinator() -> Coordinator { Coordinator() }

    /// A web view that announces its FIRST real layout. The launch bug,
    /// round two: loading at frame zero wedged the viewport units (tall
    /// buttons, #134) -- but gating the load on updateUIView assumed
    /// SwiftUI re-calls it after layout, which it does not promise, and
    /// the panel sometimes stayed blank instead. layoutSubviews is the
    /// one place that provably runs when the bounds become real.
    final class PanelWebView: WKWebView {
        var onFirstRealLayout: ((PanelWebView) -> Void)?
        var onRetryConnection: (() -> Void)?
        var onChangeCore: (() -> Void)?
        private var connectionOverlay: UIView?

        override func layoutSubviews() {
            super.layoutSubviews()
            if !bounds.isEmpty, let fire = onFirstRealLayout {
                onFirstRealLayout = nil
                fire(self)
            }
        }

        func showConnectionError() {
            guard connectionOverlay == nil else { return }
            let overlay = UIView()
            overlay.translatesAutoresizingMaskIntoConstraints = false
            overlay.backgroundColor = backgroundColor

            let spinner = UIActivityIndicatorView(style: .medium)
            spinner.startAnimating()
            let title = UILabel()
            title.text = "Connecting to avctl…"
            title.font = .preferredFont(forTextStyle: .headline)
            title.textColor = .label
            title.textAlignment = .center
            title.numberOfLines = 0
            let detail = UILabel()
            detail.text = "Waiting for the network or Tailscale. Retrying automatically."
            detail.font = .preferredFont(forTextStyle: .body)
            detail.textColor = .secondaryLabel
            detail.textAlignment = .center
            detail.numberOfLines = 0
            let retry = UIButton(type: .system)
            retry.setTitle("Try Again", for: .normal)
            retry.titleLabel?.font = .preferredFont(forTextStyle: .headline)
            retry.accessibilityIdentifier = "retry-core-connection"
            retry.addAction(UIAction { [weak self] _ in
                self?.onRetryConnection?()
            }, for: .touchUpInside)

            let change = UIButton(type: .system)
            change.setTitle("Change Core", for: .normal)
            change.titleLabel?.font = .preferredFont(forTextStyle: .headline)
            change.accessibilityIdentifier = "change-core"
            change.addAction(UIAction { [weak self] _ in
                self?.onChangeCore?()
            }, for: .touchUpInside)

            let actions = UIStackView(arrangedSubviews: [retry, change])
            actions.axis = .horizontal
            actions.alignment = .center
            actions.distribution = .fillEqually
            actions.spacing = 12

            let stack = UIStackView(arrangedSubviews: [spinner, title, detail,
                                                        actions])
            stack.axis = .vertical
            stack.alignment = .center
            stack.spacing = 12
            stack.translatesAutoresizingMaskIntoConstraints = false
            overlay.addSubview(stack)
            addSubview(overlay)
            NSLayoutConstraint.activate([
                overlay.leadingAnchor.constraint(equalTo: leadingAnchor),
                overlay.trailingAnchor.constraint(equalTo: trailingAnchor),
                overlay.topAnchor.constraint(equalTo: topAnchor),
                overlay.bottomAnchor.constraint(equalTo: bottomAnchor),
                stack.leadingAnchor.constraint(greaterThanOrEqualTo: overlay.leadingAnchor,
                                               constant: 32),
                stack.trailingAnchor.constraint(lessThanOrEqualTo: overlay.trailingAnchor,
                                                constant: -32),
                stack.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
                stack.centerYAnchor.constraint(equalTo: overlay.centerYAnchor),
                actions.widthAnchor.constraint(greaterThanOrEqualToConstant: 220),
                actions.widthAnchor.constraint(lessThanOrEqualToConstant: 260),
                retry.heightAnchor.constraint(greaterThanOrEqualToConstant: 44),
                change.heightAnchor.constraint(greaterThanOrEqualToConstant: 44),
            ])
            connectionOverlay = overlay
        }

        func hideConnectionError() {
            connectionOverlay?.removeFromSuperview()
            connectionOverlay = nil
        }
    }

    func makeUIView(context: Context) -> WKWebView {
        context.coordinator.musicLayout = musicLayout
        context.coordinator.panelColorScheme = panelColorScheme
        context.coordinator.appActive = active
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        // The page's channel to the native side: app.js posts here (behind
        // optional chaining, so Safari never notices the handler missing).
        config.userContentController.add(context.coordinator, name: "avctl")
        let view = PanelWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.scrollView.contentInsetAdjustmentBehavior = .never
        view.scrollView.bounces = false
        view.isOpaque = false
        // Match the web panel's selected card surface during navigation and
        // load too; otherwise the transparent web view flashes black/white.
        view.backgroundColor = panelColorScheme.nativePanelBackground
        view.allowsBackForwardNavigationGestures = false
        // No retain cycle: the closure captures the coordinator, and the
        // view hands ITSELF to the callback.
        view.onFirstRealLayout = { [coordinator = context.coordinator] webView in
            PanelView.loadIfNeeded(webView, coordinator)
        }
        view.onRetryConnection = { [coordinator = context.coordinator] in
            coordinator.retryConnection()
        }
        view.onChangeCore = { [coordinator = context.coordinator] in
            coordinator.changeCore()
        }
        return view
    }

    /// The one load path, callable from either trigger (first real layout,
    /// or a later updateUIView after Settings changed the server). Loads
    /// only with real bounds and only when the target actually changed.
    private static func loadIfNeeded(_ view: WKWebView,
                                     _ coordinator: Coordinator) {
        guard let base = ServerConfig.baseURL,
              !view.bounds.isEmpty,
              coordinator.loadedServer != ServerConfig.server ||
                coordinator.loadedToken != ServerConfig.token else { return }
        coordinator.loadedServer = ServerConfig.server
        coordinator.loadedToken = ServerConfig.token
        var request = URLRequest(url: base)
        // URLSession can authenticate the pairing probe with a bearer header,
        // but a WKWebView's document fetches and EventSource cannot inherit
        // that header. Exchange the saved bearer for the server's HttpOnly
        // cookie through the existing POST gate, keeping it out of the URL,
        // browser history, Referer headers, and access logs.
        if !ServerConfig.token.isEmpty {
            var form = URLComponents()
            form.queryItems = [URLQueryItem(name: "token",
                                            value: ServerConfig.token)]
            request.httpMethod = "POST"
            request.setValue("application/x-www-form-urlencoded",
                             forHTTPHeaderField: "Content-Type")
            request.httpBody = form.percentEncodedQuery?.data(using: .utf8)
        }
        view.load(request)
    }

    /// Retry the original panel request rather than WebKit's error page.
    /// `loadedServer` is set before navigation begins to prevent duplicate
    /// loads during SwiftUI updates, so a failed first load must explicitly
    /// clear it before entering the normal load path again.
    private static func retryLoad(_ view: WKWebView,
                                  _ coordinator: Coordinator) {
        coordinator.loadedServer = nil
        coordinator.loadedToken = nil
        loadIfNeeded(view, coordinator)
    }

    /// Appearance is native state, but the panel is the shared web UI. Keep
    /// the bridge deliberately tiny: closed enums become data attributes
    /// in app.js, with no reload and no second rendering implementation.
    private static func applyAppearance(_ view: WKWebView,
                                        _ coordinator: Coordinator) {
        let layout = coordinator.musicLayout.rawValue
        let scheme = coordinator.panelColorScheme.rawValue
        view.evaluateJavaScript(
            "window.avctlSetAppearance?.('\(layout)', '\(scheme)')")
    }

    /// The /app page's Install button is an itms-services: link -- iOS
    /// installs the update, not the web view. Hand it to the system so
    /// updating works from inside the app being updated.
    final class Coordinator: NSObject, WKNavigationDelegate,
                             WKScriptMessageHandler {
        /// The server string whose page is currently loaded (or loading).
        var loadedServer: String?
        var loadedToken: String?
        var musicLayout = ServerConfig.MusicLayout.docked
        var panelColorScheme = ServerConfig.PanelColorScheme.glass
        var appActive = false
        private weak var webView: WKWebView?
        private let voiceCapture = VoiceCapture()
        private var retryTask: Task<Void, Never>?
        private var navigationWatchdog: Task<Void, Never>?
        private var retryDelay: TimeInterval = 1
        private var loadNeedsRetry = false
        private var navigationInFlight = false

        override init() {
            super.init()
            voiceCapture.onEvent = { [weak self] event in
                self?.sendVoiceEvent(event)
            }
        }

        func webView(_ webView: WKWebView,
                     didFinish navigation: WKNavigation!) {
            self.webView = webView
            navigationInFlight = false
            navigationWatchdog?.cancel()
            navigationWatchdog = nil
            (webView as? PanelWebView)?.hideConnectionError()
            retryTask?.cancel()
            retryTask = nil
            retryDelay = 1
            loadNeedsRetry = false
            PanelView.applyAppearance(webView, self)
            if ServerConfig.needsSetupWizard {
                webView.evaluateJavaScript("window.avctlOpenSetup?.()")
            }
        }

        func webView(_ webView: WKWebView,
                     didStartProvisionalNavigation navigation: WKNavigation!) {
            // A result from the previous document must never land in the new
            // Ask page. The fresh JavaScript starts idle by definition.
            voiceCapture.cancel()
            self.webView = webView
            navigationInFlight = true
            (webView as? PanelWebView)?.showConnectionError()
            startNavigationWatchdog()
        }

        func webView(_ webView: WKWebView,
                     didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            if (error as NSError).code == NSURLErrorCancelled { return }
            navigationFailed(webView)
        }

        func webView(_ webView: WKWebView,
                     didFail navigation: WKNavigation!,
                     withError error: Error) {
            if (error as NSError).code == NSURLErrorCancelled { return }
            navigationFailed(webView)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            navigationFailed(webView)
        }

        /// A phone can foreground before its Tailscale tunnel is usable.
        /// Previously that one failed navigation was permanent because the
        /// requested server had already been recorded as loaded. Keep the
        /// error page only briefly, then retry with a bounded backoff.
        private func navigationFailed(_ webView: WKWebView) {
            self.webView = webView
            navigationInFlight = false
            navigationWatchdog?.cancel()
            navigationWatchdog = nil
            (webView as? PanelWebView)?.showConnectionError()
            loadNeedsRetry = true
            scheduleRetry()
        }

        /// WebKit does not always fail promptly when an iOS VPN route is
        /// present but black-holed. Turn that silent hang into the same
        /// bounded retry path as an explicit navigation error.
        private func startNavigationWatchdog() {
            navigationWatchdog?.cancel()
            guard appActive, navigationInFlight else { return }
            navigationWatchdog = Task { @MainActor [weak self] in
                do {
                    try await Task.sleep(for: .seconds(10))
                } catch {
                    return
                }
                guard let self, self.appActive, self.navigationInFlight,
                      let webView = self.webView else { return }
                webView.stopLoading()
                self.navigationFailed(webView)
            }
        }

        private func scheduleRetry(immediate: Bool = false) {
            guard appActive, loadNeedsRetry, retryTask == nil else { return }
            let delay = immediate ? 0 : retryDelay
            retryTask = Task { @MainActor [weak self] in
                guard let self else { return }
                if delay > 0 {
                    do {
                        try await Task.sleep(for: .seconds(delay))
                    } catch {
                        return
                    }
                }
                self.retryTask = nil
                guard self.appActive, self.loadNeedsRetry,
                      let webView = self.webView else { return }
                PanelView.retryLoad(webView, self)
                self.retryDelay = min(max(delay, 1) * 2, 30)
            }
        }

        func retryConnection() {
            guard let webView else { return }
            retryTask?.cancel()
            retryTask = nil
            navigationWatchdog?.cancel()
            navigationWatchdog = nil
            webView.stopLoading()
            loadNeedsRetry = true
            retryDelay = 1
            PanelView.retryLoad(webView, self)
        }

        /// Recovery must remain native: when the saved Core address is wrong,
        /// none of the server-hosted Settings UI exists to repair it.
        func changeCore() {
            stopRetrying()
            webView?.stopLoading()
            ServerConfig.isPaired = false
            NotificationCenter.default.post(
                name: .avctlConnectionChanged, object: nil)
        }

        private func sendVoiceEvent(_ event: [String: Any]) {
            guard JSONSerialization.isValidJSONObject(event),
                  let data = try? JSONSerialization.data(withJSONObject: event),
                  let json = String(data: data, encoding: .utf8)
            else { return }
            webView?.evaluateJavaScript("window.avctlVoiceEvent?.(\(json))")
        }

        private var coverCount: Int {
            guard let directory = ArtworkStore.directory,
                  let files = try? FileManager.default.contentsOfDirectory(
                    atPath: directory.path)
            else { return 0 }
            return files.filter { $0.hasSuffix(".jpg") }.count
        }

        private func sendSettingsEvent(message: String? = nil,
                                       error: String? = nil) {
            var event: [String: Any] = [
                "server": ServerConfig.server,
                "token_configured": !ServerConfig.token.isEmpty,
                "cover_count": coverCount,
                "cover_note": ArtworkStore.lastNote,
            ]
            if let message { event["message"] = message }
            if let error { event["error"] = error }
            guard JSONSerialization.isValidJSONObject(event),
                  let data = try? JSONSerialization.data(withJSONObject: event),
                  let json = String(data: data, encoding: .utf8)
            else { return }
            webView?.evaluateJavaScript("window.avctlNativeSettings?.(\(json))")
        }

        func userContentController(_ controller: WKUserContentController,
                                   didReceive message: WKScriptMessage) {
            guard message.name == "avctl",
                  let body = message.body as? [String: Any],
                  let event = body["event"] as? String
            else { return }
            switch event {
            case "library-refreshed":
                // The panel re-scanned recently-added; warm the cover cache
                // so the island can draw what the shelf just learned about.
                CoverPrefetcher.shared.kick()
            case "appearance-changed":
                guard let layoutValue = body["layout"] as? String,
                      let themeValue = body["theme"] as? String,
                      let layout = ServerConfig.MusicLayout(
                        rawValue: layoutValue),
                      let theme = ServerConfig.PanelColorScheme(
                        rawValue: themeValue)
                else { return }
                // Closed enums only: no CSS or script crosses this bridge.
                // Persist in the App Group so widgets and the next launch
                // share the exact appearance selected in the web workspace.
                ServerConfig.musicLayout = layout
                ServerConfig.panelColorScheme = theme
                musicLayout = layout
                panelColorScheme = theme
                webView?.backgroundColor = theme.nativePanelBackground
                NotificationCenter.default.post(
                    name: .avctlAppearanceChanged,
                    object: ["layout": layoutValue, "theme": themeValue])
            case "settings-request":
                sendSettingsEvent()
            case "settings-save":
                guard let server = body["server"] as? String,
                      server.count <= 2_048,
                      let components = URLComponents(string: server),
                      ["http", "https"].contains(components.scheme?.lowercased()),
                      components.host != nil
                else {
                    sendSettingsEvent(error: "Use a valid http:// or https:// address.")
                    return
                }
                let token = (body["token"] as? String ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                guard token.count <= 2_048 else {
                    sendSettingsEvent(error: "The bearer token is too long.")
                    return
                }
                ServerConfig.server = server.trimmingCharacters(
                    in: .whitespacesAndNewlines)
                ServerConfig.isPaired = true
                if body["clear_token"] as? Bool == true {
                    ServerConfig.token = ""
                } else if !token.isEmpty {
                    ServerConfig.token = token
                }
                sendSettingsEvent(message: "Saved on this device.")
                NotificationCenter.default.post(
                    name: .avctlConnectionChanged, object: nil)
            case "artwork-warm":
                CoverPrefetcher.shared.kick()
                sendSettingsEvent(message: "Artwork cache warm-up started.")
            case "setup-complete":
                ServerConfig.needsSetupWizard = false
            case "voice-start":
                guard let session = body["session"] as? String else { return }
                voiceCapture.begin(sessionID: session)
            case "voice-stop":
                voiceCapture.finish(send: true)
            case "voice-cancel":
                voiceCapture.cancel()
            default:
                break
            }
        }

        func cancelVoice() {
            voiceCapture.cancel()
        }

        func stopRetrying() {
            retryTask?.cancel()
            retryTask = nil
            navigationWatchdog?.cancel()
            navigationWatchdog = nil
        }

        func setAppActive(_ active: Bool) {
            guard appActive != active else { return }
            appActive = active
            if active {
                // WebKit may decline evaluateJavaScript while suspended, so
                // repeat the idempotent reset when it becomes runnable again.
                sendVoiceEvent(["state": "cancelled"])
                // If the first navigation raced Tailscale startup, returning
                // to the app is an explicit request to try it again now.
                scheduleRetry(immediate: true)
                startNavigationWatchdog()
            } else {
                voiceCapture.cancel()
                retryTask?.cancel()
                retryTask = nil
                navigationWatchdog?.cancel()
                navigationWatchdog = nil
            }
        }

        func webView(_ webView: WKWebView,
                     decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if let url = action.request.url, url.scheme == "itms-services" {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        _ = connectionRevision
        context.coordinator.setAppActive(active)
        // Both guards live in loadIfNeeded: real bounds (loading at frame
        // zero wedges viewport units -- tall buttons), and a changed server
        // string (reload only on first load or a Settings edit; the STRING,
        // not the URL host, per #116). The first-layout callback carries
        // the launch case; this call carries every later SwiftUI pass.
        PanelView.loadIfNeeded(view, context.coordinator)
        let changed = context.coordinator.musicLayout != musicLayout ||
            context.coordinator.panelColorScheme != panelColorScheme
        context.coordinator.musicLayout = musicLayout
        context.coordinator.panelColorScheme = panelColorScheme
        if changed {
            view.backgroundColor = panelColorScheme.nativePanelBackground
            PanelView.applyAppearance(view, context.coordinator)
        }
    }

    static func dismantleUIView(_ uiView: WKWebView,
                                coordinator: Coordinator) {
        coordinator.cancelVoice()
        coordinator.stopRetrying()
    }
}
