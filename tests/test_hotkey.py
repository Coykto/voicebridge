"""Tests for ``voicebridge.hotkey.PTTState``.

We monkeypatch ``subprocess.Popen`` to avoid spawning ``afplay`` and to capture
the exact arguments used for each cue.
"""

from __future__ import annotations

import pytest

from voicebridge import hotkey
from voicebridge.hotkey import POP_PATH, SUBMARINE_PATH, PTTState


class _PopenRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, *a, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(list(args))
        return object()


async def test_on_down_then_on_up_toggles_flag_and_plays_cues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _PopenRecorder()
    monkeypatch.setattr(hotkey.subprocess, "Popen", recorder)

    state = PTTState()
    assert not state.ptt_active.is_set()

    state.on_down()
    assert state.ptt_active.is_set()

    state.on_up()
    assert not state.ptt_active.is_set()

    assert len(recorder.calls) == 2
    assert recorder.calls[0][0] == "afplay"
    assert recorder.calls[0][1] == SUBMARINE_PATH
    assert recorder.calls[1][0] == "afplay"
    assert recorder.calls[1][1] == POP_PATH


async def test_popen_failure_does_not_block_flag_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*a, **kw):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("afplay missing")

    monkeypatch.setattr(hotkey.subprocess, "Popen", _raise)

    state = PTTState()
    state.on_down()
    assert state.ptt_active.is_set()
    state.on_up()
    assert not state.ptt_active.is_set()
