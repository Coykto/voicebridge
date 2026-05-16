# Technical Specification: Mic Capture

- **Functional Specification:** `context/spec/004-mic-capture/functional-spec.md`
- **Status:** Draft
- **Author(s):** Evgenii Basmov

---

## 1. High-Level Technical Approach

The Swift binary defined in spec 001 (`swift-capture/Sources/VoiceBridgeCapture/`) gains a new `AudioCapture` module that opens an `AVAudioEngine` input tap on the default input device, converts each buffer to **16 kHz mono PCM16** in 20 ms chunks (320 samples / 640 bytes), and writes raw PCM frames continuously to **stdout**. JSON-line events on **stderr** signal startup, permission failure, and mic loss.

Mic capture starts *before* Carbon hotkey registration. The Swift binary only emits `ready` once **both** the AVAudioEngine input tap has produced its first buffer **and** the orchestrator has spawned a stable mic stream. The orchestrator (`src/voicebridge/`) treats `ready` as "everything is up," same as 001 — so the 001 startup sequence is unchanged in shape, just with mic now part of the gate.

Per arch §2, gating to "only while PTT is held" stays on the **Python side**. Swift captures continuously; `src/voicebridge/gating.py` drops mic frames received while `ptt_active` (from 001) is false. The "capture started / capture stopped (N ms)" log lines required by func spec §2.3 are emitted by `gating.py` when it observes the PTT flag transition, counting frames forwarded to the model between transitions.

CoreAudio listener coverage from `record/swift-capture/Sources/RecordCapture/AudioCapture.swift` is copied substantially intact — it's the only part of the codebase that already knows how to survive AirPods route changes and USB unplugs without silent wedges. The behavioral difference: where `record` recovers and continues, voicebridge exits with `error{code: mic_lost}` on unrecoverable loss (func spec §2.4).

---

## 2. Proposed Solution & Implementation Plan

### 2.1 Swift side: file layout (delta over 001)

```
swift-capture/
├── Package.swift               # +linker Info.plist (same pattern as record)
├── Info.plist                  # NSMicrophoneUsageDescription, CFBundleIdentifier
└── Sources/
    └── VoiceBridgeCapture/
        ├── main.swift          # extended: mic-startup BEFORE hotkey registration
        ├── HotkeyMonitor.swift # (from 001, unchanged)
        ├── Protocol.swift      # +mic-related error codes
        ├── Permissions.swift   # NEW — copy of record/Permissions.swift, mic branch only
        └── AudioCapture.swift  # NEW — see §2.2
```

**`Package.swift` changes:**

Append the `unsafeFlags` block embedding `Info.plist` into the executable's `__TEXT,__info_plist` section — exact same form as `record/swift-capture/Package.swift` lines 22–29. Required for `AVCaptureDevice.requestAccess(for: .audio)` to actually surface the prompt and for TCC to key its grant on a stable `CFBundleIdentifier`.

**`Info.plist`:**

Two keys, copied from `record/swift-capture/Info.plist`:

| Key | Value |
|---|---|
| `NSMicrophoneUsageDescription` | "VoiceBridge captures your microphone to translate your speech in real time." |
| `CFBundleIdentifier` | `com.voicebridge.capture` |

**`Permissions.swift`:**

Copy the `Permissions.checkMicrophone(emit:)` function from `record/swift-capture/Sources/RecordCapture/Permissions.swift` (lines 60–101) **verbatim** — same `AVCaptureDevice.authorizationStatus` switch, same async `requestAccess` continuation pattern. Strip the screen-recording, accessibility, and `prime()` paths; voicebridge only needs mic. The function's `emit` closure receives `.permissionDenied(kind: .microphone)` on denial — see §2.4 for how that is mapped onto voicebridge's `error{code:}` shape.

### 2.2 `AudioCapture.swift` — module surface

A single `AudioCapture` class with one public entry point:

```
init(emit: (Event) -> Void, frameSink: (Data) -> Void) throws
func start() async throws        // brings up engine, attaches tap, returns when first buffer flows
func stop()                       // tears down engine + listeners cleanly
```

