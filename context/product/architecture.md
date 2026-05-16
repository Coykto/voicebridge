# System Architecture Overview: VoiceBridge (PoC)

_Scope: Phase 1 of the roadmap — the four-piece proof of concept. Phase 2 components (BlackHole routing, VAD, discrete pipeline) are deliberately out of scope and not described here._

---

## 1. Application & Technology Stack

- **Swift capture process:** Swift Package Manager executable, mirroring the structure of the sibling `record` project's `swift-capture` SPM package. Self-signed, no Developer ID, no Xcode project, no app bundle.
- **Python orchestrator:** Single-process async Python application using `asyncio`. Dependencies pinned and run via `uv` (`uv run`). No web framework, no Pipecat — the PoC is a plain script that wires Swift IPC to the realtime model.
- **Inter-process IPC:** Python launches the Swift binary as a subprocess. **Stdout** is reserved for raw PCM mic frames (binary, no framing other than fixed-size buffers). **Stderr** carries JSON-line control messages (hotkey events, status, errors) plus human-readable logs. Mirrors the JSON-line protocol in `record/swift-capture/Sources/RecordCapture/Protocol.swift` and `record/src/record/ipc.py`.
- **Global hotkey:** Owned by the Swift side, using Carbon's `RegisterEventHotKey` API. Reuses the `HotkeyMonitor` pattern from `record/swift-capture/Sources/RecordCapture/HotkeyMonitor.swift`, extended to subscribe to **both** `kEventHotKeyPressed` and `kEventHotKeyReleased` so the orchestrator receives separate `hotkey_down` / `hotkey_up` JSON events for streaming push-to-talk semantics. Carbon's hotkey API does not require Accessibility TCC at registration, which preserves the self-signed-friendly property.

---

## 2. Audio I/O

- **Microphone capture (Swift):** `AVAudioEngine` input tap on the default input device, reusing the pattern from `record/swift-capture/Sources/RecordCapture/AudioCapture.swift`. Captured buffers are converted in Swift to **24 kHz mono PCM16** and streamed over stdout to Python. Capture runs continuously while the process is alive; gating to "only while hotkey is held" is done on the orchestrator side (Python drops frames received while the PTT flag is false), so we don't fight `AVAudioEngine`'s start/stop semantics inside fast key chords.
- **Playback (Python):** Python uses the `sounddevice` library (PortAudio binding) to write PCM frames returned by the realtime model directly to the **default output device** (system speakers / headphones). Swift is not involved in playback for the PoC. This sidesteps the `ARCHITECTURE.md` note about `AVAudioEngine` not cleanly targeting non-default output devices, which only becomes a problem in Phase 2 when we need to target BlackHole.
- **Sample rate strategy:** Two fixed rates, no negotiation.
  - **Mic → model:** Swift resamples to **24 kHz mono PCM16** before sending to Python.
  - **Model → speakers:** OpenAI Realtime returns 24 kHz PCM16; `sounddevice` plays at 24 kHz and the OS handles any device-level resampling.

---

## 3. External Services & APIs

- **Realtime translation model:** **OpenAI Realtime API** over WebSocket. The orchestrator opens one session per PoC run, configured with a translation system prompt. Streams 24 kHz PCM16 mic frames in, receives 24 kHz PCM16 audio frames out as they arrive. Languages are configured via `.env` (`VOICEBRIDGE_TARGET_LANG=English` or `Spanish`). The source is fixed at Russian for the PoC and auto-detected by the model.
- **API key:** Read from a local `.env` file via `python-dotenv`. The `.env` file is gitignored. Expected variable: `OPENAI_API_KEY`.
- **Translation prompt:** No system-prompt template. The translation endpoint takes a target-language ISO code via `audio.output.language` on the `session.update` event. The source language is auto-detected by the model.

---

## 4. Observability & Monitoring

- **Logging:** Both Swift and Python write log files under `./logs/`. One file per process per session, named with an ISO timestamp (e.g. `logs/2026-05-16T14-32-08-orchestrator.log`, `…-capture.log`). Plain-text, structured-ish (`<timestamp> <level> <message>`). The orchestrator's stderr is also tee'd to its log file so the user can watch the terminal live during a run. No log rotation, no file size cap — sessions are short and the user is expected to clean `./logs/` manually.
- **Latency instrumentation:** The orchestrator emits structured timing events to a per-session JSONL file under `./logs/` (e.g. `…-timings.jsonl`). For each push-to-talk turn it stamps:
  - `hotkey_down` — release of the streaming gate.
  - `first_mic_frame_sent` — first PCM frame forwarded to the model.
  - `first_model_audio_received` — first audio chunk back from the model.
  - `first_audio_frame_played` — first frame written to `sounddevice`.
  - `hotkey_up` — close of the streaming gate.

  At session end, the orchestrator prints a small summary table to stderr with the **p50 and p95** of each inter-stage delta across all PTT turns in the session. This summary is the primary artifact for judging "does latency feel acceptable" against the PoC's success criterion.
- **No external monitoring.** No metrics export, no error reporting service. The terminal and `./logs/` are the entire surface.
