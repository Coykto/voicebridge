import Foundation
import Carbon.HIToolbox
import ApplicationServices

// MARK: - Translation tables

/// Canonical modifier names. Kept as a small internal helper enum; the chord
/// is serialized to the wire as a fixed string ("option+command+t") rather
/// than as a structured field, so this type never appears in `Protocol.swift`.
enum HotkeyModifier: String {
    case cmd
    case option
    case control
    case shift
}

/// Translate a list of canonical modifier names into a Carbon modifier mask
/// suitable for `RegisterEventHotKey`. Order is irrelevant — the mask is
/// commutative under OR.
func modifierMask(from modifiers: [HotkeyModifier]) -> UInt32 {
    var mask: UInt32 = 0
    for m in modifiers {
        switch m {
        case .cmd:     mask |= UInt32(cmdKey)
        case .option:  mask |= UInt32(optionKey)
        case .control: mask |= UInt32(controlKey)
        case .shift:   mask |= UInt32(shiftKey)
        }
    }
    return mask
}

/// Translate a key string from the closed grammar (`a`-`z`, `0`-`9`,
/// `f1`-`f20`, plus `space`, `tab`, `return`, `escape`, `delete`) into the
/// US ANSI virtual keycode used by Carbon. Returns nil for unknown keys.
func keyCode(for key: String) -> UInt32? {
    switch key {
    case "a": return UInt32(kVK_ANSI_A)
    case "b": return UInt32(kVK_ANSI_B)
    case "c": return UInt32(kVK_ANSI_C)
    case "d": return UInt32(kVK_ANSI_D)
    case "e": return UInt32(kVK_ANSI_E)
    case "f": return UInt32(kVK_ANSI_F)
    case "g": return UInt32(kVK_ANSI_G)
    case "h": return UInt32(kVK_ANSI_H)
    case "i": return UInt32(kVK_ANSI_I)
    case "j": return UInt32(kVK_ANSI_J)
    case "k": return UInt32(kVK_ANSI_K)
    case "l": return UInt32(kVK_ANSI_L)
    case "m": return UInt32(kVK_ANSI_M)
    case "n": return UInt32(kVK_ANSI_N)
    case "o": return UInt32(kVK_ANSI_O)
    case "p": return UInt32(kVK_ANSI_P)
    case "q": return UInt32(kVK_ANSI_Q)
    case "r": return UInt32(kVK_ANSI_R)
    case "s": return UInt32(kVK_ANSI_S)
    case "t": return UInt32(kVK_ANSI_T)
    case "u": return UInt32(kVK_ANSI_U)
    case "v": return UInt32(kVK_ANSI_V)
    case "w": return UInt32(kVK_ANSI_W)
    case "x": return UInt32(kVK_ANSI_X)
    case "y": return UInt32(kVK_ANSI_Y)
    case "z": return UInt32(kVK_ANSI_Z)
    case "0": return UInt32(kVK_ANSI_0)
    case "1": return UInt32(kVK_ANSI_1)
    case "2": return UInt32(kVK_ANSI_2)
    case "3": return UInt32(kVK_ANSI_3)
    case "4": return UInt32(kVK_ANSI_4)
    case "5": return UInt32(kVK_ANSI_5)
    case "6": return UInt32(kVK_ANSI_6)
    case "7": return UInt32(kVK_ANSI_7)
    case "8": return UInt32(kVK_ANSI_8)
    case "9": return UInt32(kVK_ANSI_9)
    case "f1":  return UInt32(kVK_F1)
    case "f2":  return UInt32(kVK_F2)
    case "f3":  return UInt32(kVK_F3)
    case "f4":  return UInt32(kVK_F4)
    case "f5":  return UInt32(kVK_F5)
    case "f6":  return UInt32(kVK_F6)
    case "f7":  return UInt32(kVK_F7)
    case "f8":  return UInt32(kVK_F8)
    case "f9":  return UInt32(kVK_F9)
    case "f10": return UInt32(kVK_F10)
    case "f11": return UInt32(kVK_F11)
    case "f12": return UInt32(kVK_F12)
    case "f13": return UInt32(kVK_F13)
    case "f14": return UInt32(kVK_F14)
    case "f15": return UInt32(kVK_F15)
    case "f16": return UInt32(kVK_F16)
    case "f17": return UInt32(kVK_F17)
    case "f18": return UInt32(kVK_F18)
    case "f19": return UInt32(kVK_F19)
    case "f20": return UInt32(kVK_F20)
    case "space":  return UInt32(kVK_Space)
    case "tab":    return UInt32(kVK_Tab)
    case "return": return UInt32(kVK_Return)
    case "escape": return UInt32(kVK_Escape)
    case "delete": return UInt32(kVK_Delete)
    default:
        return nil
    }
}

// MARK: - Carbon event handler glue

/// Module-level holder for the singleton `HotkeyMonitor`. The Carbon event
/// handler is a C function pointer with no captured state, so it needs a
/// well-known place to find the press/release callbacks.
private var sharedMonitor: HotkeyMonitor?

