import Foundation

/// Checks the averaging and the colour rule; run with `PVMonitor --selftest`.
enum SelfTest {
    static func run() -> Bool {
        var failures = 0
        func check(_ name: String, _ ok: Bool) {
            print("\(ok ? "ok  " : "FAIL") \(name)")
            if !ok { failures += 1 }
        }
        func near(_ a: Double?, _ b: Double) -> Bool { a.map { abs($0 - b) < 1e-9 } ?? false }

        let now = 1_000_000.0
        // --- window mean
        let inside = [Sample(time: now - 250, value: 100), Sample(time: now - 100, value: 300), Sample(time: now - 5, value: 200)]
        check("Mittel über die Samples im Fenster", near(windowMean(inside, now: now), 200))
        let mixed = [Sample(time: now - 400, value: 9999)] + inside
        check("Samples älter als 5 Minuten zählen nicht", near(windowMean(mixed, now: now), 200))
        let sparse = [Sample(time: now - 420, value: 50)]
        check("kein Sample im Fenster: neuestes, wenn höchstens 15 Minuten alt", near(windowMean(sparse, now: now), 50))
        let stale = [Sample(time: now - 1000, value: 50)]
        check("zu altes Sample: unbekannt", windowMean(stale, now: now) == nil)
        check("leere Reihe: unbekannt", windowMean([], now: now) == nil)
        check("aktueller Wert = neuestes Sample", near(latestValue(inside, now: now), 200))

        // --- colour rule
        check("Verbrauch-Mittel über Produktion: rot", houseState(productionMean: 500, houseMean: 501, productionComplete: true) == .red)
        check("gleich: nicht rot", houseState(productionMean: 500, houseMean: 500, productionComplete: true) == .normal)
        check("Verbrauch darunter: normal", houseState(productionMean: 2000, houseMean: 800, productionComplete: true) == .normal)
        check("Produktion unvollständig: keine Aussage", houseState(productionMean: 100, houseMean: 800, productionComplete: false) == .unknown)
        check("Verbrauch unbekannt: keine Aussage", houseState(productionMean: 100, houseMean: nil, productionComplete: true) == .unknown)

        // --- snapshot: the summed production and the rule on the *means*, not on the current values
        func series(_ v: [Double]) -> [Sample] { v.enumerated().map { Sample(time: now - Double($0.offset) * 60, value: $0.element) } }
        var data: [Series: [Sample]] = [
            .solarEdge: series([1000, 1000, 1000, 1000, 1000]),
            .hoymiles800: series([200]),
            .hoymiles1600: series([300]),
            .house: series([1600, 1600, 1600, 1600, 1600]),
        ]
        var snap = Snapshot.build(from: data, at: Date(timeIntervalSince1970: now))
        check("Produktion = Summe der drei Anlagen (1500 W)", near(snap.productionMean, 1500) && near(snap.productionNow, 1500))
        check("Hausverbrauch 1600 W > 1500 W: rot", snap.state == .red)
        // house *now* is below production, but the 5-minute mean is still above -> stays red
        data[.house] = series([100, 1900, 1900, 1900, 1900])
        snap = Snapshot.build(from: data, at: Date(timeIntervalSince1970: now))
        check("aktueller Wert unter, Mittel über der Produktion: bleibt rot", near(snap.houseNow, 100) && snap.state == .red)
        data[.house] = series([1400, 1400, 1400, 1400, 1400])
        check("Verbrauch 1400 W < 1500 W: normal", Snapshot.build(from: data, at: Date(timeIntervalSince1970: now)).state == .normal)
        data[.hoymiles1600] = []
        snap = Snapshot.build(from: data, at: Date(timeIntervalSince1970: now))
        check("fehlende Anlage: unvollständig, keine Färbung", !snap.productionComplete && snap.state == .unknown)

        // --- format
        check("Format unter 1 kW", formatPower(850).hasSuffix(" W") && formatPower(850).hasPrefix("850"))
        check("Format ab 1 kW", formatPower(2340).hasSuffix(" kW"))
        check("Format ohne Wert", formatPower(nil) == "–")

        print(failures == 0 ? "Alle Tests bestanden" : "\(failures) Test(s) fehlgeschlagen")
        return failures == 0
    }
}
