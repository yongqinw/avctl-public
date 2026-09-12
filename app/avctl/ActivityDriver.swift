import ActivityKit
import Foundation
import WidgetKit

/// Stage 2: while the app is on screen it feeds the Live Activity itself,
/// from the same /api/events stream the browser uses. This proves every
/// island state with no server changes and no APNs. Stage 4 teaches the
/// server to do this by push, and this driver becomes the low-latency path
/// for when the app happens to be open, not the only path.
@MainActor
final class ActivityDriver: ObservableObject {
    private var task: Task<Void, Never>?
    private var islandVolume: String?
    private var lastWidgetState: NowPlayingAttributes.ContentState?
    // The newest snapshot applied, for the artwork continuation below: a
    // delayed cover fetch must repaint from what is CURRENT, never from the
    // snapshot it happened to capture (#114).
    private var latest: (music: Snapshot.MusicFields, amp: Snapshot.AmpFields?)?

    func connect() {
        guard task == nil else { return }
        task = Task { await run() }
    }

    func disconnect() {
        task?.cancel()
        task = nil
        // The activity outlives the app on purpose -- that is its whole
        // point. Nothing is ended here.
    }

    /// nonisolated: the read loop must never run ON the main actor (#112).
    /// It used to -- and a server string that failed to parse made stream()
    /// return without a single suspension point, so the loop pinned the main
    /// thread at 100% and even disconnect() could no longer be scheduled to
    /// cancel it. Only apply() hops to the main actor now.
    private nonisolated func run() async {
        var backoff: Double = 3
        while !Task.isCancelled {
            var failed = false
            do {
                try await stream()
                // A clean EOF (server restart, proxy idle timeout) is a
                // disconnect too -- fall through to the sleep.
            } catch is CancellationError {
                return
            } catch {
                failed = true
            }
            // Sleep on EVERY non-cancelled path: the old loop only slept on
            // throw, so clean EOFs reconnected in a zero-delay storm (#112).
            // staleDate dims the island meanwhile.
            do { try await Task.sleep(for: .seconds(backoff)) } catch { return }
            backoff = failed ? min(backoff * 2, 60) : 3
        }
    }

