from __future__ import annotations

import queue
import sys
import threading
import time
import traceback
from typing import Any, Callable

import sounddevice


_BYTES_PER_INT16 = 2
_QUEUE_CAP_SECONDS = 5
_CLOSE_DRAIN_TIMEOUT_S = 0.2


def _upsample_int_factor_linear(pcm16le: bytes, factor: int) -> bytes:
    # Integer-factor linear-interpolation upsampler for int16 mono PCM.
    # Used when the output device's native rate is N× our input rate
    # (e.g. BlackHole 2ch runs at 48 kHz, voicebridge emits 24 kHz → 2×).
    # Cheap, no SciPy dependency; minor discontinuity at chunk boundaries
    # is inaudible at PoC quality.
    if factor <= 1:
        return pcm16le
    import array

    src = array.array("h")
    src.frombytes(pcm16le)
    n = len(src)
    if n == 0:
        return b""
    dst = array.array("h", [0] * (n * factor))
    for i in range(n - 1):
        s0 = src[i]
        s1 = src[i + 1]
        base = i * factor
        for k in range(factor):
            dst[base + k] = s0 + (s1 - s0) * k // factor
    last = src[n - 1]
    base = (n - 1) * factor
    for k in range(factor):
        dst[base + k] = last
    return dst.tobytes()


def _splat_mono_to_interleaved(
    outdata: memoryview, mono: bytes, frames: int, channels: int
) -> None:
    # The stereo path uses CPython slice assignment (C-speed) — a per-sample
    # Python loop here runs inside the PortAudio audio callback and was slow
    # enough at BlackHole's ~1ms callback cadence to cause underrun clicks.
    if channels == 2:
        stereo = bytearray(frames * 4)
        stereo[0::4] = mono[0::2]
        stereo[1::4] = mono[1::2]
        stereo[2::4] = mono[0::2]
        stereo[3::4] = mono[1::2]
        outdata[: frames * 4] = bytes(stereo)
        return
    for i in range(frames):
        b0 = mono[i * 2]
        b1 = mono[i * 2 + 1]
        base = i * channels * 2
        for c in range(channels):
            outdata[base + c * 2] = b0
            outdata[base + c * 2 + 1] = b1


class PlaybackOpenError(RuntimeError):
    def __init__(
        self,
        sample_rate: int,
        channels: int,
        dtype: str,
        cause: BaseException,
        device: str | int | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.dtype = dtype
        self.cause = cause
        self.device = device
        device_part = f", device={device!r}" if device is not None else ""
        super().__init__(
            f"failed to open playback stream "
            f"(sample_rate={sample_rate}, channels={channels}, dtype={dtype!r}{device_part}): {cause}"
        )


class Playback:
    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[bytes] = queue.SimpleQueue()
        self._carry: bytearray = bytearray()
        self._stream: sounddevice.RawOutputStream | None = None
        self._sample_rate: int | None = None
        self._channels: int = 1
        self._upsample_factor: int = 1
        self._underrun_count: int = 0
        self._overflow_count: int = 0
        self._queued_bytes: int = 0
        self._lock: threading.Lock = threading.Lock()
        self.on_first_write_after_idle: Callable[[], None] | None = None
        self._hook_armed: bool = False

    def open(self, sample_rate: int, device: str | int | None = None) -> None:
        if self._stream is not None:
            return
        # Pick channel count from the device — virtual devices like BlackHole
        # are strictly 2-channel and refuse to output anything useful when
        # opened as mono. Mono speakers / AirPods etc. report 2+ channels too,
        # so capping at 2 is safe.
        channels = 1
        device_rate = sample_rate
        try:
            info = sounddevice.query_devices(device, kind="output")
            max_out = int(info.get("max_output_channels", 1) or 1)
            channels = max(1, min(2, max_out))
            device_rate = int(info.get("default_samplerate", sample_rate) or sample_rate)
        except Exception:
            channels = 1
            device_rate = sample_rate

        # If the device's native rate is an integer multiple of our input
        # rate (e.g. BlackHole 2ch at 48 kHz vs our 24 kHz input → 2×), open
        # the stream at the device rate and upsample on write. BlackHole
        # refuses non-supported rates (it only offers 8/16/44.1/48/88.2/96
        # kHz etc., never 24 kHz), so passing our raw 24 kHz fails silently —
        # Meet receives a buffer it can't parse and drops the audio.
        upsample_factor = 1
        if device_rate >= sample_rate and device_rate % sample_rate == 0:
            upsample_factor = device_rate // sample_rate
            stream_rate = device_rate
        else:
            stream_rate = sample_rate

        try:
            # 50 ms output latency: a queue-fed audio stream needs more
            # headroom than the device's default-low value, especially on
            # virtual devices like BlackHole 2ch where the default-low
            # latency is ~1.3 ms and the callback fires hundreds of times
            # per second.
            stream = sounddevice.RawOutputStream(
                samplerate=stream_rate,
                channels=channels,
                dtype="int16",
                device=device,
                latency=0.05,
                blocksize=0,
                callback=self._callback,
            )
        except sounddevice.PortAudioError as e:
            raise PlaybackOpenError(
                stream_rate, channels=channels, dtype="int16", cause=e, device=device
            ) from e
        except Exception as e:
            raise PlaybackOpenError(
                stream_rate, channels=channels, dtype="int16", cause=e, device=device
            ) from e
        self._sample_rate = stream_rate
        self._channels = channels
        self._upsample_factor = upsample_factor
        self._stream = stream
        self._stream.start()
        self._hook_armed = True
        print(
            f"playback: opened device={device!r} samplerate={stream_rate} "
            f"channels={channels} upsample={upsample_factor}x",
            file=sys.stderr,
            flush=True,
        )

    def mark_idle(self) -> None:
        self._hook_armed = True

    def write(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        if self._upsample_factor > 1:
            pcm_bytes = _upsample_int_factor_linear(pcm_bytes, self._upsample_factor)
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
                # Queue holds mono PCM bytes; cap on the mono budget.
                cap = _QUEUE_CAP_SECONDS * self._sample_rate * _BYTES_PER_INT16
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
        # The queue carries mono int16 PCM; if the device is multi-channel
        # we duplicate each mono sample across channels at copy time.
        mono_needed = frames * _BYTES_PER_INT16
        buf = self._carry
        while len(buf) < mono_needed:
            try:
                chunk = self._queue.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._queued_bytes -= len(chunk)
                if self._queued_bytes < 0:
                    self._queued_bytes = 0
            buf.extend(chunk)

        channels = self._channels
        if len(buf) >= mono_needed:
            mono = bytes(buf[:mono_needed])
            self._carry = bytearray(buf[mono_needed:])
            if channels == 1:
                outdata[: frames * _BYTES_PER_INT16] = mono
            else:
                _splat_mono_to_interleaved(outdata, mono, frames, channels)
        else:
            have = len(buf)
            if have:
                mono = bytes(buf)
                if channels == 1:
                    outdata[:have] = mono
                else:
                    have_frames = have // _BYTES_PER_INT16
                    _splat_mono_to_interleaved(
                        outdata, mono[: have_frames * _BYTES_PER_INT16], have_frames, channels
                    )
            zero_start = (have // _BYTES_PER_INT16) * _BYTES_PER_INT16 * channels
            zero_end = frames * _BYTES_PER_INT16 * channels
            for i in range(zero_start, zero_end):
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
