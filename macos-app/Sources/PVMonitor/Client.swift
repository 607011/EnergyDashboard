import Foundation

struct Config: Decodable {
    let url: String
    let token: String

    static let path = FileManager.default
        .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("PV Monitor/config.json")

    /// Environment variables PV_URL / PV_TOKEN win over the config file (handy for testing).
    static func load() throws -> Config {
        let env = ProcessInfo.processInfo.environment
        if let url = env["PV_URL"], let token = env["PV_TOKEN"] {
            return Config(url: url, token: token)
        }
        guard let data = try? Data(contentsOf: path) else { throw ClientError.noConfig }
        return try JSONDecoder().decode(Config.self, from: data)
    }
}

enum ClientError: LocalizedError {
    case noConfig
    case http(Int)
    case malformed

    var errorDescription: String? {
        switch self {
        case .noConfig: return "Keine Konfiguration gefunden"
        case .http(401), .http(403): return "Anmeldung abgelehnt (Token prüfen)"
        case .http(let code): return "Server antwortet mit HTTP \(code)"
        case .malformed: return "Unerwartete Antwort"
        }
    }
}

/// Reads the time series through Grafana's query API (a read-only Viewer token is enough), so the
/// app needs neither Redis access nor the Pi's network: just the same HTTPS address as the browser.
struct GrafanaClient {
    let config: Config
    let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 8
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    var baseURL: URL { URL(string: config.url.hasSuffix("/") ? String(config.url.dropLast()) : config.url)! }

    /// The last 15 minutes of every series. Averages are computed here, from raw points.
    func fetch() async throws -> [Series: [Sample]] {
        let refIds = Dictionary(uniqueKeysWithValues: zip(Series.allCases, ["A", "B", "C", "D"]))
        let queries = Series.allCases.map { series -> [String: Any] in
            [
                "refId": refIds[series]!,
                "datasource": ["type": "redis-datasource", "uid": "solaredge-redis"],
                "type": "timeSeries",
                "command": "ts.range",
                "keyName": series.rawValue,
            ]
        }
        var request = URLRequest(url: baseURL.appendingPathComponent("api/ds/query"))
        request.httpMethod = "POST"
        request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "queries": queries, "from": "now-15m", "to": "now",
        ])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ClientError.malformed }
        guard http.statusCode == 200 else { throw ClientError.http(http.statusCode) }
        return try Self.parse(data, refIds: refIds)
    }

    static func parse(_ data: Data, refIds: [Series: String]) throws -> [Series: [Sample]] {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let results = root["results"] as? [String: Any] else { throw ClientError.malformed }
        var out: [Series: [Sample]] = [:]
        for (series, refId) in refIds {
            // A series that doesn't exist yet comes back as an error entry or without frames: no data.
            guard let result = results[refId] as? [String: Any],
                  let frames = result["frames"] as? [[String: Any]],
                  let values = ((frames.first?["data"] as? [String: Any])?["values"]) as? [[Any]],
                  values.count >= 2 else { out[series] = []; continue }
            let times = values[0].compactMap { ($0 as? NSNumber)?.doubleValue }
            let vals = values[1].compactMap { ($0 as? NSNumber)?.doubleValue }
            out[series] = zip(times, vals).map { Sample(time: $0 / 1000, value: $1) }
        }
        return out
    }
}
