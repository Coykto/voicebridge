import Foundation
import AVFoundation
import CoreAudio

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
internal final class LockedQueue {
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
/// ## Surface
///
/// Slice 5 brings the full route-change recovery surface on top of the Slice 1
/// baseline: the four CoreAudio HAL property listeners (default-input,
/// default-output, per-device stream-format, per-device IsRunningSomewhere),
/// the `AVAudioEngineConfigurationChange` observer, the
/// `triggerMicRestart` / `attemptMicRestartIfPending` machinery, and the
/// mic-flow watchdog (`micFlowStallThresholdSeconds = 2.0`). All of this is
/// ported from `record/swift-capture/Sources/RecordCapture/AudioCapture.swift`
/// with only mic-specific code retained — system-audio capture, WAV writers,
/// and the `source_lost`/`source_attached` event vocabulary are dropped.
///
/// ## Diff from record
///
/// - Recoverable route changes stay silent on the wire (no events emitted).
/// - Unrecoverable loss takes a *terminal* path: emit
///   `error{code: "mic_lost", message: <reason>}` and signal `onTerminalLoss`
///   so `main.swift` can `exit(5)`. record's equivalent recovers and continues.
/// - Watchdog gates the unrecoverable-loss decision: a stall plus
///   `currentDefaultInputDevice() == kAudioObjectUnknown` (no input device on
///   the system) is what flips us into terminal mode.
///
/// `start()` returns only after the first 640-byte chunk has been flushed to
/// `frameSink`, so the caller can sequence `ready` after both mic flow and
/// hotkey registration are live (tech spec §2.3).
final class AudioCapture {

    private let emit: (Event) -> Void
    private let frameSink: (Data) -> Void
    /// Called exactly once, after `handleMicLostTerminal` has emitted the
    /// `mic_lost` event and torn everything down. `main.swift` passes
    /// `{ exit(5) }`. Defined as a closure rather than throwing because the
    /// loss path runs on the main queue; `exit()` returns `Never`.
    private let onTerminalLoss: () -> Void

    /// Int16 / mono / 16 kHz interleaved processing format. The converter
    /// targets it; the ring buffer is sized in samples of this format.
    private let processingFormat: AVAudioFormat

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private var converterInputFormat: AVAudioFormat?
    internal let queue = LockedQueue()
    private var tapInstalled = false

    /// Samples-per-chunk = 320 (20 ms at 16 kHz mono). Each chunk is emitted
    /// to `frameSink` as 640 bytes of little-endian Int16.
    internal static let samplesPerChunk = 320

    /// Continuation used by `start()` to await the first 640-byte chunk. Set
    /// in `start()`, resumed (exactly once) the first time the tap callback
    /// flushes a chunk to `frameSink`. Guarded by `firstChunkLock`.
    private let firstChunkLock = NSLock()
    private var firstChunkContinuation: CheckedContinuation<Void, Never>?
    private var firstChunkSignalled = false

    // --- Source-loss tracking ---
    //
    // A single lock guards the terminal-loss flag so that two concurrent
    // loss signals (e.g. the watchdog firing at the same moment as a
    // `triggerMicRestart` that gives up) can't both pass the idempotency
    // check and double-emit `mic_lost`.
    private let lossLock = NSLock()
    private var micLost = false

    /// Token for the `AVAudioEngineConfigurationChange` observer. Retained
    /// so `stop()` and the terminal-loss handler can remove it; the
    /// block-based NotificationCenter API would leak otherwise.
    private var engineConfigObserver: NSObjectProtocol?

    /// Monotonically incremented on every restart trigger. Each scheduled
    /// fallback captures the generation it was queued under; if the counter
    /// has advanced by the time the fallback fires, a fresher route change
    /// is in flight and the older chain abandons itself. Mutated only from
    /// the main queue.
    private var micRestartGeneration: Int = 0

    /// True between the moment a restart is requested and the moment we
    /// successfully bring the engine back up. Read/written only on main.
    private var pendingMicRestart: Bool = false

