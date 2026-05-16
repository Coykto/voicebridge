from __future__ import annotations

import pytest
import sounddevice

from voicebridge.playback import Playback, PlaybackOpenError


def test_open_raises_playback_open_error_on_portaudio_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_stream(*args: object, **kwargs: object) -> object:
        raise sounddevice.PortAudioError("bad rate")

    monkeypatch.setattr(sounddevice, "RawOutputStream", raising_stream)

    playback = Playback()
    with pytest.raises(PlaybackOpenError) as exc_info:
        playback.open(99999)

    message = str(exc_info.value)
    assert "99999" in message
    assert "int16" in message
    assert "bad rate" in message
