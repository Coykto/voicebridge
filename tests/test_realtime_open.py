from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from voicebridge import realtime
from voicebridge.config import Config
from voicebridge.errors import (
    ApiKeyRejected,
    NetworkUnreachable,
    RealtimeServerError,
)


class FakeWS:
    def __init__(self, scripted: list[str | Exception]) -> None:
        self.sent: list[str] = []
        self._scripted = list(scripted)
        self._closed = False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> str:
        if not self._scripted:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self._closed = True


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeWS | None = None,
    connect_error: Exception | None = None,
) -> dict[str, Any]:
    calls: dict[str, Any] = {"count": 0, "kwargs": None, "url": None}

    async def fake_connect(url: str, **kwargs: Any) -> FakeWS:
        calls["count"] += 1
        calls["url"] = url
        calls["kwargs"] = kwargs
        if connect_error is not None:
            raise connect_error
        assert fake is not None
        return fake

    monkeypatch.setattr(realtime.websockets, "connect", fake_connect)
    return calls


def _english_config() -> Config:
    return Config(
        api_key="sk-test-1234",
        target_lang_name="English",
        target_lang_iso="en",
    )


def _spanish_config() -> Config:
    return Config(
        api_key="sk-test-1234",
        target_lang_name="Spanish",
        target_lang_iso="es",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_open_sends_session_update_english(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWS(scripted=[json.dumps({"type": "session.updated"})])
    _patch_connect(monkeypatch, fake=fake)

    async def inner() -> None:
        await realtime.RealtimeSession.open(_english_config())

    _run(inner())

    assert len(fake.sent) == 1
    payload = json.loads(fake.sent[0])
    assert payload["type"] == "session.update"
    assert payload["session"]["audio"]["output"]["language"] == "en"


def test_open_sends_session_update_spanish(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWS(scripted=[json.dumps({"type": "session.updated"})])
    _patch_connect(monkeypatch, fake=fake)

    async def inner() -> None:
        await realtime.RealtimeSession.open(_spanish_config())

    _run(inner())

    assert len(fake.sent) == 1
    payload = json.loads(fake.sent[0])
    assert payload["session"]["audio"]["output"]["language"] == "es"


def test_open_resolves_on_session_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeWS(scripted=[json.dumps({"type": "session.updated"})])
    _patch_connect(monkeypatch, fake=fake)

    async def inner() -> realtime.RealtimeSession:
        return await realtime.RealtimeSession.open(_english_config())

    session = _run(inner())
    assert isinstance(session, realtime.RealtimeSession)


def test_open_ignores_unrelated_events_then_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeWS(
        scripted=[
            json.dumps({"type": "session.created"}),
            json.dumps({"type": "session.updated"}),
        ]
    )
    _patch_connect(monkeypatch, fake=fake)

    async def inner() -> realtime.RealtimeSession:
        return await realtime.RealtimeSession.open(_english_config())

    session = _run(inner())
    assert isinstance(session, realtime.RealtimeSession)


def test_open_times_out_without_session_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeWS(scripted=[])
    _patch_connect(monkeypatch, fake=fake)
    monkeypatch.setattr(realtime, "SESSION_UPDATE_TIMEOUT_S", 0.05)

    async def inner() -> None:
        await realtime.RealtimeSession.open(_english_config())

    with pytest.raises(RealtimeServerError) as exc_info:
        _run(inner())
    assert "timed out" in str(exc_info.value)


def test_open_raises_realtime_server_error_on_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeWS(
        scripted=[json.dumps({"type": "error", "error": {"message": "bad payload"}})]
    )
    _patch_connect(monkeypatch, fake=fake)

    async def inner() -> None:
        await realtime.RealtimeSession.open(_english_config())

    with pytest.raises(RealtimeServerError) as exc_info:
        _run(inner())
    assert "bad payload" in str(exc_info.value)


def test_open_raises_api_key_rejected_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(status_code=401)
    err = realtime.InvalidStatus(response)
    _patch_connect(monkeypatch, connect_error=err)

    async def inner() -> None:
        await realtime.RealtimeSession.open(_english_config())

    with pytest.raises(ApiKeyRejected):
        _run(inner())


def test_open_raises_network_unreachable_on_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_connect(monkeypatch, connect_error=OSError("nodename nor servname"))

    async def inner() -> None:
        await realtime.RealtimeSession.open(_english_config())

    with pytest.raises(NetworkUnreachable):
        _run(inner())
