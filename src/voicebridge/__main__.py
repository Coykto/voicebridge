"""CLI entry point: ``python -m voicebridge``.

Loads config, boots the Swift capture binary, performs the startup handshake,
opens a realtime translation session against OpenAI, prints the single ready
line, then prints ``down`` / ``up`` for each hotkey event until Ctrl+C or the
realtime session terminates. Free-form (non-JSON) stderr lines from Swift are
appended to ``./logs/<ts>-capture.log``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import logging
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TextIO

from . import errors
from .capture import pcm_frames, spawn
from .config import load_config
from .gating import MicGate
from .hotkey import PTTState
from .ipc import Error, Event, HotkeyDown, HotkeyRegistered, HotkeyUp, LogLine, Ready
from .playback import Playback
from .realtime import RealtimeSession

STARTUP_TIMEOUT_SECONDS = 5.0

# Realtime translations endpoint produces and expects 24 kHz mono PCM16
# (per OpenAI's realtime guide). Swift emits 16 kHz; the gate sink upsamples
# 16 → 24 kHz before send_frame so the server's auto-VAD reads the audio at
# the correct pitch / cadence.
MODEL_AUDIO_SAMPLE_RATE = 24000
MIC_SAMPLE_RATE = 16000

# Bucket for the per-PTT-session "capture started / capture stopped" lines.
# A dedicated logger (not the root) keeps the format independent of any future
# verbose / quiet flags applied elsewhere in the process. Func spec §2.3
# requires both lines to be visible in the terminal — attach a stderr
# StreamHandler with a message-only format so they look like the `down`/`up`
# lines already emitted by `_event_loop` (no timestamp, no level prefix).
_gate_logger = logging.getLogger("voicebridge.gate")
_gate_logger.setLevel(logging.INFO)
if not _gate_logger.handlers:
    _gate_handler = logging.StreamHandler(sys.stderr)
    _gate_handler.setFormatter(logging.Formatter("%(message)s"))
    _gate_logger.addHandler(_gate_handler)
    _gate_logger.propagate = False


class _StartupError(Exception):
    """Carries the user-facing message and exit code for a Swift startup error."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="voicebridge",
        description="Real-time voice translation PoC (Phase 1).",
    )
    parser.add_argument(
        "--output-device",
        default=None,
        help=(
            "Output device for translated audio. Pass a name substring (e.g. "
            "'BlackHole 2ch') or a numeric index from --list-output-devices. "
            "Default: system default output."
        ),
    )
    parser.add_argument(
        "--list-output-devices",
        action="store_true",
        help="Print available output devices and exit.",
    )
    return parser.parse_args(argv)


def _list_output_devices() -> None:
    import sounddevice

    hostapis = sounddevice.query_hostapis()
    for idx, dev in enumerate(sounddevice.query_devices()):
        if dev.get("max_output_channels", 0) > 0:
            hostapi_idx = dev.get("hostapi", -1)
            hostapi_name = (
                hostapis[hostapi_idx]["name"]
                if 0 <= hostapi_idx < len(hostapis)
                else ""
            )
            print(f"[{idx}] {dev['name']}  ({hostapi_name})")


def _resolve_output_device(value: str | None) -> str | int | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return stripped


def _log_filename() -> Path:
    ts = _dt.datetime.now().isoformat(timespec="seconds").replace(":", "-")
    return Path("logs") / f"{ts}-capture.log"


class _CaptureLog:
    """Lazy append-mode log file for free-form Swift stderr lines."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: TextIO | None = None

    def write(self, line: str) -> None:
        if self._fh is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")
        self._fh.write(line.rstrip("\n") + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


async def _next_event(events: AsyncIterator[Event]) -> Event:
    try:
        return await events.__anext__()
    except StopAsyncIteration as exc:  # subprocess exited before sending an event
        raise RuntimeError("capture subprocess closed stderr before startup") from exc


async def _await_startup(
    events: AsyncIterator[Event], capture_log: _CaptureLog
) -> HotkeyRegistered:
    """Wait for ``Ready`` then ``HotkeyRegistered``, each within the timeout."""

    saw_ready = False
    deadline_left = STARTUP_TIMEOUT_SECONDS
    while True:
        event = await asyncio.wait_for(_next_event(events), timeout=deadline_left)
        if isinstance(event, LogLine):
            capture_log.write(event.raw)
            continue
        if isinstance(event, Ready):
            if saw_ready:
                # second Ready — ignore but keep waiting
                continue
            saw_ready = True
            deadline_left = STARTUP_TIMEOUT_SECONDS
            continue
        if isinstance(event, HotkeyRegistered):
            return event
        if isinstance(event, Error):
            message, exit_code = errors.lookup(event.code, message=event.message)
            raise _StartupError(message, exit_code)
        # Unexpected hotkey event before registration — keep waiting.


class _RuntimeError(Exception):
    """Carries a user-facing message + exit code for a mid-session Swift error."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


