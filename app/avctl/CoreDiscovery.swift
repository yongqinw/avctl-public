import Foundation
import Combine

struct DiscoveredCore: Identifiable, Equatable {
    let id: String
    let name: String
    let url: URL
}

/// Bonjour only advertises a non-secret Core id/version/address. Authentication
/// still happens over the selected URL through Tailscale identity or a token.
@MainActor
final class CoreDiscovery: NSObject, ObservableObject,
                           @preconcurrency NetServiceBrowserDelegate,
                           @preconcurrency NetServiceDelegate {
    @Published private(set) var cores: [DiscoveredCore] = []
    @Published private(set) var searching = false

    private let browser = NetServiceBrowser()
    private var resolving: [NetService] = []

    override init() {
        super.init()
        browser.delegate = self
    }

    func start() {
        guard !searching else { return }
        searching = true
        cores = []
        browser.searchForServices(ofType: "_avctl._tcp.", inDomain: "local.")
    }

    func stop() {
        browser.stop()
        resolving.forEach { $0.stop() }
        resolving = []
        searching = false
    }

    func netServiceBrowser(_ browser: NetServiceBrowser,
                           didFind service: NetService,
                           moreComing: Bool) {
        service.delegate = self
        resolving.append(service)
        service.resolve(withTimeout: 5)
    }

    func netServiceBrowserDidStopSearch(_ browser: NetServiceBrowser) {
        searching = false
    }

    func netServiceBrowser(_ browser: NetServiceBrowser,
                           didNotSearch errorDict: [String: NSNumber]) {
        searching = false
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        defer { resolving.removeAll { $0 === sender } }
        let properties = sender.txtRecordData().map(
            NetService.dictionary(fromTXTRecord:)) ?? [:]
        let advertised = properties["url"].flatMap {
            String(data: $0, encoding: .utf8)
        }.flatMap(URL.init(string:))
        let fallback: URL? = {
            guard let host = sender.hostName else { return nil }
            return URL(string: "http://\(host):\(sender.port)")
        }()
        guard let url = advertised ?? fallback else { return }
        let coreID = properties["id"].flatMap {
            String(data: $0, encoding: .utf8)
        } ?? "\(sender.name)-\(url.absoluteString)"
        let candidate = DiscoveredCore(id: coreID, name: sender.name, url: url)
        if let index = cores.firstIndex(where: { $0.id == candidate.id }) {
            cores[index] = candidate
        } else {
            cores.append(candidate)
        }
    }

    func netService(_ sender: NetService,
                    didNotResolve errorDict: [String: NSNumber]) {
        resolving.removeAll { $0 === sender }
    }
}
