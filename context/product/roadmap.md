# Product Roadmap: VoiceBridge

_This roadmap outlines our strategic direction based on customer needs and business goals. It focuses on the "what" and "why," not the technical "how."_

---

### Phase 1 — Proof of Concept

_The four pieces required to demo end-to-end streaming translation. None of them ships value on its own; together they're the PoC._

- [ ] **Hotkey Activation:** Holding a configured key opens the input stream; releasing it closes the stream. Nothing flows when the key is up.
- [ ] **Mic Capture:** A Swift process captures audio from the default microphone and streams PCM frames to a Python orchestrator over stdio/socket, reusing the IPC patterns from the sibling `record` project.
- [ ] **Realtime Model Plumbing:** The Python orchestrator opens a live session to a realtime speech-to-speech model (OpenAI Realtime or Gemini Live) with a translation system prompt, configurable source/target languages at startup, and streams mic frames in / translated audio frames out.
- [ ] **Local Playback:** Translated audio frames are played out of the Mac's default output device as they arrive, producing the "translation starts mid-sentence" effect the PoC exists to evaluate.

---

### Phase 2 — Post-PoC

_Only pursued if the PoC ends with a "yes, this feels usable." Order and scope may be refined based on what the PoC actually revealed._

- [ ] **Invisible Call Integration**
  - [ ] **BlackHole Output Routing:** Send translated audio to BlackHole instead of (or in addition to) local speakers so that Google Meet and similar apps can select it as a microphone — the core "invisible to other participants" value proposition from the architecture sketch.
  - [ ] **Aggregate Device Monitoring:** Build or document an aggregate-device setup so the author can hear both their own real voice and the translated output without one masking the other.

- [ ] **Continuous Capture**
  - [ ] **VAD-Driven Activation:** Replace push-to-talk with voice-activity detection so the user doesn't need to hold a key on a live call. PTT remains available as a fallback.

- [ ] **Quality & Control Knobs**
  - [ ] **Discrete Pipeline Fallback (Option A):** Implement the STT → LLM → TTS path from the architecture sketch as an alternative backend when translation quality or cost control matters more than minimal latency.
