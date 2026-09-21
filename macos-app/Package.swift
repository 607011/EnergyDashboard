// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "PVMonitor",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "PVMonitor", path: "Sources/PVMonitor")
    ]
)
