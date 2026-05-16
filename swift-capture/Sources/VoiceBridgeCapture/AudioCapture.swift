import Foundation
import AVFoundation

/// Errors thrown by `AudioCapture` setup.
enum AudioCaptureError: Error, CustomStringConvertible {
    case converterCreationFailed(from: AVAudioFormat, to: AVAudioFormat)
    case bufferAllocationFailed
    case engineStartFailed(underlying: Error)

    var description: String {
        switch self {
        case .converterCreationFailed(let from, let to):
            return "failed to construct AVAudioConverter from \(from) to \(to)"
        case .bufferAllocationFailed:
            return "AVAudioPCMBuffer allocation failed"
        case .engineStartFailed(let underlying):
            return "AVAudioEngine.start() failed: \(underlying.localizedDescription)"
        }
    }
}

/// A tiny thread-safe FIFO of `Int16` mono samples.
///
/// The `AVAudioEngine` tap closure runs off the audio render thread per
/// Apple's contract for `installTap`. The tap appends converted samples here;
/// the same callback synchronously drains them in 320-sample units and hands
/// each 640-byte chunk to `frameSink`. `NSLock` is fine because neither
/// producer nor consumer runs on the real-time render thread (the tap callback
/// runs on a regular audio queue, not the render thread itself).
private final class LockedQueue {
    private var samples: [Int16] = []
    private let lock = NSLock()

    /// Append a chunk of samples to the tail.
    func append(_ chunk: UnsafeBufferPointer<Int16>) {
        lock.lock()
        samples.append(contentsOf: chunk)
        lock.unlock()
    }

    /// Drain up to `count` samples from the head. If the queue has fewer,
    /// returns all of them. Returns an empty array when nothing is buffered.
    func drain(upTo count: Int) -> [Int16] {
        lock.lock()
        defer { lock.unlock() }
        if samples.isEmpty || count <= 0 {
            return []
        }
        let n = min(count, samples.count)
        let head = Array(samples.prefix(n))
        samples.removeFirst(n)
        return head
    }

    /// Number of samples currently buffered.
    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return samples.count
    }
}

/// Owns the AVAudioEngine input tap, the per-input AVAudioConverter, and the
/// ring-buffer that re-chunks converted samples into fixed 640-byte (320
/// sample / 20 ms) PCM16 frames written to `frameSink`.
///
/// ## Slice 1 scope
///
/// This is the *basic* version per tasks.md Slice 1: the AVAudioEngine input
/// tap + AVAudioConverter + ring-buffer flush only. The full surface (CoreAudio
/// HAL property listeners, the mic-flow watchdog, the `mic_lost` terminal
/// path, the `AVAudioEngineConfigurationChange` recovery) arrives in Slice 5.
///
/// `start()` returns only after the first 640-byte chunk has been flushed to
/// `frameSink`, so the caller can sequence `ready` after both mic flow and
/// hotkey registration are live (tech spec §2.3).
final class AudioCapture {

    private let emit: (Event) -> Void
    private let frameSink: (Data) -> Void

    /// Int16 / mono / 16 kHz interleaved processing format. The converter
    /// targets it; the ring buffer is sized in samples of this format.
    private let processingFormat: AVAudioFormat

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?
    private let queue = LockedQueue()
    private var tapInstalled = false

    /// Samples-per-chunk = 320 (20 ms at 16 kHz mono). Each chunk is emitted
    /// to `frameSink` as 640 bytes of little-endian Int16.
    private static let samplesPerChunk = 320

    /// Continuation used by `start()` to await the first 640-byte chunk. Set
    /// in `start()`, resumed (exactly once) the first time the tap callback
    /// flushes a chunk to `frameSink`. Guarded by `firstChunkLock`.
    private let firstChunkLock = NSLock()
    private var firstChunkContinuation: CheckedContinuation<Void, Never>?
    private var firstChunkSignalled = false

