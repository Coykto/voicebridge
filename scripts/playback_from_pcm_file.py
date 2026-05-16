"""Play a raw int16 mono 24 kHz PCM file through voicebridge.playback.

This script streams a `.pcm` file into a Playback instance in real-time-ish
chunks, exercising the same code path the orchestrator will hit when the
OpenAI Realtime API delivers translated audio.

Producing a test `.pcm` file from any wav (or other audio):

    ffmpeg -i input.wav -f s16le -ac 1 -ar 24000 input.pcm

Running this script:

    uv run python scripts/playback_from_pcm_file.py --file speech.pcm

Optional flags let you simulate a silence run inside the file. When the
script sees `--silence-min-ms` worth of consecutive "silent" chunks (peak
int16 amplitude <= `--silence-threshold`), it calls `playback.mark_idle()`,
which re-arms the `on_first_write_after_idle` hook so the next non-silent
chunk logs `playback: first_write_after_idle` to stderr.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

from voicebridge.playback import Playback


SAMPLE_RATE = 24000
BYTES_PER_SAMPLE = 2
TAIL_SLEEP_S = 0.5


def chunk_peak_abs(chunk: bytes) -> int:
    if not chunk:
        return 0
    sample_count = len(chunk) // BYTES_PER_SAMPLE
    if sample_count == 0:
        return 0
    samples = struct.unpack(f"<{sample_count}h", chunk[: sample_count * BYTES_PER_SAMPLE])
    peak = 0
    for s in samples:
        a = -s if s < 0 else s
        if a > peak:
            peak = a
    return peak


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Play a raw int16 mono 24 kHz PCM file through voicebridge.playback.",
    )
    parser.add_argument(
        "--file",
        required=True,
        type=Path,
        help="Path to a raw int16 mono 24 kHz PCM file.",
    )
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=40,
        help="Chunk size in milliseconds (default: 40).",
    )
    parser.add_argument(
        "--silence-threshold",
        type=int,
        default=200,
        help=(
            "Peak int16 amplitude at or below which a chunk counts as silent "
            "(default: 200)."
        ),
    )
    parser.add_argument(
        "--silence-min-ms",
        type=int,
        default=300,
        help=(
            "Accumulated silent duration (ms) that triggers a single "
            "playback.mark_idle() call (default: 300)."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    path: Path = args.file
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        raise SystemExit(2)

    chunk_ms: int = args.chunk_ms
    if chunk_ms <= 0:
        print("error: --chunk-ms must be positive", file=sys.stderr)
        raise SystemExit(2)

    samples_per_chunk = int(SAMPLE_RATE * chunk_ms / 1000)
    bytes_per_chunk = samples_per_chunk * BYTES_PER_SAMPLE
    if bytes_per_chunk <= 0:
        print("error: --chunk-ms is too small for 24 kHz PCM", file=sys.stderr)
        raise SystemExit(2)

    silence_threshold: int = args.silence_threshold
    silence_min_ms: int = args.silence_min_ms

    def on_first_write_after_idle() -> None:
        print("playback: first_write_after_idle", file=sys.stderr)

    playback = Playback()
    playback.on_first_write_after_idle = on_first_write_after_idle
    playback.open(SAMPLE_RATE)

    silent_ms_accum = 0
    idle_marked_for_run = False

    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(bytes_per_chunk)
                if not chunk:
                    break

                peak = chunk_peak_abs(chunk)
                is_silent = peak <= silence_threshold

                if is_silent:
                    silent_ms_accum += chunk_ms
                    if (
                        not idle_marked_for_run
                        and silent_ms_accum >= silence_min_ms
                    ):
                        playback.mark_idle()
                        idle_marked_for_run = True
                else:
                    silent_ms_accum = 0
                    idle_marked_for_run = False

                playback.write(chunk)
                time.sleep(chunk_ms / 1000)

        time.sleep(TAIL_SLEEP_S)
    finally:
        playback.close()


if __name__ == "__main__":
    main()
