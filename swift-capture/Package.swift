// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "VoiceBridgeCapture",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(name: "voicebridge-capture", targets: ["VoiceBridgeCapture"])
    ],
    targets: [
        .executableTarget(
            name: "VoiceBridgeCapture",
            path: "Sources/VoiceBridgeCapture"
        )
    ]
)
