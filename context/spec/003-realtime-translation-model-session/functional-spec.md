# Functional Specification: Realtime Translation Model Session

- **Roadmap Item:** Realtime Model Plumbing (Phase 1)
- **Status:** Draft
- **Author:** Evgenii Basmov

---

## 1. Overview and Rationale (The "Why")

VoiceBridge is a PoC for live translation during conversation. Phase 1's purpose is to find out whether a realtime speech-to-speech model can produce translated audio quickly enough that conversation still feels live. This specification covers the **translation session itself** — the running connection to the realtime translation service that takes the author's spoken Russian and produces spoken English (or Spanish) in return.

Without this slice the PoC cannot answer its core question. Mic capture, hotkey activation, and local playback all depend on a working translation session to deliver any value. This specification defines how the author configures and starts that session, how they know it is ready, and what they see when it cannot run.

Success is judged by the author after a hands-on session: the session must start cleanly on the first try with valid configuration, stay connected for the full ~10-minute conversation without dropouts, and produce translated speech in the chosen target language that is recognisably faithful to what was said.

---

## 2. Functional Requirements (The "What")

### 2.1 Pre-launch Configuration

The author sets up VoiceBridge by editing a `.env` file in the project directory before launching. The file holds two settings:

- An OpenAI account API key.
- The target language for translation. Allowed values for the PoC: **English** or **Spanish**.

The source language is always Russian for this PoC — the author does not configure it.

- **Acceptance Criteria:**
  - [ ] A `.env` file with a valid API key and a target language of `English` or `Spanish` is sufficient to launch VoiceBridge.
  - [ ] Changing the target language in `.env` and relaunching causes the next session's translated speech to be in the new target language.
  - [ ] Any target language value other than `English` or `Spanish` causes the program to print a clear message naming the rejected value and exit before opening a session.

### 2.2 Session Startup

When the author runs VoiceBridge, the program opens a connection to the realtime translation service. On success, the terminal prints a single line confirming the session is ready and naming the active language pair (e.g. `Connected. Russian → English. Ready.`). The program then waits silently for the author to begin speaking (covered by other slices).

- **Acceptance Criteria:**
  - [ ] On successful launch with valid configuration, exactly one human-readable confirmation line appears in the terminal, naming source and target languages.
  - [ ] No further startup output is printed until something happens during the session.
  - [ ] If the API key is missing or empty, the program prints a message saying so and exits without printing the "ready" line.
  - [ ] If the API key is rejected by the service, the program prints the service's failure reason and exits.
  - [ ] If the network is unreachable at launch, the program prints a clear error and exits.

### 2.3 Translation Behaviour

While a session is open, any spoken Russian input the author provides results in translated speech in the target language being produced. The translation is plain: the service is instructed only to translate — not to adjust tone, strip disfluencies, summarise, or add commentary.

- **Acceptance Criteria:**
  - [ ] Russian input produces translated audio output in the configured target language (English or Spanish) and in no other language.
  - [ ] The translated content reflects what was actually said — paraphrasing for grammar is expected, but no content is added, omitted, or summarised.
  - [ ] When the session receives no spoken input, no translated audio is produced.

### 2.4 Mid-Session Failure

If the connection to the realtime service is lost or the service returns an error mid-session, VoiceBridge prints a clear message naming the failure and exits. The author relaunches manually. No automatic reconnect is attempted.

- **Acceptance Criteria:**
  - [ ] When the network is disconnected during an active session, the terminal prints an error explaining the disconnect and the program exits.
  - [ ] When the realtime service returns an error mid-session, the terminal prints the failure reason and the program exits.
  - [ ] After such an exit, relaunching VoiceBridge with the same configuration opens a fresh session normally.

---

## 3. Scope and Boundaries

### In-Scope

- Configuration of the OpenAI API key and target language via a `.env` file in the project directory.
- Russian as the fixed source language.
- English and Spanish as the supported target languages.
- A live translation session that opens at launch and stays open for the duration of the program.
- A single confirmation line on successful startup.
- Clear terminal error messages and a clean exit for all failure modes (missing/invalid configuration, network failure, service errors at startup or mid-session).
- Plain translation behaviour — no tone, register, or filler-word handling.

### Out-of-Scope

- **Hotkey Activation**, **Mic Capture**, **Local Playback** — separate Phase 1 roadmap items.
- **BlackHole Output Routing**, **Aggregate Device Monitoring**, **VAD-Driven Activation**, **Discrete Pipeline Fallback** — Phase 2 roadmap items.
- Automatic reconnection on connection loss.
- Translation of the other party's speech (the PoC only translates the author's outgoing voice).
- Translation-quality measurement or benchmarking.
- Switching languages mid-session.
- Any service other than OpenAI's realtime translation offering.
- Distribution, packaging, or any non-source launch method.
- UI, settings panels, or any non-terminal surface.
