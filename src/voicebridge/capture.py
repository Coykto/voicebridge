"""Spawn and supervise the Swift capture subprocess."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from .ipc import Event, parse_events

DEFAULT_BINARY = "./src/voicebridge/bin/voicebridge-capture.app/Contents/MacOS/voicebridge-capture"

# 320 little-endian Int16 samples = 20 ms of 16 kHz mono audio. Locked by
# tech spec §2.4; mirrored by the Swift side's ring-buffer flush threshold.
PCM_CHUNK_BYTES = 640


async def spawn(
    binary_path: str | os.PathLike[str] = DEFAULT_BINARY,
) -> tuple[asyncio.subprocess.Process, AsyncIterator[Event]]:
    """Launch the Swift capture binary and return ``(proc, events)``.

    ``proc.stdout`` carries raw PCM frames (binary); call :func:`pcm_frames`
    on ``proc.stdout`` to consume them as 640-byte chunks. Callers are
    responsible for terminating ``proc`` on shutdown.
    """

    proc = await asyncio.create_subprocess_exec(
        os.fspath(binary_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None  # PIPE above guarantees this
    return proc, parse_events(proc.stderr)


async def pcm_frames(stdout: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield consecutive 640-byte PCM chunks from the Swift binary's stdout.

    Raises :class:`EOFError` when the stream closes — typically because the
    Swift binary has exited (SIGINT, unrecoverable mic loss, crash). Mid-chunk
    truncation is treated as EOF: a partial read can't be re-aligned without
    dropping data, and the orchestrator's 20 ms framing assumption no longer
    holds, so shutdown is the only correct response.
    """

    while True:
        try:
            chunk = await stdout.readexactly(PCM_CHUNK_BYTES)
        except asyncio.IncompleteReadError as exc:
            raise EOFError("Swift mic stream closed") from exc
        yield chunk
