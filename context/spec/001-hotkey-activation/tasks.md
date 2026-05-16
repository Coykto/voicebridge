# Task List: Hotkey Activation

Vertical slices for `001-hotkey-activation`. After each slice, the project is in a runnable, demonstrable state.

Subagents available: `macos-swift` (Swift / Carbon / AVAudioEngine), `python-backend` (Python orchestrator / asyncio / IPC).

---

## Slice 1: Swift binary prints `hotkey_down` / `hotkey_up` to stderr when ⌥⌘T is pressed from any app

**Goal:** a standalone Swift executable that, when launched, registers ⌥⌘T globally and emits one JSON line per key event. No Python yet, no audio.

- [x] Create `swift-capture/` SPM package: `Package.swift` declaring an executable target `voicebridge-capture` with `Sources/VoiceBridgeCapture/`. Use record's `Package.swift` as a template (same Swift tools version, same macOS deployment target). **[Agent: macos-swift]**
- [x] Create `swift-capture/Sources/VoiceBridgeCapture/Protocol.swift` with `Codable` types for the five event shapes in tech spec §2.4 (`ready`, `hotkey_registered`, `hotkey_down`, `hotkey_up`, `error`) and a small JSON-line writer that prints to `FileHandle.standardError`. **[Agent: macos-swift]**
- [x] Port `swift-capture/Sources/VoiceBridgeCapture/HotkeyMonitor.swift` from `record/swift-capture/Sources/RecordCapture/HotkeyMonitor.swift` and **extend**: add a second `EventTypeSpec` for `kEventHotKeyReleased`, take both `onPress` and `onRelease` closures, dispatch by `GetEventKind(event)`. Keep the closed-set error tokens and the `AXIsProcessTrusted()` defensive check. **[Agent: macos-swift]**
- [x] Create `swift-capture/Sources/VoiceBridgeCapture/main.swift`: emit `ready`, call `AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt: true])` on first run to surface the prompt, register chord `[.option, .cmd] + "t"`, emit `hotkey_registered` on success, emit `error` on failure and exit non-zero. On press → emit `hotkey_down` with ISO8601 timestamp; on release → `hotkey_up`. Spin a `CFRunLoopRun()` to keep the process alive. **[Agent: macos-swift]**
- [x] **Verify:** Run `swift build -c release --package-path swift-capture` — exits 0. Run `./swift-capture/.build/release/voicebridge-capture 2>&1 1>/dev/null`. Confirm `ready` and `hotkey_registered` lines appear. Focus another app (e.g. browser), press and hold ⌥⌘T → `hotkey_down` line appears in the launching terminal; release → `hotkey_up`. Ctrl+C to exit. **[Agent: macos-swift — requires user to perform the keypress]**

---

## Slice 2: Python orchestrator launches the Swift binary and surfaces parsed events

**Goal:** `python -m voicebridge` spawns the Swift binary, performs the startup handshake, and prints typed Python events for each `hotkey_down` / `hotkey_up`. Still no audio cues, no real translation.

