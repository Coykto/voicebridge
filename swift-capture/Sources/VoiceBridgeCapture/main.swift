import Foundation
import Carbon.HIToolbox
import ApplicationServices
import AppKit

// Force line-buffered stderr so the Python supervisor sees each JSON event as
// soon as it's written. macOS defaults to block-buffered when stderr is a pipe.
setvbuf(stderr, nil, _IOLBF, 0)

/// ISO-8601 UTC formatter, e.g. `2026-05-16T14:32:08Z`.
let iso8601: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = TimeZone(identifier: "UTC")
    return f
}()

// Surface the macOS Accessibility prompt on first run. The call returns the
// current trust status (likely false here, since this is the prompt path), but
// presents the system UI as a side effect. The actual gating decision is what
// `HotkeyMonitor.register()`'s AX check sees once the user responds.
if !AXIsProcessTrusted() {
    let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue()
    _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
}

// Stdout writer for raw PCM frames. The IPC contract reserves stdout for the
// continuous 640-byte PCM stream (tech spec §2.4); stderr carries every JSON
// event. `FileHandle.standardOutput.write` is unbuffered, so each chunk is
// flushed immediately.
let pcmSink: (Data) -> Void = { data in
    FileHandle.standardOutput.write(data)
}

// `AudioCapture` is constructed inside the async bootstrap below but referenced
// from a SIGINT handler and from NSApplication's atexit path, so it lives in a
// top-level optional. The signal handler is registered after `audioCapture` is
// non-nil; before that, SIGINT just exits the process (no resources held).
var audioCapture: AudioCapture?

// Async bootstrap: permissions → mic startup → hotkey registration → `ready`.
//
// Top-level await isn't available in Swift Package executables built with
// swift-tools 5.9 unless the file is `@main`-annotated, so we kick off the
// async work in a detached Task and gate the NSApp.run() call on a semaphore.
// The Task either:
//   - signals the semaphore on success, letting main fall through to NSApp.run();
//   - calls exit(N) on failure, terminating the process before the semaphore is
//     ever signalled (so the main thread blocks on .wait() and is torn down by
//     the exit, no leak).
//
// SIGINT: the default disposition (exit) is sufficient for a clean shutdown —
// AVAudioEngine releases the mic input when the process exits, and macOS
// removes the menu-bar mic indicator within a moment. Slice 5 will install an
// explicit handler that calls `audioCapture.stop()` to be defensive against
// route-listener teardown order, but for Slice 1 the implicit path is fine.
let bootstrapDone = DispatchSemaphore(value: 0)

Task {
    // Step 1: microphone permission. On denial, `Permissions.checkMicrophone`
    // already emits `error{code: microphone_denied}`. Exit 4 per tech spec §2.3.
    let micGranted = await Permissions.checkMicrophone(emit: { event in emit(event) })
    guard micGranted else {
        exit(4)
    }

    // Step 2: bring up the mic engine + tap. `start()` only returns after the
    // first 640-byte chunk has flushed to stdout, so by the time we proceed,
    // PCM is flowing.
    let capture: AudioCapture
    do {
        // `onTerminalLoss` runs after `AudioCapture` has emitted
        // `error{code: mic_lost, message: <reason>}` to stderr and torn
        // everything down. Exit code 5 mirrors the orchestrator's
        // `errors.py` mapping for `mic_lost` — see tech spec §2.3.
        capture = try AudioCapture(
            emit: { event in emit(event) },
            frameSink: pcmSink,
            onTerminalLoss: { exit(5) }
        )
        try await capture.start()
    } catch {
        emit(.error(code: "mic_start_failed"))
        exit(1)
    }
    audioCapture = capture

    // Step 3: hotkey registration. Same shape as Slice 001 — only difference is
    // that we now reach this point with mic already streaming.
    let monitor = HotkeyMonitor(
        onPress: {
            emit(.hotkeyDown(ts: iso8601.string(from: Date())))
        },
        onRelease: {
            emit(.hotkeyUp(ts: iso8601.string(from: Date())))
        }
    )

    let mask = modifierMask(from: [.option, .cmd])
    guard let code = keyCode(for: "t") else {
        emit(.error(code: "unknown_key:t"))
        exit(1)
    }

    switch monitor.register(modifiers: mask, keyCode: code) {
    case .registered:
        emit(.hotkeyRegistered(chord: "option+command+t"))
    case .conflict:
        emit(.error(code: "conflict"))
        exit(1)
    case .invalid(let message):
        emit(.error(code: message))
        exit(1)
    }

    // Step 4: announce readiness. `ready` lands only after both mic flow and
    // hotkey registration are live (tech spec §2.3).
    emit(.ready)

    // Retain the monitor for the process lifetime. `HotkeyMonitor` installs the
    // Carbon handler against the application event target, and a leak here is
    // intentional — the daemon stays up until SIGINT.
    _ = monitor

    bootstrapDone.signal()
}

bootstrapDone.wait()

// Carbon's `RegisterEventHotKey` delivers events through the HIToolbox
// application event dispatcher, which is pumped by NSApplication's run loop
// (not by a bare `CFRunLoopRun()` / `RunLoop.main.run()`). `.accessory` keeps
// the process out of the Dock — it's a background CLI helper, not a UI app.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