    init(emit: @escaping (Event) -> Void, frameSink: @escaping (Data) -> Void) throws {
        self.emit = emit
        self.frameSink = frameSink
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 16000,
            channels: 1,
            interleaved: true
        ) else {
            throw AudioCaptureError.bufferAllocationFailed
        }
        self.processingFormat = format
    }

    /// Bring up the AVAudioEngine input tap and the converter, start the
    /// engine, then suspend until the first 640-byte chunk reaches
    /// `frameSink`. Throws if the engine cannot start.
    func start() async throws {
        // On macOS the engine's `inputNode` automatically follows the system
        // default input device — no per-device selection is needed.
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)

        // Passing `nil` as the tap format captures the bus's natural format
        // (typically float32 / native sample rate / native channel count),
        // which is the safest choice across input devices.
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            self?.handleMicBuffer(buffer)
        }
        tapInstalled = true

        // Pre-build the converter so the first tap callback doesn't have to
        // allocate one under the audio thread's timing pressure. If the
        // initial inputFormat is degenerate (some Bluetooth devices report a
        // 0-channel bus until the route settles), `convert(...)` will rebuild
        // on the first real buffer.
        if let converter = AVAudioConverter(from: inputFormat, to: processingFormat) {
            self.converter = converter
            self.converterInputFormat = inputFormat
        }

        engine.prepare()
        do {
            try engine.start()
        } catch {
            // Tear the tap back down so we don't leave a dangling input bus.
            if tapInstalled {
                engine.inputNode.removeTap(onBus: 0)
                tapInstalled = false
            }
            throw AudioCaptureError.engineStartFailed(underlying: error)
        }

        // Suspend until the tap callback flushes the first 640-byte chunk.
        // Stored on `self` so the callback can resume it exactly once; the
        // `firstChunkSignalled` flag prevents a double-resume if any future
        // path triggers the first-chunk hook more than once.
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            firstChunkLock.lock()
            if firstChunkSignalled {
                firstChunkLock.unlock()
                continuation.resume()
            } else {
                firstChunkContinuation = continuation
                firstChunkLock.unlock()
            }
        }
    }

    /// Stop the engine and remove the tap. Idempotent.
    func stop() {
        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }
        converter = nil
        converterInputFormat = nil
    }

    // MARK: - Mic tap handler

    private func handleMicBuffer(_ inputBuffer: AVAudioPCMBuffer) {
        guard let outputBuffer = convert(inputBuffer) else { return }
        enqueue(outputBuffer)
        flushChunks()
    }

    /// Convert one `AVAudioPCMBuffer` (whatever native format) into the
    /// processing format (int16 / mono / 16 kHz / interleaved). Lazily
    /// rebuilds the converter when the input format changes (e.g. across a
    /// route change in a future slice).
    private func convert(_ inputBuffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let outputFormat = processingFormat

        if converter == nil || converterInputFormat != inputBuffer.format {
            guard let built = AVAudioConverter(from: inputBuffer.format, to: outputFormat) else {
                emit(.error(code: AudioCaptureError.converterCreationFailed(
                    from: inputBuffer.format,
                    to: outputFormat
                ).description))
                return nil
            }
            converter = built
            converterInputFormat = inputBuffer.format
        }

        guard let converter = converter else { return nil }

        let ratio = outputFormat.sampleRate / inputBuffer.format.sampleRate
        let outputCapacity = AVAudioFrameCount(Double(inputBuffer.frameLength) * ratio) + 32
        guard let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: outputFormat,
            frameCapacity: outputCapacity
        ) else {
            return nil
        }

        var fed = false
        var convertError: NSError?
        let status = converter.convert(to: outputBuffer, error: &convertError) { _, statusOut in
            if fed {
                statusOut.pointee = .noDataNow
                return nil
            }
            fed = true
            statusOut.pointee = .haveData
            return inputBuffer
        }

        switch status {
        case .haveData, .inputRanDry, .endOfStream:
            return outputBuffer.frameLength > 0 ? outputBuffer : nil
        case .error:
            let msg = convertError?.localizedDescription ?? "unknown converter error"
            emit(.error(code: "audio_converter_failed:\(msg)"))
            return nil
        @unknown default:
            return nil
        }
    }

    /// Append the int16 samples in `buffer` to the ring queue.
    private func enqueue(_ buffer: AVAudioPCMBuffer) {
        guard let int16Data = buffer.int16ChannelData else { return }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return }
        let ptr = UnsafeBufferPointer(start: int16Data[0], count: frameCount)
        queue.append(ptr)
    }

    /// Drain the ring queue in 320-sample (640-byte) units and hand each
    /// chunk to `frameSink`. Partial trailing samples carry over to the next
    /// call. Resumes `firstChunkContinuation` the first time a chunk leaves.
    private func flushChunks() {
        while queue.count >= Self.samplesPerChunk {
            let samples = queue.drain(upTo: Self.samplesPerChunk)
            if samples.count != Self.samplesPerChunk { break }
            // Little-endian Int16 → Data. macOS is little-endian natively, so
            // the in-memory representation already matches the wire format.
            var copy = samples
            let data = copy.withUnsafeMutableBufferPointer { ptr -> Data in
                Data(bytes: ptr.baseAddress!, count: ptr.count * MemoryLayout<Int16>.size)
            }
            frameSink(data)

            firstChunkLock.lock()
            if !firstChunkSignalled {
                firstChunkSignalled = true
                let cont = firstChunkContinuation
                firstChunkContinuation = nil
                firstChunkLock.unlock()
                cont?.resume()
            } else {
                firstChunkLock.unlock()
            }
        }
    }
}