/// Static C-compatible callback. Dispatches by `GetEventKind(event)` so a
/// single handler services both press and release subscriptions.
private func hotkeyEventHandler(
    _ nextHandler: EventHandlerCallRef?,
    _ event: EventRef?,
    _ userData: UnsafeMutableRawPointer?
) -> OSStatus {
    guard let event = event, let monitor = sharedMonitor else {
        return noErr
    }
    let kind = GetEventKind(event)
    switch Int(kind) {
    case kEventHotKeyPressed:
        let onPress = monitor.onPress
        DispatchQueue.main.async { onPress() }
    case kEventHotKeyReleased:
        let onRelease = monitor.onRelease
        DispatchQueue.main.async { onRelease() }
    default:
        break
    }
    return noErr
}

// MARK: - HotkeyMonitor

/// Thin wrapper around Carbon's `RegisterEventHotKey` / `InstallEventHandler`
/// pair. Tech spec §1 commits us to Carbon (not `NSEvent` global monitors)
/// because Carbon hotkeys do NOT require Accessibility TCC at registration
/// time — `AXIsProcessTrusted()` is consulted defensively here so the binary
/// can surface a clean `accessibility_denied` before touching Carbon. This
/// keeps the startup-check pattern aligned with the sibling `record` repo.
///
/// Diff from record: subscribes to both `kEventHotKeyPressed` AND
/// `kEventHotKeyReleased` (record only handles press), since the PoC's
/// push-to-talk semantics need the release edge to close the mic stream.
final class HotkeyMonitor {
    enum RegistrationResult: Equatable {
        case registered
        /// `OSStatus == eventHotKeyExistsErr` (-9878). Another process holds
        /// the same global hotkey.
        case conflict
        /// Stable machine-readable token:
        ///   - `"accessibility_denied"`     `AXIsProcessTrusted()` was false
        ///   - `"param_err"`                Carbon `paramErr` (-50)
        ///   - `"unknown_key:<key>"`        key not in the closed grammar
        ///   - `"unknown_osstatus_<code>"`  catch-all
        case invalid(message: String)
    }

    private static let eventHotKeyExistsErrValue: OSStatus = -9878
    private static let paramErrValue: OSStatus = -50

    let onPress: () -> Void
    let onRelease: () -> Void

    private var hotKeyRef: EventHotKeyRef?
    private var handlerInstalled: Bool = false
    private var handlerRef: EventHandlerRef?

    init(onPress: @escaping () -> Void, onRelease: @escaping () -> Void) {
        self.onPress = onPress
        self.onRelease = onRelease
        sharedMonitor = self
    }

    deinit {
        unregister()
        if sharedMonitor === self {
            sharedMonitor = nil
        }
    }

    func register(modifiers: UInt32, keyCode: UInt32) -> RegistrationResult {
        unregister()

        // Defensive AX check — see class doc for why we keep this even though
        // Carbon doesn't strictly require it.
        if !AXIsProcessTrusted() {
            return .invalid(message: "accessibility_denied")
        }

        // Install the shared Carbon handler on first use. Two specs — one for
        // press, one for release — passed as a contiguous array with count=2,
        // so a single handler services the full push-to-talk lifecycle.
        if !handlerInstalled {
            var specs = [
                EventTypeSpec(
                    eventClass: OSType(kEventClassKeyboard),
                    eventKind: UInt32(kEventHotKeyPressed)
                ),
                EventTypeSpec(
                    eventClass: OSType(kEventClassKeyboard),
                    eventKind: UInt32(kEventHotKeyReleased)
                )
            ]
            let status = InstallEventHandler(
                GetApplicationEventTarget(),
                hotkeyEventHandler,
                2,
                &specs,
                nil,
                &handlerRef
            )
            if status != noErr {
                return .invalid(message: "unknown_osstatus_\(status)")
            }
            handlerInstalled = true
        }

        // Four-char-code signature unique to voicebridge-capture ("vbcp"),
        // computed by bit-shifting so we don't depend on optional NSString
        // extensions.
        let signature: OSType =
            (OSType(UInt8(ascii: "v")) << 24) |
            (OSType(UInt8(ascii: "b")) << 16) |
            (OSType(UInt8(ascii: "c")) << 8)  |
             OSType(UInt8(ascii: "p"))
        let hotKeyID = EventHotKeyID(signature: signature, id: 1)

        var ref: EventHotKeyRef?
        let status = RegisterEventHotKey(
            keyCode,
            modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &ref
        )
        switch status {
        case noErr:
            hotKeyRef = ref
            return .registered
        case Self.eventHotKeyExistsErrValue:
            return .conflict
        case Self.paramErrValue:
            return .invalid(message: "param_err")
        default:
            return .invalid(message: "unknown_osstatus_\(status)")
        }
    }

    func unregister() {
        if let ref = hotKeyRef {
            UnregisterEventHotKey(ref)
            hotKeyRef = nil
        }
    }
}