- [x] Create `pyproject.toml` at repo root with `voicebridge` package, `uv`-managed, Python ≥3.11, package layout `src/voicebridge/`. No third-party deps yet beyond what's in stdlib (`asyncio`, `json`). **[Agent: python-backend]**
- [x] Create `src/voicebridge/__init__.py`, `src/voicebridge/ipc.py` exposing typed events (e.g. `@dataclass Ready`, `HotkeyRegistered`, `HotkeyDown`, `HotkeyUp`, `Error`) and an async generator that reads lines from a stream, parses each as JSON, dispatches to typed events. Free-form (non-JSON) lines are returned as a separate `LogLine` event. **[Agent: python-backend]**
- [x] Create `src/voicebridge/capture.py`: `async def spawn() -> tuple[Process, AsyncIterator[Event]]` that runs `asyncio.create_subprocess_exec("./swift-capture/.build/release/voicebridge-capture", stdout=PIPE, stderr=PIPE)` and exposes the parsed stderr stream. Stdout (PCM) is exposed but not consumed in this slice. **[Agent: python-backend]**
- [x] Create `src/voicebridge/__main__.py`: parse `--source` / `--target` (accept but don't use yet), call `spawn()`, await `Ready` (5 s timeout) then `HotkeyRegistered` (5 s timeout). On success, print `[ready] press ⌥⌘T to translate (Ctrl+C to quit)` and loop forever printing `down` / `up` for each event. Free-form `LogLine` events go to `./logs/<ts>-capture.log` (auto-create directory). **[Agent: python-backend]**
- [x] Create `tests/test_ipc.py`: feed crafted JSON-line strings into the parser, assert the right typed events come out; include malformed lines and confirm they appear as `LogLine`. **[Agent: python-backend]**
- [x] **Verify:** `uv sync` then `uv run pytest tests/test_ipc.py` — all pass. `uv run python -m voicebridge --source ru --target en` — Swift launches, "[ready]" prints. Press ⌥⌘T from a focused browser → `down` then `up` print on each tap. Ctrl+C cleanly terminates Swift subprocess. **[Agent: python-backend — requires user to perform the keypress]**

---

## Slice 3: Audible Submarine / Pop cues fire on press / release

**Goal:** functional spec §2.1 and §2.2 satisfied — the author hears the cue without looking at the terminal.

- [x] Create `src/voicebridge/hotkey.py`: define `class PTTState` holding an `asyncio.Event` (`ptt_active`), and methods `on_down()` / `on_up()` that toggle the flag and fire-and-forget `subprocess.Popen(["afplay", "/System/Library/Sounds/Submarine.aiff"])` (resp. `Pop.aiff`). Catch `FileNotFoundError` / `OSError` and log a warning without raising. **[Agent: python-backend]**
- [x] Wire `hotkey.py` into `__main__.py`: instantiate `PTTState` after `HotkeyRegistered`; route `HotkeyDown` → `state.on_down()`, `HotkeyUp` → `state.on_up()`. Keep the `down` / `up` console prints from Slice 2 for now (sanity). **[Agent: python-backend]**
- [x] Add `tests/test_hotkey.py`: monkeypatch `subprocess.Popen`, feed a `down` then `up`, assert (a) `ptt_active` transitions set→clear, (b) `Popen` was called with the Submarine path first and Pop path second. **[Agent: python-backend]**
- [x] **Verify:** `uv run pytest tests/test_hotkey.py` — all pass. `uv run python -m voicebridge --source ru --target en`, focus a non-terminal app, press and hold ⌥⌘T → hear Submarine; release → hear Pop. Rapid tap a half-dozen times → no crash, overlapping sounds are acceptable. **[Agent: python-backend — requires user to listen]**

---

## Slice 4: Startup errors (Accessibility denied, hotkey conflict) print clear messages and exit cleanly

**Goal:** functional spec §2.4 and §2.5 satisfied. The orchestrator never silently hangs on a startup failure.

- [x] Create `src/voicebridge/errors.py` mapping the closed set of error codes from tech spec §2.4 to (human-readable message, exit code) tuples:
  - `accessibility_denied` → §2.4 wording, exit 2
  - `conflict` → §2.5 wording, exit 1
  - everything else → "hotkey could not be registered: `<code>`", exit 1
  **[Agent: python-backend]**
- [x] Extend `__main__.py` startup: while waiting for `HotkeyRegistered`, also accept `Error`; on `Error`, look up via `errors.py`, print the message to stderr, terminate the Swift subprocess, exit with the mapped code. Add a timeout branch: if neither event arrives within 5 s, kill subprocess, print "Swift capture did not start in time", exit 3. **[Agent: python-backend]**
- [x] Add `tests/test_startup.py`: feed a fake event stream of (`ready` → `error{accessibility_denied}`), assert the process exits with code 2 and the right message; repeat for `conflict` (exit 1) and `param_err` (exit 1, generic message); and for timeout (exit 3). **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/` — all three test files pass. **[Agent: python-backend]**
- [ ] **Verify manual — AX denied:** Open System Settings → Privacy & Security → Accessibility → toggle the `voicebridge-capture` binary OFF (or its parent terminal). Run `uv run python -m voicebridge --source ru --target en` → see the Accessibility error message naming System Settings → process exits with code 2 (`echo $?`). Re-enable AX afterward. **[Agent: python-backend — requires user to toggle System Settings]**
- [ ] **Verify manual — conflict:** Bind ⌥⌘T in another app (Hammerspoon one-liner: `hs.hotkey.bind({"alt","cmd"}, "T", function() end)`, or Keyboard Maestro). Run `uv run python -m voicebridge ...` → see the conflict error → exit code 1. Unbind afterward. **[Agent: python-backend — requires user to bind ⌥⌘T in another tool]**

---

## Slice 5: Focus-switch mid-hold smoke test (Risk mitigation gate)

**Goal:** confirm the open risk from tech spec §3 — that `kEventHotKeyReleased` survives focus changes during a hold — *before* downstream slices depend on PTT semantics. If this fails, escalate to the polling fallback noted in the risk table.

- [ ] **Verify manual:** Run `uv run python -m voicebridge --source ru --target en`. Focus Chrome. Press and hold ⌥⌘T. While still holding, ⌘-Tab to Finder. While still holding, ⌘-Tab back to Chrome. Release. Confirm: exactly one `hotkey_down` log, exactly one `hotkey_up` log, Submarine + Pop both played. **[Agent: macos-swift — requires user-driven app switching]**
- [ ] **Conditional escalation:** If the manual test in the previous step fails (no `hotkey_up` or a duplicate event), open a follow-up note in `tasks.md` to implement the `CGEventSource.keyState` polling fallback described in tech spec §3 — do not implement preemptively. **[Agent: macos-swift]**

---

## Recommendations

| Task/Slice | Issue | Recommendation |
|---|---|---|
| Slices 1, 2, 3, 4 (manual verify), Slice 5 | Verification requires a human keypress, audible listening, or System Settings toggle — there's no MCP that can drive macOS Accessibility-protected global hotkeys. | None — accept that the manual smoke steps stay in human hands. `/awos:implement` should pause and prompt the author when it hits a manual verify step. |
