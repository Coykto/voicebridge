# Technical Specification: Realtime Translation Model Session

- **Functional Specification:** `context/spec/003-realtime-translation-model-session/functional-spec.md`
- **Status:** Approved
- **Author(s):** Evgenii Basmov

---

## 1. High-Level Technical Approach

A new Python module `src/voicebridge/realtime.py` owns a single live WebSocket session to OpenAI's **dedicated realtime translation endpoint** (`wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate`). The session opens at process startup and stays open for the lifetime of the orchestrator. PCM mic frames arrive from the gating layer (defined in the mic-capture spec) and are forwarded as base64-encoded `input_audio_buffer.append`-style events. Translated audio chunks coming back are exposed as an async iterator of raw PCM frames that the playback module (separate spec) consumes.

Configuration (`OPENAI_API_KEY`, `VOICEBRIDGE_TARGET_LANG`) is loaded from `.env` via `python-dotenv` in a new `src/voicebridge/config.py`. The orchestrator's startup sequence (begun in spec 001) is extended with config load, session open, and the single "Ready" line. Any failure — bad config, network unreachable, API key rejected, mid-session disconnect, server error event — surfaces a clear stderr message and exits the process. No automatic reconnect.

The implementation uses the `websockets` async library directly. Rationale: the translation endpoint has translation-specific event names the official SDK's typed realtime client doesn't model yet (it targets `/v1/realtime`, not `/v1/realtime/translations`). The WS protocol surface for this PoC is small (≈5 event types) and the OpenAI translation guide is the canonical reference for shapes. Future migration to the SDK is a one-file swap once SDK support lands.

---

## 2. Architecture Document Updates

This spec resolves three discrepancies with `context/product/architecture.md`. The architecture is updated **as part of this spec's deliverables**, in the same branch:

| § | Old | New |
|---|---|---|
| §2 sample rate | Swift resamples mic to **16 kHz** mono PCM16 | Swift resamples mic to **24 kHz** mono PCM16 |
| §3 translation prompt | Hardcoded system-prompt template with `{source}` / `{target}` placeholders | No prompt template — translation endpoint takes a target-language ISO code via `audio.output.language` |
| §3 language config | Both languages via CLI flags (`--source ru --target en`) | Target via `.env` (`VOICEBRIDGE_TARGET_LANG=English`/`Spanish`); source is auto-detected by the model |

---

## 3. Proposed Solution & Implementation Plan

### 3.1 New Python files

| File | Responsibility |
|---|---|
| `src/voicebridge/config.py` | Load `.env`, validate `OPENAI_API_KEY` and `VOICEBRIDGE_TARGET_LANG`, map language name → ISO code, return a typed `Config` dataclass. |
| `src/voicebridge/realtime.py` | Open and own the WebSocket session. Public async API: `RealtimeSession.open(config)`, `session.send_frame(pcm: bytes)`, `session.audio_frames() -> AsyncIterator[bytes]`, `session.close()`. |
| `src/voicebridge/lang.py` | Tiny mapping: `English → en`, `Spanish → es` (case-insensitive). Helper to validate and produce a `LanguageRejected` error with the user-facing message. |

### 3.2 Updated files

| File | Change |
|---|---|
| `src/voicebridge/__main__.py` | Extend startup: load config → open `RealtimeSession` → print the single "Connected. Russian → \<Target\>. Ready." line → enter main loop. Removes any `--source`/`--target` CLI flags. |
| `src/voicebridge/errors.py` | Add code → human-message mappings: `config_missing_api_key`, `config_invalid_target_lang`, `network_unreachable`, `api_key_rejected`, `realtime_server_error`, `connection_lost`. |
| `src/voicebridge/gating.py` | Interface for forwarding gated PCM into `session.send_frame()`. The actual call site lives in the mic-capture spec; here we only define the contract. |

### 3.3 `.env` contract

| Variable | Required | Allowed values | Maps to |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes | Any non-empty string | `Authorization: Bearer <value>` header on WS upgrade |
| `VOICEBRIDGE_TARGET_LANG` | Yes | `English`, `Spanish` (case-insensitive) | `en`, `es` respectively, sent in `audio.output.language` |

`.env` is gitignored (already established by spec 001).

### 3.4 WebSocket session lifecycle

**Open:**
1. Construct URL: `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate`.
2. Connect with header `Authorization: Bearer $OPENAI_API_KEY`.
3. On TCP/TLS/WS failure → raise `NetworkUnreachable`. On HTTP 401 during the upgrade handshake → raise `ApiKeyRejected`.
4. On open, send a `session.update` event with `audio.output.language = <iso>`, audio formats pinned to PCM16, and any voice/sample-rate fields per the translation guide.
5. Wait for the session-updated confirmation event (exact name verified against the live API on first connection). Timeout: 5 s.

**Streaming:**
- **Inbound (model → us):** for each translated-audio-delta event, base64-decode the PCM payload and put it on the `audio_frames()` async iterator's queue.
- **Outbound (us → model):** each `send_frame(pcm)` call base64-encodes the bytes and emits an `input_audio_buffer.append`-style event. Per the translation guide ("keep appending audio, including silence between phrases"), the **gating layer feeds zeroed PCM (silence) while PTT is up** instead of stopping outbound — connection stays warm between PTT turns.
- **Latency markers** (per architecture §4): first `send_frame` of a turn stamps `first_mic_frame_sent`; first audio chunk on the iterator stamps `first_model_audio_received`. These hooks live in this module.

