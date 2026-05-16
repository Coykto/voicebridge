"""Spawn and supervise the Swift capture subprocess."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from .ipc import Event, parse_events

DEFAULT_BINARY = "./swift-capture/.build/release/voicebridge-capture"


async def spawn(
    binary_path: str | os.PathLike[str] = DEFAULT_BINARY,
) -> tuple[asyncio.subprocess.Process, AsyncIterator[Event]]:
    """Launch the Swift capture binary and return ``(proc, events)``.

    ``proc.stdout`` carries raw PCM frames (binary) and is intentionally not
    consumed here — Slice 2 only cares about parsed stderr events. Callers are
    responsible for terminating ``proc`` on shutdown.
    """

    proc = await asyncio.create_subprocess_exec(
        os.fspath(binary_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None  # PIPE above guarantees this
    return proc, parse_events(proc.stderr)