- `emit` is the same JSON-line emitter passed throughout the binary (writes to stderr).
- `frameSink` is the binary writer to stdout (each call = one 640-byte PCM chunk; caller is responsible for flushing).

**Internal structure mirrors `record`'s `AudioCapture.swift`:**

| Concern | Source in `record` | Adapted? |
|---|---|---|
| AVAudioEngine input-tap install | `record/AudioCapture.swift` (the engine + tap setup section) | Yes, near-verbatim |
| `AVAudioConverter` 48k/44.1k → 16k mono PCM16 | same | Yes, verbatim |
| `AVAudioEngineConfigurationChange` notification handler | same (`handleEngineConfigurationChange`) | Yes |
| Default-input listener (`kAudioHardwarePropertyDefaultInputDevice`) | `defaultInputListenerBlock` | Yes |
| Default-output listener (catches AirPods-as-output wedge) | `defaultOutputListenerBlock` | Yes |
| Per-device stream-format listener | `formatListenerBlock` | Yes |
| `IsRunningSomewhere` listener | `runningSomewhereListenerBlock` | Yes |
| Mic-flow watchdog (`micFlowStallThresholdSeconds = 2.0`) | same | Yes |
| WAV writer / `MixerPump` | `WAVWriter`, mixer pump | **No** — replaced by `frameSink` stdout writer |
| `audio_file` / `source_lost` events | `Event.audioFile`, `Event.sourceLost` | **No** — see §2.4 |
| Test-mode silent-feeder paths | `testSilentSources`, `injectMicLossAfterSeconds`, `silentMicSource` | **No** — voicebridge has no WAV files to compare against; testing strategy in §4 |

**Re-chunking to 20 ms:**

AVAudioEngine emits variable-size buffers (~1024 frames @ device rate). After the converter produces 16 kHz mono PCM16, an internal ring buffer of `Int16` samples accumulates output and flushes to `frameSink` whenever **≥ 320 samples** are available. Partial trailing samples stay in the ring until the next call. No timestamp metadata travels with the frames; latency timing is the orchestrator's job (arch §4).

**Unrecoverable loss path:**

In `record`, repeated restart failures eventually emit `source_lost` and the capture session moves toward `stopped`. In voicebridge, the equivalent terminal state is:

- Stop watchdog timer, remove CoreAudio listeners, tear down engine.
- `emit(.error(code: "mic_lost", message: <one-line reason — "default input removed", "engine failed to restart after route change", etc.>))`.
- Return from `main` with non-zero exit code so the orchestrator's stderr reader sees EOF and exits cleanly itself.

### 2.3 `main.swift` startup sequence (delta over 001)

```
1. Install Permissions.checkMicrophone → if false → emit error(microphone_denied), exit 4.
2. Construct AudioCapture(emit:, frameSink: FileHandle.standardOutput.write).
3. Call audioCapture.start() — completes only after first PCM chunk has flowed.
4. Construct + register HotkeyMonitor (per 001).
5. Emit `ready` once both mic is flowing and hotkey is registered.
6. Run main RunLoop until SIGINT / EOF on stdin / mic_lost.
7. On any exit path: audioCapture.stop(), hotkey.unregister(), then return.
```

The 001 spec's startup-sequence error semantics are extended:

| Failure | New `error.code` | Exit code |
|---|---|---|
| Mic permission denied or never determined-and-denied | `microphone_denied` | `4` |
| Mic device disappears mid-session, unrecoverable | `mic_lost` | `5` |

(001 used exit codes 1–3.)

### 2.4 IPC contract additions

Additions to the event table in 001 §2.4:

| Event | Shape | When |
|---|---|---|
| `error` (extended) | `{"event":"error","code":"microphone_denied"\|"mic_lost"}` | Mic permission denial at startup; unrecoverable device loss mid-session. |

PCM stream (**stdout**, binary):

- Continuous stream of 640-byte chunks. Each chunk = 320 little-endian Int16 samples = 20 ms of 16 kHz mono audio.
- No framing, no length prefix, no headers. Python reads in 640-byte units.
- Stream begins after the first chunk is produced (i.e., after `ready` on stderr).
- Stream ends on process exit (EOF on the pipe).

### 2.5 Python side: file layout (delta over 001)

