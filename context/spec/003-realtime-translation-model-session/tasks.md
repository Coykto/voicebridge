# Task List: Realtime Translation Model Session

Vertical slices for `003-realtime-translation-model-session`. Each slice leaves the project in a runnable, demonstrable state. Tech spec §3.5 places this work *after* spec 001 lands (its startup sequence wires the `RealtimeSession` into the existing `__main__.py` flow). Slices assume spec 001's bones — `pyproject.toml`, `src/voicebridge/__main__.py`, `src/voicebridge/errors.py` — are already present, and extend them.

Subagents available: `python-backend` (Python orchestrator / asyncio / WebSocket / .env config).

---

## Slice 1: Architecture doc reflects the new contract

**Goal:** tech spec §2 deliverable — update `context/product/architecture.md` so downstream specs (mic-capture especially) see the corrected sample rate, language config, and prompt approach **before** any code is written that would re-encode the old contract.

- [x] In `context/product/architecture.md` §2, change "Swift resamples to 16 kHz mono PCM16" to "Swift resamples to **24 kHz mono PCM16**" in both the "Microphone capture (Swift)" bullet and the "Mic → model" sample-rate bullet. **[Agent: python-backend]**
- [x] In `context/product/architecture.md` §3, replace the "Translation prompt" paragraph with: "No system-prompt template. The translation endpoint takes a target-language ISO code via `audio.output.language` on the `session.update` event. The source language is auto-detected by the model." **[Agent: python-backend]**
- [x] In `context/product/architecture.md` §3, replace the CLI-flags sentence in the realtime-model paragraph with: "Languages are configured via `.env` (`VOICEBRIDGE_TARGET_LANG=English` or `Spanish`). The source is fixed at Russian for the PoC and auto-detected by the model." **[Agent: python-backend]**
- [x] **Verify:** `grep -n "16 kHz" context/product/architecture.md` returns no hits in §2. `grep -n "{source}\|{target}\|--source\|--target" context/product/architecture.md` returns no hits. `grep -n "24 kHz" context/product/architecture.md` returns the two updated bullets in §2. `grep -n "VOICEBRIDGE_TARGET_LANG\|audio.output.language" context/product/architecture.md` returns the two updated paragraphs in §3. **[Agent: python-backend]**

---

## Slice 2: `.env` loads into a typed `Config`; invalid values exit with named errors

**Goal:** functional spec §2.1 satisfied at the config layer. After this slice, `uv run python -m voicebridge.config` (a tiny debug entry) prints the parsed config or a clear failure — no WebSocket, no Swift yet.

