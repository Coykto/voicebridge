# Task List: Local Playback

Vertical slices for `002-local-playback`. After each slice, the playback module is more capable and demonstrable in isolation — no dependency on the (separately-specced) realtime-model integration. Slice 5 produces the file-driven smoke harness the team will use to validate playback end-to-end once mic + model land.

Subagents available: `python-backend` (Python orchestrator / asyncio / audio).

---

## Slice 1: A test tone plays through the default output device via `Playback`

**Goal:** prove the module + `sounddevice` path works end-to-end. Module skeleton, the `RawOutputStream` + callback contract, and the queue plumbing all exist and produce audible audio.

- [x] Add `sounddevice` as a `voicebridge` runtime dependency in `pyproject.toml`; run `uv sync` to pin it. **[Agent: python-backend]**
- [x] Create `src/voicebridge/playback.py` with `class Playback` exposing `open(sample_rate: int)`, `write(pcm_bytes: bytes)`, `close()`, and a read-only `underrun_count` (returns `0` for now). Internals per tech spec §2.2–§2.4: `sounddevice.RawOutputStream` with `channels=1`, `dtype='int16'`, `device=None`, `latency='low'`, `blocksize=0`, callback function bound to a `queue.SimpleQueue[bytes]` and a "carry" `bytearray` for partially-consumed chunks. **[Agent: python-backend]**
- [x] Create `scripts/play_test_tone.py`: builds 1.5 s of 440 Hz mono int16 PCM at 24 kHz, instantiates `Playback`, `open(24000)`, splits the tone into ~20 ms chunks, calls `playback.write(chunk)` in a tight `time.sleep(0.02)` loop, then `close()` after a short tail wait. **[Agent: python-backend]**
- [x] Add `tests/test_playback_callback.py`: feed a sequence of known PCM byte chunks into `Playback.write`, invoke the callback directly with various `frames` sizes (smaller than a chunk, larger than a chunk, spanning chunk boundaries), assert the output bytes match the concatenation of inputs in order. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_playback_callback.py` — passes. **[Agent: python-backend]**
- [ ] **Verify manual:** `uv run python scripts/play_test_tone.py` — a clear 440 Hz tone is audible through the current default output device. Change the system default output (e.g., plug in headphones), re-run, confirm audio follows the new default. During playback, hit the macOS volume keys → loudness changes in real time. **[Agent: python-backend — requires user to listen and toggle device/volume]**

---

## Slice 2: Underrun and overflow counters surface in the end-of-session log

**Goal:** functional spec §2.6 "if something goes wrong on the playback path, the author sees evidence." Tech spec §2.4 (underruns) and §3 risk table (overflow cap at ~5 s of audio).

- [x] In `playback.py`, add the underrun counter logic per tech spec §2.4: when the callback cannot fill `frames * 2` bytes from queue + carry, pad with zeros and increment `underrun_count` by 1 per callback invocation. **[Agent: python-backend]**
- [x] Add a bounded queue policy: cap accumulated queued bytes at 5 seconds of audio at the open sample rate (e.g. `5 * sample_rate * 2` bytes at int16 mono). If `write` would exceed the cap, drop the **oldest** chunk(s) from the queue first and increment a new `overflow_count`. Expose `overflow_count` as a read-only property. **[Agent: python-backend]**
- [x] `Playback.close()`: drain the queue with up to ~200 ms timeout, stop and close the stream, then log a single line to stderr: `playback: <underrun_count> underruns, <overflow_count> overflows in session`. **[Agent: python-backend]**
- [x] Add to `tests/test_playback_callback.py`: invoke the callback with an empty queue → assert `outdata` is all zeros and `underrun_count` increments by 1 per call. Push more bytes than the 5 s cap → assert oldest chunks dropped and `overflow_count` increments by the number of dropped chunks. **[Agent: python-backend]**
- [x] Create `scripts/playback_starve_smoke.py`: feeds 0.5 s of tone, sleeps 0.3 s (no writes), feeds another 0.5 s, then closes. Prints the final log line. **[Agent: python-backend]**
- [x] **Verify automated:** `uv run pytest tests/test_playback_callback.py` — passes (including new cases). **[Agent: python-backend]**
- [ ] **Verify manual:** `uv run python scripts/playback_starve_smoke.py` — hear tone, brief silence (underruns accumulating), tone again. Final log line shows a non-zero `underruns` count and `overflows=0`. Repeat with a flooding variant (write 6 s of tone instantly) — final log line shows `overflows>0`. **[Agent: python-backend — requires user to listen and read the final log line]**

---

## Slice 3: `first_audio_frame_played`-style hook fires at the right boundaries

**Goal:** architecture §4 timing instrumentation contract — `Playback.write` exposes a hook that fires the first time `write` is called after an idle period, so the orchestrator's (separately-specced) timing module can stamp `first_audio_frame_played` per PTT turn.

- [ ] In `playback.py`, add an `on_first_write_after_idle: Callable[[], None] | None` attribute and a `mark_idle()` method. Semantics: `mark_idle()` arms the hook; the next `write` call fires the hook synchronously (from the asyncio caller's thread, **not** from the audio callback) and disarms it. Subsequent `write` calls do not fire until another `mark_idle()`. Initial state after `open()` is armed. **[Agent: python-backend]**
- [ ] Add `tests/test_playback_hook.py`: register a counting hook, call `open()` → first `write` fires hook once, second `write` does not fire. Call `mark_idle()` → next `write` fires again. Hook exceptions are caught and logged (do not propagate into `write`). **[Agent: python-backend]**
- [ ] **Verify automated:** `uv run pytest tests/test_playback_hook.py` — passes. **[Agent: python-backend]**

_No manual verify — this is a pure-data contract consumed by the orchestrator timing module in a sibling spec._

---

## Slice 4: Unopenable stream parameters yield a clear startup error

**Goal:** tech spec §3 risk — if the realtime session reports an audio format we can't open, fail loudly with a named error rather than silently mis-rendering audio.

- [ ] In `playback.py`, wrap the `sounddevice.RawOutputStream(...)` construction in `Playback.open()` with a `try/except`. On any `sounddevice.PortAudioError` (and `Exception` as a catch-all), raise a new `PlaybackOpenError` carrying the requested `sample_rate`, `channels`, `dtype`, and the original exception's message. **[Agent: python-backend]**
- [ ] Add `tests/test_playback_open_error.py`: monkeypatch `sounddevice.RawOutputStream` to raise `sounddevice.PortAudioError("bad rate")`; call `Playback().open(99999)`; assert `PlaybackOpenError` is raised and its message contains `99999`, `int16`, and `bad rate`. **[Agent: python-backend]**
- [ ] **Verify automated:** `uv run pytest tests/test_playback_open_error.py` — passes. **[Agent: python-backend]**

---

## Slice 5: File-driven smoke harness mirrors the model integration shape end-to-end

**Goal:** simulate the eventual model-plumbing wiring (tech spec §2.5) without depending on the realtime-model spec. Produces the harness that will validate Manual Smoke Tests 1–6 in tech spec §4 once mic + model are in place, and proves the integration shape today.

- [ ] Create `scripts/playback_from_pcm_file.py`: takes `--file <path.pcm>` (raw int16 mono 24 kHz PCM) and `--chunk-ms <int, default 40>`; opens `Playback(24000)` **eagerly** before the read loop starts; reads the file in chunks, calls `playback.write(chunk)` while sleeping `chunk_ms / 1000` between writes to mimic real-time arrival; on EOF, waits a short tail (~500 ms) then `close()`. Calls `playback.mark_idle()` whenever the script sees a configurable silence run in the file (so the first-write hook is exercised). **[Agent: python-backend]**
- [ ] Document in the script's header how to produce a test `.pcm` file from any wav: `ffmpeg -i input.wav -f s16le -ac 1 -ar 24000 input.pcm`. **[Agent: python-backend]**
- [ ] **Verify manual — mid-stream audibility:** prepare a 30-second speech `.pcm` per the header instructions. Run `uv run python scripts/playback_from_pcm_file.py --file speech.pcm` and listen — speech plays continuously from the first chunk, no audible gap at the start, no end-of-stream click. End-of-session log shows underruns/overflows. **[Agent: python-backend — requires user to listen]**
- [ ] **Verify manual — system volume:** during playback, press macOS volume keys → loudness changes immediately. **[Agent: python-backend — requires user to listen and press keys]**
- [ ] **Verify manual — default device:** with the script playing, do **not** change the system default mid-run (out of scope per functional spec §2.2). Quit, change default output, re-run, confirm audio follows. **[Agent: python-backend — requires user to switch devices between runs]**
- [ ] **Verify manual — 10-minute stability proxy:** loop the script for ~10 minutes of cumulative playback (long file or repeat) and confirm the final log line shows underruns in the single digits (or document the count for tuning `latency='high'` later). **[Agent: python-backend — requires user to run the long session]**

---

## Recommendations

| Task / Slice | Issue | Recommendation |
|---|---|---|
| Slices 1, 2, 5 manual verifies | The pass criteria are subjective ("speech sounds continuous", "no click on stream open"). No MCP can substitute for the author's ears. | None — accept that the audible smoke steps stay in human hands. `/awos:implement` should pause and prompt the author at each manual verify step. |
| Full functional spec §2.4 (trailing audio after PTT release) | Cannot be tested in this spec's tasks alone — requires the hotkey spec and the model-plumbing spec to be wired up. | Defer the final integrated smoke test to whichever spec finishes last; this spec's slices cover the playback half of the behavior (input gating, not output truncation) per tech spec §2.7. |
