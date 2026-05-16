"""Tests for ``voicebridge.capture.pcm_frames``."""

from __future__ import annotations

import asyncio

import pytest

from voicebridge.capture import PCM_CHUNK_BYTES, pcm_frames


def _stream_from(payload: bytes) -> asyncio.StreamReader:
    """Build an ``asyncio.StreamReader`` pre-loaded with ``payload`` and EOF."""

    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


async def test_yields_exact_chunks_until_eof() -> None:
    n = 7
    payload = bytes(range(256)) * (n * PCM_CHUNK_BYTES // 256 + 1)
    payload = payload[: n * PCM_CHUNK_BYTES]
    reader = _stream_from(payload)

    out: list[bytes] = []
    with pytest.raises(EOFError):
        async for chunk in pcm_frames(reader):
            out.append(chunk)

    assert len(out) == n
    assert all(len(c) == PCM_CHUNK_BYTES for c in out)
    assert b"".join(out) == payload


async def test_mid_chunk_truncation_raises_eof_error() -> None:
    # Three full chunks + 100 extra bytes — the trailing partial read can't
    # be completed and must surface as EOFError after the three valid yields.
    n = 3
    payload = b"\x00" * (n * PCM_CHUNK_BYTES) + b"\xff" * 100
    reader = _stream_from(payload)

    out: list[bytes] = []
    with pytest.raises(EOFError):
        async for chunk in pcm_frames(reader):
            out.append(chunk)

    assert len(out) == n
    assert all(len(c) == PCM_CHUNK_BYTES for c in out)


async def test_clean_eof_at_chunk_boundary_raises_eof_error() -> None:
    # Empty stream → first readexactly raises immediately.
    reader = _stream_from(b"")

    out: list[bytes] = []
    with pytest.raises(EOFError):
        async for chunk in pcm_frames(reader):
            out.append(chunk)

    assert out == []
