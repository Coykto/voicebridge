import XCTest
import AVFoundation
@testable import VoiceBridgeCapture

/// Feed-and-drain tests for `AudioCapture`'s 320-sample chunk-flush logic.
///
/// These tests bypass `start()` (which would bring up a real `AVAudioEngine`
/// against the host machine's mic — not suitable for unit tests) and instead
/// drive `enqueue(_:)` / `flushChunks()` directly. The ring buffer logic is
/// independent of the engine: it accepts any `AVAudioPCMBuffer` shaped like
/// the processing format (Int16 / mono / 16 kHz / interleaved).
///
/// // MARK: - Skipped: listener-lifecycle test
/// CoreAudio HAL property listeners can't be cleanly exercised from XCTest
/// without a real audio device — record skips equivalent tests in CI. The
/// manual AirPods + USB-unplug verifies in `tasks.md` Slice 5 cover the real
/// behavior.
final class AudioCaptureRingBufferTests: XCTestCase {

    /// Int16 / mono / 16 kHz interleaved — matches `AudioCapture.processingFormat`.
    private func makeProcessingFormat() -> AVAudioFormat {
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 16000,
            channels: 1,
            interleaved: true
        ) else {
            fatalError("test setup: AVAudioFormat construction failed")
        }
        return format
    }

    /// Build an Int16 mono PCM buffer of `count` samples (all zero — content
    /// doesn't matter for the ring-buffer test, only the framing does).
    private func makeBuffer(samples count: Int) -> AVAudioPCMBuffer {
        let format = makeProcessingFormat()
        guard let buf = AVAudioPCMBuffer(
            pcmFormat: format,
            frameCapacity: AVAudioFrameCount(max(count, 1))
        ) else {
            fatalError("test setup: AVAudioPCMBuffer allocation failed")
        }
        buf.frameLength = AVAudioFrameCount(count)
        if count > 0, let dst = buf.int16ChannelData?[0] {
            for i in 0..<count {
                dst[i] = 0
            }
        }
        return buf
    }

    /// Helper: build an AudioCapture with a frame-collecting sink.
    private func makeCapture() throws -> (AudioCapture, () -> [Data]) {
        let lock = NSLock()
        var captured: [Data] = []
        let sink: (Data) -> Void = { data in
            lock.lock()
            captured.append(data)
            lock.unlock()
        }
        let capture = try AudioCapture(
            emit: { _ in },
            frameSink: sink,
            onTerminalLoss: {}
        )
        let snapshot: () -> [Data] = {
            lock.lock()
            defer { lock.unlock() }
            return captured
        }
        return (capture, snapshot)
    }

    /// Exactly 320 samples → exactly one 640-byte chunk, queue empty afterward.
    func testFeed320SamplesEmitsOneChunk() throws {
        let (capture, snapshot) = try makeCapture()
        capture.enqueue(makeBuffer(samples: 320))
        capture.flushChunks()

        let chunks = snapshot()
        XCTAssertEqual(chunks.count, 1, "exactly one 640-byte chunk")
        XCTAssertEqual(chunks.first?.count, 640, "chunk size is 640 bytes")
        XCTAssertEqual(capture.queue.count, 0, "queue empty after flush")
    }

    /// 500 samples → one chunk emitted, 180 samples retained for the next call.
    func testFeed500SamplesRetains180() throws {
        let (capture, snapshot) = try makeCapture()
        capture.enqueue(makeBuffer(samples: 500))
        capture.flushChunks()

        let chunks = snapshot()
        XCTAssertEqual(chunks.count, 1, "one chunk for the first 320 samples")
        XCTAssertEqual(chunks.first?.count, 640)
        XCTAssertEqual(capture.queue.count, 180, "180 samples carry over")
    }

    /// Two 200-sample feeds → one chunk emitted, 80 samples retained.
    /// Exercises the cross-buffer carry-over: neither buffer alone reaches
    /// 320 samples, but the second one combined with the residual does.
    func testTwoFeedsOf200SamplesEmitsOneChunk() throws {
        let (capture, snapshot) = try makeCapture()
        capture.enqueue(makeBuffer(samples: 200))
        capture.flushChunks()
        XCTAssertEqual(snapshot().count, 0, "no chunk yet — below threshold")
        XCTAssertEqual(capture.queue.count, 200)

        capture.enqueue(makeBuffer(samples: 200))
        capture.flushChunks()

        let chunks = snapshot()
        XCTAssertEqual(chunks.count, 1, "one chunk after combined samples cross 320")
        XCTAssertEqual(chunks.first?.count, 640)
        XCTAssertEqual(capture.queue.count, 80, "80 samples carry over (400 - 320)")
    }

    /// Zero-length buffer is a no-op — no chunk, no crash, queue untouched.
    func testFeedZeroSamplesIsNoOp() throws {
        let (capture, snapshot) = try makeCapture()
        capture.enqueue(makeBuffer(samples: 0))
        capture.flushChunks()

        XCTAssertEqual(snapshot().count, 0)
        XCTAssertEqual(capture.queue.count, 0)
    }
}
