from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import websockets
from websockets.exceptions import InvalidStatus

from voicebridge import errors
from voicebridge.config import Config

WS_URL = "wss://api.openai.com/v1/realtime/translations"
MODEL = "gpt-realtime-translate"
SESSION_UPDATE_TIMEOUT_S = 5.0

_LOG_DIR = Path("./logs")

_INBOUND_QUEUE_MAX = 200
_OUTBOUND_QUEUE_MAX = 200


def _log_path() -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _LOG_DIR / f"{ts}-orchestrator.log"


def _open_log() -> Any:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _log_path().open("a", encoding="utf-8")


def _log_event(log_fp: Any, event_type: str) -> None:
    ts = datetime.now().isoformat(timespec="milliseconds")
    log_fp.write(f"{ts} realtime: recv {event_type}\n")
    log_fp.flush()


def _status_code_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    if response is not None:
        code = getattr(response, "status_code", None)
        if code is not None:
            return code
    return getattr(exc, "status_code", None)


def _extract_audio_b64(event: dict[str, Any]) -> str | None:
    for key in ("delta", "audio", "data"):
        val = event.get(key)
        if isinstance(val, str) and val:
            return val
    return None


class RealtimeSession:
    def __init__(self, ws: Any, log_fp: Any | None = None) -> None:
        self._ws = ws
        self._closed = False
        self._log_fp = log_fp

        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_INBOUND_QUEUE_MAX
        )
        self._outbound: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_OUTBOUND_QUEUE_MAX
        )

        self._inbound_drops = 0
        self._outbound_drops = 0

        self._reader_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None

        self._closing = False
        self._closed_exc: errors.RealtimeError | None = None

        # Timing-marker hooks (set externally).
        self.on_first_mic_frame_sent: Callable[[], None] | None = None
        self.on_first_model_audio_received: Callable[[], None] | None = None
        self._mic_armed = False
        self._model_armed = False

    @property
    def inbound_drops(self) -> int:
        return self._inbound_drops

    @property
    def outbound_drops(self) -> int:
        return self._outbound_drops

    @classmethod
    async def open(cls, config: Config) -> "RealtimeSession":
        url = f"{WS_URL}?model={MODEL}"
        headers = {"Authorization": f"Bearer {config.api_key}"}
        try:
            ws = await websockets.connect(url, additional_headers=headers)
        except InvalidStatus as exc:
            status = _status_code_of(exc)
            if status == 401:
                raise errors.ApiKeyRejected() from exc
            raise errors.RealtimeServerError(
                f"server rejected handshake: HTTP {status}"
            ) from exc
        except (OSError, socket.gaierror, asyncio.TimeoutError) as exc:
            raise errors.NetworkUnreachable() from exc

        log_fp = _open_log()
        keep_log_open = False
        try:
            # The realtime translations endpoint rejects top-level
            # `input_audio_format` / `output_audio_format` keys (they belong
            # to the chat realtime endpoint, not translations). PCM16 is the
            # default I/O format for this endpoint; only the target language
            # needs to be set explicitly.
            session_update = {
                "type": "session.update",
                "session": {
                    "audio": {
                        "output": {
                            "language": config.target_lang_iso,
                        },
                    },
                },
            }
            await ws.send(json.dumps(session_update))

            try:
                await asyncio.wait_for(
                    _await_session_updated(ws, log_fp),
                    timeout=SESSION_UPDATE_TIMEOUT_S,
                )
            except asyncio.TimeoutError as exc:
                await _safe_close(ws)
                raise errors.RealtimeServerError("session.update timed out") from exc
            except errors.RealtimeServerError:
                await _safe_close(ws)
                raise

            keep_log_open = True
        finally:
            if not keep_log_open:
                log_fp.close()

        instance = cls(ws, log_fp=log_fp)
        instance._reader_task = asyncio.create_task(instance._reader_loop())
        instance._writer_task = asyncio.create_task(instance._writer_loop())
        return instance

    async def send_frame(self, pcm: bytes) -> None:
        # Fire the first-mic-frame hook when armed (before enqueue).
        if self._mic_armed and self.on_first_mic_frame_sent is not None:
            self._mic_armed = False
            try:
                self.on_first_mic_frame_sent()
            except Exception as exc:
                print(
                    f"realtime: first-mic hook raised: {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )

        try:
            self._outbound.put_nowait(pcm)
        except asyncio.QueueFull:
            # Drop oldest, then enqueue the new frame.
            try:
                _ = self._outbound.get_nowait()
                self._outbound_drops += 1
                print(
                    "realtime: dropped 1 outbound frame, queue full",
                    file=sys.stderr,
                    flush=True,
                )
            except asyncio.QueueEmpty:
                pass
            try:
                self._outbound.put_nowait(pcm)
            except asyncio.QueueFull:
                # Pathological: still full after a drop. Drop the new frame.
                self._outbound_drops += 1

    async def audio_frames(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._inbound.get()
            if frame is None:
                return
            yield frame

    def mark_turn_start(self) -> None:
        self._mic_armed = True
        self._model_armed = True

    async def _reader_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type", "") or ""
                if self._log_fp is not None:
                    try:
                        _log_event(self._log_fp, event_type)
                    except Exception:
                        pass

                if event_type.endswith("error"):
                    message = "realtime server error"
                    err = event.get("error")
                    if isinstance(err, dict):
                        msg = err.get("message")
                        if isinstance(msg, str) and msg:
                            message = msg
                    raise errors.RealtimeServerError(message)

                if "audio.delta" in event_type:
                    b64 = _extract_audio_b64(event)
                    if not b64:
                        continue
                    try:
                        pcm = base64.b64decode(b64)
                    except (ValueError, TypeError):
                        continue

                    # Fire the first-model-audio hook (before enqueue).
                    if self._model_armed:
                        self._model_armed = False
                        if self.on_first_model_audio_received is not None:
                            try:
                                self.on_first_model_audio_received()
                            except Exception as exc:
                                print(
                                    f"realtime: first-model-audio hook raised: {exc!r}",
                                    file=sys.stderr,
                                    flush=True,
                                )

                    try:
                        self._inbound.put_nowait(pcm)
                    except asyncio.QueueFull:
                        try:
                            _ = self._inbound.get_nowait()
                            self._inbound_drops += 1
                            print(
                                "realtime: dropped 1 inbound frame, queue full",
                                file=sys.stderr,
                                flush=True,
                            )
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            self._inbound.put_nowait(pcm)
                        except asyncio.QueueFull:
                            self._inbound_drops += 1
        except asyncio.CancelledError:
            raise
        except errors.RealtimeServerError as exc:
            self._closed_exc = exc
        except websockets.exceptions.ConnectionClosed as exc:
            if not self._closing:
                reason = ""
                rcvd = getattr(exc, "rcvd", None)
                if rcvd is not None:
                    reason = getattr(rcvd, "reason", "") or ""
                if not reason:
                    reason = str(exc) or "connection closed"
                self._closed_exc = errors.ConnectionLost(reason)
        except Exception as exc:
            print(
                f"realtime: reader unexpected exception: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            if not self._closing:
                self._closed_exc = errors.RealtimeServerError(str(exc))
        finally:
            # Signal audio_frames() consumers to terminate.
            try:
                self._inbound.put_nowait(None)
            except asyncio.QueueFull:
                # Best effort: drop one and push sentinel.
                try:
                    _ = self._inbound.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    self._inbound.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _writer_loop(self) -> None:
        try:
            while True:
                frame = await self._outbound.get()
                if frame is None:
                    return
                b64 = base64.b64encode(frame).decode("ascii")
                # Translations endpoint namespaces client → server event
                # types under `session.` — only `session.update`,
                # `session.input_audio_buffer.append`, and `session.close`
                # are accepted (rejected with "Invalid value" otherwise).
                payload = json.dumps(
                    {"type": "session.input_audio_buffer.append", "audio": b64}
                )
                await self._ws.send(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Slice 5 hardens error handling.
            pass

    async def wait_closed(self) -> None:
        if self._reader_task is not None:
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except errors.RealtimeError:
                # Captured in self._closed_exc via the reader's except handler.
                pass
            except Exception:
                # Should not happen — reader normalises everything.
                pass
        if self._closed_exc is not None:
            raise self._closed_exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closing = True

        # Push sentinels to terminate the loops.
        try:
            self._inbound.put_nowait(None)
        except asyncio.QueueFull:
            try:
                _ = self._inbound.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._inbound.put_nowait(None)
            except asyncio.QueueFull:
                pass

        try:
            self._outbound.put_nowait(None)
        except asyncio.QueueFull:
            try:
                _ = self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._outbound.put_nowait(None)
            except asyncio.QueueFull:
                pass

        tasks = [t for t in (self._reader_task, self._writer_task) if t is not None]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        await _safe_close(self._ws)

        if self._log_fp is not None:
            try:
                self._log_fp.close()
            except Exception:
                pass
            self._log_fp = None


async def _await_session_updated(ws: Any, log_fp: Any) -> None:
    while True:
        raw = await ws.recv()
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        event_type = event.get("type", "")
        _log_event(log_fp, event_type)
        if event_type == "error":
            message = ""
            err = event.get("error")
            if isinstance(err, dict):
                message = err.get("message", "") or ""
            raise errors.RealtimeServerError(message or "realtime server error")
        if event_type == "session.updated":
            return


async def _safe_close(ws: Any) -> None:
    try:
        await ws.close()
    except Exception:
        pass
