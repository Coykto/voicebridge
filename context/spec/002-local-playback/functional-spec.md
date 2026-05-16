# Functional Specification: Local Playback

- **Roadmap Item:** Local Playback — translated audio frames are played out of the Mac's default output device as they arrive, producing the "translation starts mid-sentence" effect the PoC exists to evaluate.
- **Status:** Draft
- **Author:** Evgenii Basmov

---

## 1. Overview and Rationale (The "Why")

The entire PoC exists to answer one question: *does streaming push-to-talk through a realtime translation model feel fast enough to use mid-conversation?* Local Playback is the surface that answers it — it's where the author actually **hears** the latency. Every other Phase 1 item (hotkey, mic capture, realtime model plumbing) is plumbing leading to this moment.

The defining behavior is **mid-sentence playback**: as soon as the realtime model emits its first translated audio frame, that frame plays. The author hears their translated voice begin before they have finished speaking the source-language sentence. If playback waited for the model to finish the whole utterance, the experience would read as "speak, pause, hear translation" — exactly what the PoC is trying to disprove.

For the PoC, the destination is the Mac's **current default system output device** — whatever macOS is currently routing system audio to (headphones, AirPods, built-in speakers, etc.). No device picker, no virtual-mic routing (that's a separate Phase 2 item), no monitoring setup.

**Success for this slice:**

- During a ~10-minute hands-on session, holding PTT and speaking produces translated audio that starts arriving *before* the author finishes the source sentence.
- Audio plays out of the same output device the author already uses for system audio — no extra setup.
- A full session runs without dropouts, crackle that overwhelms the speech, or the playback stream silently stalling.

---

## 2. Functional Requirements (The "What")

### 2.1 Translated audio plays as it arrives

- **As the** author, **when I** hold the push-to-talk key and begin speaking, **I want** translated audio to begin playing out of my current output device as soon as the model produces the first frame — not after the model has finished the full translated utterance.
- **Acceptance Criteria:**
  - [ ] Playback starts within a perceptibly short delay after the model emits the first translated audio (subjective: feels like a "live" delay, not a "press then wait" delay).
  - [ ] While the author keeps speaking with the key held, translated audio keeps flowing out without gaps that read as a stall.
  - [ ] The author hears the translation begin **before** they finish speaking the source sentence on at least some utterances during the session — this is the "translation starts mid-sentence" effect the PoC is testing.
  - [ ] No buffering policy holds the first frame longer than is needed to keep subsequent playback gap-free.

### 2.2 Playback uses the Mac's current default output device

- **As the** author, **when I** start VoiceBridge, **I want** translated audio to come out of whichever output device macOS is currently using for system audio — so I don't have to configure a device.
- **Acceptance Criteria:**
  - [ ] On startup, VoiceBridge sends translated audio to the Mac's current default output device.
  - [ ] If the system default is AirPods, audio plays through AirPods; if it's the built-in speakers, it plays through the built-in speakers. Whatever macOS calls "default" at the moment playback starts.
  - [ ] No CLI flag, no config file, no in-tool picker for choosing a different device.
  - [ ] Behavior when the macOS default output device changes mid-session (e.g., AirPods connect, headphones unplug) is explicitly **undefined** for the PoC — see Out-of-Scope.

### 2.3 Volume is controlled by the system

- **As the** author, **when** translated audio is too loud or too quiet, **I want** to adjust it the same way I adjust everything else — using macOS system volume and my headphone hardware.
- **Acceptance Criteria:**
  - [ ] VoiceBridge exposes no in-tool volume control, gain flag, or runtime command for loudness.
  - [ ] macOS system volume changes (function keys, Control Center, headphone buttons) affect the translated audio just like any other app's audio.

### 2.4 Trailing audio finishes after PTT release

- **As the** author, **when I** release the push-to-talk key while the model is still streaming translated audio, **I want** the in-flight translation to finish playing — so I hear a natural tail instead of a clipped final word.
- **Acceptance Criteria:**
  - [ ] Releasing PTT closes the microphone input (defined by the Hotkey Activation spec) but does **not** stop playback of audio the model has already produced.
  - [ ] Any translated audio frames the model emits after release — for words the author had already finished saying before release — still play.
  - [ ] When the model stops emitting frames, playback stops naturally. There is no fixed cutoff timer.

### 2.5 No additional audible signals from playback itself

- **As the** author, **when** the PoC is idle and no PTT press is active, **I want** silence from the output device — no readiness tone, no heartbeat, no test buffer.
- **Acceptance Criteria:**
  - [ ] When VoiceBridge has connected to the realtime model and is waiting for PTT, the output device is silent.
  - [ ] No tone is played by Local Playback to indicate "ready" or "connection healthy." (The Hotkey Activation spec already defines press/release cues; this spec adds none of its own.)
  - [ ] Between PTT presses, with no translated audio arriving, the output device is silent — no audible buffer flush, no end-of-stream click.

### 2.6 Playback runs cleanly for a full session

- **As the** author, **during** a ~10-minute back-and-forth test session, **I want** playback to keep working without me having to restart the process or fiddle with audio.
- **Acceptance Criteria:**
  - [ ] Across many PTT presses in a ~10-minute window, every translated utterance is heard end-to-end — none are silently dropped on the playback side.
  - [ ] No audible artifacts dominate the speech. Some crackle or compression-style artifacts are acceptable for a realtime-model PoC; outright dropouts that hide whole words are not.
  - [ ] If the audio output path does fail, the failure is visible in the terminal logs — the author is not left guessing whether the model went silent or the playback broke.

---

## 3. Scope and Boundaries

### In-Scope

- Streaming playback of translated audio frames as they arrive from the realtime model.
- Output destination: the Mac's current default output device only.
- Volume controlled exclusively by macOS system volume and hardware.
- After PTT release, in-flight translated audio is allowed to finish playing.
- Silent idle behavior — no readiness/heartbeat tones from this subsystem.
- Stability sufficient for a ~10-minute single-author session.

### Out-of-Scope

- **BlackHole / virtual-mic routing.** Sending translated audio to a virtual device so Google Meet can pick it up is a separate Phase 2 roadmap item.
- **Aggregate-device monitoring.** Hearing your own real voice plus the translated voice on different channels is a separate Phase 2 roadmap item.
- **Device picker / configurable output device.** Default device only; no CLI flag or UI.
- **In-tool volume / gain control.** None — system volume only.
- **Re-press while previous translation is still playing back.** Out of scope by agreement — the author won't intentionally do this during the 10-minute test, and behavior is undefined.
- **Default-output-device changes mid-session.** Behavior when the macOS default output changes after VoiceBridge has started is explicitly undefined for the PoC. The author will not change devices mid-session during the hands-on test.
- **Translating / playing back the other side of the call.** PoC translates only the author's outgoing speech.
- **VAD-driven continuous capture** and the **discrete STT → LLM → TTS pipeline.** Both Phase 2 roadmap items.
- **All other Phase 1 roadmap items:** Hotkey Activation, Mic Capture, and Realtime Model Plumbing are separate specifications.
