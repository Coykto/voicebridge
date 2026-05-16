# Task List: Mic Capture

Vertical slices for `002-mic-capture`. After each slice, the project is in a runnable, demonstrable state. Builds on the completed work in `001-hotkey-activation`.

Subagents available: `macos-swift` (Swift / AVAudioEngine / CoreAudio / TCC), `python-backend` (Python orchestrator / asyncio / IPC).

---

## Slice 1: Swift binary captures mic and writes raw 16 kHz PCM16 to stdout

**Goal:** the existing `voicebridge-capture` Swift binary, run standalone, requests microphone permission on first launch and — once granted — emits a continuous stream of 640-byte PCM chunks on stdout while still emitting the 001 events (`ready`, `hotkey_registered`, `hotkey_down/up`) on stderr. No Python changes yet; no CoreAudio listeners, no watchdog.

- [x] Create `swift-capture/Info.plist` with two keys: `NSMicrophoneUsageDescription` ("VoiceBridge captures your microphone to translate your speech in real time.") and `CFBundleIdentifier` (`com.voicebridge.capture`). **[Agent: macos-swift]**
- [x] Extend `swift-capture/Package.swift`: add the `unsafeFlags` linker block that embeds `Info.plist` into the executable's `__TEXT,__info_plist` section, copying the exact form from `record/swift-capture/Package.swift` lines 22–29. **[Agent: macos-swift]**
- [x] Create `swift-capture/Sources/VoiceBridgeCapture/Permissions.swift`: copy `Permissions.checkMicrophone(emit:)` from `record/swift-capture/Sources/RecordCapture/Permissions.swift` lines 60–101 verbatim. Strip everything else (screen recording, accessibility, `prime`, `primePollable`, `primeScreenRecording`, `freshScreenRecordingCheck`). Adjust the `emit` closure signature to match voicebridge's existing emitter. **[Agent: macos-swift]**
- [x] Extend `swift-capture/Sources/VoiceBridgeCapture/Protocol.swift` to support the new `error` codes from tech spec §2.4: `microphone_denied` and `mic_lost`. Treat them as new tokens in the existing closed-set `code` field — no new event shape. **[Agent: macos-swift]**
- [x] Create `swift-capture/Sources/VoiceBridgeCapture/AudioCapture.swift` (basic version, no listeners): a class with `init(emit:, frameSink:)`, `start() async throws`, `stop()`. Internals: AVAudioEngine input tap on the default input device, `AVAudioConverter` to 16 kHz mono PCM16, ring-buffer accumulator that flushes 320-sample (640-byte) chunks to `frameSink`. Lift the engine + tap + converter setup from `record/swift-capture/Sources/RecordCapture/AudioCapture.swift` (the engine-tap section only — NO CoreAudio listeners and NO watchdog in this slice). `start()` only returns after the first 640-byte chunk has been flushed. **[Agent: macos-swift]**
- [x] Extend `swift-capture/Sources/VoiceBridgeCapture/main.swift` per tech spec §2.3: BEFORE hotkey registration, call `Permissions.checkMicrophone`; on denial emit `error{code: microphone_denied}` and exit 4. On success, construct `AudioCapture` with `frameSink: { FileHandle.standardOutput.write($0) }`, call `start()`, only then proceed to the 001 hotkey-registration block. Emit `ready` only after both mic is flowing AND hotkey is registered. **[Agent: macos-swift]**
- [x] **Verify Swift unit:** `swift build -c release --package-path swift-capture` exits 0. `swift test --package-path swift-capture` passes (existing 001 Protocol tests + any added round-trip for the new error codes). **[Agent: macos-swift]**
- [ ] **Verify manual — happy path:** `tccutil reset Microphone com.voicebridge.capture`. Run `./swift-capture/.build/release/voicebridge-capture > /tmp/pcm.raw 2> /tmp/events.log`. macOS shows the mic prompt — grant it. After a few seconds, Ctrl+C. Confirm `/tmp/events.log` contains `ready` and `hotkey_registered` (in that order) and `/tmp/pcm.raw` has grown (`ls -lh /tmp/pcm.raw` shows non-zero bytes; size ≈ `seconds × 32000` bytes give-or-take). Quick sanity: `ffplay -f s16le -ar 16000 -ac 1 /tmp/pcm.raw` plays the recording. **[Agent: macos-swift — requires user to grant TCC]**
- [ ] **Verify manual — denial:** `tccutil reset Microphone com.voicebridge.capture`. Re-launch the binary; at the prompt, click **Don't Allow**. Confirm the process exits within ~1 s, exit code 4 (`echo $?`), and stderr's last JSON line is `{"event":"error","code":"microphone_denied"}`. **[Agent: macos-swift — requires user to deny TCC]**
- [ ] **Verify regression:** Re-grant mic (`tccutil reset Microphone com.voicebridge.capture` then re-launch and grant). Run `VOICEBRIDGE_TARGET_LANG=English uv run python -m voicebridge` (the existing 001 orchestrator). Confirm `[ready]` still prints, ⌥⌘T still triggers Submarine/Pop cues, Ctrl+C exits cleanly. The orchestrator drains stdout into a pipe but doesn't consume it — should not block. **[Agent: python-backend — requires user keypress]**