- [x] Add `python-dotenv` to `pyproject.toml` runtime deps; run `uv sync` to pin. **[Agent: python-backend]**
- [x] Create `src/voicebridge/lang.py` with a case-insensitive map `{"english": "en", "spanish": "es"}`, a `validate(name: str) -> str` function returning the ISO code, and a `LanguageRejected` exception carrying the rejected value verbatim for the user-facing message. **[Agent: python-backend]**
- [x] Create `src/voicebridge/config.py` with `@dataclass(frozen=True) class Config: api_key: str; target_lang_name: str; target_lang_iso: str` and `load_config() -> Config` that calls `dotenv.load_dotenv()`, reads `OPENAI_API_KEY` and `VOICEBRIDGE_TARGET_LANG`, raises a `ConfigError` subclass for each named failure (`MissingApiKey`, `InvalidTargetLang`). Treat empty strings as missing. **[Agent: python-backend]**
- [x] Extend `src/voicebridge/errors.py` (created by spec 001) with the §3.2 codes: `config_missing_api_key` (exit 2), `config_invalid_target_lang` (exit 2), `network_unreachable` (exit 1), `api_key_rejected` (exit 1), `realtime_server_error` (exit 1), `connection_lost` (exit 1). Each maps a `ConfigError`/`RealtimeError` subclass to (human-readable stderr message, exit code). **[Agent: python-backend]**
- [x] Add a `__main__` block to `src/voicebridge/config.py` that prints the parsed `Config` (key redacted as `sk-…<last4>`) or routes the exception through `errors.py` and `sys.exit`s with the mapped code. This stays in place as a debug entry; the orchestrator entrypoint imports `load_config` directly. **[Agent: python-backend]**
- [x] Create `tests/test_lang.py`: `validate("English") == "en"`, `validate("ENGLISH") == "en"`, `validate("Spanish") == "es"`, `validate("French")` raises `LanguageRejected` with `"French"` in `str(exc)`. **[Agent: python-backend]**
- [x] Create `tests/test_config.py`: monkeypatch `os.environ` (and bypass `.env` loading) to cover missing key, empty key, missing target, empty target, target=`French`, valid `English`, valid `Spanish` (mixed case). Assert the right exception type per case and that valid cases produce `Config(target_lang_iso="en"/"es")`. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_lang.py tests/test_config.py` — all pass. **[Agent: python-backend]**
- [x] **Verify manual:** Write `.env` with `OPENAI_API_KEY=sk-test-1234` and `VOICEBRIDGE_TARGET_LANG=English`. `uv run python -m voicebridge.config` prints `Config(target_lang_name='English', target_lang_iso='en', api_key='sk-…1234')`. Switch to `Spanish` → prints `target_lang_iso='es'`. Switch to `French` → stderr names `French` as rejected, exit code 2 (`echo $?`). Remove `OPENAI_API_KEY` → stderr names the missing key, exit code 2. **[Agent: python-backend]**

---

## Slice 3: `RealtimeSession.open()` connects to OpenAI and the orchestrator prints the single "Ready" line

**Goal:** functional spec §2.2 satisfied. After this slice, `uv run python -m voicebridge` opens the WSS session, prints `Connected. Russian → English. Ready.`, and idles. Audio plumbing comes in Slice 4.

- [x] Add `websockets` to `pyproject.toml` runtime deps; run `uv sync`. **[Agent: python-backend]**
- [x] Create `src/voicebridge/realtime.py` with module-level constants `WS_URL = "wss://api.openai.com/v1/realtime/translations"`, `MODEL = "gpt-realtime-translate"`, and `RealtimeSession` exposing the **lifecycle-only** public API for this slice: `@classmethod async def open(cls, config: Config) -> "RealtimeSession"` and `async def close(self) -> None`. **[Agent: python-backend]**
- [x] In `RealtimeSession.open()`: build URL with `?model=gpt-realtime-translate`, connect with `Authorization: Bearer <api_key>` extra header. Catch `OSError` / `socket.gaierror` / `websockets.exceptions.InvalidStatus` / `websockets.exceptions.InvalidStatusCode` and raise `NetworkUnreachable` or `ApiKeyRejected` (HTTP 401) — both subclasses of `RealtimeError` from `errors.py`. **[Agent: python-backend]**
- [x] On open, send `session.update` with `audio.output.language = config.target_lang_iso`, audio formats pinned to `pcm16`, voice and sample-rate fields per the OpenAI translation guide. Await the session-updated confirmation event with a 5 s timeout; on timeout raise `RealtimeServerError("session.update timed out")`. Log every received event type verbatim to `./logs/<ts>-orchestrator.log` for the first connection (per tech spec §4 risk mitigation). **[Agent: python-backend]**
- [x] Extend `src/voicebridge/__main__.py` (created by spec 001): after the existing Swift handshake (`Ready` + `HotkeyRegistered`), call `config = load_config()` (move earlier in the sequence per tech spec §3.5: parse CLI → load_config → spawn Swift → open session), then `session = await RealtimeSession.open(config)`. Print exactly one line to stdout: `Connected. Russian → <Target>. Ready.` where `<Target>` is the `target_lang_name` from `Config`. Remove any `--source` / `--target` CLI flags from `argparse`. On any exception, route through `errors.py` and `sys.exit` with the mapped code; ensure the Swift subprocess is terminated first. **[Agent: python-backend]**
- [x] Create `tests/test_realtime_open.py` with a **fake in-memory WebSocket** (a class with `send` / `recv` / `close` coroutines backed by `asyncio.Queue`s; sends are captured, recvs are scripted). Patch `websockets.connect` to return the fake. Cover: (a) open sends `session.update` containing `"language": "en"` for an English config; (b) `"language": "es"` for Spanish; (c) scripted session-updated event resolves `open()`; (d) absence of session-updated within 5 s raises `RealtimeServerError`; (e) `websockets.connect` raising `InvalidStatusCode(401)` → `ApiKeyRejected`; (f) `OSError("nodename nor servname")` → `NetworkUnreachable`. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_realtime_open.py` — all pass. **[Agent: python-backend]**
  _Manual verifies require live API key / network / Wi-Fi toggling — defer to user smoke test._
