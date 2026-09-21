import SwiftUI

struct ContentView: View {
    @ObservedObject var model: AppModel
    @AppStorage("alwaysOnTop") private var alwaysOnTop = false

    private static let time: DateFormatter = {
        let f = DateFormatter()
        f.timeStyle = .medium
        return f
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let snapshot = model.snapshot {
                bigFigure(
                    title: "Produktion",
                    value: formatPower(snapshot.productionNow) + (snapshot.productionComplete ? "" : "*"),
                    color: .primary)
                bigFigure(
                    title: "Hausverbrauch",
                    value: formatPower(snapshot.houseNow),
                    color: snapshot.state == .red ? .red : .primary)
                if snapshot.state == .red {
                    Text("5-Min-Mittel liegt über der Produktion")
                        .font(.caption).foregroundColor(.red)
                } else if snapshot.state == .unknown {
                    Text("* Produktionsdaten unvollständig, kein Vergleich")
                        .font(.caption).foregroundColor(.secondary)
                }
                Divider()
                details(snapshot)
            } else if !model.configured {
                Text("Keine Konfiguration").font(.headline)
                Text("Erwartet in:\n\(model.configPath)").font(.caption).foregroundColor(.secondary)
                    .textSelection(.enabled)
            } else {
                ProgressView().controlSize(.small)
            }

            if let error = model.error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundColor(.red)
            }

            Spacer(minLength: 0)
            Divider()
            HStack {
                Toggle("Immer im Vordergrund", isOn: $alwaysOnTop)
                    .toggleStyle(.checkbox)
                Spacer()
                if let url = model.dashboardURL {
                    Button("Dashboard") { NSWorkspace.shared.open(url) }
                        .buttonStyle(.link)
                }
            }
            .font(.caption)
            if let snapshot = model.snapshot {
                Text("Stand \(Self.time.string(from: snapshot.fetchedAt))")
                    .font(.caption2).foregroundColor(.secondary)
            }
        }
        .padding(16)
        .frame(minWidth: 250, minHeight: 300)
    }

    private func bigFigure(title: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title).font(.caption).foregroundColor(.secondary)
            Text(value)
                .font(.system(size: 38, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .foregroundColor(color)
                .lineLimit(1).minimumScaleFactor(0.6)
        }
    }

    /// Per-system figures: current value and 5-minute mean.
    private func details(_ s: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("").frame(maxWidth: .infinity, alignment: .leading)
                Text("jetzt").frame(width: 62, alignment: .trailing)
                Text("Ø 5 Min").frame(width: 62, alignment: .trailing)
            }
            .font(.caption2).foregroundColor(.secondary)
            ForEach(Series.production, id: \.self) { series in
                row(series.shortTitle, s.now[series], s.mean[series])
            }
            row("Summe", s.productionNow, s.productionMean, bold: true)
            row("Hausverbrauch", s.houseNow, s.houseMean, bold: true, color: s.state == .red ? .red : .primary)
        }
    }

    private func row(_ title: String, _ now: Double?, _ mean: Double?,
                     bold: Bool = false, color: Color = .primary) -> some View {
        HStack {
            Text(title).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
            Text(formatPower(now)).frame(width: 62, alignment: .trailing)
            Text(formatPower(mean)).frame(width: 62, alignment: .trailing)
        }
        .font(.system(size: 12, weight: bold ? .semibold : .regular)).monospacedDigit()
        .foregroundColor(color)
    }
}
