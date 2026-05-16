"""IPC event types and stderr parser for the Swift capture binary.

The Swift binary emits one JSON object per line on stderr, plus the occasional
free-form log line. We dispatch each line into a typed event. Anything that
does not parse as JSON, or whose ``event`` field is unrecognized, becomes a
:class:`LogLine` carrying the original (newline-stripped) text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class Ready:
    """Swift process has started and is about to register the hotkey."""


@dataclass(frozen=True)
class HotkeyRegistered:
    """Carbon hotkey registration succeeded."""

    chord: str


@dataclass(frozen=True)
class HotkeyDown:
    """Hotkey was pressed."""

    ts: str


@dataclass(frozen=True)
class HotkeyUp:
    """Hotkey was released."""

    ts: str


@dataclass(frozen=True)
class Error:
    """Swift-side error event. ``code`` is a closed-set token.

    ``message`` is an optional free-form one-line reason — currently only
    ``mic_lost`` carries one (e.g. ``"default input removed"``). Absent on
    startup-time errors where the code alone identifies the failure.
    """

    code: str
    message: str | None = None


@dataclass(frozen=True)
class LogLine:
    """Any stderr line that is not a recognized JSON event."""

    raw: str


Event = Ready | HotkeyRegistered | HotkeyDown | HotkeyUp | Error | LogLine


def _decode(line: str) -> Event:
    stripped = line.rstrip("\n").rstrip("\r")
    if not stripped:
        return LogLine(raw=stripped)

    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return LogLine(raw=stripped)

    if not isinstance(obj, dict):
        return LogLine(raw=stripped)

    kind = obj.get("event")
    match kind:
        case "ready":
            return Ready()
        case "hotkey_registered":
            chord = obj.get("chord")
            if isinstance(chord, str):
                return HotkeyRegistered(chord=chord)
            return LogLine(raw=stripped)
        case "hotkey_down":
            ts = obj.get("ts")
            if isinstance(ts, str):
                return HotkeyDown(ts=ts)
            return LogLine(raw=stripped)
        case "hotkey_up":
            ts = obj.get("ts")
            if isinstance(ts, str):
                return HotkeyUp(ts=ts)
            return LogLine(raw=stripped)
        case "error":
            code = obj.get("code")
            if isinstance(code, str):
                message = obj.get("message")
                return Error(
                    code=code,
                    message=message if isinstance(message, str) else None,
                )
            return LogLine(raw=stripped)
        case _:
            return LogLine(raw=stripped)


async def parse_events(stream: asyncio.StreamReader) -> AsyncIterator[Event]:
    """Yield typed :class:`Event` values for every line read from ``stream``.

    Stops cleanly at EOF. UTF-8 decoding errors fall back to ``replace`` so a
    single garbled byte does not kill the orchestrator.
    """

    while True:
        raw = await stream.readline()
        if not raw:
            return
        line = raw.decode("utf-8", errors="replace")
        yield _decode(line)
