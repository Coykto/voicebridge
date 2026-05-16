# Functional Specification: Hotkey Activation

- **Roadmap Item:** Hotkey Activation — holding a configured key opens the input stream; releasing it closes the stream. Nothing flows when the key is up.
- **Status:** Approved
- **Author:** Evgenii Basmov

---

## 1. Overview and Rationale (The "Why")

The PoC needs an explicit, gated way to decide *when* the author's voice is sent to the translation model. The author will be on real video calls and will routinely speak — to themselves, to a colleague off-call, to a pet — without wanting any of it translated and played back. A continuous always-on capture would translate every cough and aside; a UI button would force the author to leave the call window. The only acceptable trigger for the PoC is a **global push-to-talk hotkey**: hold to translate, release to stop.

The same pattern is already proven in the sibling `record` tool, where ⌥⌘R toggles a global capture from inside any focused app. VoiceBridge reuses that mechanism but with **press-and-hold semantics** instead of toggle, because translation is meant to track a single spoken utterance, not span minutes of silence.

**Success for this slice:**
- The author can sit in a focused Google Meet window, hold the hotkey, speak a sentence, release, and observe that translation began only while the key was held.
- No part of any spoken audio is captured or sent to the model while the key is up.
- The hotkey is reliable enough that a ~10-minute back-and-forth session doesn't require the author to look at the terminal.

---

## 2. Functional Requirements (The "What")

### 2.1 Pressing the hotkey starts an input stream

- **As the** author, **when I** press and hold **⌥⌘T (Option + Command + T)** from any focused application, **I want** VoiceBridge to immediately open the microphone input stream so my speech starts being translated.
- **Acceptance Criteria:**
  - [ ] The chord is **⌥⌘T**. It is hardcoded for the PoC — no flag, no config file, no UI to change it.
  - [ ] The hotkey works regardless of which application is in the foreground (terminal, browser, Google Meet, anything).
  - [ ] On press, a short audible "start" cue plays through the Mac's default output device (same pattern as `record`'s Submarine ping).
  - [ ] Within the same press, translated audio begins arriving at the speakers as soon as the model produces it. (Latency itself is owned by the model-plumbing/playback specs; this spec only asserts that the stream is *open* on press.)
  - [ ] macOS intercepts the chord — the focused meeting app does **not** receive ⌥⌘T as a keystroke, so the call app's own shortcuts are not triggered.

### 2.2 Releasing the hotkey closes the input stream

- **As the** author, **when I** release ⌥⌘T, **I want** VoiceBridge to stop capturing my microphone immediately, so anything I say next is not translated or played back.
- **Acceptance Criteria:**
  - [ ] On release, the microphone stream closes immediately — no further audio frames leave the machine for translation.
  - [ ] On release, a short audible "stop" cue plays through the Mac's default output device (same pattern as `record`'s Pop sound).
  - [ ] Translated audio that the model has **already** produced for the just-spoken phrase is still allowed to finish playing — releasing the key only cuts the *input* side. The author hears a natural tail rather than a clipped final word.
  - [ ] Anything the author says after release — coughs, asides, talking to a colleague off-call — is not captured, transmitted, or translated.

### 2.3 Nothing flows while the key is up

- **As the** author, **when I am** not holding the hotkey, **I want** to be confident that VoiceBridge is silent — no mic capture, no streaming to the model, no playback.
- **Acceptance Criteria:**
  - [ ] Between sessions, with the key up, the microphone indicator in the macOS menu bar does not show VoiceBridge actively listening.
  - [ ] Speaking into the microphone with the key up produces no translated audio and no perceptible activity in the terminal logs beyond an idle "waiting for hotkey" state.

### 2.4 macOS permission requirement

- **As the** author, **when I** launch VoiceBridge for the first time on a new machine, **I want** to be told clearly what permission is needed and what to do if it's missing — so a denied prompt doesn't leave me with a silently broken hotkey.
- **Acceptance Criteria:**
  - [ ] On first launch, macOS prompts the author for **Accessibility** permission (this is the OS-level permission required for any app to listen for global keyboard shortcuts).
  - [ ] If Accessibility is granted, VoiceBridge prints a one-line confirmation that the hotkey is registered and begins listening for ⌥⌘T.
  - [ ] If Accessibility is denied — or revoked later — VoiceBridge prints a clear error message naming the missing permission, points the author to **System Settings → Privacy & Security → Accessibility**, and exits. It does **not** start up in a degraded "translation works but hotkey doesn't" state, because there is no other way to trigger translation in the PoC.
  - [ ] Indicative wording: `Accessibility permission is required for the push-to-talk hotkey. Grant it in System Settings → Privacy & Security → Accessibility, then re-run.` (Exact phrasing is at the engineer's discretion as long as it names the permission and the path to grant it.)

### 2.5 Hotkey already in use by another app

- **As the** author, **when** ⌥⌘T is already claimed by another running application (e.g. a Keyboard Maestro macro), **I want** VoiceBridge to fail fast at startup rather than silently never receiving key events.
- **Acceptance Criteria:**
  - [ ] If the OS reports that ⌥⌘T cannot be registered because another app already holds it, VoiceBridge prints a clear error naming the conflict and exits.
  - [ ] Indicative wording: `Another app is already using ⌥⌘T as a global shortcut. Quit it or unbind the shortcut, then re-run.` (Exact phrasing is at the engineer's discretion as long as it names the conflict and what the author needs to do.)

---

## 3. Scope and Boundaries

### In-Scope

- A single, hardcoded global push-to-talk hotkey: **⌥⌘T**.
- Press-and-hold semantics (down = stream open, up = stream closed).
- Audible "start" and "stop" cues on press and release, played through the Mac's default output device, mirroring the pattern from the `record` tool.
- Mic stream closes immediately on release; in-flight translated audio is allowed to finish playing.
- Accessibility-permission handling at startup: explicit error + exit if missing.
- Conflict handling at startup: explicit error + exit if another app holds the chord.

### Out-of-Scope

- **Configurable hotkey.** No CLI flag, no config file, no UI. Changing the key requires editing the source.
- **Visible UI for hotkey state.** No menu bar icon, no overlay, no on-screen indicator. Terminal logs and the audible cues are the only feedback.
- **Toggle / latching modes.** Only press-and-hold is supported.
- **VAD-driven activation.** Always deferred — Phase 2 work.
- **Recovering gracefully from a missing/denied Accessibility grant at runtime.** If the OS revokes the grant mid-session, behavior is undefined; the author re-launches.
- **Edge case of the key being held when VoiceBridge starts up.** Behavior is left undefined; not worth specifying for a single-user PoC.
- **Behavior of the chord in the focused app when VoiceBridge is *not* running.** Outside the scope of VoiceBridge entirely.
- **All other Phase 1 roadmap items:** Mic Capture, Realtime Model Plumbing, and Local Playback are separate specifications.
- **All Phase 2 roadmap items:** BlackHole routing, aggregate-device monitoring, VAD activation, and the discrete STT→LLM→TTS fallback are out of scope.
