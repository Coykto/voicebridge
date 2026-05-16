from __future__ import annotations

import math
import struct
import time

from voicebridge.playback import Playback


SAMPLE_RATE = 24000
FREQ_HZ = 440.0
DURATION_S = 1.5
CHUNK_MS = 20


def build_tone() -> bytes:
    total_samples = int(SAMPLE_RATE * DURATION_S)
    amplitude = 0.3 * 32767
    two_pi_f_over_sr = 2.0 * math.pi * FREQ_HZ / SAMPLE_RATE
    samples = [
        int(amplitude * math.sin(two_pi_f_over_sr * n)) for n in range(total_samples)
    ]
    return struct.pack(f"<{total_samples}h", *samples)


def main() -> None:
    pcm = build_tone()
    samples_per_chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
    bytes_per_chunk = samples_per_chunk * 2

    playback = Playback()
    playback.open(SAMPLE_RATE)
    try:
        for offset in range(0, len(pcm), bytes_per_chunk):
            chunk = pcm[offset : offset + bytes_per_chunk]
            playback.write(chunk)
        time.sleep(DURATION_S + 0.3)
    finally:
        playback.close()


if __name__ == "__main__":
    main()
