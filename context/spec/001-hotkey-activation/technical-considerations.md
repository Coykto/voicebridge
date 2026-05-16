# Technical Specification: Hotkey Activation

- **Functional Specification:** `context/spec/001-hotkey-activation/functional-spec.md`
- **Status:** Approved
- **Author(s):** Evgenii Basmov

---

## 1. High-Level Technical Approach

A new Swift Package Manager executable (`swift-capture/`) owns the global hotkey using Carbon's `RegisterEventHotKey`, extending the `HotkeyMonitor` pattern from the sibling `record` project to subscribe to **both** `kEventHotKeyPressed` and `kEventHotKeyReleased` (record only handles the press). The Swift binary emits `hotkey_down` / `hotkey_up` events as JSON lines on stderr; the Python orchestrator (`src/voicebridge/`) reads those events, plays the audible start/stop cues, and toggles an in-process **PTT flag** that gates mic frames on the orchestrator side.

The hotkey itself is the only thing the orchestrator needs from the Swift process for *this* spec. Mic capture, model plumbing, and playback are separate specs — but the IPC channel and process boundary defined here are the same one they will share.

Mirroring `record` defensively: even though Carbon's `RegisterEventHotKey` does not require Accessibility TCC at registration time, we still call `AXIsProcessTrusted()` before registering and surface `accessibility_denied` if the trust check fails, so the functional spec's "permission required" branch is reachable and the two repos stay aligned in their startup-check pattern.

---

## 2. Proposed Solution & Implementation Plan

### 2.1 Architecture Changes

Two net-new components, both at repo root:

| Component | Path | Role |
|---|---|---|
| Swift capture binary | `swift-capture/` (SPM package) | Owns Carbon hotkey + mic capture. Stdout = raw PCM, stderr = JSON-line events + logs. |
| Python orchestrator | `src/voicebridge/` (uv-managed) | Launches Swift binary, reads IPC, gates frames, plays cues, talks to OpenAI Realtime. |

Layout mirrors `record` 1:1 so patterns are obvious to anyone who has read that repo.

### 2.2 Swift side: file layout

```
swift-capture/
├── Package.swift
└── Sources/
    └── VoiceBridgeCapture/
        ├── main.swift              # entrypoint: install monitors, run RunLoop, emit JSON
        ├── HotkeyMonitor.swift     # extended copy of record's; press + release
        ├── Protocol.swift          # JSON event shapes (Codable)
        └── AudioCapture.swift      # (mic capture — owned by a separate spec)
```

**`HotkeyMonitor.swift` — diff from `record`:**

- Add a second `EventTypeSpec` for `kEventHotKeyReleased` and a second call to `InstallEventHandler` (or one handler that switches on `GetEventKind(event)`).
- Constructor takes `onPress` and `onRelease` closures (record takes only `onPress`).
- Same `register / unregister` API and same `RegistrationResult` cases (`registered`, `conflict`, `invalid(message:)`) — including the same closed set of error tokens: `accessibility_denied`, `param_err`, `unknown_key:<key>`, `unknown_osstatus_<code>`.
- Keep the `AXIsProcessTrusted()` defensive check exactly as record does it.

**`main.swift`:**

- Hardcoded chord: `modifiers = [.option, .cmd]`, `key = "t"`.
- On `register()` result:
  - `.registered` → emit `{"event":"hotkey_registered","chord":"option+command+t"}`.
  - `.conflict` → emit `{"event":"error","code":"conflict"}` and exit non-zero.
  - `.invalid(message)` → emit `{"event":"error","code":message}` and exit non-zero.
- On press → emit `{"event":"hotkey_down","ts":"<ISO8601>"}`.
- On release → emit `{"event":"hotkey_up","ts":"<ISO8601>"}`.

### 2.3 Python side: file layout

```
src/voicebridge/
├── __init__.py
├── __main__.py        # `python -m voicebridge --source ru --target en`
├── capture.py         # spawn swift binary, expose stdout (PCM) + stderr (events)
├── ipc.py             # parse JSON-line events from stderr
├── hotkey.py          # PTT flag + audible-cue dispatch
├── gating.py          # frame gating (mic and playback)
└── errors.py          # map error codes → human-readable startup messages
```

