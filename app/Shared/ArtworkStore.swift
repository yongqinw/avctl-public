import Foundation
import ImageIO
import UIKit

/// Local album-cover cache: App Group disk first, the mini only on a miss.
///
/// The key is opaque here -- today it is Music.app's 16-hex persistent ID,
/// resolved at /api/music/artwork/<key>. A future source (Roon, say) mints
/// its own keys behind the same route contract, or never sets a key at all:
/// everything downstream degrades to the speaker glyph, never to an error.
///
/// Covers are content-addressed by key, so a cached file never goes stale --
/// eviction is about disk space, never correctness. The store lives in the
/// App Group because the widget extension may read files but may never touch
/// the network: the app fills the cache, the island reads it.
enum ArtworkStore {
    /// TRUE PIXELS, and modest ones: a Live Activity quietly refuses
    /// images past its own size and draws a gray box instead. The first
    /// two cache generations were secretly 3x too big -- the renderer
    /// defaults to the device scale, so "300" and "192" wrote 900px and
    /// 576px bitmaps, gray-boxed every time. v3 pinned the renderer to
    /// scale 1; v5 sizes the stored thumb for the LARGEST activity
    /// surface (the lock screen's 64pt card = 192px @3x). Smaller
    /// surfaces decode bounded via image(for:at:), never over-drawn.
    static let side: CGFloat = 192
    /// The widget-cover size: 84pt @3x. Bounded so the widget process --
    /// hard ~30MB ceiling -- never decodes original bytes; a 3000px
    /// embedded cover decodes to ~36MB RGBA and jetsams the extension
    /// mid-render (#115).
    static let largeSide: CGFloat = 252
    private static let capacity = 300   // covers kept before pruning

