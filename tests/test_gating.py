"""Tests for ``voicebridge.gating.MicGate``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from voicebridge.gating import MicGate


class _ScriptedPTT:
    """Drives ``ptt_active`` from a per-chunk schedule.

    ``schedule[i]`` is the desired state of ``ptt_active`` BEFORE the i-th
    chunk is delivered to the gate. The source coroutine sets / clears the
    event as needed before yielding, so the gate's transition detection sees
    a consistent state for each chunk.
    """

    def __init__(self, schedule: list[bool]) -> None:
        self.schedule = schedule
        self.ptt_active = asyncio.Event()

    async def feed(self, total_chunks: int) -> AsyncIterator[bytes]:
        chunk = b"\x00" * 640
        for i in range(total_chunks):
            should_be_set = self.schedule[i] if i < len(self.schedule) else False
            if should_be_set and not self.ptt_active.is_set():
                self.ptt_active.set()
            elif not should_be_set and self.ptt_active.is_set():
                self.ptt_active.clear()
            yield chunk


async def test_forwards_only_while_ptt_active_and_logs_one_pair(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 100 chunks; active for indices 20..79 inclusive (60 chunks).
    schedule = [False] * 20 + [True] * 60 + [False] * 20
    driver = _ScriptedPTT(schedule)

    forwarded: list[bytes] = []

    async def sink(chunk: bytes) -> None:
        forwarded.append(chunk)

    logger = logging.getLogger("voicebridge.test.gating.one")
    logger.setLevel(logging.INFO)

    gate = MicGate(driver.ptt_active, logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await gate.run(driver.feed(100), sink)

    assert len(forwarded) == 60
    msgs = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert msgs == ["capture started", "capture stopped (1200 ms captured)"]


async def test_two_on_off_cycles_produce_two_pairs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 50 chunks; two distinct active windows.
    schedule = (
        [False] * 5
        + [True] * 10  # session 1: 10 chunks = 200 ms
        + [False] * 5
        + [True] * 15  # session 2: 15 chunks = 300 ms
        + [False] * 15
    )
    driver = _ScriptedPTT(schedule)

    forwarded: list[bytes] = []

    async def sink(chunk: bytes) -> None:
        forwarded.append(chunk)

    logger = logging.getLogger("voicebridge.test.gating.two")
    logger.setLevel(logging.INFO)

    gate = MicGate(driver.ptt_active, logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await gate.run(driver.feed(50), sink)

    assert len(forwarded) == 25
    msgs = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert msgs == [
        "capture started",
        "capture stopped (200 ms captured)",
        "capture started",
        "capture stopped (300 ms captured)",
    ]


async def test_drops_when_never_active(
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule = [False] * 30
    driver = _ScriptedPTT(schedule)

    forwarded: list[bytes] = []

    async def sink(chunk: bytes) -> None:
        forwarded.append(chunk)

    logger = logging.getLogger("voicebridge.test.gating.none")
    logger.setLevel(logging.INFO)

    gate = MicGate(driver.ptt_active, logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await gate.run(driver.feed(30), sink)

    assert forwarded == []
    msgs = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert msgs == []


async def test_default_sink_is_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    schedule = [True] * 5 + [False] * 5
    driver = _ScriptedPTT(schedule)

    logger = logging.getLogger("voicebridge.test.gating.noop")
    logger.setLevel(logging.INFO)

    gate = MicGate(driver.ptt_active, logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await gate.run(driver.feed(10))

    msgs = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert msgs == ["capture started", "capture stopped (100 ms captured)"]


async def test_stream_ends_mid_session_emits_stop_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Active throughout; source ends after 7 chunks. We still want the
    # `capture stopped` log line so press/release pairs stay balanced.
    schedule = [True] * 7
    driver = _ScriptedPTT(schedule)

    logger = logging.getLogger("voicebridge.test.gating.eof")
    logger.setLevel(logging.INFO)

    gate = MicGate(driver.ptt_active, logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await gate.run(driver.feed(7))

    msgs = [r.getMessage() for r in caplog.records if r.name == logger.name]
    assert msgs == ["capture started", "capture stopped (140 ms captured)"]
