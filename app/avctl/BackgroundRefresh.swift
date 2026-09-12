import BackgroundTasks
import Foundation
import WidgetKit

/// Opportunistic resync while the app is suspended (#126).
///
/// iOS grants these windows on its own schedule -- minutes to hours apart,
/// weighted by when it predicts the app will be opened -- so this is the
/// belt, not the mechanism: widget pushes from the mini are what keep the
/// widget honest. What a granted window buys: a fresh /api/state lands in
/// PlaybackStore, the widget repaints, and the cover for whatever is playing
/// gets cached before the island needs it.
///
/// Declaring the `fetch` background mode is also what makes avctl appear
/// under Settings > General > Background App Refresh at all.
enum BackgroundRefresh {
    static let taskId = (Bundle.main.bundleIdentifier ?? "org.avctl.remote")
        + ".refresh"

    static func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: taskId,
                                        using: nil) { task in
            handle(task as! BGAppRefreshTask)
        }
    }

    /// Called on every background transition; BGTaskScheduler collapses
    /// duplicates, so asking often costs nothing.
    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: taskId)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private static func handle(_ task: BGAppRefreshTask) {
        schedule()   // always re-arm; a missed window must not end the chain
        let work = Task {
            defer { task.setTaskCompleted(success: true) }
            guard let request = AvctlClient.request(path: "/api/state",
                                                    timeout: 15),
                  let (data, response) = try? await URLSession.shared
                      .data(for: request),
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let snap = try? JSONDecoder().decode(Snapshot.self,
                                                       from: data),
                  snap.devices.music.fields.state != nil
            else { return }
            if let state = snap.contentState() {
                PlaybackStore.save(state)
                if let key = state.artworkKey {
                    await ArtworkStore.ensure(key)
                }
            } else {
                PlaybackStore.clear()
            }
            WidgetCenter.shared.reloadTimelines(ofKind: HomeWidgetKind.kind)
        }
        task.expirationHandler = { work.cancel() }
    }
}