    // --- CoreAudio listeners ---

    /// CoreAudio system-object listener that fires when the *system default
    /// input device* changes (AirPods becoming default, USB mic unplugged,
    /// etc.). Used to know when to re-target the per-device stream-format
    /// listener.
    private var defaultInputListenerBlock: AudioObjectPropertyListenerBlock?

    /// CoreAudio system-object listener that fires when the *system default
    /// output device* changes. Important even though we don't render output:
    /// AirPods becoming the default output can cause the OS to silently
    /// pause our mic input AU without firing any other notification we
    /// observe.
    private var defaultOutputListenerBlock: AudioObjectPropertyListenerBlock?

    /// CoreAudio device listener that fires when the currently-default input
    /// device's stream format changes. For Bluetooth headsets this is the
    /// "SCO profile finished negotiating, mic is ready" signal — exactly
    /// when `engine.start()` will succeed after a route change.
    private var formatListenerBlock: AudioObjectPropertyListenerBlock?

    /// CoreAudio device listener for
    /// `kAudioDevicePropertyDeviceIsRunningSomewhere` on the current
    /// default input device.
    private var runningSomewhereListenerBlock: AudioObjectPropertyListenerBlock?

    /// AudioObjectID of the device currently being watched by the per-input
    /// listeners. Tracked so we know which object to call
    /// `AudioObjectRemovePropertyListenerBlock` against when the default
    /// input changes (or capture stops).
    private var watchedInputDeviceID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)

    /// Background queue the CoreAudio property listeners dispatch on. All
    /// listener blocks hop to `DispatchQueue.main.async` before mutating
    /// engine state.
    private let coreAudioListenerQueue = DispatchQueue(
        label: "voicebridge.audiocapture.coreaudio",
        qos: .userInitiated
    )

    // --- Mic-flow watchdog ---
    //
    // The route-change recovery path above is purely event-driven. In
    // practice, some macOS scenarios — most notably AirPods connecting as
    // the new default *output* device only — cause AVAudioEngine to
    // silently stop calling the mic tap closure without ever firing
    // `AVAudioEngineConfigurationChange` and without any CoreAudio
    // default-input change. The watchdog periodically checks whether mic
    // buffers have arrived recently; on stall, it triggers the same restart
    // path as the listener handlers.
    private let micFlowLock = NSLock()
    private var lastMicBufferAt: Date?
    private var micWatchdogTimer: DispatchSourceTimer?
    /// Restart the engine if no mic buffer has arrived in this long.
    /// 2 s is well above the typical inter-buffer interval (~25 ms at
    /// 1024-frame buffers) but short enough that the user hears the
    /// recovery rather than losing minutes of audio to a silent wedge.
    private let micFlowStallThresholdSeconds: Double = 2.0
    /// Watchdog poll interval. Same shape as record's pattern.
    private let watchdogIntervalMs: Int = 250

    /// Background queue the watchdog timer fires on.
    private let watchdogQueue = DispatchQueue(
        label: "voicebridge.audiocapture.watchdog",
        qos: .userInitiated
    )

    init(
        emit: @escaping (Event) -> Void,
        frameSink: @escaping (Data) -> Void,
        onTerminalLoss: @escaping () -> Void = {}
    ) throws {
        self.emit = emit
        self.frameSink = frameSink
        self.onTerminalLoss = onTerminalLoss
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

        // Watch for hardware route changes (AirPods connect/disconnect, USB
        // mic plug/unplug, system default input swap). Per Apple's docs the
        // engine has already stopped itself by the time this fires — we
        // rebuild the tap against the new input format and restart, which
        // keeps mic capture alive across the route change.
        engineConfigObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: .main
        ) { [weak self] _ in
            self?.handleEngineConfigurationChange()
        }

        // Register CoreAudio property listeners that drive route-change
        // recovery off real device events rather than a polling timer.
        installCoreAudioListeners()

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

