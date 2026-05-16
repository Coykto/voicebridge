# Technical Specification: Local Playback

- **Functional Specification:** `context/spec/002-local-playback/functional-spec.md`
- **Status:** Approved
- **Author(s):** Evgenii Basmov

---

## 1. High-Level Technical Approach

A new `voicebridge/playback.py` module in the Python orchestrator owns local playback end-to-end. It opens a single `sounddevice` output stream at the realtime model's output sample rate (24 kHz for OpenAI Realtime / `pcm16`), pointed at the Mac's default output device, and uses PortAudio's **callback API**. PCM bytes returned by the realtime session are deposited on a thread-safe queue; the audio callback drains the queue into PortAudio's output buffer. This decouples the asyncio event loop from the real-time audio thread and lets the first audio chunk play as soon as it arrives — the "mid-sentence" effect the PoC is built to evaluate.

The stream is opened **eagerly**, immediately after the realtime session is up but before the user's first PTT press, so the first translated utterance of the session pays only model latency — not PortAudio's open cost. The stream stays open for the lifetime of the orchestrator and is closed on shutdown.

Trailing-audio behavior (functional spec §2.4) requires no work in this spec: gating is input-only (hotkey spec §2.7), so any audio the model emits — including frames that arrive after PTT release — is unconditionally queued and played.

---

## 2. Proposed Solution & Implementation Plan

### 2.1 New module

| Path | Role |
|---|---|
| `src/voicebridge/playback.py` | Owns the `sounddevice` output stream, the audio queue, the callback, underrun accounting, and the `first_audio_frame_played` timing stamp. |

No Swift changes. No new top-level components. Wiring lives in `src/voicebridge/__main__.py` (orchestrator entrypoint) and in whichever module owns the realtime session (defined by the model-plumbing spec; this spec just specifies the integration point).

### 2.2 Public interface of `playback.py`

| Symbol | Purpose |
|---|---|
| `class Playback` | Encapsulates the output stream and audio queue. |
| `Playback.open(sample_rate: int) -> None` | Opens the PortAudio output stream lazily-callable but called eagerly from the orchestrator. Idempotent. |
| `Playback.write(pcm_bytes: bytes) -> None` | Asyncio-loop-safe (non-blocking) — appends to the internal queue. Called by the realtime-session task for each model audio chunk. First call records the `first_audio_frame_played` timing event (see §2.6). |
| `Playback.close() -> None` | Drains the queue with a short timeout, stops the stream, releases PortAudio resources. Returns the underrun count for the session summary. |
| `Playback.underrun_count -> int` | Read-only property exposing the running counter for the end-of-session summary. |

`Playback` is intentionally not async — its `write` method is sync-safe to call from an asyncio task (queue append is constant-time and lock-free for `queue.SimpleQueue`). The PortAudio callback runs in PortAudio's own audio thread, not in the asyncio loop.

### 2.3 Stream configuration

| Setting | Value | Note |
|---|---|---|
| Library / class | `sounddevice.RawOutputStream` | `Raw` variant takes/returns `bytes`, matching the model's PCM bytes; no numpy in the hot path. |
| Sample rate | Determined at session open — passed in by the orchestrator from the realtime session's audio-format metadata. | Practically 24 kHz today (OpenAI Realtime / `pcm16`). Reading it from the session avoids a hidden assumption in code. |
| Channels | 1 | Mono. |
| `dtype` | `'int16'` | Matches `pcm16` directly. |
| `device` | `None` | `sounddevice` default = the OS default output device, per functional spec §2.2. |
| `latency` | `'low'` | Lets PortAudio pick the smallest jitter-free buffer macOS will give us. |
| `blocksize` | `0` | Variable block size; PortAudio chooses per host. |
| `callback` | Module-level function bound to the queue. | See §2.4. |

There is no separate `prebuffer` knob — the stream starts as soon as `open()` is called; the callback simply produces zeros until the queue has data.

### 2.4 Audio callback contract

The callback receives `(outdata: bytes, frames: int, time, status)` from PortAudio and must produce exactly `frames * 2` bytes (mono int16). Algorithm:

1. Read up to `frames * 2` bytes from `queue.SimpleQueue`, in a non-blocking loop, concatenating into a buffer.
2. If the buffer is shorter than required (queue is empty / partially full), pad the remainder with zero bytes and increment `underrun_count` by **1** for this callback invocation (not per missing sample).
3. Copy the buffer into `outdata`.
4. Return.

Chunks from the model can be arbitrarily sized; a single model chunk may span multiple callbacks, and a single callback may pull from multiple chunks. The queue stores chunks as-is; a small "carry" buffer inside the callback holds the leftover bytes from a partially-consumed chunk between callback invocations.

`status` is ignored beyond logging (PortAudio's own `CallbackFlags` like `output_underflow` are not used for the counter — we measure underruns from our queue's perspective, which is what the user actually hears).

### 2.5 Lifecycle and orchestrator wiring

Sequence on orchestrator startup (extends the hotkey spec §2.5 startup sequence):

1. Hotkey spec steps 1–5 complete (`hotkey_registered` received).
2. Open the realtime session (owned by the model-plumbing spec).
3. Read the session's output audio format → derive `sample_rate`.
4. `playback = Playback(); playback.open(sample_rate)`.
5. Wire the realtime-session "audio chunk received" handler to call `playback.write(chunk_bytes)`.
6. Enter main loop.

On shutdown (SIGINT, model session error, EOF on Swift stderr):

1. Stop receiving from the realtime session.
2. `playback.close()` — drains queue (up to ~200 ms), stops the stream, returns underrun count.
3. Log: `playback: <n> underruns in session`.
4. Exit.