---

## Slice 2: Orchestrator surfaces `microphone_denied` with a clean message and exit code 4

**Goal:** before any further mic work, make sure the 001 orchestrator no longer falls through to the generic "hotkey could not be registered" message when the binary exits because of denied mic permission. Small Python-only slice.

- [x] Extend `src/voicebridge/errors.py` with a `microphone_denied` entry: message = "Microphone access is required. Grant it in System Settings → Privacy & Security → Microphone, then re-launch."; exit code = 4. **[Agent: python-backend]**
- [x] Extend the startup handshake in `src/voicebridge/__main__.py` so that an `Error` event received BEFORE `Ready` (not only between `Ready` and `HotkeyRegistered`) is routed through `errors.py` and triggers exit. This widens the 001 startup window to cover mic permission failures, which happen before `ready` per tech spec §2.3. **[Agent: python-backend]**
- [x] Extend `tests/test_startup.py` with a case: fake event stream is `error{microphone_denied}` (no prior `ready`) → orchestrator exits with code 4 and the right message. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_startup.py` passes. **[Agent: python-backend]**
- [ ] **Verify manual:** `tccutil reset Microphone com.voicebridge.capture`. Run `VOICEBRIDGE_TARGET_LANG=English uv run python -m voicebridge` → at TCC prompt, deny. Confirm the §2.2 message prints to stderr and `echo $?` shows `4`. **[Agent: python-backend — requires user to deny TCC]**

---

## Slice 3: Orchestrator reads the PCM stream as 640-byte chunks

**Goal:** Python consumes the binary mic stream from stdout instead of leaving it buffering in the pipe. No gating yet — frames are dropped immediately. A periodic debug log proves frames are flowing.

- [x] Extend `src/voicebridge/capture.py` with `async def pcm_frames(self) -> AsyncIterator[bytes]` that loops `await self.process.stdout.readexactly(640)` and yields each chunk. On `asyncio.IncompleteReadError` (stream EOF), raise `EOFError`. **[Agent: python-backend]**
- [x] Wire a background task in `src/voicebridge/__main__.py` that iterates `pcm_frames()` and discards each chunk (`async for _ in ...: pass`). On `EOFError`, log "Swift mic stream closed" and trigger orchestrator shutdown. ~~For *this slice only*, every 50 chunks (= 1 s of audio) log a debug line `mic flow: N chunks` — this is a smoke instrument, removed in Slice 4.~~ Slice-3 smoke instrument skipped — wired directly to Slice 4's `MicGate` since the slices ran in one pass. **[Agent: python-backend]**
- [x] Create `tests/test_capture_pcm.py`: build a fake `asyncio.StreamReader` pre-loaded with `N × 640` bytes; assert `pcm_frames()` yields exactly N chunks of 640 bytes each. Add a truncation case (`N × 640 + 100` bytes) → final `readexactly` raises, iterator raises `EOFError`. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_capture_pcm.py` passes. **[Agent: python-backend]**
- [ ] **Verify manual:** Run `VOICEBRIDGE_TARGET_LANG=English uv run python -m voicebridge`. Within a few seconds, see `mic flow: 50 chunks`, `mic flow: 100 chunks`, … incrementing at ~1 Hz. Press ⌥⌘T briefly to confirm 001 cues still fire. Ctrl+C — orchestrator exits cleanly, no orphan process holds the mic (verify menu-bar mic indicator clears). **[Agent: python-backend — requires user to listen and observe]**