```
src/voicebridge/
├── capture.py    # extended: expose stdout PCM reader as async iterator
├── ipc.py        # extended: parse new error codes
├── gating.py     # NEW (introduced by 001 as stub; this spec defines its interface)
└── errors.py     # extended: messages for microphone_denied, mic_lost
```

**`capture.py` extension:**

Adds an async iterator `async def pcm_frames() -> AsyncIterator[bytes]:` that yields each 640-byte chunk read from the subprocess's stdout. Reads in 640-byte units (`await stdout.readexactly(640)`); raises `EOFError` on stream end, which the main loop translates to "Swift exited."

**`gating.py` interface:**

```
class MicGate:
    def __init__(self, ptt_active: asyncio.Event, logger: Logger): ...
    async def run(self, source: AsyncIterator[bytes], sink: AsyncIterator-like) -> None
```

- On each chunk from `source`:
  - If `ptt_active.is_set()`: forward to `sink`, increment `frames_this_session`.
  - Else: drop.
- On `ptt_active` transition false → true: log `capture started`, reset `frames_this_session = 0`.
- On true → false: log `capture stopped (N ms captured)` where `N = frames_this_session * 20`.

For *this* spec, `sink` is a no-op placeholder (the realtime-model spec wires it to OpenAI). The frame-counting log line is observable end-to-end without the model attached.

**`errors.py` additions:**

| Code | Stderr message |
|---|---|
| `microphone_denied` | `"Microphone access is required. Grant it in System Settings → Privacy & Security → Microphone, then re-launch."` |
| `mic_lost` | `"Microphone disconnected (<reason from event>). Restart the tool to use the current default input."` |

Orchestrator exit codes:

| Code | Trigger |
|---|---|
| `4` | `error{code: microphone_denied}` |
| `5` | `error{code: mic_lost}` |

