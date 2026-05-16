"""Push-to-talk gating for the mic PCM stream.

Tech spec §2.5: :class:`MicGate` consumes the continuous 640-byte PCM stream
from the Swift binary and forwards a chunk to ``sink`` only while
``ptt_active`` is set. Per session (false→true→false) it emits exactly two
log lines: one ``capture started`` on press and one
``capture stopped (N ms captured)`` on release, where ``N`` is the count of
forwarded frames × 20 ms.

``sink`` is callable as ``sink(chunk: bytes)``. For this spec it's a no-op
placeholder; the realtime-model spec will wire it to OpenAI. Sync or async
callables are both supported — the gate ``await``s the return value if it is
awaitable so future async sinks (e.g. ``websocket.send``) drop in cleanly.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncio

# 20 ms per chunk — matches the Swift ring-buffer flush threshold and the
# Python reader's ``PCM_CHUNK_BYTES`` framing.
_CHUNK_MS = 20

Sink = Callable[[bytes], Awaitable[None] | None]


async def _noop_sink(_chunk: bytes) -> None:
    """Default sink for Slice 4 — drops every forwarded frame on the floor."""

    return None


class MicGate:
    """Drop / forward PCM chunks based on a shared ``ptt_active`` flag."""

    def __init__(
        self,
        ptt_active: asyncio.Event,
        logger: logging.Logger,
    ) -> None:
        self._ptt_active = ptt_active
        self._logger = logger
        self._was_active = False
        self._frames_this_session = 0

    async def run(
        self,
        source: AsyncIterator[bytes],
        sink: Sink = _noop_sink,
    ) -> None:
        """Pump chunks from ``source`` into ``sink`` while PTT is held.

        Returns cleanly when ``source`` exhausts (e.g. Swift exits). If a
        session was still open at that moment, its ``capture stopped`` line
        is still emitted so the per-press / per-release pairing stays
        balanced even across an unexpected stream end.
        """

        try:
            async for chunk in source:
                active = self._ptt_active.is_set()

                if active and not self._was_active:
                    self._frames_this_session = 0
                    self._logger.info("capture started")
                elif self._was_active and not active:
                    self._emit_stopped()

                self._was_active = active

                if active:
                    self._frames_this_session += 1
                    result: Any = sink(chunk)
                    if inspect.isawaitable(result):
                        await result
        finally:
            # Stream ended mid-press: emit the matching `capture stopped` so
            # the on/off log pairing isn't left dangling.
            if self._was_active:
                self._emit_stopped()
                self._was_active = False

    def _emit_stopped(self) -> None:
        ms = self._frames_this_session * _CHUNK_MS
        self._logger.info("capture stopped (%d ms captured)", ms)
