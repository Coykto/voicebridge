import XCTest
@testable import VoiceBridgeCapture

/// Round-trip and shape tests for `Event` JSON encoding/decoding.
///
/// The `message` field on `.error` is *optional* and absent on the wire when
/// nil — every startup-time error (`microphone_denied`, `conflict`, etc.) uses
/// the no-message form; only the mid-session `mic_lost` event carries a
/// human-readable reason.
final class ProtocolTests: XCTestCase {

    /// `error{code: microphone_denied}` (no message): encoded JSON must not
    /// carry a `message` key, and re-decoding must yield an equal value with
    /// `message == nil`.
    func testErrorWithoutMessage() throws {
        let event = Event.error(code: "microphone_denied", message: nil)
        let line = try IPCCodec.encode(event)

        XCTAssertTrue(line.contains("\"event\":\"error\""), "event field present")
        XCTAssertTrue(line.contains("\"code\":\"microphone_denied\""), "code field present")
        XCTAssertFalse(line.contains("\"message\""), "no message key when nil")

        // Round-trip back through the decoder.
        let data = Data(line.utf8)
        let decoded = try JSONDecoder().decode(Event.self, from: data)
        XCTAssertEqual(decoded, event)
    }

    /// `error{code: mic_lost, message: <reason>}`: encoded JSON must carry both
    /// keys, and re-decoding must yield an equal value.
    func testErrorWithMessage() throws {
        let event = Event.error(code: "mic_lost", message: "default input removed")
        let line = try IPCCodec.encode(event)

        XCTAssertTrue(line.contains("\"event\":\"error\""), "event field present")
        XCTAssertTrue(line.contains("\"code\":\"mic_lost\""), "code field present")
        XCTAssertTrue(line.contains("\"message\":\"default input removed\""), "message field present")

        let data = Data(line.utf8)
        let decoded = try JSONDecoder().decode(Event.self, from: data)
        XCTAssertEqual(decoded, event)
    }

    /// Decoder must tolerate the no-message shape on the wire — anything that
    /// `Permissions.swift` or `main.swift` emits with the default-nil shape
    /// has to decode back without throwing.
    func testDecodeErrorWithoutMessageField() throws {
        let json = #"{"event":"error","code":"microphone_denied"}"#
        let decoded = try JSONDecoder().decode(Event.self, from: Data(json.utf8))
        XCTAssertEqual(decoded, .error(code: "microphone_denied", message: nil))
    }
}
