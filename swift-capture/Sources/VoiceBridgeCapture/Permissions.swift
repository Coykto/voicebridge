import Foundation
import AVFoundation

/// Microphone permission preflight + prompt orchestration for the capture
/// daemon. Ported from `record/swift-capture/Sources/RecordCapture/Permissions.swift`
/// — only the `checkMicrophone` branch survives the port; screen-recording and
/// accessibility priming are not in voicebridge's scope.
///
/// Adaptation note: `record`'s emitter takes `.permissionRequired` and
/// `.permissionDenied` events with a `kind` field. voicebridge's `Event` enum
/// only carries `.error(code:)`, so denials are reported as
/// `error{code: "microphone_denied"}` directly (per tech spec §2.3/§2.4) and
/// the "permission required" announcement is dropped — func spec §2.2 only
/// requires a one-line error on denial, nothing on the prompt path.
enum Permissions {

    /// Check (and, if needed, request) Microphone permission.
    ///
    /// Returns `true` when access is granted by the time the function returns;
    /// `false` when the user denied the prompt, the permission is restricted by
    /// MDM/parental-controls, or otherwise not granted. On `false`, emits a
    /// single `error{code: "microphone_denied"}` event via the supplied closure
    /// before returning so the caller can `exit(4)` without re-emitting.
    static func checkMicrophone(emit: (Event) -> Void) async -> Bool {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        switch status {
        case .authorized:
            return true
        case .notDetermined:
            // `requestAccess(for:)` is a completion-handler API; bridge into
            // async via a checked continuation. The completion handler runs
            // exactly once, so the continuation is resumed exactly once.
            //
            // NOTE: macOS only presents the prompt when this process is in a
            // terminal-rooted process tree. A launchd-spawned daemon cannot
            // show TCC UI — `requestAccess` returns false immediately there.
            // voicebridge runs from a terminal, so this is fine; if it ever
            // returns false on the not-determined path we land on the
            // denied-path below, which is the right behavior either way.
            let granted: Bool = await withCheckedContinuation { continuation in
                AVCaptureDevice.requestAccess(for: .audio) { allowed in
                    continuation.resume(returning: allowed)
                }
            }
            if granted {
                return true
            }
            emit(.error(code: "microphone_denied"))
            return false
        case .denied, .restricted:
            emit(.error(code: "microphone_denied"))
            return false
        @unknown default:
            emit(.error(code: "microphone_denied"))
            return false
        }
    }
}
