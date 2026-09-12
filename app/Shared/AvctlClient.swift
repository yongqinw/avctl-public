import Foundation

/// The one door commands go through: POST /api/cmd with an id from the
/// server's command table. The island is just one more client of that table,
/// exactly like the browser.
enum AvctlClient {
    struct ServerError: Error {
        let status: Int
        let body: String
    }

    static func request(path: String, timeout: TimeInterval,
                        query: [String: String] = [:]) -> URLRequest? {
        guard let base = ServerConfig.baseURL else { return nil }
        var url = base.appending(path: path)
        if !query.isEmpty {
            // appending(path:) would percent-encode a "?", so the query
            // goes through URLComponents like it means it.
            guard var comps = URLComponents(url: url,
                                            resolvingAgainstBaseURL: false)
            else { return nil }
            comps.queryItems = query.map { URLQueryItem(name: $0.key,
                                                        value: $0.value) }
            guard let withQuery = comps.url else { return nil }
            url = withQuery
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        if !ServerConfig.token.isEmpty {
            request.setValue("Bearer \(ServerConfig.token)",
                             forHTTPHeaderField: "Authorization")
        }
        return request
    }

    /// Run one command. `background: true` asks the server to answer 202 and
    /// keep working -- for scenes, whose 45s is longer than an intent may
    /// live. The outcome still reaches the island via the state feed.
    static func cmd(_ id: String,
                    args: [String: String] = [:],
                    background: Bool = false,
                    timeout: TimeInterval = 5) async throws {
        guard var request = request(path: "/api/cmd", timeout: timeout) else {
            throw URLError(.badURL)
        }
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var payload: [String: Any] = ["cmd": id]
        if !args.isEmpty { payload["args"] = args }
        if background { payload["background"] = true }
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)

        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw ServerError(status: status,
                              body: String(decoding: data, as: UTF8.self))
        }
    }
}
