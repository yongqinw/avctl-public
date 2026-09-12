import Foundation

/// Warms the cover cache from the mini's recently-added shelf.
///
/// This is what makes the island's artwork actually appear: the widget may
/// only draw covers already on the phone's disk (no network in a widget
/// process, Apple's rule), and before this existed the only fill path was
/// "the currently playing track, while the app is open" -- a cache that was
/// empty in practice. Now the shelf's covers land in the App Group whenever
/// the app comes to the foreground, and again when the panel's Refresh
/// button reports the library changed.
actor CoverPrefetcher {
    static let shared = CoverPrefetcher()

    /// How much of the shelf to warm. The recently-added grid is what gets
    /// played; 80 albums ≈ a few MB of thumbnails, one-time cost per album.
    private let limit = 80
    private var running = false
    private var rerun = false

    nonisolated func kick() {
        Task { await self.run() }
    }

    private func run() async {
        // A kick that lands mid-run is coalesced, not dropped (#116): the
        // in-flight run fetched its shelf BEFORE the library changed, so it
        // cannot know about new albums -- one more pass after it finishes
        // can. Actor isolation makes this check-and-set race-free, and the
        // runs stay strictly sequential (the mini must never be hit by two
        // warm-ups at once).
        guard !running else {
            rerun = true
            return
        }
        running = true
        defer { running = false }
        repeat {
            rerun = false
            await warm()
        } while rerun
    }

    private func warm() async {

        guard let request = AvctlClient.request(
            path: "/api/music/recent", timeout: 15,
            query: ["limit": String(limit)]) else { return }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let shelf = try? JSONDecoder().decode(Shelf.self, from: data)
        else { return }

        for album in shelf.albums {
            guard let pid = album.pid,
                  ArtworkStore.image(for: pid) == nil else { continue }
            // Sequential on purpose: this is a background warm-up, and the
            // mini extracting covers should not be hammered in parallel.
            await ArtworkStore.ensure(pid)
        }
    }

    private struct Shelf: Decodable {
        var albums: [Album]
        struct Album: Decodable { var pid: String? }
    }
}