    /// Versioned so a thumbnail-size change orphans the old files instead
    /// of serving them: v5 = 192 real pixels for the 64pt island cover
    /// (v4 was 156 for the 52pt one; the cache refills itself from the
    /// mini on the next foreground).
    static var directory: URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: ServerConfig.appGroup)?
            .appending(path: "artwork-v5", directoryHint: .isDirectory)
    }

    /// The store's last word, for Settings: what the most recent save did,
    /// or exactly why it could not. The cache was invisible once and cost a
    /// day of guessing; never again.
    static var lastNote: String {
        UserDefaults(suiteName: ServerConfig.appGroup)?
            .string(forKey: "artworkNote") ?? "nothing attempted yet"
    }

    private static func note(_ line: String) {
        UserDefaults(suiteName: ServerConfig.appGroup)?
            .set(line, forKey: "artworkNote")
    }

    /// Three copies per cover: the island's thumb (156px -- all a Live
    /// Activity will deign to draw), a bounded widget-size copy (252px --
    /// what BigCover draws, sized so the widget process never touches the
    /// original), and the original bytes verbatim for a future full-screen
    /// surface in the APP process only. The fetch had the full bytes in
    /// hand anyway; keeping them costs disk, not network.
    enum Variant: String {
        case thumb = ".jpg"
        case large = "@large.jpg"
        case full = "@full.img"
    }

    static func file(for key: String, _ variant: Variant = .thumb) -> URL? {
        // The key becomes a filename: anything path-shaped is not a cover.
        guard key.range(of: "^[0-9A-Za-z._-]+$",
                        options: .regularExpression) != nil else { return nil }
        return directory?.appending(path: key + variant.rawValue)
    }

    /// What the island calls at render time: disk or nothing, by Apple's
    /// rules -- a widget process has no network to fall back to.
    static func image(for key: String?,
                      _ variant: Variant = .thumb) -> UIImage? {
        guard let key, let url = file(for: key, variant) else { return nil }
        return UIImage(contentsOfFile: url.path)
    }

    /// The thumb decoded to at most `pt` points (x3 true pixels): a Live
    /// Activity refuses images larger than they are drawn, and the island
    /// (52pt) and lock screen (64pt) render the same stored cover at
    /// different sizes -- each surface gets exactly the pixels it draws.
    static func image(for key: String?, at pt: CGFloat) -> UIImage? {
        guard let key, let url = file(for: key),
              let data = try? Data(contentsOf: url) else { return nil }
        return boundedImage(data, maxPixels: pt * 3)
    }

    /// Have the cover on disk when this returns true. Cheap when already
    /// cached; one fetch from the mini otherwise.
    @discardableResult
    static func ensure(_ key: String) async -> Bool {
        guard let url = file(for: key),
              let largeURL = file(for: key, .large),
              let fullURL = file(for: key, .full) else {
            note("refused key \(key)")
            return false
        }
        if FileManager.default.fileExists(atPath: url.path) {
            // Cache generations before the large variant hold thumb+full
            // only; backfill the widget-size copy from the held bytes (a
            // bounded decode, so this is safe even in the widget process).
            if !FileManager.default.fileExists(atPath: largeURL.path),
               let held = try? Data(contentsOf: fullURL),
               let large = boundedImage(held, maxPixels: largeSide),
               let jpeg = large.jpegData(compressionQuality: 0.85) {
                try? jpeg.write(to: largeURL, options: .atomic)
            }
            return true
        }
        guard let dir = directory else {
            note("no app-group container")
            return false
        }
        // A full copy without its thumb (a crash mid-save, an old cache):
        // regenerate from disk, no network needed.
        var data: Data
        if let held = try? Data(contentsOf: fullURL) {
            data = held
        } else {
            guard let request = AvctlClient.request(
                path: "/api/music/artwork/\(key)", timeout: 10) else {
                note("no server url configured")
                return false
            }
            guard let (fetched, response) = try? await URLSession.shared
                .data(for: request) else {
                note("fetch failed \(key) (network)")
                return false
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200 else {
                note("fetch \(key) -> HTTP \(status)")
                return false
            }
            data = fetched
        }
        // Bounded decodes ONLY (#115): ensure() also runs in the widget
        // extension, and UIImage(data:) on the original bytes materializes
        // the full bitmap against the ~30MB ceiling.
        guard let large = boundedImage(data, maxPixels: largeSide),
              let thumbImage = boundedImage(data, maxPixels: side) else {
            note("\(key): \(data.count)B not decodable as an image")
            return false
        }
        try? FileManager.default.createDirectory(
            at: dir, withIntermediateDirectories: true)
        // Plain files in the App Group container: they survive app
        // restarts, reboots and every update the deployer ships (only a
        // full delete of the app wipes them -- iOS wipes everything then).
        // Deliberately NOT under Library/Caches, so the system never purges
        // them under storage pressure; excluded from backup instead, since
        // a re-fetchable cover has no business in an iCloud backup.
        var dirValues = URLResourceValues()
        dirValues.isExcludedFromBackup = true
        var dirURL = dir
        try? dirURL.setResourceValues(dirValues)
        guard let jpeg = thumbImage.jpegData(compressionQuality: 0.85),
              let largeJpeg = large.jpegData(compressionQuality: 0.85) else {
            note("\(key): could not re-encode")
            return false
        }
        do {
            // Full and large first: the thumb doubles as the "this key is
            // done" marker, so it must land last.
            if !FileManager.default.fileExists(atPath: fullURL.path) {
                try data.write(to: fullURL, options: .atomic)
            }
            try largeJpeg.write(to: largeURL, options: .atomic)
            try jpeg.write(to: url, options: .atomic)
        } catch {
            note("write \(key): \(error.localizedDescription)")
            return false
        }
        note("ok \(key) \(Int(thumbImage.size.width * thumbImage.scale))px "
             + "+large +full \(data.count / 1024)KB")
        prune()
        return true
    }

    /// Decode at most `maxPixels` on the long side, straight from the
    /// encoded bytes -- CGImageSource never materializes the full bitmap,
    /// which is what makes this safe inside the widget process. The result
    /// carries scale 1: true pixels, like everything else in this store.
    private static func boundedImage(_ data: Data,
                                     maxPixels: CGFloat) -> UIImage? {
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixels,
            kCGImageSourceCreateThumbnailWithTransform: true,
        ]
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let cg = CGImageSourceCreateThumbnailAtIndex(
                  source, 0, options as CFDictionary) else { return nil }
        return UIImage(cgImage: cg)
    }

    /// Oldest-first eviction past the cap, counted and removed by KEY so a
    /// cover's thumb and full copy live and die together. Content-
    /// addressing makes this safe: dropping files only ever costs a
    /// re-fetch.
    private static func prune() {
        guard let dir = directory,
              let files = try? FileManager.default.contentsOfDirectory(
                  at: dir,
                  includingPropertiesForKeys: [.contentModificationDateKey])
        else { return }
        let thumbs = files.filter {
            $0.lastPathComponent.hasSuffix(".jpg")
                && !$0.lastPathComponent.hasSuffix(Variant.large.rawValue)
        }
        guard thumbs.count > capacity else { return }
        let dated = thumbs.map { url in
            (url, (try? url.resourceValues(
                forKeys: [.contentModificationDateKey]))?
                .contentModificationDate ?? .distantPast)
        }
        for (url, _) in dated.sorted(by: { $0.1 < $1.1 })
            .prefix(thumbs.count - capacity) {
            let key = url.deletingPathExtension().lastPathComponent
            try? FileManager.default.removeItem(at: url)
            for variant in [Variant.large, Variant.full] {
                if let sibling = file(for: key, variant) {
                    try? FileManager.default.removeItem(at: sibling)
                }
            }
        }
    }
}