async def _event_loop(
    events: AsyncIterator[Event],
    capture_log: _CaptureLog,
    state: PTTState,
    session: RealtimeSession,
) -> None:
    async for event in events:
        if isinstance(event, HotkeyDown):
            state.on_down()
            # Arm the realtime first-mic-frame / first-model-audio hooks for
            # the new turn. The translations endpoint auto-detects turns via
            # VAD, but `mark_turn_start` is also the signal that resets
            # internal latency timers.
            session.mark_turn_start()
            print("down", flush=True)
        elif isinstance(event, HotkeyUp):
            state.on_up()
            print("up", flush=True)
        elif isinstance(event, LogLine):
            capture_log.write(event.raw)
        elif isinstance(event, Error):
            # `mic_lost` is terminal — same handler as startup-time mic errors,
            # just arriving mid-session. Surface as an exception so the wait()
            # below returns and the orchestrator tears down cleanly.
            if event.code == "mic_lost":
                message, exit_code = errors.lookup(event.code, message=event.message)
                raise _RuntimeError(message, exit_code)
            print(f"capture error: {event.code}", file=sys.stderr, flush=True)
        # Ready / HotkeyRegistered after startup: ignore silently.


def _upsample_16k_to_24k(pcm16le: bytes) -> bytes:
    """Linear 16 kHz → 24 kHz upsample of mono PCM16.

    The realtime translations endpoint expects 24 kHz PCM16 input. Swift
    emits 16 kHz; rather than ship 16 kHz through and let the server
    misread our cadence (chipmunk effect), interpolate 2 source samples
    into 3 output samples (16 × 3 / 2 = 24). Linear interpolation is good
    enough for speech; no quality bar to hit at the PoC stage.
    """

    import array

    src = array.array("h")
    src.frombytes(pcm16le)
    n = len(src)
    if n == 0:
        return b""
    # Output count: ceil(n * 3 / 2) — every pair of source samples becomes
    # three output samples; an odd trailing sample becomes one trailing
    # sample with no interpolated neighbor.
    dst = array.array("h", [0] * ((n * 3 + 1) // 2))
    j = 0
    for i in range(0, n - 1, 2):
        s0 = src[i]
        s1 = src[i + 1]
        # Positions 0/3, 1/3, 2/3 across the s0..s1 segment.
        dst[j] = s0
        dst[j + 1] = (2 * s0 + s1) // 3
        dst[j + 2] = (s0 + 2 * s1) // 3
        j += 3
    # Odd trailing source sample, if any.
    if n % 2 == 1:
        dst[j] = src[-1]
        j += 1
    return dst[:j].tobytes()


async def _pcm_pump(
    stdout: asyncio.StreamReader,
    state: PTTState,
    session: RealtimeSession,
) -> None:
    """Drain the Swift binary's PCM stdout through the PTT gate into the model.

    Returns cleanly on EOF (Swift exited). The caller treats that as the
    signal to shut the orchestrator down — same effect as Ctrl+C.
    """

    async def sink(chunk: bytes) -> None:
        await session.send_frame(_upsample_16k_to_24k(chunk))

    gate = MicGate(state.ptt_active, _gate_logger)
    try:
        await gate.run(pcm_frames(stdout), sink)
    except EOFError:
        _gate_logger.info("Swift mic stream closed")


async def _playback_pump(
    session: RealtimeSession, playback: Playback
) -> None:
    """Drain translated-audio frames from the realtime session into Playback."""

    async for frame in session.audio_frames():
        playback.write(frame)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=2.0)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(proc.wait())


def _hard_kill_sync(proc: asyncio.subprocess.Process) -> None:
    """Synchronous best-effort termination — runs after the loop is gone."""

    import os as _os
    import signal as _signal
    import time as _time

    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        _os.kill(pid, _signal.SIGTERM)
    except ProcessLookupError:
        return
    # Give it up to a second to exit gracefully.
    for _ in range(20):
        try:
            done_pid, _status = _os.waitpid(pid, _os.WNOHANG)
            if done_pid == pid:
                return
        except ChildProcessError:
            return
        _time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        _os.kill(pid, _signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        _os.waitpid(pid, 0)


_proc_for_cleanup: asyncio.subprocess.Process | None = None


async def run(args: argparse.Namespace, config) -> int:
    global _proc_for_cleanup
    capture_log = _CaptureLog(_log_filename())
    proc, events = await spawn()
    _proc_for_cleanup = proc

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)

    session: RealtimeSession | None = None
    playback: Playback | None = None
    try:
        try:
            await _await_startup(events, capture_log)
        except asyncio.TimeoutError:
            print("Swift capture did not start in time", file=sys.stderr, flush=True)
            return 3
        except _StartupError as exc:
            print(exc.message, file=sys.stderr, flush=True)
            return exc.exit_code
        except RuntimeError as exc:
            print(f"voicebridge: {exc}", file=sys.stderr, flush=True)
            return 1

        session = await RealtimeSession.open(config)
        playback = Playback()
        output_device = _resolve_output_device(getattr(args, "output_device", None))
        playback.open(MODEL_AUDIO_SAMPLE_RATE, device=output_device)
        device_label = (
            f" [output: {output_device}]" if output_device is not None else ""
        )
        print(
            f"Connected. Russian → {config.target_lang_name}.{device_label} Ready.",
            flush=True,
        )

        state = PTTState()
        event_task = asyncio.create_task(
            _event_loop(events, capture_log, state, session)
        )
        shutdown_task = asyncio.create_task(shutdown.wait())
        session_task = asyncio.create_task(session.wait_closed())
        assert proc.stdout is not None  # spawn() always uses PIPE
        pcm_task = asyncio.create_task(_pcm_pump(proc.stdout, state, session))
        playback_task = asyncio.create_task(_playback_pump(session, playback))
        try:
            await asyncio.wait(
                {event_task, shutdown_task, session_task, pcm_task, playback_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            event_task.cancel()
            shutdown_task.cancel()
            session_task.cancel()
            pcm_task.cancel()
            playback_task.cancel()
            # Suppress every exception while draining cancellation — a task
            # that already completed with an error (e.g. `_event_loop`
            # raising `_RuntimeError` for mid-session `mic_lost`) would
            # re-raise it here and shadow the dedicated exit-code path
            # below. The actual exception is read off the task afterward.
            with contextlib.suppress(BaseException):
                await event_task
            with contextlib.suppress(BaseException):
                await shutdown_task
            with contextlib.suppress(BaseException):
                await session_task
            with contextlib.suppress(BaseException):
                await pcm_task

        # `mic_lost` from the stderr event loop surfaces as `_RuntimeError`
        # and outranks every other completion condition — print + exit
        # with the dedicated code so the user sees the disconnection reason
        # rather than a generic shutdown.
        if event_task.done() and not event_task.cancelled():
            event_exc = event_task.exception()
            if isinstance(event_exc, _RuntimeError):
                print(event_exc.message, file=sys.stderr, flush=True)
                return event_exc.exit_code
            if event_exc is not None:
                raise event_exc

        if session_task.done() and not session_task.cancelled():
            session_exc = session_task.exception()
            if session_exc is not None:
                raise session_exc
        return 0
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        if playback is not None:
            with contextlib.suppress(Exception):
                playback.close()
        await _terminate(proc)
        capture_log.close()


def main() -> None:
    args = _parse_args()
    if args.list_output_devices:
        _list_output_devices()
        raise SystemExit(0)
    try:
        config = load_config()
    except errors.ConfigError as exc:
        errors.handle(exc)
    try:
        exit_code = asyncio.run(run(args, config))
    except KeyboardInterrupt:
        exit_code = 0
    except errors.RealtimeError as exc:
        if _proc_for_cleanup is not None:
            _hard_kill_sync(_proc_for_cleanup)
        errors.handle(exc)
    finally:
        if _proc_for_cleanup is not None:
            _hard_kill_sync(_proc_for_cleanup)
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
