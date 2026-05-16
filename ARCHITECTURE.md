# VoiceBridge — Architecture Sketch

Real-time voice translation for calls: capture my mic, transcribe → translate → TTS,
and feed the altered audio into a call app (Google Meet, etc.) in place of my own voice.

Status: **design sketch**, not yet a project. Lives as a sibling to `record` at
`/Users/e/PycharmProjects/PythonProject/voicebridge/`. The `record` project
(`/Users/e/PycharmProjects/PythonProject/record/`) is used as a reference for
proven approaches — see [Reference points](#reference-points-in-record).

---

## Goal & constraints

- Input: my real microphone.
- Output: translated speech, injected as a "microphone" the call app can select.
- **Invisible to other call participants** — OS-level audio only, no meeting bots,
  no recording-API integrations, no banners. (Same principle `record` follows.)
- Swift for mic capture + CoreAudio device manipulation, **self-signed, no Developer ID**.
- Python for the STT → translate → TTS pipeline (Pipecat or a realtime model).
- Latency is acceptable; turn-taking cadence ("walkie-talkie") is acceptable.

---

## Data flow

```
   real mic
      │
      ▼
┌─────────────┐   PCM frames (16k mono)   ┌──────────────────────┐
│ Swift       │ ────────────────────────▶ │ Python pipeline      │
│ capture +   │                           │                      │
│ device I/O  │                           │  VAD → STT →         │
│  (self-     │                           │   translate → TTS    │
│   signed)   │ ◀──────────────────────── │                      │
└─────────────┘   PCM frames (TTS out)    └──────────────────────┘
      │
      ▼
  BlackHole (virtual audio device)  ──▶  selected as "microphone" in Google Meet
      │
      └──▶ (optional) aggregate device → my headphones, to monitor what the caller hears
```

This is the same shape as `record`'s Swift-capture-binary / Python-orchestrator
split, except audio flows **both directions** instead of capture-only.

---

## Component split

### Swift side — owns everything touching CoreAudio + TCC

- **Mic capture** — `AVAudioEngine` input tap → PCM buffers, resampled to 16 kHz mono.
  Directly analogous to `record`'s existing mic capture.
- **Playback into BlackHole** — do **not** use `AVAudioEngine` for output here; it
  does not cleanly let you target a non-default output device on macOS. Use a plain
  **AUHAL output unit** (`kAudioUnitSubType_HALOutput`) with
  `kAudioOutputUnitProperty_CurrentDevice` set to the BlackHole device, fed by a
  ring buffer that the Python TTS frames write into.
- **Device manipulation** — enumerate CoreAudio devices, locate BlackHole,
  optionally build an **Aggregate Device** so I can still monitor my own output.
- **IPC** — JSON-line control messages on stdio (same pattern as `record`), plus
  raw PCM over a separate socket / fd in both directions.

### Python side — pure pipeline, no real device I/O

Reads/writes PCM frames from the Swift pipe. Stages:

1. **VAD** (e.g. Silero) — segment utterances; emit silence to BlackHole while I speak.
2. **STT** — streaming (Deepgram is already used in `record`, natural starting point).
3. **Translate** — LLM with a translation system prompt, or a dedicated translation API.
4. **TTS** — streaming TTS for low latency.

---

## Key decision: do NOT write a custom HAL driver

This is what makes "self-signed, no Developer ID" actually viable.

A custom CoreAudio HAL plugin (your own virtual mic) must live in
`/Library/Audio/Plug-Ins/HAL/` and be loaded by `coreaudiod`, which on modern
macOS effectively requires a valid signature + notarization. **Use BlackHole
instead** — it is already notarized, installs separately, and the self-signed
Swift app only needs to *write* to it.

Everything the Swift app does — mic capture (TCC mic permission works with ad-hoc
signing), enumerating devices, creating aggregate devices, writing to BlackHole via
AUHAL — needs **no special entitlement and no Developer ID**. The signing
constraint only bites if you try to *be* a driver.

---

## Pipeline options

### Option A — discrete STT → translate → TTS (Pipecat)

Pipecat is purpose-built for streaming voice pipelines. Caveats:

- **Transport-centric** (Daily/WebRTC/websocket/telephony). For a local PCM-pipe
  setup, write a small **custom transport** that reads/writes frames from the Swift
  socket. This is the main glue we own. In exchange: VAD, streaming STT/TTS service
  abstractions, frame timing, and interruption handling come for free.
- **"Translate" is not first-class** — chain STT → LLM (translation prompt) → TTS.
  Works, but it is assembled by hand.

### Option B — speech-to-speech realtime model

OpenAI Realtime / Gemini Live do STT + translate + TTS in one model with much
lower latency (Pipecat can also drive these). If "translate" can be a
system-prompt instruction, this collapses the 3-stage pipeline and removes the
biggest latency source.

**Recommendation:** start with Option B for latency, keep Option A as the fallback
when a translation quality/cost knob is needed.

---

## Latency & turn-taking

- VAD-gated, utterance-level. While I speak, BlackHole receives silence; after I
  finish, the pipeline runs and TTS plays into BlackHole.
- The caller hears: silence while I talk → my translated voice a few seconds later.
  This is the "walkie-talkie" cadence — inherent to the discrete pipeline.
- Partial-STT streaming can shave latency but hurts translation quality; keep
  translation utterance-level.

---

## Reference points in `record`

Full paths, so they remain valid if VoiceBridge becomes its own project:

- Mic capture via AVFoundation:
  `/Users/e/PycharmProjects/PythonProject/record/swift-capture/Sources/RecordCapture/AudioCapture.swift`
- TCC permission handling (self-signed friendly):
  `/Users/e/PycharmProjects/PythonProject/record/swift-capture/Sources/RecordCapture/Permissions.swift`
- JSON-line IPC protocol (Swift side):
  `/Users/e/PycharmProjects/PythonProject/record/swift-capture/Sources/RecordCapture/Protocol.swift`
- Subprocess entrypoint / stdio event loop:
  `/Users/e/PycharmProjects/PythonProject/record/swift-capture/Sources/RecordCapture/main.swift`
- SPM package setup / self-signed build:
  `/Users/e/PycharmProjects/PythonProject/record/swift-capture/Package.swift`
- JSON-line IPC protocol (Python side):
  `/Users/e/PycharmProjects/PythonProject/record/src/record/ipc.py`
- Subprocess supervision / lifecycle:
  `/Users/e/PycharmProjects/PythonProject/record/src/record/supervisor.py`
- Capture orchestration:
  `/Users/e/PycharmProjects/PythonProject/record/src/record/capture.py`
- Overall architecture doc (format reference):
  `/Users/e/PycharmProjects/PythonProject/record/context/product/architecture.md`

---

## Open questions

- PCM transport between Swift and Python: Unix domain socket vs. fd vs. shared
  memory ring buffer — latency vs. simplicity.
- Aggregate-device monitoring: build it programmatically vs. ask the user to set
  it up once.
- BlackHole as a runtime dependency: bundle an installer flow vs. document manual
  install.
- Sample-rate strategy: where to resample (Swift vs. Python), and the BlackHole
  device rate (48k vs. 16k).
- Interruption semantics: with the discrete pipeline, the "user" and the "bot" are
  both me — Pipecat's interruption model may not map cleanly.
