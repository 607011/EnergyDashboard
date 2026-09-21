import Foundation

/// One time-series point: seconds since 1970 and the value (watts).
struct Sample {
    let time: Double
    let value: Double
}

/// The four series the app reads, by their Redis time-series key.
enum Series: String, CaseIterable {
    case solarEdge = "ts:inverter:power_pv_total"
    case hoymiles800 = "ts:hoymiles:hms_800w_2t:power_w"
    case hoymiles1600 = "ts:hoymiles:hms_1600_4wb:power_w"
    case house = "ts:inverter:house_consumption_total"

    var title: String {
        switch self {
        case .solarEdge: return "SolarEdge SE10K"
        case .hoymiles800: return "Hoymiles HMS-800W-2T"
        case .hoymiles1600: return "Hoymiles HMS-1600-4WB"
        case .house: return "Hausverbrauch"
        }
    }

    /// Short label for the narrow window.
    var shortTitle: String {
        switch self {
        case .solarEdge: return "SolarEdge"
        case .hoymiles800: return "HMS-800W-2T"
        case .hoymiles1600: return "HMS-1600-4WB"
        case .house: return "Haus"
        }
    }

    static let production: [Series] = [.solarEdge, .hoymiles800, .hoymiles1600]
}

enum Averaging {
    /// Length of the averaging window.
    static let window: Double = 5 * 60
    /// A series without a sample in the window falls back to its newest sample, but only if it is
    /// at most this old. The Hoymiles cloud data arrives every five minutes, so this is not rare.
    static let maxStale: Double = 15 * 60
}

/// Mean of the samples in the last `window` seconds. If there are none, the newest sample counts
/// when it is at most `maxStale` seconds old; otherwise the value is unknown (nil).
func windowMean(_ samples: [Sample], now: Double,
                window: Double = Averaging.window, maxStale: Double = Averaging.maxStale) -> Double? {
    let inWindow = samples.filter { $0.time >= now - window && $0.time <= now + 5 }
    if !inWindow.isEmpty {
        return inWindow.map(\.value).reduce(0, +) / Double(inWindow.count)
    }
    return latestValue(samples, now: now, maxStale: maxStale)
}

/// The newest sample's value, unless it is older than `maxStale` seconds.
func latestValue(_ samples: [Sample], now: Double, maxStale: Double = Averaging.maxStale) -> Double? {
    guard let last = samples.filter({ $0.time <= now + 5 }).max(by: { $0.time < $1.time }),
          now - last.time <= maxStale else { return nil }
    return last.value
}

enum HouseState: Equatable {
    case normal
    /// The house consumption's 5-minute mean exceeds the production's 5-minute mean.
    case red
    /// Not all production figures are known, so a comparison would be misleading.
    case unknown
}

/// Red exactly when the house consumption's 5-minute mean is above the summed production's
/// 5-minute mean. Without complete production data no verdict is given: a missing Hoymiles value
/// would make the production look too low and the consumption falsely red.
func houseState(productionMean: Double?, houseMean: Double?, productionComplete: Bool) -> HouseState {
    guard productionComplete, let productionMean, let houseMean else { return .unknown }
    return houseMean > productionMean ? .red : .normal
}

struct Snapshot {
    var now: [Series: Double] = [:]
    var mean: [Series: Double] = [:]
    var fetchedAt = Date()

    var productionComplete: Bool { Series.production.allSatisfy { now[$0] != nil && mean[$0] != nil } }
    var productionNow: Double? { sum(now) }
    var productionMean: Double? { sum(mean) }
    var houseNow: Double? { now[.house] }
    var houseMean: Double? { mean[.house] }
    var state: HouseState {
        houseState(productionMean: productionMean, houseMean: houseMean, productionComplete: productionComplete)
    }

    /// Sum of whatever production systems are known (nil if none).
    private func sum(_ values: [Series: Double]) -> Double? {
        let known = Series.production.compactMap { values[$0] }
        return known.isEmpty ? nil : known.reduce(0, +)
    }

    static func build(from data: [Series: [Sample]], at date: Date = Date()) -> Snapshot {
        let t = date.timeIntervalSince1970
        var snapshot = Snapshot(fetchedAt: date)
        for (series, samples) in data {
            snapshot.now[series] = latestValue(samples, now: t)
            snapshot.mean[series] = windowMean(samples, now: t)
        }
        return snapshot
    }
}

/// "850 W" below one kilowatt, "2,3 kW" above (decimal separator follows the system locale).
func formatPower(_ watts: Double?) -> String {
    guard let watts else { return "–" }
    if abs(watts) >= 1000 {
        return String(format: "%.1f kW", locale: Locale.current, watts / 1000)
    }
    return String(format: "%.0f W", locale: Locale.current, watts)
}