- [ ] **Verify manual — happy:** Valid `.env` (real key, `English`). `uv run python -m voicebridge`. Swift launches (from 001), then exactly one new line appears: `Connected. Russian → English. Ready.`. Process idles. Ctrl+C exits cleanly. Switch `.env` target to `Spanish`, relaunch → line reads `Connected. Russian → Spanish. Ready.`. **[Agent: python-backend — requires the user's real API key + network]**
- [x] **Verify manual — missing key:** Remove `OPENAI_API_KEY` from `.env`. Launch → stderr names the missing key, no "Ready" line, exit code 2. **[Agent: python-backend]**
- [ ] **Verify manual — bad key:** Set `OPENAI_API_KEY=sk-bogus-not-a-real-key`. Launch → stderr surfaces the server's rejection reason, no "Ready" line, exit code 1. **[Agent: python-backend — requires network]**
- [ ] **Verify manual — network unreachable:** Wi-Fi off. Launch → stderr names the network failure, no "Ready" line, exit code 1. Wi-Fi back on. **[Agent: python-backend — requires user to toggle Wi-Fi]**

---

## Slice 4: Bidirectional audio streaming over the session

**Goal:** the `RealtimeSession` accepts outbound PCM frames and exposes inbound audio as an async iterator. A file-driven smoke harness exercises this end-to-end against the live model without touching the (separately-specced) mic or playback paths.

- [x] Extend `src/voicebridge/realtime.py`: add `async def send_frame(self, pcm: bytes) -> None` and `def audio_frames(self) -> AsyncIterator[bytes]`. Implement two asyncio tasks created by `open()`: a **reader** task consuming WS messages → decoding `*.audio.delta`-style events (base64 → bytes) → putting on a bounded `asyncio.Queue[bytes]` of capacity ~200 frames (~4 s at 24 kHz / 20 ms); a **writer** task draining a bounded outbound `asyncio.Queue[bytes]` of the same capacity → emitting `input_audio_buffer.append` events with base64 PCM. **[Agent: python-backend]**
- [x] Queue overflow policy per tech spec §3.7: when a queue is full on `put`, drop the **oldest** frame and log a single-line warning to stderr (`realtime: dropped 1 inbound/outbound frame, queue full`). Expose `inbound_drops` and `outbound_drops` counters as read-only attributes. **[Agent: python-backend]**
- [x] Add timing-marker hooks per tech spec §3.4 / architecture §4: `on_first_mic_frame_sent: Callable[[], None] | None` fires synchronously the first time `send_frame` is called after `mark_turn_start()`; `on_first_model_audio_received: Callable[[], None] | None` fires synchronously the first time the reader puts a frame onto the inbound queue after `mark_turn_start()`. `mark_turn_start()` re-arms both. Hook exceptions are caught and logged. **[Agent: python-backend]**
- [x] Tear down both tasks in `close()`; await them with a 1 s timeout; force-cancel on timeout. Ensure `audio_frames()`'s iterator terminates cleanly when the session closes. **[Agent: python-backend]**
- [x] Extend `tests/test_realtime_open.py` (or split into `tests/test_realtime_streaming.py`): with the fake WS, (a) `send_frame(b"\x00\x01...")` emits an `input_audio_buffer.append` event with base64 of those bytes; (b) scripted output `audio.delta` events with base64 PCM are decoded and yielded by `audio_frames()` in order; (c) pushing >200 inbound frames without drain drops the oldest and increments `inbound_drops`; (d) `mark_turn_start()` → `send_frame()` fires the first-mic hook once; subsequent `send_frame()` calls do not fire until another `mark_turn_start()`; same shape for the first-model-audio hook. **[Agent: python-backend]**
- [x] Create `scripts/realtime_send_pcm_file.py`: takes `--in <path.pcm>` (raw int16 mono 24 kHz Russian speech) and `--out <path.pcm>` (raw int16 mono PCM written from incoming audio deltas). Loads config, opens a `RealtimeSession`, streams the input file in 20 ms chunks via `send_frame` (with `asyncio.sleep(0.02)` between writes to mimic real-time arrival), concurrently drains `audio_frames()` and appends to the output file. On EOF + 2 s of inbound idle, closes the session. Prints `inbound_drops` / `outbound_drops` at the end. Document in the header: produce input with `ffmpeg -i russian.wav -f s16le -ac 1 -ar 24000 russian.pcm`; verify output with `ffplay -f s16le -ac 1 -ar 24000 out.pcm` or `ffmpeg -f s16le -ac 1 -ar 24000 -i out.pcm out.wav`. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_realtime_streaming.py` (or the merged file) — all pass. **[Agent: python-backend]**
- [ ] **Verify manual:** prepare `russian.pcm` from any Russian-speech recording per the script header. Valid `.env` with `English`. `uv run python scripts/realtime_send_pcm_file.py --in russian.pcm --out english.pcm`. The script completes without crash; `english.pcm` is non-empty. Play it back with `ffplay -f s16le -ac 1 -ar 24000 english.pcm` (or convert and open in QuickTime) — audible English, recognisably faithful to the Russian input. Switch `.env` to `Spanish`, rerun → output audio is Spanish. **[Agent: python-backend — requires the user's API key, network, an `ffmpeg`/`ffplay` install, and a Russian-speech sample]**

---

## Slice 5: Mid-session failures print a clear message and exit; relaunch is clean

**Goal:** functional spec §2.4 satisfied. Server `error` events, WS closes, and transport exceptions during a live session terminate the program with a named error and a nonzero exit code. The next launch is unaffected.

- [x] In `src/voicebridge/realtime.py`, harden the reader task: on any `*.error` server event, raise `RealtimeServerError(<server-reason>)`; on `websockets.exceptions.ConnectionClosed*`, raise `ConnectionLost(<reason>)`; on any other exception, log + re-raise as `RealtimeServerError`. Expose the live task via an `awaitable` (`session.wait_closed()`) that resolves to the raised exception or returns normally on graceful `close()`. **[Agent: python-backend]**
- [x] In `src/voicebridge/__main__.py`, after printing the "Ready" line, `await session.wait_closed()` as part of the main loop. On any `RealtimeError` subclass coming back, route through `errors.py` (prints the human-readable line to stderr, terminates the Swift subprocess, `sys.exit` with the mapped code). On graceful shutdown (Ctrl+C → `KeyboardInterrupt`), close the session and Swift cleanly, exit 0. **[Agent: python-backend]**
- [x] Extend the streaming tests (`tests/test_realtime_streaming.py`): (a) scripted server `error` event (e.g. `{"type": "error", "error": {"message": "rate limit"}}`) → `wait_closed()` resolves with `RealtimeServerError("rate limit")`; (b) fake WS raising `ConnectionClosedError` mid-stream → `wait_closed()` resolves with `ConnectionLost`; (c) `session.close()` then `wait_closed()` resolves without raising. **[Agent: python-backend]**
- [x] Add `tests/test_main_exit_codes.py`: patch `RealtimeSession.open` to return a fake session whose `wait_closed()` returns a chosen exception; assert `__main__` exits with the right code and stderr line for each of `RealtimeServerError`, `ConnectionLost`, `ApiKeyRejected`, `NetworkUnreachable`, `MissingApiKey`, `InvalidTargetLang`. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/` — full suite green. **[Agent: python-backend]**
- [ ] **Verify manual — mid-session disconnect:** valid `.env`, Wi-Fi on. `uv run python -m voicebridge` → "Ready" line. While the program is idling, turn Wi-Fi off. Within a few seconds, a stderr line names the disconnect (e.g. "connection lost: …"), the program exits with a nonzero code, and the Swift subprocess is gone (`ps aux | grep voicebridge-capture`). **[Agent: python-backend — requires user to toggle Wi-Fi]**
- [ ] **Verify manual — relaunch after failure:** with Wi-Fi back on, immediately relaunch `uv run python -m voicebridge`. Swift starts, "Ready" line appears, no leftover state from the prior failure. Ctrl+C exits 0. **[Agent: python-backend]**

---

## Recommendations

| Task / Slice | Issue | Recommendation |
|---|---|---|
| Slice 3 / 4 / 5 manual verifies | Each requires the user's real OpenAI API key, live network, and (Slice 4) an `ffmpeg`/`ffplay` install plus a Russian-speech sample. None of these can be substituted by an MCP. | Accept manual smoke. `/awos:implement` should pause at each manual verify step and prompt the author. If `ffmpeg` is missing, instruct: `brew install ffmpeg`. |
| Slice 4 verification — pass criteria "recognisably faithful translation" | Subjective; only the human author can judge translation fidelity. | None — this is precisely the PoC question the spec is meant to answer. Keep the criterion qualitative. |
| Tech spec §5 manual smoke tests #1–#2 (end-to-end mic + speakers) | Cannot be performed inside this spec alone — they require spec 001 (mic capture) and spec 002 (playback) wired in. | Defer integrated end-to-end smoke to whichever spec finishes last. This task list covers Slice 4's offline equivalent (file → file) plus tech spec §5 tests #3–#8. |
| Subagent coverage | All sub-tasks are assigned to `python-backend`. No `general-purpose` fallbacks. The architecture-doc edits in Slice 1 are markdown-only but stay with `python-backend` since the agent already has the project context. | No action. |