(matches Swift's exit codes — easy to correlate in logs)

### 2.6 Hardcoded values

| Value | Where | Note |
|---|---|---|
| Output format = 16 kHz mono PCM16 | `AudioCapture.swift` converter setup | Per arch §2. |
| Chunk size = 320 samples / 640 bytes (20 ms) | `AudioCapture.swift` ring-buffer flush threshold; Python `readexactly(640)` | Per design decision. |
| Mic watchdog stall threshold = 2.0 s | `AudioCapture.swift` | Same as record. |
| Watchdog drain interval = 10 ms | `AudioCapture.swift` | Same as record. |
| `NSMicrophoneUsageDescription` literal | `swift-capture/Info.plist` | English-only, PoC. |

---

## 3. Impact and Risk Analysis

### System Dependencies

- **AVFoundation** (`AVAudioEngine`, `AVAudioConverter`, `AVCaptureDevice.requestAccess(for: .audio)`).
- **CoreAudio HAL** (`AudioObjectAddPropertyListenerBlock`, the four property selectors listed in §2.2).
- **macOS TCC** for microphone (NSMicrophoneUsageDescription + CFBundleIdentifier embedded via the same `__TEXT,__info_plist` linker trick `record` uses).
- **No new Python dependencies** for this spec (`sounddevice`, `websockets` arrive in later specs).
- **Depends on 001:** the Swift binary process, the stderr JSON-line transport, the `ready` event, and `capture.py`'s subprocess spawning.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| AVAudioEngine silently stops calling the input tap closure when AirPods become default *output* (no input route change, no `AVAudioEngineConfigurationChange`). Reported by `record`'s field notes. | Mic-flow watchdog (2 s stall → restart) copied from record. If restart fails, the same path lands on `error{code: mic_lost}` — explicit, not silent. |
| TCC prompt won't appear when binary is launched via `uv run` in some terminal configurations (e.g., not attached to a controlling TTY). | The orchestrator spawns the Swift binary with stdio pipes but inherits the controlling terminal; same shape as `record install --prime-permissions` which is verified to work. If the prompt is suppressed (e.g., headless run), `AVCaptureDevice.requestAccess` returns `false` immediately — we land on `microphone_denied`, which is the right behavior. |
| Re-keying TCC every build because the binary path is unstable under `swift build`. | Use stable `CFBundleIdentifier` (`com.voicebridge.capture`) in `Info.plist` — TCC keys on bundle ID, not path. Same approach as record. |
| Chunk size of 20 ms costs ~50 syscalls/sec on stdout. Excessive for some macOS pipe configurations? | 50 syscalls/sec is well within macOS pipe throughput (record sustains higher rates). Measured cost is part of the smoke test (§4). |
| Ring-buffer drift if AVAudioConverter produces slightly non-integer-multiple sample counts per device buffer. | Ring buffer is sample-count-based, not buffer-count-based; partial trailing samples carry over. No drift, no accumulated error. |
| `record` lives behind a richer event vocabulary (`source_lost`, `source_attached`); voicebridge collapses these into `error{code: mic_lost}`. Future Phase 2 specs may need finer granularity. | Acceptable for the PoC. If Phase 2 needs distinction, the closed-set `code` token can grow without breaking the existing wire shape. |
| `mic_lost` exit takes down the orchestrator, ending the session. User must restart. | Explicitly per func spec §2.4 — no auto-restart. The error message tells the user what to do. |

---

## 4. Testing Strategy

**Swift unit tests** (`swift-capture/Tests/VoiceBridgeCaptureTests/`, optional, same skip-on-no-XCTest as record):

- `Protocol.swift` round-trip: encode/decode the new `error{code: microphone_denied}` and `error{code: mic_lost}` shapes.
- Ring-buffer logic in `AudioCapture.swift`: feed synthetic `AVAudioPCMBuffer`s of varying sizes, assert output chunks are exactly 640 bytes and that residual samples carry over correctly across calls.
- Listener-attach/detach lifecycle: assert `stop()` calls `AudioObjectRemovePropertyListenerBlock` for every block added in `start()` (leak guard).

**Python unit tests** (`tests/test_capture.py`, `tests/test_gating.py`, `tests/test_errors.py`):

- `capture.pcm_frames()`: given a fake subprocess yielding a known byte stream, the iterator yields exactly N 640-byte chunks and raises `EOFError` on truncation mid-chunk.
- `gating.MicGate.run()`: with a synthetic PCM source and an asyncio `Event`, assert (a) frames during PTT are forwarded, (b) frames outside PTT are dropped, (c) the `capture started / capture stopped (N ms)` log lines fire on each transition with the correct count.
- `errors.py`: each new error code maps to the expected human message and exit code.

**Manual smoke tests** (single author, target Mac):

1. **Happy path.** `uv run python -m voicebridge --source ru --target en` → TCC prompt → grant → terminal shows orchestrator startup → hold ⌥⌘T, speak 3 s, release → confirm exactly one `capture started` and one `capture stopped (≈3000 ms)` log line, no per-frame chatter.
2. **First-launch denial.** Reset mic TCC for the binary (`tccutil reset Microphone com.voicebridge.capture`) → launch → at prompt, deny → orchestrator prints the §2.5 message and exits with code 4 → no orphan process holds the mic (verify via Activity Monitor menu-bar indicator).
3. **Previously denied, no prompt.** Re-launch after step 2 without re-granting → orchestrator exits with code 4 immediately, no prompt.
4. **AirPods route change mid-session.** Start with built-in mic, hold ⌥⌘T briefly to confirm, then connect AirPods (becomes default input + output) → next PTT press uses AirPods mic without restart. Engine config-change recovery exercised live.
5. **USB mic unplug mid-session.** Plug in a USB mic (becomes default) → start tool → confirm capture works → unplug USB mic mid-session → orchestrator logs the §2.5 `mic_lost` message and exits with code 5 within ~2 s (watchdog).
6. **Ctrl-C during active PTT.** Hold ⌥⌘T while speaking, Ctrl-C in terminal → both processes exit cleanly, macOS menu-bar mic indicator disappears within ~1 s.
7. **TCC stability across builds.** Rebuild the binary, re-launch → no new TCC prompt (CFBundleIdentifier preserves the grant).

No formal latency criterion *for the mic stage* — end-to-end latency is the realtime-model spec's success metric. This spec's bar is "the mic stage adds no observable delay beyond one 20 ms chunk."
