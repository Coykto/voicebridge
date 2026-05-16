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

emit(.ready)

// Surface the macOS Accessibility prompt on first run. The call returns the
// current trust status (likely false here, since this is the prompt path), but
// presents the system UI as a side effect. The actual gating decision is what
// `HotkeyMonitor.register()`'s AX check sees once the user responds.
if !AXIsProcessTrusted() {
    let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue()
    _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
}

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

// Carbon's `RegisterEventHotKey` delivers events through the HIToolbox
// application event dispatcher, which is pumped by NSApplication's run loop
// (not by a bare `CFRunLoopRun()` / `RunLoop.main.run()`). `.accessory` keeps
// the process out of the Dock — it's a background CLI helper, not a UI app.
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
