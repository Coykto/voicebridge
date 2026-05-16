import Foundation

/// JSON-line events written to stderr per tech spec §2.4.
///
/// Discriminator is the `event` field. Stdout is reserved for raw PCM frames,
/// so all protocol traffic lives on stderr.
///
/// `error.code` is an open-ended closed-set token. Known values today:
///   - `"accessibility_denied"` / `"conflict"` / `"param_err"` /
///     `"unknown_key:<key>"` / `"unknown_osstatus_<code>"` — hotkey registration
///     failures (spec 001).
///   - `"microphone_denied"` — mic permission denied at startup (spec 004 §2.4).
///   - `"mic_lost"` — unrecoverable mic-device loss mid-session (spec 004 §2.4).
///   - `"encode_failed"` — defensive fallback when JSON encoding itself fails.
enum Event: Equatable {
    case ready
    case hotkeyRegistered(chord: String)
    case hotkeyDown(ts: String)
    case hotkeyUp(ts: String)
    case error(code: String, message: String? = nil)

    private enum CodingKeys: String, CodingKey {
        case event
        case chord
        case ts
        case code
        case message
    }

    private enum EventKind: String, Codable {
        case ready
        case hotkeyRegistered = "hotkey_registered"
        case hotkeyDown = "hotkey_down"
        case hotkeyUp = "hotkey_up"
        case error
    }
}

extension Event: Codable {
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let kind = try container.decode(EventKind.self, forKey: .event)
        switch kind {
        case .ready:
            self = .ready
        case .hotkeyRegistered:
            let chord = try container.decode(String.self, forKey: .chord)
            self = .hotkeyRegistered(chord: chord)
        case .hotkeyDown:
            let ts = try container.decode(String.self, forKey: .ts)
            self = .hotkeyDown(ts: ts)
        case .hotkeyUp:
            let ts = try container.decode(String.self, forKey: .ts)
            self = .hotkeyUp(ts: ts)
        case .error:
            let code = try container.decode(String.self, forKey: .code)
            let message = try container.decodeIfPresent(String.self, forKey: .message)
            self = .error(code: code, message: message)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .ready:
            try container.encode(EventKind.ready, forKey: .event)
        case .hotkeyRegistered(let chord):
            try container.encode(EventKind.hotkeyRegistered, forKey: .event)
            try container.encode(chord, forKey: .chord)
        case .hotkeyDown(let ts):
            try container.encode(EventKind.hotkeyDown, forKey: .event)
            try container.encode(ts, forKey: .ts)
        case .hotkeyUp(let ts):
            try container.encode(EventKind.hotkeyUp, forKey: .event)
            try container.encode(ts, forKey: .ts)
        case .error(let code, let message):
            try container.encode(EventKind.error, forKey: .event)
            try container.encode(code, forKey: .code)
            if let message = message {
                try container.encode(message, forKey: .message)
            }
        }
    }
}

/// Compact single-line JSON codec.
enum IPCCodec {
    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = []
        return e
    }()

    static func encode(_ event: Event) throws -> String {
        let data = try encoder.encode(event)
        guard let s = String(data: data, encoding: .utf8) else {
            throw IPCCodecError.invalidUTF8
        }
        return s
    }
}

enum IPCCodecError: Error {
    case invalidUTF8
}

/// Lock around `FileHandle.standardError` so concurrent emits from the main
/// queue and any future background thread don't interleave bytes on the wire.
private let stderrLock = NSLock()

/// Write a single event as one JSON line followed by `\n` to **stderr**.
/// Stdout is reserved for raw PCM frames per the architecture's IPC contract.
@inline(__always)
func emit(_ event: Event) {
    do {
        let line = try IPCCodec.encode(event)
        // Defensive: encoder shouldn't produce embedded newlines, but if some
        // future field ever did, collapse them so the line protocol stays intact.
        let safe = line.replacingOccurrences(of: "\n", with: " ")
        stderrLock.lock()
        FileHandle.standardError.write(Data((safe + "\n").utf8))
        stderrLock.unlock()
    } catch {
        let fallback = #"{"event":"error","code":"encode_failed"}"# + "\n"
        stderrLock.lock()
        FileHandle.standardError.write(Data(fallback.utf8))
        stderrLock.unlock()
    }
}
