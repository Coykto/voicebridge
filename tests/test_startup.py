"""End-to-end startup-error tests for ``voicebridge.__main__.run``.

We stub ``voicebridge.__main__.spawn`` to return a fake process plus a scripted
async event stream, so no real subprocess (and no real Swift binary) is needed.
Tests assert the exit code returned by ``run()`` and the stderr message printed
on the way out.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator

import pytest

from voicebridge import __main__ as vb_main
from voicebridge.ipc import Error, Event, HotkeyRegistered, Ready


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process``.

    Only the attributes/methods touched by ``_terminate`` are implemented.
    ``_hard_kill_sync`` is bypassed because tests call ``run()`` directly.
    """

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 0
        self.terminated = False
        self.killed = False

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
    # After scripted events, stall forever — exercises the timeout path when
    # no terminal HotkeyRegistered/Error is scripted.
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
    return argparse.Namespace(source="ru", target="en")


async def test_accessibility_denied_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_spawn(monkeypatch, [Ready(), Error(code="accessibility_denied")])

    exit_code = await vb_main.run(_args())

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "Accessibility" in err
    assert "System Settings" in err


async def test_conflict_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_spawn(monkeypatch, [Ready(), Error(code="conflict")])

    exit_code = await vb_main.run(_args())

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "⌥⌘T" in err


async def test_param_err_exits_1_with_generic_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_spawn(monkeypatch, [Ready(), Error(code="param_err")])

    exit_code = await vb_main.run(_args())

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "hotkey could not be registered" in err
    assert "param_err" in err


async def test_timeout_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fake_spawn(monkeypatch, [Ready()])
    # Keep the test fast — the production default is 5s.
    monkeypatch.setattr(vb_main, "STARTUP_TIMEOUT_SECONDS", 0.1)

    exit_code = await vb_main.run(_args())

    assert exit_code == 3
    err = capsys.readouterr().err
    assert "Swift capture did not start in time" in err


async def test_successful_startup_does_not_print_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Sanity check: a healthy startup followed by a stall does not surface any
    # of the error messages. We trigger shutdown via a tiny sleep + cancel.
    _install_fake_spawn(
        monkeypatch,
        [Ready(), HotkeyRegistered(chord="option+command+t")],
    )

    task = asyncio.create_task(vb_main.run(_args()))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    err = capsys.readouterr().err
    assert "Accessibility" not in err
    assert "hotkey could not be registered" not in err
    assert "did not start in time" not in err
