from __future__ import annotations

from typing import Any

import pytest

import voicebridge.playback as playback_module
from voicebridge.playback import Playback


class _FakeStream:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(playback_module.sounddevice, "RawOutputStream", _FakeStream)


def test_hook_fires_once_on_first_write_then_not_again(fake_stream: None) -> None:
    pb = Playback()
    calls: list[int] = []
    pb.on_first_write_after_idle = lambda: calls.append(1)
    pb.open(sample_rate=24000)

    pb.write(b"\x00\x00" * 10)
    assert calls == [1]

    pb.write(b"\x00\x00" * 10)
    assert calls == [1]


def test_mark_idle_rearms_hook(fake_stream: None) -> None:
    pb = Playback()
    calls: list[int] = []
    pb.on_first_write_after_idle = lambda: calls.append(1)
    pb.open(sample_rate=24000)

    pb.write(b"\x00\x00" * 10)
    assert calls == [1]

    pb.mark_idle()
    pb.write(b"\x00\x00" * 10)
    assert calls == [1, 1]

    pb.write(b"\x00\x00" * 10)
    assert calls == [1, 1]


def test_hook_exception_caught_and_arm_consumed(
    fake_stream: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pb = Playback()
    call_count = [0]

    def boom() -> None:
        call_count[0] += 1
        raise RuntimeError("boom")

    pb.on_first_write_after_idle = boom
    pb.open(sample_rate=24000)

    # Must not raise.
    pb.write(b"\x00\x00" * 10)
    assert call_count[0] == 1

    # Arm consumed even though hook raised.
    pb.write(b"\x00\x00" * 10)
    assert call_count[0] == 1

    captured = capsys.readouterr()
    assert "on_first_write_after_idle" in captured.err or "boom" in captured.err


def test_empty_write_does_not_fire_hook(fake_stream: None) -> None:
    pb = Playback()
    calls: list[int] = []
    pb.on_first_write_after_idle = lambda: calls.append(1)
    pb.open(sample_rate=24000)

    pb.write(b"")
    assert calls == []

    pb.write(b"\x00\x00" * 10)
    assert calls == [1]