    private nonisolated func stream() async throws {
        guard var request = AvctlClient.request(path: "/api/events",
                                                timeout: 3600) else {
            // Unparseable server string (typo in Settings): throwing routes
            // it into run()'s backoff instead of a hot loop (#112).
            throw URLError(.badURL)
        }
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            // A 401/404 body is not an event stream; iterating it to EOF
            // just dressed the error up as a clean close (#112).
            throw URLError(.badServerResponse)
        }
        var isState = false
        for try await line in bytes.lines {
            if line.hasPrefix("event:") {
                isState = line.contains("state")
            } else if isState, line.hasPrefix("data:") {
                let data = Data(line.dropFirst(5)
                    .trimmingCharacters(in: .whitespaces).utf8)
                await apply(data)
                isState = false
            }
        }
    }

    private func apply(_ data: Data) {
        guard let snap = try? JSONDecoder().decode(Snapshot.self, from: data)
        else { return }
        let music = snap.devices.music.fields
        islandVolume = snap.islandVolume
        switch music.state {
        case "playing", "paused":
            update(music: music, amp: snap.devices.amp?.fields)
        case nil, "fastForwarding", "rewinding":
            // nil is StateSnapshot's contract for "a readout the poller
            // could not get" -- one flaky osascript must not tear down an
            // island mid-album (#114). Hold what is showing; staleDate dims
            // it if the silence persists. Scrubbing states likewise: the
            // show is not over.
            break
        default:
            // Stopped or quit: the show is over. Leave the final state on
            // the lock screen briefly, then clean up.
            endAll()
        }
    }

    private func update(music: Snapshot.MusicFields, amp: Snapshot.AmpFields?) {
        latest = (music, amp)
        // A stream echo that contradicts a guess made moments ago is a
        // readout that predates the command, not a correction -- honoring
        // it would snap the island backwards under the finger. Inside the
        // guess window the guess wins; after it, the server is the truth.
        var volume = music.volume
        var ampVolume = amp?.volume
        var playState = music.state ?? "paused"
        if let guess = ServerConfig.recentGuess {
            if let guessed = guess.volume, volume != guessed { volume = guessed }
            if let guessed = guess.ampVolume, ampVolume != guessed {
                ampVolume = guessed
            }
            if let guessed = guess.playState, playState != guessed {
                playState = guessed
            }
        }
        // Fill the cover cache on a miss, then re-run: the second pass finds
        // the file and the island repaints with it. One level deep -- a
        // fetch that fails leaves the glyph and no loop. The re-run uses the
        // DRIVER'S latest snapshot, not the captured one: the fetch can take
        // seconds, and repainting a track the user already skipped past
        // snapped the island (and the widget's stored state) backwards (#114).
        if let pid = music.pid, ArtworkStore.image(for: pid) == nil {
            Task { [weak self] in
                guard await ArtworkStore.ensure(pid), let self else { return }
                if let current = self.latest, current.music.pid == pid {
                    self.update(music: current.music, amp: current.amp)
                }
            }
        }
        let now = Date()
        var trackStart: Date?
        var trackEnd: Date?
        if let position = music.position, let duration = music.duration,
           duration > 0 {
            trackStart = now.addingTimeInterval(-position)
            trackEnd = trackStart!.addingTimeInterval(duration)
        }
        let state = NowPlayingAttributes.ContentState(
            track: music.track ?? "—",
            artist: music.artist ?? "",
            album: music.album ?? "",
            playState: playState,
            volume: volume,
            muted: music.muted ?? false,
            ampVolume: ampVolume,
            ampMuted: amp?.muted,
            volumeStyle: islandVolume,
            trackStart: trackStart,
            trackEnd: trackEnd,
            failed: false,
            artworkKey: music.pid)
        // Stale a beat after the next poll should have arrived: a dimmed
        // island says "held", never a wrong one saying "current".
        let content = ActivityContent(state: state,
                                      staleDate: now.addingTimeInterval(90))

        // The home-screen widget shares this state. Persist + nudge only
        // when something it shows moved -- the timeline budget is real.
        if state.track != lastWidgetState?.track
            || state.artist != lastWidgetState?.artist
            || state.album != lastWidgetState?.album
            || state.playState != lastWidgetState?.playState
            || state.volume != lastWidgetState?.volume
            || state.muted != lastWidgetState?.muted
            || state.volumeStyle != lastWidgetState?.volumeStyle
            || state.artworkKey != lastWidgetState?.artworkKey {
            lastWidgetState = state
            PlaybackStore.save(state)
            WidgetCenter.shared.reloadTimelines(ofKind: HomeWidgetKind.kind)
        }

        if let activity = Activity<NowPlayingAttributes>.activities.first {
            Task { await activity.update(content) }
        } else if ActivityAuthorizationInfo().areActivitiesEnabled {
            // pushType .token: even a locally-started activity gets an APNs
            // token, so the server can keep feeding it after the app leaves
            // the screen (PushRegistrar posts the token as it mints).
            _ = try? Activity.request(
                attributes: NowPlayingAttributes(server: ServerConfig.server),
                content: content,
                pushType: .token)
        }
    }

    private func endAll() {
        // The stream has a fresh, explicit stopped state. Remove the held
        // player before repainting so a widget placeholder cannot revive the
        // last song while its own network refresh is still pending.
        lastWidgetState = nil
        PlaybackStore.clear()
        WidgetCenter.shared.reloadTimelines(ofKind: HomeWidgetKind.kind)
        for activity in Activity<NowPlayingAttributes>.activities {
            Task {
                await activity.end(activity.content,
                                   dismissalPolicy: .after(.now + 30))
            }
        }
    }
}

// Snapshot (the /api/state wire shape) lives in Shared/StateSnapshot.swift:
// the home-screen widget's provider decodes the same endpoint.
