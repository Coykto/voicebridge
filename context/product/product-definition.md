# Product Definition: VoiceBridge (PoC)

- **Version:** 1.0
- **Status:** Proposed — PoC scope

---

## 1. The Big Picture (The "Why")

### 1.1. Project Vision & Purpose

VoiceBridge lets a person speak their native language on a video call while other participants hear them in a different language — in real time, with no meeting bots, no recording-API integrations, and no banners visible to other participants. It is a personal, OS-level audio layer that makes language differences disappear from one-on-one and small-group calls.

**The PoC has a narrower goal:** prove that streaming push-to-talk through a realtime speech-to-speech model produces a latency and turn-taking experience that *feels usable in conversation*. Everything else — virtual-mic routing into call apps, monitoring, packaging — is deferred until the latency/UX question is answered.

### 1.2. Target Audience

The PoC is a personal tool for its author only — a single developer running it on their own Mac. Polish, multi-user concerns, and install UX are explicitly not in scope. The eventual product is aimed at people who regularly take calls with participants who speak a different language and don't want a third-party assistant or bot visible to those participants.

### 1.3. User Personas

- **Persona 1: "The Author"** (the only persona for the PoC)
  - **Role:** Developer building the tool, also its first and only user during the PoC.
  - **Goal:** Find out whether streaming PTT through a realtime translation model feels fast enough to actually use mid-conversation — before investing in CoreAudio device routing, packaging, or a second persona.
  - **Frustration:** Existing meeting-translation tools are either bots that announce themselves to the room, browser extensions tied to one platform, or async transcript tools that don't translate the user's own outgoing voice.

### 1.4. Success Metrics

The PoC succeeds if both of these hold during a hands-on test session:

- **Latency feels acceptable** — from the moment the user starts speaking with the push-to-talk key held, translated audio begins playing soon enough that the experience reads as "live translation," not "wait, then hear playback." The author judges this subjectively; no automated SLA.
- **Plumbing just runs** — Swift mic capture, the Python pipeline, the realtime-model connection, and audio playback stay stable for a full ~10-minute session without crashes, dropouts, or manual restarts.

Explicit non-metrics for the PoC: translation quality, real-call success in Google Meet, multi-user reliability, install ergonomics.

---

## 2. The Product Experience (The "What")

### 2.1. Core Features

- **Streaming push-to-talk capture.** Holding a designated key opens a live mic stream; releasing closes it. Nothing is captured or transmitted while the key is up.
- **Realtime speech-to-speech translation.** While the key is held, mic audio streams to a realtime model (OpenAI Realtime or Gemini Live) configured with a translation system prompt. The model produces translated audio as soon as it can — often before the user finishes the sentence.
- **Immediate local playback.** Translated audio is played out of the Mac's default output device (speakers/headphones) as it arrives. The author hears their own translated voice; no virtual microphone or call app is involved in the PoC.
- **Configurable language pair (one direction).** Source and target languages are set at startup. One direction per session — the user's speech is translated; the other party's audio is not captured or processed.

### 2.2. User Journey

The author launches VoiceBridge from a terminal, specifying the source and target languages (e.g. Russian → English). The tool initializes mic capture, connects to the realtime model, and waits. The author presses and holds the push-to-talk key and begins speaking in the source language; within a short delay, translated speech begins playing out of their headphones. They keep speaking; translated audio continues to flow. They release the key; the mic stream closes and playback ends shortly after. They press again to translate the next utterance. After ~10 minutes of back-and-forth, they stop the process and judge whether the latency felt usable.

---

## 3. Project Boundaries

### 3.1. What's In-Scope for this Version

- Swift mic capture, reusing patterns from the sibling `record` project.
- Python orchestrator that connects to a realtime speech-to-speech model (OpenAI Realtime or Gemini Live).
- Streaming push-to-talk activation tied to a single configurable key.
- Configurable source and target language at startup, one direction per session.
- Playback of translated audio to the Mac's default system output device.
- Stability sufficient for a single ~10-minute hands-on session.

### 3.2. What's Out-of-Scope (Non-Goals)

- **BlackHole or any virtual-microphone routing.** Output goes to system speakers/headphones only. Routing into Google Meet as a fake mic is a later phase.
- **Aggregate devices and monitoring.** No programmatic device creation, no "hear yourself + translated voice on different ears" setup.
- **Translating the other side of the call.** The PoC only translates the user's own outgoing speech. Capturing the caller's audio is not included.
- **Always-on / VAD-driven capture.** The PoC uses push-to-talk only; VAD-segmented continuous capture is deferred.
- **The discrete STT → LLM → TTS pipeline (Option A in the architecture sketch).** The PoC commits to the realtime speech-to-speech model only. The discrete pipeline remains a documented fallback, not built.
- **Installer, packaging, or distribution.** Running from source via a script is the only supported way to launch the PoC.
- **Polish:** no UI, no settings, no error reporting beyond logs, no multi-user/multi-session support.
