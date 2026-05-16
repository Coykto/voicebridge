from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable

import sounddevice


_BYTES_PER_SAMPLE = 2  # int16, mono
_QUEUE_CAP_SECONDS = 5
_CLOSE_DRAIN_TIMEOUT_S = 0.2


class PlaybackOpenError(RuntimeError):
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        dtype: str,
        cause: BaseException,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.cause = cause
        super().__init__(
            f"failed to open playback stream "
            f"(sample_rate={sample_rate}, channels={channels}, dtype={dtype!r}): {cause}"
        )


class Playback:
    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._carry: bytearray = bytearray()
        self._stream: sounddevice.RawOutputStream | None = None
        self._sample_rate: int | None = None
        self._underrun_count: int = 0
        self._overflow_count: int = 0
        self._queued_bytes: int = 0
        self._lock: threading.Lock = threading.Lock()
        self.on_first_write_after_idle: Callable[[], None] | None = None
        self._hook_armed: bool = False

    def open(self, sample_rate: int) -> None:
        if self._stream is not None:
            return
        try:
            stream = sounddevice.RawOutputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                device=None,
                latency="low",
                blocksize=0,
                callback=self._callback,
            )
        except sounddevice.PortAudioError as e:
            raise PlaybackOpenError(sample_rate, channels=1, dtype="int16", cause=e) from e
        except Exception as e:
            raise PlaybackOpenError(sample_rate, channels=1, dtype="int16", cause=e) from e
        self._sample_rate = sample_rate
        self._stream = stream
        self._stream.start()
        self._hook_armed = True

    def mark_idle(self) -> None:
        self._hook_armed = True

    def write(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        if self._hook_armed and self.on_first_write_after_idle is not None:
            # Disarm before invoking so a raising hook doesn't re-fire on every write.
            self._hook_armed = False
            hook = self.on_first_write_after_idle
            try:
                hook()
            except Exception:
                print("playback: on_first_write_after_idle hook error", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
        with self._lock:
            # Cap enforcement is meaningless until open() has set the sample rate.
            # In production, write() is only called after open(); tests that drive
            # _fill directly may skip open() entirely.
            if self._sample_rate is not None:
                cap = _QUEUE_CAP_SECONDS * self._sample_rate * _BYTES_PER_SAMPLE
                # Drop oldest chunks until the new chunk fits under the cap.
                while self._queued_bytes + len(pcm_bytes) > cap:
                    try:
                        dropped = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._queued_bytes -= len(dropped)
                    if self._queued_bytes < 0:
                        self._queued_bytes = 0
                    self._overflow_count += 1
            self._queue.put(pcm_bytes)
            self._queued_bytes += len(pcm_bytes)

    def close(self) -> None:
        if self._stream is None:
            return
        try:
            # Give the audio callback a brief window to flush in-flight audio.
            deadline = time.monotonic() + _CLOSE_DRAIN_TIMEOUT_S
            while time.monotonic() < deadline:
                with self._lock:
                    remaining = self._queued_bytes
                if remaining <= 0:
                    break
                time.sleep(0.01)
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None
            print(
                f"playback: {self._underrun_count} underruns, "
                f"{self._overflow_count} overflows in session",
                file=sys.stderr,
            )

    @property
    def underrun_count(self) -> int:
        return self._underrun_count

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    def _fill(self, outdata: memoryview, frames: int) -> None:
        needed = frames * _BYTES_PER_SAMPLE
        buf = self._carry
        while len(buf) < needed:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._queued_bytes -= len(chunk)
                if self._queued_bytes < 0:
                    self._queued_bytes = 0
            buf.extend(chunk)

        if len(buf) >= needed:
            outdata[:needed] = bytes(buf[:needed])
            self._carry = bytearray(buf[needed:])
        else:
            have = len(buf)
            if have:
                outdata[:have] = bytes(buf)
            # Zero-pad the rest and count this callback as one underrun.
            for i in range(have, needed):
                outdata[i] = 0
            self._carry = bytearray()
            self._underrun_count += 1

    def _callback(
        self,
        outdata: Any,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        # PortAudio kills the audio thread on uncaught exceptions; swallow + zero-fill.
        try:
            self._fill(memoryview(outdata).cast("B"), frames)
        except Exception:
            print("playback: callback error", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            try:
                mv = memoryview(outdata).cast("B")
                for i in range(len(mv)):
                    mv[i] = 0
            except Exception:
                pass