### 2.6 Timing instrumentation

Per architecture §4, the orchestrator emits a `first_audio_frame_played` event per PTT turn. Implementation:

- `Playback.write` exposes a `on_first_write_after_idle` hook — fires the **first time** `write` is called after the previous PTT turn ended (i.e. after a `hotkey_up` event followed by a quiet period). The orchestrator's timing module registers this hook and stamps the event into the per-session timings JSONL.
- The hook fires from the asyncio task that owns `write`, not from the audio callback — so the timestamp reflects "first chunk handed to playback", not "first chunk PortAudio actually emitted". For the PoC's perceived-latency question, that ~5 ms difference is irrelevant; it keeps the timing path away from the audio thread.

### 2.7 What this spec deliberately does **not** do

- No fade-in/out on stream open or PTT release. The model produces silence between utterances; playback faithfully reproduces it.
- No per-utterance stream open/close.
- No volume / gain transform. PCM bytes are written as-is. System volume is the only knob, per functional spec §2.3.
- No automatic recovery from a default-device change mid-session. PortAudio holds the device it was opened with; if the user changes the default mid-session, audio continues routing to the original device (functional spec §2.2 explicitly leaves this undefined for the PoC).

---

## 3. Impact and Risk Analysis

### System Dependencies

- **`sounddevice`** (PortAudio binding) — new Python dependency for this spec. Pinned via `uv`.
- **PortAudio** — bundled with `sounddevice` wheels on macOS; no separate install.
- **macOS Core Audio** — used by PortAudio under the hood; gives us the default output device automatically.
- **Realtime session output format** — read from session metadata at startup. Owned by the model-plumbing spec; this spec only consumes the rate.

### Potential Risks & Mitigations

| Risk | Mitigation |
|---|---|
| `latency='low'` may pick a buffer too small for jittery network arrivals, causing audible underruns. | Underrun counter exposes this directly. If the end-of-session summary shows non-trivial underrun counts, bump `latency` to `'high'` (still ~tens of ms, acceptable for PoC) and re-test. |
| PortAudio callback exceptions silently kill the audio thread on some hosts. | Wrap the callback body in `try/except`, log to stderr on exception, fill `outdata` with zeros, and continue. Never raise out of the callback. |
| Default device changes mid-session leave the stream playing to a now-unrouted device (e.g. AirPods disconnect). | Out of scope per functional spec §2.2. PortAudio will either keep playing into the void or surface an error in `status`; we log either way but don't try to recover. |
| Realtime session reports an audio format we can't open (unusual rate, channel count, encoding). | At `open()` time, if `sounddevice.OutputStream(...)` raises, log a clear error naming the rejected parameters and exit. Don't fall back to a hardcoded rate — the user would silently get the wrong pitch. |
| Queue grows unboundedly if the model produces audio faster than the callback consumes it (shouldn't happen at steady state but theoretically possible during a stall). | Cap the queue at a generous size (e.g. 5 seconds of audio = ~240 KB at 24 kHz int16 mono). Beyond that, drop oldest chunks and increment a separate `overflow_count`. Log at session end alongside underruns. |
| Calling `Playback.write` from inside an asyncio task while the GIL is contended could in theory add ms-scale latency. | `queue.SimpleQueue.put` is constant-time and lock-free for single-producer use; acceptable for PoC. |

---

## 4. Testing Strategy

**Python unit tests** (`tests/test_playback.py`):

- **Queue → callback pipeline.** Feed a sequence of known PCM byte chunks into `Playback.write`, then invoke the callback directly with various `frames` sizes (smaller than a chunk, larger than a chunk, spanning chunk boundaries). Assert the output bytes match the concatenation of inputs and that `underrun_count` is 0.
- **Underrun accounting.** Invoke the callback with an empty queue; assert `outdata` is all zeros and `underrun_count` increments by 1 per call.
- **Overflow accounting.** Push more than the cap; assert oldest chunks are dropped and `overflow_count` increments.
- **First-write hook.** Register a hook; assert it fires exactly once on the first `write` after `open()` and again only after a quiet-period reset.
- **Format-mismatch error.** Mock `sounddevice.RawOutputStream` to raise on construction; assert `Playback.open` propagates a clearly-named error.

PortAudio itself is **not** unit-tested — sounddevice is a thin binding to a well-known library. The callback function is testable in isolation because it takes plain bytes in and out.

**Manual smoke tests** (single author, on the target Mac, must pass end-to-end with the rest of Phase 1 wired up):

1. **Mid-sentence audibility.** Hold ⌥⌘T, speak a 5–6 second sentence in the source language. Translated speech in the target language must begin playing before the source sentence finishes. Subjective pass criterion.
2. **Tail finishes after release.** Hold ⌥⌘T, speak a sentence, release **immediately** at the last source-language word. Translated audio for the tail must continue playing for a beat or two after release, not cut off mid-word.
3. **Default device honored.** Set system output to built-in speakers → launch → press PTT → audio comes from speakers. Quit, switch system output to a connected headphone set, relaunch → audio comes from headphones.
4. **System volume controls loudness.** During an utterance, hit the macOS volume-down keys → translated audio gets quieter in real time.
5. **10-minute session stability.** Run a full ~10-minute back-and-forth session. At end, check the end-of-session log line: `playback: <n> underruns in session`. A single-digit count is acceptable for a PoC; a three-digit count means we need to revisit `latency`/buffer settings.
6. **Idle silence.** Start the tool, do not press PTT for 30 s, listen — output device must be silent the whole time. No "stream-open hiss," no buffer-flush click.

No automated end-to-end test — the realtime model can't be deterministically driven, and the PoC's success criterion is subjective.
