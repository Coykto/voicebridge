"""CLI entry point: ``python -m voicebridge``.

Boots the Swift capture binary, performs the startup handshake, then prints
``down`` / ``up`` for each hotkey event until Ctrl+C. Free-form (non-JSON)
stderr lines from Swift are appended to ``./logs/<ts>-capture.log``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import os
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TextIO

from . import errors
from .capture import spawn
from .hotkey import PTTState
from .ipc import Error, Event, HotkeyDown, HotkeyRegistered, HotkeyUp, LogLine, Ready

STARTUP_TIMEOUT_SECONDS = 5.0


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
        "--source",
        default="ru",
        help="Source language code (accepted but unused in Slice 2).",
    )
    parser.add_argument(
        "--target",
        default="en",
        help="Target language code (accepted but unused in Slice 2).",
    )
    return parser.parse_args(argv)


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
            message, exit_code = errors.lookup(event.code)
            raise _StartupError(message, exit_code)
        # Unexpected hotkey event before registration — keep waiting.


async def _event_loop(
    events: AsyncIterator[Event], capture_log: _CaptureLog, state: PTTState
) -> None:
    async for event in events:
        if isinstance(event, HotkeyDown):
            state.on_down()
            print("down", flush=True)
        elif isinstance(event, HotkeyUp):
            state.on_up()
            print("up", flush=True)
        elif isinstance(event, LogLine):
            capture_log.write(event.raw)
        elif isinstance(event, Error):
            print(f"capture error: {event.code}", file=sys.stderr, flush=True)
        # Ready / HotkeyRegistered after startup: ignore silently.


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


async def run(args: argparse.Namespace) -> int:
    global _proc_for_cleanup
    capture_log = _CaptureLog(_log_filename())
    proc, events = await spawn()
    _proc_for_cleanup = proc

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)

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

        print(
            "[ready] press ⌥⌘T to translate (Ctrl+C to quit)",
            flush=True,
        )

        state = PTTState()
        event_task = asyncio.create_task(_event_loop(events, capture_log, state))
        shutdown_task = asyncio.create_task(shutdown.wait())
        try:
            done, _pending = await asyncio.wait(
                {event_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            event_task.cancel()
            shutdown_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
            with contextlib.suppress(asyncio.CancelledError):
                await shutdown_task
        return 0
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
        await _terminate(proc)
        capture_log.close()


def main() -> None:
    args = _parse_args()
    try:
        exit_code = asyncio.run(run(args))
    except KeyboardInterrupt:
        exit_code = 0
    finally:
        if _proc_for_cleanup is not None:
            _hard_kill_sync(_proc_for_cleanup)
    raise SystemExit(exit_code)


if __name__ == "__main__":  # pragma: no cover
    main()
