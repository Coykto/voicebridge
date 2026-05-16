from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
import websockets

from voicebridge import errors, realtime
from voicebridge.config import Config


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._inbox: asyncio.Queue[str | Exception] = asyncio.Queue()
        self._closed = False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> str:
        item = await self._inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self._closed = True

    def push(self, item: str | Exception) -> None:
        self._inbox.put_nowait(item)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, fake: FakeWS) -> None:
    async def fake_connect(url: str, **kwargs: Any) -> FakeWS:
        return fake

    monkeypatch.setattr(realtime.websockets, "connect", fake_connect)


def _config() -> Config:
    return Config(
        api_key="sk-test-1234",
        target_lang_name="English",
        target_lang_iso="en",
    )


async def _open_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[realtime.RealtimeSession, FakeWS]:
    fake = FakeWS()
    fake.push(json.dumps({"type": "session.updated"}))
    _patch_connect(monkeypatch, fake)
    session = await realtime.RealtimeSession.open(_config())
    return session, fake


async def test_send_frame_emits_input_audio_buffer_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = await _open_session(monkeypatch)
    try:
        # First sent message was session.update during open(). Track that
        # baseline so we can assert about the next send.
        baseline = len(fake.sent)
        pcm = b"\x00\x01\x02\x03"
        await session.send_frame(pcm)

        # Let the writer task pick the frame off the queue and send it.
        for _ in range(50):
            if len(fake.sent) > baseline:
                break
            await asyncio.sleep(0.01)

        assert len(fake.sent) == baseline + 1
        payload = json.loads(fake.sent[-1])
        assert payload["type"] == "input_audio_buffer.append"
        assert payload["audio"] == base64.b64encode(pcm).decode("ascii")
    finally:
        await session.close()


async def test_audio_frames_yields_decoded_pcm_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = await _open_session(monkeypatch)
    try:
        fake.push(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(b"\xaa\xbb").decode("ascii"),
                }
            )
        )
        fake.push(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(b"\xcc\xdd").decode("ascii"),
                }
            )
        )

        frames: list[bytes] = []

        async def collect() -> None:
            async for f in session.audio_frames():
                frames.append(f)
                if len(frames) == 2:
                    break

        await asyncio.wait_for(collect(), timeout=1.0)
        assert frames == [b"\xaa\xbb", b"\xcc\xdd"]
    finally:
        await session.close()


async def test_inbound_queue_overflow_drops_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = await _open_session(monkeypatch)
    try:
        # Push 205 audio deltas; do not consume audio_frames().
        for i in range(205):
            b = bytes([i % 256])
            fake.push(
                json.dumps(
                    {
                        "type": "response.audio.delta",
                        "delta": base64.b64encode(b).decode("ascii"),
                    }
                )
            )

        # Let the reader process all events.
        for _ in range(100):
            if session._inbound.qsize() >= 200 and session.inbound_drops >= 5:
                break
            await asyncio.sleep(0.01)

        assert session.inbound_drops == 5
        assert session._inbound.qsize() == 200
    finally:
        await session.close()


async def test_first_mic_hook_fires_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _fake = await _open_session(monkeypatch)
    try:
        calls: list[str] = []
        session.on_first_mic_frame_sent = lambda: calls.append("mic")

        session.mark_turn_start()
        await session.send_frame(b"\x00\x00")
        await session.send_frame(b"\x00\x00")
        assert calls == ["mic"]

        session.mark_turn_start()
        await session.send_frame(b"\x00\x00")
        assert calls == ["mic", "mic"]
    finally:
        await session.close()


async def test_first_model_audio_hook_fires_once_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = await _open_session(monkeypatch)
    try:
        calls: list[str] = []
        session.on_first_model_audio_received = lambda: calls.append("model")

        session.mark_turn_start()
        fake.push(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(b"\x01").decode("ascii"),
                }
            )
        )
        fake.push(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(b"\x02").decode("ascii"),
                }
            )
        )

        for _ in range(100):
            if len(calls) >= 1 and session._inbound.qsize() >= 2:
                break
            await asyncio.sleep(0.01)
        assert calls == ["model"]

        session.mark_turn_start()
        fake.push(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "delta": base64.b64encode(b"\x03").decode("ascii"),
                }
            )
        )
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        assert calls == ["model", "model"]
    finally:
        await session.close()


async def test_close_terminates_audio_frames_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _fake = await _open_session(monkeypatch)

    async def consumer() -> None:
        async for _ in session.audio_frames():
            pass

    task = asyncio.create_task(consumer())
    # Give it a tick to enter the loop.
    await asyncio.sleep(0.01)

    await session.close()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    assert task.exception() is None


async def test_send_frame_overflow_drops_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeWS()
    fake.push(json.dumps({"type": "session.updated"}))
    _patch_connect(monkeypatch, fake)

    # Block the writer by making FakeWS.send() wait on an event,
    # but the session.update send during open() must still succeed.
    block = asyncio.Event()
    open_done = False
    original_send = fake.send

    async def blocking_send(msg: str) -> None:
        await original_send(msg)
        if open_done:
            await block.wait()

    fake.send = blocking_send  # type: ignore[method-assign]

    session = await realtime.RealtimeSession.open(_config())
    open_done = True

    try:
        for _ in range(205):
            await session.send_frame(b"\x00\x00")

        # Let the writer task settle (it's blocked after picking 1 frame
        # off the queue, which leaves the queue at capacity 200).
        await asyncio.sleep(0.05)

        assert session.outbound_drops == 5
    finally:
        block.set()
        await session.close()


async def test_wait_closed_raises_realtime_server_error_on_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = await _open_session(monkeypatch)
    try:
        fake.push(
            json.dumps({"type": "error", "error": {"message": "rate limit"}})
        )
        with pytest.raises(errors.RealtimeServerError) as exc_info:
            await asyncio.wait_for(session.wait_closed(), timeout=1.0)
        assert "rate limit" in str(exc_info.value)
    finally:
        await session.close()


async def test_wait_closed_raises_connection_lost_on_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = await _open_session(monkeypatch)
    try:
        fake.push(
            websockets.exceptions.ConnectionClosedError(rcvd=None, sent=None)
        )
        with pytest.raises(errors.ConnectionLost):
            await asyncio.wait_for(session.wait_closed(), timeout=1.0)
    finally:
        await session.close()


async def test_wait_closed_returns_none_after_graceful_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _fake = await _open_session(monkeypatch)
    await session.close()
    result = await asyncio.wait_for(session.wait_closed(), timeout=1.0)
    assert result is None
