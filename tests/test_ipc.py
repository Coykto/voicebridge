"""Tests for ``voicebridge.ipc.parse_events``.

We feed crafted bytes into an in-memory ``asyncio.StreamReader`` and verify the
parser produces the right typed events in the right order.
"""

from __future__ import annotations

import asyncio

import pytest

from voicebridge.ipc import (
    Error,
    Event,
    HotkeyDown,
    HotkeyRegistered,
    HotkeyUp,
    LogLine,
    Ready,
    parse_events,
)


def _reader_with(payload: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


async def _collect(reader: asyncio.StreamReader) -> list[Event]:
    return [event async for event in parse_events(reader)]


async def test_ready_event() -> None:
    events = await _collect(_reader_with(b'{"event":"ready"}\n'))
    assert events == [Ready()]


async def test_hotkey_registered_event() -> None:
    payload = b'{"event":"hotkey_registered","chord":"option+command+t"}\n'
    events = await _collect(_reader_with(payload))
    assert events == [HotkeyRegistered(chord="option+command+t")]


async def test_hotkey_down_and_up_carry_timestamps() -> None:
    payload = (
        b'{"event":"hotkey_down","ts":"2026-05-16T12:00:00Z"}\n'
        b'{"event":"hotkey_up","ts":"2026-05-16T12:00:01Z"}\n'
    )
    events = await _collect(_reader_with(payload))
    assert events == [
        HotkeyDown(ts="2026-05-16T12:00:00Z"),
        HotkeyUp(ts="2026-05-16T12:00:01Z"),
    ]


async def test_error_event() -> None:
    payload = b'{"event":"error","code":"accessibility_denied"}\n'
    events = await _collect(_reader_with(payload))
    assert events == [Error(code="accessibility_denied")]


async def test_malformed_json_becomes_logline() -> None:
    payload = b"not json at all\n"
    events = await _collect(_reader_with(payload))
    assert events == [LogLine(raw="not json at all")]


async def test_unknown_event_kind_becomes_logline() -> None:
    payload = b'{"event":"frobnicate","x":1}\n'
    events = await _collect(_reader_with(payload))
    assert len(events) == 1
    only = events[0]
    assert isinstance(only, LogLine)
    assert only.raw == '{"event":"frobnicate","x":1}'


async def test_multiple_events_arrive_in_order() -> None:
    payload = (
        b'{"event":"ready"}\n'
        b'{"event":"hotkey_registered","chord":"option+command+t"}\n'
        b"some swift log message\n"
        b'{"event":"hotkey_down","ts":"2026-05-16T12:00:00Z"}\n'
        b'{"event":"hotkey_up","ts":"2026-05-16T12:00:00.500Z"}\n'
    )
    events = await _collect(_reader_with(payload))
    assert events == [
        Ready(),
        HotkeyRegistered(chord="option+command+t"),
        LogLine(raw="some swift log message"),
        HotkeyDown(ts="2026-05-16T12:00:00Z"),
        HotkeyUp(ts="2026-05-16T12:00:00.500Z"),
    ]


async def test_eof_closes_iteration_cleanly() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    events = [event async for event in parse_events(reader)]
    assert events == []


async def test_hotkey_registered_without_chord_falls_back_to_logline() -> None:
    payload = b'{"event":"hotkey_registered"}\n'
    events = await _collect(_reader_with(payload))
    assert isinstance(events[0], LogLine)


@pytest.mark.parametrize("trailing", [b"\n", b"\r\n", b""])
async def test_handles_various_line_endings(trailing: bytes) -> None:
    # An asyncio.StreamReader.readline only splits on '\n'; the final line
    # without a newline is still yielded thanks to feed_eof.
    payload = b'{"event":"ready"}' + trailing
    events = await _collect(_reader_with(payload))
    assert events == [Ready()]
