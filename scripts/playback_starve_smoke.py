from __future__ import annotations

import argparse
import math
import struct
import time

from voicebridge.playback import Playback


SAMPLE_RATE = 24000
FREQ_HZ = 440.0
BURST_DURATION_S = 0.5
SILENCE_GAP_S = 0.3
CHUNK_MS = 20
FLOOD_DURATION_S = 6.0
FLOOD_CHUNK_S = 1.0
FLOOD_TAIL_S = 0.5


def build_tone(duration_s: float) -> bytes:
    total_samples = int(SAMPLE_RATE * duration_s)
    amplitude = 0.3 * 32767
    two_pi_f_over_sr = 2.0 * math.pi * FREQ_HZ / SAMPLE_RATE
    samples = [
        int(amplitude * math.sin(two_pi_f_over_sr * n)) for n in range(total_samples)
    ]
    return struct.pack(f"<{total_samples}h", *samples)


def feed_burst(playback: Playback, pcm: bytes) -> None:
    samples_per_chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
    bytes_per_chunk = samples_per_chunk * 2
    for offset in range(0, len(pcm), bytes_per_chunk):
        chunk = pcm[offset : offset + bytes_per_chunk]
        playback.write(chunk)
        time.sleep(CHUNK_MS / 1000)


def run_starve() -> None:
    burst = build_tone(BURST_DURATION_S)
    playback = Playback()
    playback.open(SAMPLE_RATE)
    try:
        feed_burst(playback, burst)
        time.sleep(SILENCE_GAP_S)
        feed_burst(playback, burst)
        time.sleep(0.3)
    finally:
        playback.close()


def run_flood() -> None:
    tone = build_tone(FLOOD_DURATION_S)
    samples_per_chunk = int(SAMPLE_RATE * FLOOD_CHUNK_S)
    bytes_per_chunk = samples_per_chunk * 2
    playback = Playback()
    playback.open(SAMPLE_RATE)
    try:
        for offset in range(0, len(tone), bytes_per_chunk):
            playback.write(tone[offset : offset + bytes_per_chunk])
        time.sleep(FLOOD_TAIL_S)
    finally:
        playback.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["starve", "flood"], default="starve")
    args = parser.parse_args()
    if args.mode == "flood":
        run_flood()
    else:
        run_starve()


if __name__ == "__main__":
    main()
