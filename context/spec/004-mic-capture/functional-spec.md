# Functional Specification: Mic Capture

- **Roadmap Item:** Mic Capture — a Swift process captures audio from the default microphone and streams PCM frames to a Python orchestrator over stdio/socket, reusing the IPC patterns from the sibling `record` project.
- **Status:** Draft
- **Author:** Evgenii Basmov

---

## 1. Overview and Rationale (The "Why")

Mic Capture is the entry point of the VoiceBridge PoC pipeline. While the author holds the push-to-talk key, their voice has to reach the realtime translation model; without a working capture path, none of the downstream pieces (model, playback) have anything to act on.

The PoC runs from a terminal and has no UI. Mic Capture therefore has almost no visible surface of its own — its job is to be reliably *there* when push-to-talk activates, and to get out of the way otherwise. The decisions in this spec are about the few moments when it *is* visible to the author: launch, permissions, normal active periods, and failures.

**Success looks like:** during a ~10-minute hands-on session, every press of the push-to-talk key produces audio that reaches the next stage, with no dropouts and no manual restarts. The author never has to think about which mic is being used; the tool picks the same one their Mac is currently using for everything else.

---

## 2. Functional Requirements (The "What")

### 2.1 Microphone selection

- The tool uses the **system default input device** — the same microphone macOS would use for a FaceTime call started at that moment.
- If the author changes their system default mid-session (e.g., plugs in headphones with a mic, or switches to AirPods), the next push-to-talk press uses the new default automatically. No restart is required.
  - **Acceptance Criteria:**
    - [ ] Given the system default mic is the built-in mic, when the author presses push-to-talk and speaks, the audio captured corresponds to the built-in mic.
    - [ ] Given the author switches the system default to a different mic mid-session, when they next press push-to-talk, the audio captured corresponds to the new default mic.

### 2.2 Microphone permission

- The first time the tool runs, macOS will prompt the author to grant microphone access.
- If the author grants access, the tool continues normally.
- If the author denies access — or if access has not been granted by the time the tool needs the mic — the tool **exits with a one-line error message** that explains:
  - that microphone access is required, and
  - where in System Settings to grant it (Privacy & Security → Microphone).
- The author re-launches the tool after granting access. There is no in-session retry.
  - **Acceptance Criteria:**
    - [ ] Given the tool has never been granted mic access, when the author launches it, then macOS shows its standard mic permission prompt.
    - [ ] Given the author denies the prompt, then the tool exits and prints a one-line error pointing to Privacy & Security → Microphone.
    - [ ] Given access has been previously denied, when the author re-launches the tool, then it exits immediately with the same one-line error (no second prompt).

### 2.3 Feedback while capturing

- During normal operation, the tool prints **one line per push-to-talk session**:
  - On press: a single "capture started" line.
  - On release: a single "capture stopped" line that includes a simple count of what was captured (e.g., number of frames or duration in milliseconds — enough for the author to confirm at a glance that something flowed).
- No per-frame logging, no level meter, no progress indicator. The terminal stays quiet between sessions.
  - **Acceptance Criteria:**
    - [ ] Given the author presses and holds the push-to-talk key for 3 seconds, then exactly two log lines are printed: one for press, one for release.
    - [ ] The release line includes a non-zero count reflecting the ~3 seconds of audio captured.
    - [ ] Between push-to-talk sessions, no log output is produced by Mic Capture.

### 2.4 Loss of the microphone mid-session

- If the microphone the tool is currently using disappears (e.g., the USB mic is unplugged) and no usable input is available, the tool **logs a one-line warning explaining what happened and exits**.
- The author restarts the tool, at which point it picks up whatever the new system default is.
- The tool does not attempt to recover, fall back, or auto-restart within the same session.
  - **Acceptance Criteria:**
    - [ ] Given the tool is running and the current input device is removed, then the tool prints a one-line warning identifying the loss and exits with a non-zero status.
    - [ ] After exit, re-launching the tool produces a normal startup against the (new) system default mic.

### 2.5 Shutdown

- When the author stops the tool (Ctrl-C in the terminal), Mic Capture releases the microphone cleanly. macOS's red microphone indicator in the menu bar disappears within a moment of exit.
  - **Acceptance Criteria:**
    - [ ] Given a push-to-talk session is in progress, when the author presses Ctrl-C, then the tool exits and macOS's mic-in-use indicator disappears.
    - [ ] After exit, no orphaned processes hold the microphone (a subsequent launch of the tool — or any other app — can immediately use the mic).

---

## 3. Scope and Boundaries

### In-Scope

- Capturing audio from the system default input device on macOS while push-to-talk is active.
- Handing captured audio to the next stage of the pipeline as it arrives.
- The first-launch macOS microphone permission prompt and the one-line error if permission is denied.
- One log line on capture start and one log line on capture stop.
- Clean release of the microphone on shutdown and on mid-session device loss.

### Out-of-Scope

- **Hotkey Activation** — deciding *when* capture is on/off is a separate roadmap item.
- **Realtime Model Plumbing** — what happens to captured audio after it leaves Mic Capture is a separate roadmap item.
- **Local Playback** — the author hearing translated audio is a separate roadmap item.
- **Phase 2 work:** virtual-mic routing (BlackHole), aggregate-device monitoring, voice-activity detection, and the discrete STT → LLM → TTS pipeline.
- Choosing a specific microphone other than the system default; in-app mic selection UI.
- Recovery, auto-restart, or fallback after the mic is lost mid-session.
- Visual or audible cues beyond the two log lines per session (no beep, no level meter, no menu-bar icon).
- Capturing the other party's audio.