For this spec only `__main__.py`, `capture.py`, `ipc.py`, `hotkey.py`, and `errors.py` are touched; `gating.py` is shared with the mic-capture spec and only the PTT-flag interface is defined here.

### 2.4 IPC contract (events relevant to this spec)

One JSON object per line on the Swift binary's **stderr**:

| Event | Shape | When |
|---|---|---|
| `ready` | `{"event":"ready"}` | First line after Swift main starts, before Carbon registration. |
| `hotkey_registered` | `{"event":"hotkey_registered","chord":"option+command+t"}` | After successful `RegisterEventHotKey`. |
| `hotkey_down` | `{"event":"hotkey_down","ts":"<ISO8601>"}` | Carbon `kEventHotKeyPressed`. |
| `hotkey_up` | `{"event":"hotkey_up","ts":"<ISO8601>"}` | Carbon `kEventHotKeyReleased`. |
| `error` | `{"event":"error","code":"<token>"}` | Any startup failure. `code` ∈ {`accessibility_denied`, `conflict`, `param_err`, `unknown_key:<k>`, `unknown_osstatus_<n>`}. |

Anything Swift writes to stderr that does **not** parse as one of the above JSON shapes is treated as a free-form log line and tee'd to `./logs/<ts>-capture.log` (per architecture §4) — not interpreted.

### 2.5 Orchestrator startup sequence

`__main__.py`:

1. Parse CLI flags (`--source`, `--target`).
2. Load `OPENAI_API_KEY` from `.env`.
3. Spawn Swift binary via `asyncio.create_subprocess_exec`.
4. Read stderr until `ready` (timeout: 5 s) → then until `hotkey_registered` or `error` (timeout: 5 s).
   - `hotkey_registered` → proceed to main loop.
   - `error.code == "accessibility_denied"` → print the §2.4-style error from the func spec, exit `2`.
   - `error.code == "conflict"` → print the §2.5-style error, exit `1`.
   - Any other `error.code` → print a generic "hotkey could not be registered: `<code>`" message, exit `1`.
   - Timeout → kill subprocess, print "Swift capture did not start in time", exit `3`.
5. Main loop: consume `hotkey_down` / `hotkey_up` events on a task; relay to `hotkey.py`.

### 2.6 PTT flag + audible cues

`hotkey.py` exposes:

- A single `asyncio.Event`-like object — `ptt_active` — set on `hotkey_down`, cleared on `hotkey_up`. The gating layer (mic frames in / playback tail out) reads this; for *this* spec we only define and toggle it.
- On `hotkey_down`: fire-and-forget `afplay /System/Library/Sounds/Submarine.aiff` via `subprocess.Popen` (matches `record`'s pattern; verified to work without a controlling TTY in record's verification notes).
- On `hotkey_up`: same, with `Pop.aiff`.
- Errors playing the cue (e.g. `afplay` missing) log a warning and don't take down the session.

### 2.7 Tail behavior

Per func spec 2.2: the mic stream closes immediately on release, but in-flight translated audio continues to play. This is naturally enforced by gating only the *input* direction via `ptt_active`:

- Mic frames are dropped at the gating layer when `ptt_active.is_set() is False`.
- Audio frames coming back from the model are written to `sounddevice` unconditionally; the model stops producing them once the session-side input stream goes silent.

No "flush on release" or "kill playback" logic is required.

### 2.8 Hardcoded values for the PoC

| Value | Where | Note |
|---|---|---|
| Chord = `⌥⌘T` | `swift-capture/Sources/VoiceBridgeCapture/main.swift` | No CLI flag, no config file. |
| Start cue = `Submarine.aiff`, stop cue = `Pop.aiff` | `src/voicebridge/hotkey.py` | Fixed paths. |
| AX defensive check | `HotkeyMonitor.register()` | Kept identical to record. |

---

