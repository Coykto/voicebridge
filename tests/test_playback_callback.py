from __future__ import annotations

import struct

from voicebridge.playback import Playback


def _drive(playback: Playback, chunks: list[bytes], frames_per_call: int, total_bytes: int) -> bytes:
    for c in chunks:
        playback.write(c)

    out = bytearray()
    bytes_per_call = frames_per_call * 2
    while len(out) < total_bytes:
        buf = bytearray(bytes_per_call)
        playback._fill(memoryview(buf), frames_per_call)
        out.extend(buf)
    return bytes(out[:total_bytes])


def _pcm(start: int, count: int) -> bytes:
    return struct.pack(f"<{count}h", *[(start + i) & 0x7FFF for i in range(count)])


def test_frames_smaller_than_chunk() -> None:
    # one 400-sample chunk; drain it across 4 calls of 100 samples each
    pb = Playback()
    chunk = _pcm(0, 400)
    expected = chunk
    got = _drive(pb, [chunk], frames_per_call=100, total_bytes=len(expected))
    assert got == expected


def test_frames_larger_than_chunk() -> None:
    # three 100-sample chunks; one 300-sample call should consume all
    pb = Playback()
    c1 = _pcm(0, 100)
    c2 = _pcm(1000, 100)
    c3 = _pcm(2000, 100)
    expected = c1 + c2 + c3
    got = _drive(pb, [c1, c2, c3], frames_per_call=300, total_bytes=len(expected))
    assert got == expected


def test_frames_split_across_chunk_boundary() -> None:
    # carry behavior: two 150-sample chunks fed; pulled in 100-sample frames
    pb = Playback()
    c1 = _pcm(0, 150)
    c2 = _pcm(1000, 150)
    expected = c1 + c2  # 300 samples = 600 bytes
    got = _drive(pb, [c1, c2], frames_per_call=100, total_bytes=len(expected))
    assert got == expected


def test_uneven_carry_then_drain() -> None:
    # Chunk sizes that don't align with frames in any direction
    pb = Playback()
    chunks = [_pcm(0, 37), _pcm(1000, 91), _pcm(2000, 53), _pcm(3000, 119)]
    expected = b"".join(chunks)
    got = _drive(pb, chunks, frames_per_call=64, total_bytes=len(expected))
    assert got == expected


def test_underrun_count_increments_per_callback_on_empty_queue() -> None:
    pb = Playback()
    assert pb.underrun_count == 0

    frames_per_call = 100
    bytes_per_call = frames_per_call * 2
    buf = bytearray(bytes_per_call)

    pb._fill(memoryview(buf), frames_per_call)
    assert pb.underrun_count == 1
    assert bytes(buf) == b"\x00" * bytes_per_call

    pb._fill(memoryview(buf), frames_per_call)
    pb._fill(memoryview(buf), frames_per_call)
    assert pb.underrun_count == 3
    assert bytes(buf) == b"\x00" * bytes_per_call


def test_underrun_partial_fill_then_zero_pad() -> None:
    pb = Playback()
    # Provide 40 samples, request 100 -> partial fill + underrun
    partial = _pcm(0, 40)
    pb.write(partial)

    frames_per_call = 100
    bytes_per_call = frames_per_call * 2
    buf = bytearray(bytes_per_call)
    pb._fill(memoryview(buf), frames_per_call)

    assert pb.underrun_count == 1
    assert bytes(buf[: len(partial)]) == partial
    assert bytes(buf[len(partial):]) == b"\x00" * (bytes_per_call - len(partial))


def test_overflow_drops_oldest_and_counts_dropped_chunks() -> None:
    pb = Playback()
    sample_rate = 24000
    pb._sample_rate = sample_rate
    cap = 5 * sample_rate * 2  # 240000 bytes

    # Use chunk size that divides cap evenly: 24000 bytes (0.5s) -> cap holds 10 chunks
    chunk_size_bytes = 24000
    chunk_samples = chunk_size_bytes // 2

    # Push 10 chunks: should exactly fill the cap, no drops yet.
    chunks = [_pcm(i * 1000, chunk_samples) for i in range(10)]
    for c in chunks:
        pb.write(c)
    assert pb.overflow_count == 0
    assert pb._queued_bytes == cap

    # Push 3 more chunks: each should evict exactly one oldest chunk.
    extra = [_pcm(100000 + i * 1000, chunk_samples) for i in range(3)]
    for c in extra:
        pb.write(c)

    assert pb.overflow_count == 3
    assert pb._queued_bytes == cap

    # Drain the queue and confirm the remaining content matches the newest 10 chunks
    # (chunks[3:] + extra), in FIFO order.
    expected_remaining = chunks[3:] + extra
    drained: list[bytes] = []
    while True:
        try:
            drained.append(pb._queue.get_nowait())
        except Exception:
            break
    assert drained == expected_remaining


def test_overflow_oversized_single_write_drops_everything() -> None:
    pb = Playback()
    sample_rate = 24000
    pb._sample_rate = sample_rate
    cap = 5 * sample_rate * 2

    # Pre-fill with two small chunks
    c1 = _pcm(0, 1000)
    c2 = _pcm(1000, 1000)
    pb.write(c1)
    pb.write(c2)
    assert pb.overflow_count == 0

    # Write a chunk larger than cap: queue should be flushed of older chunks.
    big = _pcm(0, cap // 2 + 1000)  # in samples; bytes = (cap//2 + 1000) * 2 > cap
    assert len(big) > cap
    pb.write(big)

    # Both pre-existing chunks should be dropped (2 increments).
    assert pb.overflow_count == 2