        // First chunk has flowed — arm the watchdog. From this point the tap
        // callback is updating `lastMicBufferAt`, so the watchdog has a
        // baseline to compare against.
        startMicWatchdog()
    }

    /// Stop the engine and remove the tap. Idempotent.
    func stop() {
        // Drop the engine-config observer and the CoreAudio listeners
        // before tearing anything down: once we stop the engine ourselves,
        // AVAudioEngine may post a final notification — and CoreAudio may
        // fire one final format-change — that we don't want to misinterpret
        // as a mid-capture loss.
        if let token = engineConfigObserver {
            NotificationCenter.default.removeObserver(token)
            engineConfigObserver = nil
        }
        uninstallCoreAudioListeners()
        stopMicWatchdog()

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
        // Watchdog heartbeat — record the time of every buffer the tap sees,
        // *before* any work that could short-circuit on conversion failure.
        // A buffer arriving means the OS is still calling us, which is what
        // the watchdog cares about.
        micFlowLock.lock()
        lastMicBufferAt = Date()
        micFlowLock.unlock()

        guard let outputBuffer = convert(inputBuffer) else { return }
        enqueue(outputBuffer)
        flushChunks()
    }

    /// Convert one `AVAudioPCMBuffer` (whatever native format) into the
    /// processing format (int16 / mono / 16 kHz / interleaved). Lazily
    /// rebuilds the converter when the input format changes (e.g. across a
    /// route change).
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
    internal func enqueue(_ buffer: AVAudioPCMBuffer) {
        guard let int16Data = buffer.int16ChannelData else { return }
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else { return }
        let ptr = UnsafeBufferPointer(start: int16Data[0], count: frameCount)
        queue.append(ptr)
    }

    /// Drain the ring queue in 320-sample (640-byte) units and hand each
    /// chunk to `frameSink`. Partial trailing samples carry over to the next
    /// call. Resumes `firstChunkContinuation` the first time a chunk leaves.
    internal func flushChunks() {
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

    // MARK: - Route-change recovery

    /// Rebuild the mic tap and restart the engine after an
    /// `AVAudioEngineConfigurationChange`. Apple's documented recovery path
    /// for route changes: the engine has already stopped itself, the tap is
    /// invalid, and we need to re-tap against whatever the new input format
    /// is.
    ///
    /// Stays silent on the wire — recoverable route changes don't surface to
    /// the orchestrator. Only the terminal-loss path emits an event.
    private func handleEngineConfigurationChange() {
        FileHandle.standardError.write(
            Data("DBG mic-route: AVAudioEngineConfigurationChange\n".utf8)
        )
        triggerMicRestart(reason: "AVAudioEngineConfigurationChange")
    }

    /// Shared restart helper invoked by every event source that wants the
    /// mic engine torn down and brought back up. Idempotent w.r.t. an
    /// in-flight restart (the generation counter ensures the latest event
    /// wins). Must run on the main queue.
    private func triggerMicRestart(reason: String) {
        lossLock.lock()
        let alreadyLost = micLost
        lossLock.unlock()
        if alreadyLost { return }

        // `stop()` clears the configuration observer before tearing down —
        // treat that as the signal that recovery is no longer wanted.
        if engineConfigObserver == nil { return }

        // Re-entrance guard. `engine.stop()` below itself flips
        // `IsRunningSomewhere` to false, which re-fires that listener and
        // re-enters this method while we're still mid-restart. If a
        // restart is already pending and the engine is already torn down,
        // just re-arm listeners and retry — don't bump the generation or
        // redo the teardown.
        if pendingMicRestart && !engine.isRunning {
            rearmListenersOnCurrentDefaultInput()
            attemptMicRestartIfPending()
            return
        }

        micRestartGeneration += 1
        pendingMicRestart = true

        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }
        converter = nil
        converterInputFormat = nil

        // The current default input device may have just changed, so
        // re-point both per-input listeners (format + IsRunningSomewhere)
        // at whatever the new default is. Then try to bring the engine
        // back up immediately — it will succeed if the format has already
        // settled; otherwise the format listener (or a later
        // IsRunningSomewhere transition back to true) will retrigger us.
        rearmListenersOnCurrentDefaultInput()
        attemptMicRestartIfPending()
    }

    /// Try to bring the engine back up *if* a restart is pending and the
    /// current input bus reports a valid format. Idempotent: a no-op when
    /// nothing is pending or the engine is already running.
    ///
    /// Must run on the main queue — mutates main-only state.
    private func attemptMicRestartIfPending() {
        if !pendingMicRestart { return }

        lossLock.lock()
        let alreadyLost = micLost
        lossLock.unlock()
        if alreadyLost { return }

        // `stop()` removes the configuration observer before tearing things
        // down — treat that as the signal to stop attempting recovery.
        if engineConfigObserver == nil {
            pendingMicRestart = false
            return
        }

        let input = engine.inputNode
        let newFormat = input.outputFormat(forBus: 0)
        guard newFormat.channelCount > 0, newFormat.sampleRate > 0 else {
            // Format isn't settled yet — wait for the next listener fire.
            return
        }

        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: nil) { [weak self] buffer, _ in
            self?.handleMicBuffer(buffer)
        }
        tapInstalled = true

        do {
            try startEngineWithRetries()
            pendingMicRestart = false
            // Reset the watchdog baseline — without this, the watchdog could
            // fire on the elapsed-stall during the restart and re-trigger
            // immediately on the first post-restart tick.
            micFlowLock.lock()
            lastMicBufferAt = Date()
            micFlowLock.unlock()
        } catch {
            // Engine still refusing — drop the tap, reset, and leave the
            // pending flag set. The next CoreAudio format-change listener
            // fire (or the watchdog) will re-evaluate.
            if tapInstalled {
                engine.inputNode.removeTap(onBus: 0)
                tapInstalled = false
            }
            engine.reset()
        }
    }

    /// Call `engine.prepare()` + `engine.start()` with a small retry loop.
    ///
    /// AVAudioEngine on macOS can throw `kAudioUnitErr_FormatNotSupported`
    /// (-10868) on the first start attempt right after the previous engine's
    /// teardown, or while a Bluetooth audio route is still transitioning to
    /// or from the AirPods/headset. `engine.reset()` followed by a brief
    /// pause is the documented recovery — it returns the engine to a known
    /// state and gives the audio HAL a chance to settle.
    private func startEngineWithRetries() throws {
        let maxAttempts = 3
        var lastError: Error?
        for attempt in 0..<maxAttempts {
            if attempt > 0 {
                engine.reset()
                Thread.sleep(forTimeInterval: 0.1)
            }
            do {
                engine.prepare()
                try engine.start()
                return
            } catch {
                lastError = error
            }
        }
        throw lastError ?? AudioCaptureError.bufferAllocationFailed
    }

    // MARK: - CoreAudio listeners

    /// Look up the system default input device.
    private func currentDefaultInputDevice() -> AudioObjectID {
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address, 0, nil, &size, &deviceID
        )
        return status == noErr ? deviceID : AudioObjectID(kAudioObjectUnknown)
    }

    /// Register the system-object listeners (default input + default
    /// output) and the per-device listeners on whatever the current
    /// default input is.
    private func installCoreAudioListeners() {
        var defaultInputAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let defaultInputBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.handleDefaultInputDeviceChanged()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject),
            &defaultInputAddress,
            coreAudioListenerQueue,
            defaultInputBlock
        ) == noErr {
            defaultInputListenerBlock = defaultInputBlock
        }

        var defaultOutputAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let defaultOutputBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.handleDefaultOutputDeviceChanged()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject),
            &defaultOutputAddress,
            coreAudioListenerQueue,
            defaultOutputBlock
        ) == noErr {
            defaultOutputListenerBlock = defaultOutputBlock
        }

        installPerInputListenersOnCurrentDefaultInput()
    }

    /// Detach every CoreAudio listener. Safe to call multiple times.
    private func uninstallCoreAudioListeners() {
        if let block = defaultInputListenerBlock {
            var address = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultInputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            _ = AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                coreAudioListenerQueue,
                block
            )
            defaultInputListenerBlock = nil
        }
        if let block = defaultOutputListenerBlock {
            var address = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            _ = AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject),
                &address,
                coreAudioListenerQueue,
                block
            )
            defaultOutputListenerBlock = nil
        }
        removePerInputListeners()
    }

    /// Detach both per-input listeners (format + IsRunningSomewhere) from
    /// whichever device they were attached to (if any).
    private func removePerInputListeners() {
        let device = watchedInputDeviceID
        if device != AudioObjectID(kAudioObjectUnknown) {
            if let block = formatListenerBlock {
                var address = AudioObjectPropertyAddress(
                    mSelector: kAudioDevicePropertyStreamFormat,
                    mScope: kAudioDevicePropertyScopeInput,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(
                    device, &address, coreAudioListenerQueue, block
                )
            }
            if let block = runningSomewhereListenerBlock {
                var address = AudioObjectPropertyAddress(
                    mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
                    mScope: kAudioObjectPropertyScopeGlobal,
                    mElement: kAudioObjectPropertyElementMain
                )
                _ = AudioObjectRemovePropertyListenerBlock(
                    device, &address, coreAudioListenerQueue, block
                )
            }
        }
        formatListenerBlock = nil
        runningSomewhereListenerBlock = nil
        watchedInputDeviceID = AudioObjectID(kAudioObjectUnknown)
    }

    /// Install both per-input listeners on the *current* system default
    /// input device. If no input device is present this is a no-op; the
    /// default-input listener will re-invoke us once one shows up.
    private func installPerInputListenersOnCurrentDefaultInput() {
        let device = currentDefaultInputDevice()
        guard device != AudioObjectID(kAudioObjectUnknown) else { return }

        var formatAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamFormat,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        let formatBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                FileHandle.standardError.write(
                    Data("DBG mic-route: input stream format changed\n".utf8)
                )
                self?.attemptMicRestartIfPending()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            device, &formatAddress, coreAudioListenerQueue, formatBlock
        ) == noErr {
            formatListenerBlock = formatBlock
        }

        var runningAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let runningBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            DispatchQueue.main.async {
                self?.handleInputDeviceIsRunningSomewhereChanged()
            }
        }
        if AudioObjectAddPropertyListenerBlock(
            device, &runningAddress, coreAudioListenerQueue, runningBlock
        ) == noErr {
            runningSomewhereListenerBlock = runningBlock
        }

        watchedInputDeviceID = device
    }

    /// Detach the per-input listeners and reattach them to whatever device
    /// is the default input *now*.
    private func rearmListenersOnCurrentDefaultInput() {
        let current = currentDefaultInputDevice()
        if current == watchedInputDeviceID
            && formatListenerBlock != nil
            && runningSomewhereListenerBlock != nil {
            return
        }
        removePerInputListeners()
        installPerInputListenersOnCurrentDefaultInput()
    }

    /// Read `kAudioDevicePropertyDeviceIsRunningSomewhere` for `deviceID`.
    /// Returns false on any CoreAudio error.
    private func isInputDeviceRunning(_ deviceID: AudioObjectID) -> Bool {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(
            deviceID, &address, 0, nil, &size, &value
        )
        return status == noErr && value != 0
    }

    /// Default input changed (e.g. AirPods became default *input*). The
    /// engine's tap is bound to the old input, so a full restart is
    /// always warranted.
    private func handleDefaultInputDeviceChanged() {
        FileHandle.standardError.write(
            Data("DBG mic-route: default input device changed\n".utf8)
        )
        triggerMicRestart(reason: "default input device changed")
    }

    /// Default *output* changed. Restart only if the input is now actually
    /// stopped — otherwise this is a benign output swap.
    private func handleDefaultOutputDeviceChanged() {
        FileHandle.standardError.write(
            Data("DBG mic-route: default output device changed\n".utf8)
        )
        let device = watchedInputDeviceID
        if device == AudioObjectID(kAudioObjectUnknown) {
            return
        }
        if isInputDeviceRunning(device) {
            FileHandle.standardError.write(
                Data("DBG mic-route: input still running, no restart\n".utf8)
            )
            return
        }
        triggerMicRestart(reason: "default output changed and input stopped")
    }

    /// `DeviceIsRunningSomewhere` on the watched input toggled.
    private func handleInputDeviceIsRunningSomewhereChanged() {
        let device = watchedInputDeviceID
        if device == AudioObjectID(kAudioObjectUnknown) { return }
        let running = isInputDeviceRunning(device)
        FileHandle.standardError.write(
            Data("DBG mic-route: input IsRunningSomewhere=\(running)\n".utf8)
        )
        if running {
            attemptMicRestartIfPending()
        } else {
            triggerMicRestart(reason: "input device IsRunningSomewhere=false")
        }
    }

    // MARK: - Mic-flow watchdog

    /// Start the watchdog timer. Periodically checks `lastMicBufferAt`; on
    /// stall, hops to main and triggers a restart. If the stall coincides
    /// with no system default input device, escalates to terminal loss.
    private func startMicWatchdog() {
        let timer = DispatchSource.makeTimerSource(queue: watchdogQueue)
        timer.schedule(
            deadline: .now() + .milliseconds(watchdogIntervalMs),
            repeating: .milliseconds(watchdogIntervalMs)
        )
        timer.setEventHandler { [weak self] in
            self?.watchdogTick()
        }
        micWatchdogTimer = timer
        timer.resume()
    }

    private func stopMicWatchdog() {
        micWatchdogTimer?.cancel()
        micWatchdogTimer = nil
    }

    /// One tick of the watchdog. Runs on `watchdogQueue`; hops to main if
    /// it needs to mutate engine state.
    private func watchdogTick() {
        micFlowLock.lock()
        let last = lastMicBufferAt
        micFlowLock.unlock()

        guard let last = last else { return }
        let gap = Date().timeIntervalSince(last)
        guard gap >= micFlowStallThresholdSeconds else { return }

        // Stall detected. Decide between recoverable restart and terminal
        // loss based on whether the system has any default input device at
        // all. The CoreAudio call itself is safe off-main.
        let hasInput = currentDefaultInputDevice() != AudioObjectID(kAudioObjectUnknown)

        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            if !hasInput {
                self.handleMicLostTerminal(
                    reason: "mic-flow stalled for \(String(format: "%.1f", gap)) s and no default input device"
                )
            } else {
                self.triggerMicRestart(reason: "mic-flow stalled for \(String(format: "%.1f", gap)) s")
            }
        }
    }

    // MARK: - Unrecoverable loss

    /// Tear down everything and emit `error{code: mic_lost, message: reason}`,
    /// then signal `onTerminalLoss` so `main.swift` can `exit(5)`. Idempotent.
    ///
    /// Must run on the main queue. The global `emit()` writes to stderr via
    /// an unbuffered `FileHandle.standardError.write`, so by the time
    /// `onTerminalLoss` runs `exit(5)` the JSON line is already on the wire —
    /// no explicit flush needed.
    private func handleMicLostTerminal(reason: String) {
        lossLock.lock()
        if micLost {
            lossLock.unlock()
            return
        }
        micLost = true
        lossLock.unlock()

        stopMicWatchdog()

        if let token = engineConfigObserver {
            NotificationCenter.default.removeObserver(token)
            engineConfigObserver = nil
        }
        uninstallCoreAudioListeners()

        if tapInstalled {
            engine.inputNode.removeTap(onBus: 0)
            tapInstalled = false
        }
        if engine.isRunning {
            engine.stop()
        }
        pendingMicRestart = false

        emit(.error(code: "mic_lost", message: reason))
        onTerminalLoss()
    }
}
