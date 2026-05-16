"""Push-to-talk state and audible press/release cues."""

from __future__ import annotations

import asyncio
import logging
import subprocess

logger = logging.getLogger(__name__)

SUBMARINE_PATH = "/System/Library/Sounds/Submarine.aiff"
POP_PATH = "/System/Library/Sounds/Pop.aiff"


def _play(path: str) -> None:
    try:
        subprocess.Popen(["afplay", path])
    except (FileNotFoundError, OSError) as exc:
        logger.warning("failed to play cue %s: %s", path, exc)


class PTTState:
    """Tracks whether the push-to-talk hotkey is currently held."""

    def __init__(self) -> None:
        self.ptt_active: asyncio.Event = asyncio.Event()

    def on_down(self) -> None:
        self.ptt_active.set()
        _play(SUBMARINE_PATH)

    def on_up(self) -> None:
        self.ptt_active.clear()
        _play(POP_PATH)
