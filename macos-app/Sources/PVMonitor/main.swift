import AppKit
import SwiftUI

// Command line modes, used for testing without a GUI:
//   PVMonitor --print      fetch once, print the figures and the colour verdict, exit
//   PVMonitor --selftest   check the averaging and colour rules, exit
if CommandLine.arguments.contains("--selftest") {
    exit(SelfTest.run() ? 0 : 1)
}
if CommandLine.arguments.contains("--print") {
    Task {
        do {
            let client = GrafanaClient(config: try Config.load())
            let snapshot = Snapshot.build(from: try await client.fetch())
            print(Report.text(snapshot))
            exit(0)
        } catch {
            fputs("Fehler: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
    RunLoop.main.run()
}

//   PVMonitor --snapshot out.png [red]   render the window with sample data to a PNG, exit
if let i = CommandLine.arguments.firstIndex(of: "--snapshot"), i + 1 < CommandLine.arguments.count {
    let path = CommandLine.arguments[i + 1]
    let red = CommandLine.arguments.contains("red")
    MainActor.assumeIsolated {
        _ = NSApplication.shared
        let now = Date().timeIntervalSince1970
        func s(_ v: Double) -> [Sample] { [Sample(time: now - 30, value: v)] }
        let data: [Series: [Sample]] = [
            .solarEdge: s(red ? 300 : 3700), .hoymiles800: s(red ? 50 : 143), .hoymiles1600: s(red ? 60 : 267),
            .house: s(red ? 1240 : 1100),
        ]
        let model = AppModel(preview: Snapshot.build(from: data))
        let renderer = ImageRenderer(content: ContentView(model: model)
            .frame(width: 280, height: 380).background(Color(nsColor: .windowBackgroundColor)))
        renderer.scale = 2
        if let image = renderer.nsImage, let tiff = image.tiffRepresentation,
           let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]) {
            try? png.write(to: URL(fileURLWithPath: path))
            print("wrote \(path)")
        } else {
            fputs("render failed\n", stderr)
            exit(1)
        }
    }
    exit(0)
}

/// Human-readable dump of a snapshot (used by --print).
enum Report {
    static func text(_ s: Snapshot) -> String {
        var lines = ["Produktion jetzt / 5-Min-Mittel"]
        for series in Series.production {
            lines.append("  \(series.title): \(formatPower(s.now[series])) / \(formatPower(s.mean[series]))")
        }
        lines.append("  Summe: \(formatPower(s.productionNow)) / \(formatPower(s.productionMean))"
                     + (s.productionComplete ? "" : "  (unvollständig)"))
        lines.append("Hausverbrauch: \(formatPower(s.houseNow)) / \(formatPower(s.houseMean))")
        let verdict: String
        switch s.state {
        case .red: verdict = "ROT (Hausverbrauch-Mittel > Produktions-Mittel)"
        case .normal: verdict = "normal"
        case .unknown: verdict = "unbekannt (Produktionsdaten unvollständig)"
        }
        lines.append("Farbe Hausverbrauch: \(verdict)")
        return lines.joined(separator: "\n")
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.regular)
    app.run()
}
