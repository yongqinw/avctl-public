import Foundation
import AppKit
import MusicKit

private struct BridgeCommand: Decodable {
    let id: String
    let action: String
    let catalogIDs: [String]?
    let developerToken: String?

    enum CodingKeys: String, CodingKey {
        case id, action
        case catalogIDs = "catalog_ids"
        case developerToken = "developer_token"
    }
}

private final class AvctlMusicTokenProvider: MusicUserTokenProvider,
    MusicDeveloperTokenProvider, @unchecked Sendable {
    private let token: String

    init(token: String) {
        self.token = token
        super.init()
    }

    func developerToken(options: MusicTokenRequestOptions) async throws -> String {
        token
    }
}

private struct BridgeReply: Encodable {
    let id: String
    let ok: Bool
    let code: String?
    let error: String?
    let state: PlayerState?
}

private struct PlayerState: Encodable {
    let state: String
    let catalogID: String?
    let track: String?
    let artist: String?
    let artwork: String?
    let position: Double
    let duration: Double?
    let shuffle: Bool
    let repeatOne: Bool

    enum CodingKeys: String, CodingKey {
        case state, track, artist, artwork, position, duration, shuffle
        case catalogID = "catalog_id"
        case repeatOne = "repeat_one"
    }
}

private enum BridgeFailure: LocalizedError {
    case unauthorized(MusicAuthorization.Status)
    case emptyQueue
    case unresolved([String])
    case unknownAction(String)

    var errorDescription: String? {
        switch self {
        case .unauthorized(let status):
            if status == .denied {
                return "Apple Music permission is off. On the Mac mini, open System Settings > Privacy & Security > Media & Apple Music, enable AvctlMusicBridge, then try again."
            }
            if status == .restricted {
                return "macOS is restricting Apple Music access for AvctlMusicBridge. Allow Media & Apple Music access on the Mac mini, then try again."
            }
            return "Apple Music needs permission on the Mac mini. Open AvctlMusicBridge once from the logged-in desktop, approve access, then try again."
        case .emptyQueue:
            return "no Apple Music catalog songs were supplied"
        case .unresolved(let ids):
            return "Apple Music could not resolve catalog IDs: \(ids.joined(separator: ","))"
        case .unknownAction(let action):
            return "unknown bridge action: \(action)"
        }
    }

    var code: String {
        switch self {
        case .unauthorized:
            return "authorization_required"
        case .emptyQueue:
            return "empty_queue"
        case .unresolved:
            return "unresolved_catalog_ids"
        case .unknownAction:
            return "unknown_action"
        }
    }
}

@main
struct AvctlMusicBridge {
    private static let player = ApplicationMusicPlayer.shared

    static func main() async {
        // Finder/open launches have no stdin commands. Their sole job is to
        // put the signed app in front and complete macOS's consent flow.
        // The API explicitly uses --stdio for the persistent bridge mode.
        if !CommandLine.arguments.contains("--stdio") {
            do {
                try await authorize()
            } catch {
                await showAuthorizationFailure(
                    (error as? LocalizedError)?.errorDescription
                        ?? "Apple Music authorization failed."
                )
            }
            return
        }
        while let line = readLine(strippingNewline: true) {
            guard let data = line.data(using: .utf8) else { continue }
            let command: BridgeCommand
            do {
                command = try JSONDecoder().decode(BridgeCommand.self, from: data)
            } catch {
                write(BridgeReply(
                    id: "invalid", ok: false,
                    code: "invalid_command",
                    error: "invalid bridge command", state: nil
                ))
                continue
            }
            do {
                let state = try await execute(command)
                write(BridgeReply(
                    id: command.id, ok: true, code: nil,
                    error: nil, state: state
                ))
            } catch {
                let failure = error as? BridgeFailure
                write(BridgeReply(
                    id: command.id, ok: false,
                    code: failure?.code ?? "playback_failed",
                    error: (error as? LocalizedError)?.errorDescription
                        ?? "Apple Music playback failed",
                    state: nil
                ))
            }
        }
        player.stop()
    }

    private static func authorize() async throws {
        var status = MusicAuthorization.currentStatus
        if status == .notDetermined {
            await MainActor.run {
                let app = NSApplication.shared
                app.setActivationPolicy(.accessory)
                app.activate(ignoringOtherApps: true)
            }
            status = await MusicAuthorization.request()
        }
        guard status == .authorized else {
            throw BridgeFailure.unauthorized(status)
        }
    }

    @MainActor
    private static func showAuthorizationFailure(_ message: String) {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Apple Music access is required"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    private static func songs(_ rawIDs: [String]?) async throws -> [Song] {
        let values = rawIDs ?? []
        guard !values.isEmpty else { throw BridgeFailure.emptyQueue }
        let ids = values.map { MusicItemID($0) }
        var request = MusicCatalogResourceRequest<Song>(
            matching: \.id, memberOf: ids
        )
        request.limit = ids.count
        let response = try await request.response()
        let byID: [String: Song] = Dictionary(uniqueKeysWithValues: response.items.map {
            ($0.id.rawValue, $0)
        })
        let missing = values.filter { byID[$0] == nil }
        guard missing.isEmpty else { throw BridgeFailure.unresolved(missing) }
        return values.compactMap { byID[$0] }
    }

    private static func execute(_ command: BridgeCommand) async throws -> PlayerState {
        try await authorize()
        if let token = command.developerToken, !token.isEmpty {
            MusicDataRequest.tokenProvider = AvctlMusicTokenProvider(token: token)
        }
        switch command.action {
        case "authorize":
            break
        case "replace":
            let resolved = try await songs(command.catalogIDs)
            player.queue = ApplicationMusicPlayer.Queue(for: resolved)
            try await player.play()
        case "append":
            let resolved = try await songs(command.catalogIDs)
            try await player.queue.insert(resolved, position: .tail)
        case "play":
            try await player.play()
        case "pause":
            player.pause()
        case "next":
            try await player.skipToNextEntry()
        case "previous":
            try await player.skipToPreviousEntry()
        case "shuffle_on":
            player.state.shuffleMode = .songs
        case "shuffle_off":
            player.state.shuffleMode = .off
        case "repeat_one_on":
            player.state.repeatMode = .one
        case "repeat_off":
            player.state.repeatMode = MusicPlayer.RepeatMode.none
        case "clear", "stop":
            player.stop()
            player.queue = ApplicationMusicPlayer.Queue(
                [] as [MusicPlayer.Queue.Entry]
            )
        case "state":
            break
        default:
            throw BridgeFailure.unknownAction(command.action)
        }
        return snapshot()
    }

    private static func snapshot() -> PlayerState {
        let entry = player.queue.currentEntry
        let item = entry?.item
        let duration: Double?
        switch item {
        case .song(let song): duration = song.duration
        default: duration = nil
        }
        let status: String
        switch player.state.playbackStatus {
        case .playing, .seekingForward, .seekingBackward: status = "playing"
        case .paused, .interrupted: status = "paused"
        default: status = "stopped"
        }
        return PlayerState(
            state: status,
            catalogID: item?.id.rawValue,
            track: entry?.title,
            artist: entry?.subtitle,
            artwork: entry?.artwork?.url(width: 400, height: 400)?.absoluteString,
            position: player.playbackTime,
            duration: duration,
            shuffle: player.state.shuffleMode == .songs,
            repeatOne: player.state.repeatMode == .one
        )
    }

    private static func write(_ reply: BridgeReply) {
        guard let data = try? JSONEncoder().encode(reply) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0a]))
    }
}
