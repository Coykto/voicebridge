"""Slice 4 smoke harness — stream a raw PCM file into the realtime translation
session and write the translated audio back to a raw PCM file.

Prepare input from any Russian-speech wav:
    ffmpeg -i russian.wav -f s16le -ac 1 -ar 24000 russian.pcm

Play back the output:
    ffplay -f s16le -ac 1 -ar 24000 english.pcm
    # or convert to wav:
    ffmpeg -f s16le -ac 1 -ar 24000 -i english.pcm english.wav

Usage:
    uv run python scripts/realtime_send_pcm_file.py --in russian.pcm --out english.pcm
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from voicebridge import errors
from voicebridge.config import load_config
from voicebridge.realtime import RealtimeSession

FRAME_BYTES = 960  # 20 ms @ 24 kHz mono int16
FRAME_PERIOD_S = 0.02
INBOUND_IDLE_TIMEOUT_S = 2.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream a raw PCM file through the realtime translation session.",
    )
    parser.add_argument(
        "--in", dest="in_path", type=Path, required=True, help="Input PCM file"
    )
    parser.add_argument(
        "--out", dest="out_path", type=Path, required=True, help="Output PCM file"
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    in_path: Path = args.in_path
    out_path: Path = args.out_path

    try:
        config = load_config()
    except errors.ConfigError as exc:
        errors.handle(exc)

    session = await RealtimeSession.open(config)

    last_inbound_time = time.monotonic()

    async def consume() -> None:
        nonlocal last_inbound_time
        with out_path.open("wb") as out_file:
            async for frame in session.audio_frames():
                out_file.write(frame)
                out_file.flush()
                last_inbound_time = time.monotonic()

    consumer_task = asyncio.create_task(consume())

    try:
        with in_path.open("rb") as in_file:
            session.mark_turn_start()
            while True:
                chunk = in_file.read(FRAME_BYTES)
                if not chunk:
                    break
                if len(chunk) < FRAME_BYTES:
                    # Pad the final partial frame with silence.
                    chunk = chunk + b"\x00" * (FRAME_BYTES - len(chunk))
                await session.send_frame(chunk)
                await asyncio.sleep(FRAME_PERIOD_S)

        # Wait for the model to finish emitting audio.
        eof_time = time.monotonic()
        while True:
            now = time.monotonic()
            # Idle is measured from the last inbound frame, or from EOF if
            # no inbound frames have arrived yet.
            reference = max(last_inbound_time, eof_time) if last_inbound_time < eof_time else last_inbound_time
            if now - reference >= INBOUND_IDLE_TIMEOUT_S:
                break
            await asyncio.sleep(0.1)
    finally:
        await session.close()
        try:
            await asyncio.wait_for(consumer_task, timeout=2.0)
        except asyncio.TimeoutError:
            consumer_task.cancel()
            try:
                await consumer_task
            except (asyncio.CancelledError, Exception):
                pass

    print(
        f"inbound_drops={session.inbound_drops} outbound_drops={session.outbound_drops}",
        flush=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (errors.ConfigError, errors.RealtimeError) as exc:
        errors.handle(exc)
