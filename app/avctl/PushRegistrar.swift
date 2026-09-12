import ActivityKit
import Foundation

/// Stage 4's app half: every APNs token the system mints is handed to the
/// server, which does the actual pushing (api/phonelink.py). Two kinds:
/// the per-install push-to-start token (lets the mini raise an island with
/// the app closed) and a per-activity update token (lets it feed one).
enum PushRegistrar {
    static func start() {
        Task.detached {
            for await token in Activity<NowPlayingAttributes>.pushToStartTokenUpdates {
                await post(kind: "push_to_start", token: hex(token))
            }
        }
        Task.detached {
            // Fires for activities we start locally AND ones the server
            // push-to-starts -- iOS wakes the app briefly for the latter,
            // which is exactly the window this loop needs.
            for await activity in Activity<NowPlayingAttributes>.activityUpdates {
                Task {
                    for await token in activity.pushTokenUpdates {
                        await post(kind: "activity", token: hex(token),
                                   activityId: activity.id)
                    }
                }
            }
        }
    }

    private static func hex(_ token: Data) -> String {
        token.map { String(format: "%02x", $0) }.joined()
    }

    private static func post(kind: String, token: String,
                             activityId: String? = nil) async {
        guard var request = AvctlClient.request(path: "/api/phone/register",
                                                timeout: 10) else { return }
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: String] = ["kind": kind, "token": token]
        if let activityId { payload["activity_id"] = activityId }
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        // Best effort: if the tailnet is down the token is lost until the
        // next mint -- the system re-sends on every app launch, so a missed
        // one heals itself.
        _ = try? await URLSession.shared.data(for: request)
    }
}