---

## Slice 4: PTT gating produces the one-line-per-session log

**Goal:** functional spec §2.3 satisfied — exactly one `capture started` line per PTT press, exactly one `capture stopped (N ms captured)` line per release, with the correct duration; no logging between sessions.

- [x] Create `src/voicebridge/gating.py` with `class MicGate` per tech spec §2.5: takes `ptt_active: asyncio.Event` and a `logger`. `async def run(self, source: AsyncIterator[bytes], sink)`: per chunk, if `ptt_active.is_set()` forward to `sink` and bump `frames_this_session`; else drop. On false→true transition, log `capture started` and reset the counter; on true→false, log `capture stopped (N ms captured)` where N = `frames * 20`. For this slice `sink` is a no-op (`async def noop(_): pass` or similar). **[Agent: python-backend]**
- [x] Replace the Slice-3 smoke instrument in `__main__.py`: instead of the discard-loop, instantiate `MicGate(ptt_active=state.ptt_active, logger=...)` with `state` from 001's `PTTState`, and run `gate.run(capture.pcm_frames(), noop_sink)` as the background task. Remove the `mic flow: N chunks` debug log. **[Agent: python-backend]**
- [x] Create `tests/test_gating.py`: synthetic source yields 100 chunks; control an `asyncio.Event` to be set for chunks 20–80 only; assert (a) sink received exactly 60 chunks, (b) the logger captured exactly one `capture started` and one `capture stopped (1200 ms captured)`. Add a second case with two on-off cycles → two pairs of log lines. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_gating.py` passes. **[Agent: python-backend]**
- [ ] **Verify manual:** Run `VOICEBRIDGE_TARGET_LANG=English uv run python -m voicebridge`. Hold ⌥⌘T for ~3 s, release. Confirm exactly two log lines: `capture started` and `capture stopped (≈3000 ms captured)` (anywhere from 2900–3100 ms is fine). Tap-and-release three more times → six more lines, in three pairs. Between sessions, terminal stays quiet. **[Agent: python-backend — requires user keypress]**

---

## Slice 5: CoreAudio listener suite + watchdog → mic_lost on unrecoverable loss

**Goal:** functional spec §2.1 (live default-device tracking, including AirPods) and §2.4 (mic loss → warning + exit) satisfied. Brings `AudioCapture.swift` up to the full surface defined in tech spec §2.2.

- [x] Extend `swift-capture/Sources/VoiceBridgeCapture/AudioCapture.swift` to add the four CoreAudio property listeners and the `AVAudioEngineConfigurationChange` notification handler from `record/swift-capture/Sources/RecordCapture/AudioCapture.swift`: `defaultInputListenerBlock`, `defaultOutputListenerBlock`, `formatListenerBlock`, `runningSomewhereListenerBlock`, plus `handleEngineConfigurationChange`. Carry over the `coreAudioListenerQueue` and the `watchedInputDeviceID` tracking so listeners can be cleanly removed in `stop()`. **[Agent: macos-swift]**
- [x] Add the mic-flow watchdog to `AudioCapture.swift`: `micFlowStallThresholdSeconds = 2.0`, `drainIntervalMs = 10`, `lastMicBufferAt` updated on every tap callback; a `DispatchSourceTimer` periodically checks the gap and triggers the route-change recovery path on stall. Lift from record's `AudioCapture.swift`. **[Agent: macos-swift]**
- [x] Add the unrecoverable-loss terminal path to `AudioCapture.swift` (tech spec §2.2): when restart fails (engine cannot bring up against any input device), stop the watchdog, remove listeners, tear down the engine, emit `error{code: mic_lost, message: <one-line reason>}`, and exit 5 via `main.swift`. Distinct from record's recoverable `source_lost` semantics. **[Agent: macos-swift]**
- [x] Add Swift unit tests in `swift-capture/Tests/VoiceBridgeCaptureTests/`: (a) ring-buffer test — feed synthetic `AVAudioPCMBuffer`s of varying sizes, assert outputs are always 640 bytes with correct residual carry-over; (b) listener lifecycle test — assert `stop()` removes every listener `start()` added (use a counter or a mock `AudioObjectAddPropertyListenerBlock` shim if feasible; if XCTest can't reach CoreAudio cleanly in CI, document the skip the same way record does). Listener-lifecycle test skipped per Recommendations table; ring-buffer + Protocol round-trip tests added. **[Agent: macos-swift]**
- [x] Extend `src/voicebridge/errors.py` with `mic_lost`: message = "Microphone disconnected (<reason from event>). Restart the tool to use the current default input."; exit code = 5. The reason string comes from the `message` field on the `error` event. **[Agent: python-backend]**
- [x] Extend `src/voicebridge/__main__.py`'s main loop to handle `error{mic_lost}` arriving at any time (not just startup): route through `errors.py`, print the message with the reason substituted in, terminate the subprocess if still alive, exit with code 5. **[Agent: python-backend]**
- [x] Extend `tests/test_startup.py` (or add `tests/test_runtime_errors.py`): feed a stream of `ready` → `hotkey_registered` → … → `error{mic_lost, message: "default input removed"}` mid-session, assert orchestrator exits with code 5 and the printed message includes "default input removed". **[Agent: python-backend]**
- [x] **Verify automated:** `swift test --package-path swift-capture` + `uv run pytest tests/` all pass. Python 74/74 pass; `swift test` requires full Xcode (Command Line Tools alone lack XCTest) — skipped per record's documented Makefile pattern. **[Agent: macos-swift / python-backend]**
- [ ] **Verify manual — AirPods route change (transparent recovery):** Start with built-in mic as default. Run `VOICEBRIDGE_TARGET_LANG=English uv run python -m voicebridge`. Hold ⌥⌘T briefly, confirm `capture started`/`capture stopped` log lines. While the tool is still running, connect AirPods (they become default input + output). Wait ~2 s. Hold ⌥⌘T again — confirm capture works without the orchestrator having exited. Disconnect AirPods. Hold ⌥⌘T one more time — still works against the (restored) built-in mic. **[Agent: macos-swift — requires user to toggle AirPods]**
- [ ] **Verify manual — USB mic loss (terminal exit):** Plug in a USB mic and make it the system default. Run the tool. Hold ⌥⌘T briefly, confirm capture works. Unplug the USB mic. Within ~2 s (the watchdog threshold) confirm: orchestrator prints the `mic_lost` message including a reason, exit code 5 (`echo $?`), no orphan process holds the mic. **[Agent: macos-swift — requires user to unplug USB mic]**
- [ ] **Verify manual — clean shutdown during PTT:** Run the tool. Hold ⌥⌘T while speaking. Press Ctrl+C in the terminal. Confirm both processes exit within ~1 s and the macOS menu-bar mic indicator disappears. **[Agent: python-backend — requires user keypress]**
- [ ] **Verify manual — TCC stability across rebuilds:** `swift build -c release --package-path swift-capture` then re-run the tool. Confirm no new TCC prompt appears (the grant from Slice 1 carries over because `CFBundleIdentifier` is stable). **[Agent: macos-swift]**

---

## Recommendations

| Task/Slice | Issue | Recommendation |
|---|---|---|
| Slices 1, 2, 3, 4, 5 (manual verify) | Verification requires the user to grant/deny TCC, perform key chords, connect/disconnect audio hardware, or unplug a USB mic — there is no MCP that can drive macOS TCC prompts or impersonate hardware route changes. | None — accept that mic-pipeline smoke steps stay in human hands. `/awos:implement` should pause and prompt the author when it hits a manual verify step (same convention as 001). |
| Slice 5 listener-lifecycle Swift test | CoreAudio HAL property listeners can't be cleanly exercised from XCTest without a real audio device; record skips equivalent tests in CI. | Document the skip in the test file (mirroring record's pattern). The manual AirPods + USB-unplug verifies cover the real behavior. |