**Failures:** any uncaught exception in the read loop, any server-side `error` event, or any WS close → log to stderr + `./logs/<ts>-orchestrator.log`, call `session.close()`, signal orchestrator to exit nonzero.

### 3.5 Startup sequence wiring

Insertion points relative to spec 001's sequence (only deltas shown):

1. _(existing)_ Parse CLI flags. _(After this spec: `--source` / `--target` removed.)_
2. **(new)** `config = load_config()`. On missing/invalid value → print matching error, `sys.exit(2)`.
3. _(existing)_ Spawn Swift binary; wait for `ready` + `hotkey_registered`.
4. **(new)** `session = await RealtimeSession.open(config)`. On any failure → print error, `sys.exit(1)`.
5. **(new)** Print exactly one line: `Connected. Russian → English. Ready.` (or `→ Spanish`).
6. _(existing)_ Enter main loop.

### 3.6 Hardcoded values

| Value | Where | Note |
|---|---|---|
| Source language = Russian (auto-detected; not sent as a parameter) | n/a | Per functional spec; the endpoint auto-detects. |
| Model = `gpt-realtime-translate` | `realtime.py` constant | Per the OpenAI translation guide. |
| WS endpoint = `wss://api.openai.com/v1/realtime/translations` | `realtime.py` constant | Per the OpenAI translation guide. |
| Voice + exact `session.update` payload shape | `realtime.py` | Built per the translation guide; not duplicated here (would go stale). |

### 3.7 Concurrency model

`RealtimeSession` owns two asyncio tasks: a **reader** (consuming WS messages → bounded inbound audio queue) and a **writer** (consuming a bounded outbound frame queue → WS sends). Both are created in `open()` and torn down in `close()`. Queue overflow drops the oldest frame and logs a warning (audio latency is already gone by then).

---

## 4. Impact and Risk Analysis

### System Dependencies

- **OpenAI Realtime Translation API** (`gpt-realtime-translate` over WSS) — external; the only error vector with no engineering mitigation.
- **`websockets`** — new Python dependency.
- **`python-dotenv`** — new Python dependency.
- **`asyncio`** — already used by spec 001.
- **No new Swift-side work** beyond the 16 kHz → 24 kHz mic resample, which the mic-capture spec owns.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Event names derived from the translation guide (e.g. `session.output_audio.delta`) may differ from what the live API actually emits — the guide is recent. | On first connection, log all received event types verbatim. Pin constants in `realtime.py` once names are confirmed. Spec deliberately does not freeze exact names — they live next to the doc URL in code. |
| Audio queue backpressure if playback drains slower than the model produces. | Bounded inbound queue (~200 frames ≈ 4 s). Overflow → drop newest + log a warning. PoC priority is "feels live," not "no audio loss." |
| Continuous-streaming requirement interacts awkwardly with PTT. | Gating layer sends **zeroed PCM** (silence) at the input sample rate while PTT is up. Model treats it as silence; connection stays warm between turns. |
| Mid-session disconnect ends the user-test session abruptly. | Functional spec explicitly accepts "print error and exit." No reconnect logic. |
| `gpt-realtime-translate` rejects Russian source or English/Spanish targets due to a server allowlist. | Caught by manual smoke tests #1–2 below. Escalate before the rest of the PoC commits effort. |
| Architecture-doc edits collide with other in-flight specs. | Single author, small file; trivial conflicts at worst. |

---

## 5. Testing Strategy

**Unit tests** (`tests/test_config.py`, `tests/test_lang.py`, `tests/test_realtime.py`):

- `config.py`: various `.env` permutations (missing key, empty target, target = `French`) produce the right error code or correct `Config`.
- `lang.py`: `English → en`, `Spanish → es`, case-insensitive; anything else → `LanguageRejected`.
- `realtime.py` with a **fake in-memory WebSocket** (a thin double recording sends and replaying scripted server events):
  - Open sequence sends a `session.update` with the correct language code.
  - Server `error` event triggers `close()` and signals exit.
  - Mid-stream WS close from the server triggers `close()` and signals exit.
  - Inbound audio deltas are decoded and emitted on `audio_frames()` in order.
  - `send_frame()` payloads are base64-encoded PCM16 in the expected event shape.

**Manual smoke tests** (single author, on the target Mac):

1. **Happy path, Russian → English.** Valid `.env`. Launch → "Ready" line → end-to-end translation works (once 001 / mic / playback specs land).
2. **Happy path, Russian → Spanish.** Switch `.env` target to `Spanish`, relaunch — translated audio is now Spanish.
3. **Missing API key.** Remove `OPENAI_API_KEY`. Launch → clear stderr line, exit `2`, no "Ready".
4. **Bad target language.** Set `VOICEBRIDGE_TARGET_LANG=French`. Launch → stderr names `French` as rejected, exit `2`.
5. **Invalid API key.** `OPENAI_API_KEY=sk-bogus`. Launch → stderr shows server rejection reason, exit `1`.
6. **Network unreachable at launch.** Wi-Fi off. Launch → stderr names the network failure, exit `1`.
7. **Mid-session disconnect.** Happy-path session, pull Wi-Fi after "Ready". → stderr line + clean exit.
8. **Relaunch after failure.** After any of #5–#7, fix the cause, relaunch — clean happy-path startup.

Tests #1–#2 depend on specs 001/mic/playback being implemented; mark as deferred-blocked in the task list. Tests #3–#8 are runnable with this spec in isolation — the "Ready" line is the success signal.
