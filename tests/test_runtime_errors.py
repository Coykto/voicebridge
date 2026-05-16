"""Mid-session error handling for ``voicebridge.__main__.run``.

Startup-time errors are covered by ``test_startup.py``; this file exercises
errors that arrive AFTER ``ready`` + ``hotkey_registered`` — currently just
``mic_lost`` (a watchdog or route-loss declaration from the Swift side).
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator

import pytest

from voicebridge import __main__ as vb_main
from voicebridge.config import Config
from voicebridge.ipc import Error, Event, HotkeyRegistered, Ready


class _FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 0
        self.terminated = False
        self.killed = False
        # Empty StreamReader with no EOF — the pcm pump stays suspended for
        # the duration of the test, so the runtime-error path is the only
        # thing that can complete the orchestrator.
        self.stdout: asyncio.StreamReader | None = asyncio.StreamReader()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = 0

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


async def _stream(events: list[Event]) -> AsyncIterator[Event]:
    for event in events:
        yield event
    await asyncio.Event().wait()


def _install_fake_spawn(
    monkeypatch: pytest.MonkeyPatch, events: list[Event]
) -> _FakeProc:
    proc = _FakeProc()

    async def fake_spawn() -> tuple[_FakeProc, AsyncIterator[Event]]:
        return proc, _stream(events)

    monkeypatch.setattr(vb_main, "spawn", fake_spawn)
    return proc


def _args() -> argparse.Namespace:
    return argparse.Namespace()


def _config() -> Config:
    return Config(
        api_key="sk-test-1234",
        target_lang_name="English",
        target_lang_iso="en",
    )


class _StubSession:
    @classmethod
    async def open(cls, config: Config) -> "_StubSession":
        return cls()

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _stub_realtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vb_main, "RealtimeSession", _StubSession)


async def test_mic_lost_mid_session_exits_5_with_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_spawn(
        monkeypatch,
        [
            Ready(),
            HotkeyRegistered(chord="option+command+t"),
            Error(code="mic_lost", message="default input removed"),
        ],
    )

    exit_code = await vb_main.run(_args(), _config())

    assert exit_code == 5
    err = capsys.readouterr().err
    assert "Microphone disconnected" in err
    assert "default input removed" in err
    assert "Restart the tool" in err


async def test_mic_lost_without_message_still_exits_5(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Defensive: an older Swift binary or a corrupted event might omit the
    # message field. We still exit cleanly with code 5 and a sensible default
    # rather than blowing up.
    _install_fake_spawn(
        monkeypatch,
        [
            Ready(),
            HotkeyRegistered(chord="option+command+t"),
            Error(code="mic_lost"),
        ],
    )

    exit_code = await vb_main.run(_args(), _config())

    assert exit_code == 5
    err = capsys.readouterr().err
    assert "Microphone disconnected" in err