## 3. Impact and Risk Analysis

### System Dependencies

- **macOS Carbon HIToolbox** — `RegisterEventHotKey`, `kEventHotKeyPressed`, `kEventHotKeyReleased`. Deprecated-but-stable. Record uses it in production.
- **macOS ApplicationServices** — `AXIsProcessTrusted()` for the defensive AX check.
- **`afplay` + system sounds** — both verified present on macOS 13+; record's smoke tests rely on the same paths.
- **`asyncio.create_subprocess_exec`** — standard.
- **No new third-party dependencies** for this spec specifically. (`python-dotenv`, `websockets`, `sounddevice`, etc. land in sibling specs.)

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Carbon's `kEventHotKeyReleased` is less commonly used than `kEventHotKeyPressed`; possible quirks (e.g. release event swallowed if focus changes mid-hold). | Manual smoke test: hold the chord, switch app focus mid-hold, release. Verify `hotkey_up` still fires. If it doesn't, fall back to a polling `CGEventSource.keyState` check on a short timer while `ptt_active` is set. |
| AX prompt not appearing on first run, leaving the user with a silent failure. | If `AXIsProcessTrusted()` returns false on startup, call `AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt: true])` to force the prompt (record's approach). Error message also points to System Settings explicitly. |
| Chord held when binary starts (Carbon doesn't fire for already-held chords). | Func spec explicitly leaves this undefined — no mitigation. |
| Swift subprocess crash mid-session leaves orchestrator hanging on stderr read. | When the stderr stream EOFs, log and exit the orchestrator. (Out of scope for *correctness* of this spec, but cheap to wire up here.) |
| Hotkey events arrive faster than `afplay` can spawn (rapid press/release). | Fire-and-forget `Popen`; if a press arrives while the previous cue is still playing, the OS will mix them. No queueing, no debouncing — acceptable for a PoC. |

---

## 4. Testing Strategy

**Swift unit tests** (`swift-capture/Tests/`, optional — skipped if XCTest unavailable, same as record):

- `modifierMask(from:)` and `keyCode(for:)` reused verbatim from record; pin tests for `[.option, .cmd]` + `"t"`.
- `HotkeyMonitor` tests: cannot exercise Carbon without a UI session; cover only mask/keycode translation and the closed-set error tokens (same scope as record).

**Python unit tests** (`tests/test_ipc.py`, `tests/test_hotkey.py`):

- Given a stream of JSON-line strings, `ipc.py` produces the expected sequence of typed events.
- Given a sequence of typed events, `hotkey.py` toggles `ptt_active` correctly and calls the cue player exactly once per transition.
- Startup-sequence test: feed a fake stderr with `ready` → `hotkey_registered` → ready-to-run; and the three error variants (`accessibility_denied`, `conflict`, `param_err`) → orchestrator exits with the right code.

**Manual smoke tests** (single author, on the target Mac):

1. **Happy path.** Launch from terminal → grant AX prompt if it appears → focus Google Meet → hold ⌥⌘T (Submarine), speak, release (Pop). Confirm translated audio of the just-spoken phrase plays through speakers; nothing plays when key is up.
2. **AX denied.** Revoke Accessibility for the Swift binary in System Settings → relaunch → orchestrator prints the AX error and exits with code `2`.
3. **Hotkey conflict.** Bind ⌥⌘T in another app (Keyboard Maestro, or a quick Hammerspoon line) → relaunch → orchestrator prints the conflict error and exits with code `1`.
4. **Focus-switch mid-hold.** Hold ⌥⌘T, alt-tab between two apps without releasing, release. Verify `hotkey_up` still fires (cue plays). If it doesn't, escalate to the polling fallback in Risk table.
5. **Rapid press/release.** Tap ⌥⌘T a half-dozen times in a second. Confirm no crash; the cue sounds may overlap, which is acceptable.

No formal performance criterion for the hotkey itself — only that the start/stop cues are perceptibly instant (≪ 100 ms perceived). The PoC's latency budget belongs to the model plumbing spec, not here.
