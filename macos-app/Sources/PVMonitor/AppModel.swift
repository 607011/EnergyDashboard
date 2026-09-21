import Foundation
import Combine

/// Polls Grafana every 10 seconds and publishes the latest snapshot for the window.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var error: String?
    private var client: GrafanaClient?
    private var timer: Timer?
    private var refreshing = false

    var dashboardURL: URL? { client?.baseURL.appendingPathComponent("d/pv-overview") }
    var configPath: String { Config.path.path }
    var configured: Bool { client != nil }

    /// Fixed data for `--snapshot` (rendering the window without a network).
    convenience init(preview: Snapshot) {
        self.init()
        snapshot = preview
        client = GrafanaClient(config: Config(url: "https://example.invalid", token: ""))
    }

    func start() {
        do {
            client = GrafanaClient(config: try Config.load())
        } catch {
            self.error = error.localizedDescription
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        timer?.tolerance = 2
    }

    func refresh() {
        guard let client, !refreshing else { return }
        refreshing = true
        Task {
            defer { refreshing = false }
            do {
                snapshot = Snapshot.build(from: try await client.fetch())
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
