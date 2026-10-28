export interface RecorderCallbacks {
  onChunk: (buffer: ArrayBuffer, capturedAtEpochMs: number) => void;
  onLevel: (level: number) => void;
  onLimit?: () => void;
  onError?: (error: Error) => void;
}
export class RecorderError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}
interface Capture {
  phase: 'starting' | 'recording' | 'flushing' | 'closed';
  callbacks: RecorderCallbacks;
  stream: MediaStream | null;
  context: AudioContext | null;
  source: MediaStreamAudioSourceNode | null;
  node: AudioWorkletNode | null;
  discard: boolean;
  failure: Error | null;
  contextEpochMs: number | null;
  beginContextTime: number | null;
  emittedSamples: number;
  acknowledgedSamples: number;
  lastFrameTime: number | null;
  shortFrame: boolean;
  limitReached: boolean;
  /** This session's server-granted sample limit (V2 §4.1). */
  maxSamples: number;
  /** True when the limit was granted explicitly and must be sent to the worklet. */
  explicitLimit: boolean;
  flushedReceived: boolean;
  finishFlush: ((error?: Error) => void) | null;
  startup: 'unprepared' | 'preparing' | 'begin_pending' | 'begun';
  finishStart: ((error?: Error) => void) | null;
  closing: Promise<void> | null;
}
const MAX_SAMPLES = 2_880_000;
const epochNow = (): number => performance.timeOrigin + performance.now();

/** Captures 16 kHz mono PCM. Server ACKs, not IPC delivery, replenish audio credit. */
export class PcmRecorder {
  private capture: Capture | null = null;
  private stopping: Promise<void> | null = null;
  get active(): boolean { return this.capture !== null; }

  async start(callbacks: RecorderCallbacks, options: { maxSamples?: number } = {}): Promise<void> {
    if (this.stopping) await this.stopping;
    if (this.capture) throw new Error('麦克风正在使用');
    const capture: Capture = { phase: 'starting', callbacks, stream: null, context: null, source: null, node: null,
      discard: false, failure: null, contextEpochMs: null, beginContextTime: null, emittedSamples: 0, acknowledgedSamples: 0,
      lastFrameTime: null, shortFrame: false, limitReached: false, flushedReceived: false, finishFlush: null, startup: 'unprepared', finishStart: null, closing: null,
      maxSamples: options.maxSamples ?? MAX_SAMPLES, explicitLimit: options.maxSamples !== undefined };
    this.capture = capture;
    const current = () => this.capture === capture && capture.phase !== 'closed';
    try {
      // Keep the speech signal intact, including quiet syllables and deliberate
      // loopback fixtures. The worklet performs explicit mono mixing/resampling.
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1,
          echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
      } catch (error) {
        // Scope this classification to microphone acquisition. Other browser
        // operations (for example clipboard access) can also be NotAllowedError.
        if (error instanceof DOMException && error.name === 'NotAllowedError') {
          throw new RecorderError('MICROPHONE_PERMISSION_DENIED', '麦克风权限被拒绝');
        }
        throw error;
      }
      if (!current()) {
        stream.getTracks().forEach(track => track.stop());
        throw new Error('录音已取消');
      }
      capture.stream = stream;
      // macOS may ignore a requested 16 kHz rate. Use the hardware clock and
      // resample its actual rate in the worklet before applying output credit.
      const context = new AudioContext({ latencyHint: 'interactive' });
      capture.context = context;
      if (![16000, 44100, 48000].includes(context.sampleRate)) throw new Error('音频设备采样率暂不支持，请使用 16、44.1 或 48 kHz 设备');
      await context.audioWorklet.addModule(new URL('./pcm-worklet.js', window.location.href).toString());
      if (!current()) throw new Error('录音已取消');
      const source = context.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(context, 'pcm-capture', { numberOfInputs: 1, numberOfOutputs: 0,
        channelCount: 2, channelCountMode: 'max', channelInterpretation: 'discrete' });
      capture.source = source; capture.node = node;
      node.port.onmessage = (event: MessageEvent) => {
        if (!current() || capture.discard) return;
        this.message(capture, event.data);
      };
      const interrupted = () => {
        if (!current() || capture.limitReached) return;
        this.fail(capture, new RecorderError('AUDIO_INTERRUPTED', '音频采集中断，请重新开始录音'));
      };
      node.onprocessorerror = interrupted;
      stream.getAudioTracks().forEach(track => { track.onended = interrupted; });
      context.onstatechange = () => { if (context.state !== 'running') interrupted(); };
      // Establish the graph before preparation; the worklet emits no capture
      // data until ready -> begin is confirmed by an actual render quantum.
      source.connect(node);
      await context.resume();
      if (!current()) throw new Error('录音已取消');
      if (context.state !== 'running' || stream.getAudioTracks().some(track => track.readyState === 'ended')) {
        throw new Error('音频采集未能启动，请重新开始录音');
      }
      capture.contextEpochMs = epochNow() - context.currentTime * 1000;
      if (!Number.isFinite(capture.contextEpochMs)) throw new Error('音频采样时钟不可用');
      await new Promise<void>((resolve, reject) => {
        capture.finishStart = error => {
          capture.finishStart = null;
          if (error) reject(error); else resolve();
        };
        capture.startup = 'preparing';
        try { node.port.postMessage({ type: 'prepare' }); }
        catch (error) { capture.finishStart(error instanceof Error ? error : new Error('音频采集未能启动')); }
      });
      if (!current() || capture.phase !== 'recording' || capture.startup !== 'begun') throw new Error('录音已取消');
    } catch (error) {
      await this.close(capture);
      throw error;
    }
  }

  /** Called only with the current session's cumulative server ACK from Main. */
  acknowledge(totalSamples: number): void {
    const capture = this.capture;
    if (!capture || capture.phase === 'closed' || capture.discard) return;
    if (!Number.isSafeInteger(totalSamples) || totalSamples < capture.acknowledgedSamples || totalSamples > capture.emittedSamples) {
      this.fail(capture, new RecorderError('AUDIO_PROTOCOL_ERROR', '音频累计确认不匹配，已停止本轮录音'));
      return;
    }
    if (totalSamples === capture.acknowledgedSamples) return;
    capture.acknowledgedSamples = totalSamples;
    try { capture.node?.port.postMessage({ type: 'ack', total_samples: totalSamples }); }
    catch { this.fail(capture, new RecorderError('AUDIO_INTERRUPTED', '无法确认音频接收，已停止本轮录音')); }
  }

  private message(capture: Capture, value: unknown): void {
    const data = value as Record<string, unknown> | null;
    if (!data || typeof data !== 'object') { this.protocolError(capture); return; }
    try {
      if (data.type === 'ready') {
        if (capture.phase !== 'starting' || capture.startup !== 'preparing' || !capture.finishStart || !capture.node) {
          this.protocolError(capture); return;
        }
        capture.startup = 'begin_pending';
        capture.node.port.postMessage(capture.explicitLimit ? { type: 'begin', max_samples: capture.maxSamples } : { type: 'begin' });
      } else if (data.type === 'begun') {
        if (capture.phase !== 'starting' || capture.startup !== 'begin_pending' || !capture.finishStart
          || typeof data.context_time !== 'number' || !Number.isFinite(data.context_time) || data.context_time < 0
          || capture.contextEpochMs === null) {
          this.protocolError(capture); return;
        }
        capture.beginContextTime = data.context_time;
        capture.startup = 'begun'; capture.phase = 'recording'; capture.finishStart();
      } else if (data.type === 'pcm') {
        const buffer = data.buffer, samples = data.samples, total = data.total_samples, time = data.context_time;
        if ((capture.phase !== 'recording' && capture.phase !== 'flushing') || capture.startup !== 'begun' || !(buffer instanceof ArrayBuffer) || !Number.isSafeInteger(samples) || Number(samples) < 1 || Number(samples) > 4000
          || buffer.byteLength !== Number(samples) * 2 || total !== capture.emittedSamples + Number(samples)
          || Number(total) > capture.maxSamples || Number(total) - capture.acknowledgedSamples > 32_000
          || capture.shortFrame || capture.flushedReceived || capture.limitReached
          || (Number(samples) !== 4000 && capture.phase !== 'flushing') || typeof time !== 'number' || !Number.isFinite(time) || time < 0
          || (capture.lastFrameTime !== null && time < capture.lastFrameTime) || capture.contextEpochMs === null
          || capture.beginContextTime === null || time < capture.beginContextTime) {
          this.protocolError(capture); return;
        }
        capture.emittedSamples = Number(total); capture.lastFrameTime = time; capture.shortFrame = Number(samples) < 4000;
        capture.callbacks.onChunk(buffer, capture.contextEpochMs + time * 1000);
      } else if (data.type === 'flushed') {
        if ((capture.phase !== 'flushing' && !capture.limitReached) || data.total_samples !== capture.emittedSamples) {
          this.protocolError(capture); return;
        }
        capture.flushedReceived = true; capture.finishFlush?.();
      } else if (data.type === 'limit') {
        if (capture.limitReached || capture.flushedReceived || data.total_samples !== capture.maxSamples || capture.emittedSamples !== capture.maxSamples) {
          this.protocolError(capture); return;
        }
        capture.limitReached = true;
        capture.callbacks.onLimit?.();
        // Main may already have sent finalizing for the exact last frame before
        // this port event arrives. Close locally even if UI stop is now a no-op.
        void this.stop().catch(error => {
          if (capture.failure || capture.discard) return;
          capture.failure = error instanceof Error ? error : new Error('音频尾帧未确认');
          try { capture.callbacks.onError?.(capture.failure); } catch { /* Capture is already closed. */ }
        });
      } else if (data.type === 'level') {
        if (typeof data.level !== 'number' || !Number.isFinite(data.level) || data.level < 0 || data.level > 1) {
          this.protocolError(capture); return;
        }
        if (capture.phase === 'recording' && !capture.limitReached) capture.callbacks.onLevel(data.level);
      } else if (data.type === 'error') {
        this.fail(capture, new RecorderError(typeof data.code === 'string' ? data.code : 'AUDIO_INTERRUPTED',
          typeof data.message === 'string' ? data.message : '音频采集中断，请重新开始录音'));
      } else { this.protocolError(capture); }
    } catch (error) {
      this.fail(capture, error instanceof Error ? error : new Error('音频采集回调失败'));
    }
  }

  private protocolError(capture: Capture): void {
    this.fail(capture, new RecorderError('AUDIO_PROTOCOL_ERROR', '音频帧或完成确认无效，已停止本轮录音'));
  }
  private fail(capture: Capture, error: Error): void {
    if (this.capture !== capture || capture.phase === 'closed' || capture.failure) return;
    capture.failure = error; capture.discard = true; capture.finishStart?.(error); capture.finishFlush?.(error);
    try { capture.callbacks.onError?.(error); }
    catch { /* Callback failure cannot prevent releasing the microphone. */ }
    finally { void this.stop({ discard: true }).catch(() => undefined); }
  }

  stop(options: { discard?: boolean } = {}): Promise<void> {
    const capture = this.capture;
    if (this.stopping) {
      // Cancel may overtake an already pending normal flush. Suppress every
      // queued frame immediately and unblock that flush without sending it.
      if (capture && options.discard === true) {
        capture.discard = true;
        try { capture.node?.port.postMessage({ type: 'discard' }); } catch { /* Closing below. */ }
        capture.finishFlush?.(capture.failure ?? undefined);
      }
      return this.stopping;
    }
    if (!capture) return Promise.resolve();
    capture.discard ||= options.discard === true;
    if (capture.phase === 'starting') {
      // Do not wait for getUserMedia permission to settle. A late stream is
      // stopped by the original start() closure and cannot replace a new one.
      capture.discard = true;
      capture.finishStart?.(capture.failure ?? new Error('录音已取消'));
      try { capture.node?.port.postMessage({ type: 'discard' }); } catch { /* Closing releases the graph. */ }
      capture.phase = 'closed'; this.capture = null;
      return this.close(capture);
    }
    capture.phase = 'flushing';
    const stopping = this.flushAndClose(capture);
    this.stopping = stopping;
    void stopping.then(() => { if (this.stopping === stopping) this.stopping = null; }, () => { if (this.stopping === stopping) this.stopping = null; });
    return stopping;
  }

  private async flushAndClose(capture: Capture): Promise<void> {
    try {
      if (capture.discard) {
        try { capture.node?.port.postMessage({ type: 'discard' }); } catch { /* Close still releases the microphone. */ }
      } else if (!capture.flushedReceived && capture.node) {
        const node = capture.node;
        await new Promise<void>((resolve, reject) => {
          const timer = setTimeout(() => {
            capture.finishFlush = null;
            reject(new Error('音频尾帧未确认，已停止本轮录音'));
          }, 2000);
          capture.finishFlush = error => {
            clearTimeout(timer); capture.finishFlush = null;
            if (error) reject(error); else resolve();
          };
          try { if (!capture.limitReached) node.port.postMessage({ type: 'stop' }); }
          catch (error) { clearTimeout(timer); capture.finishFlush = null; reject(error); }
        });
      }
    } finally { await this.close(capture); }
  }

  private close(capture: Capture): Promise<void> {
    if (capture.closing) return capture.closing;
    capture.phase = 'closed';
    if (this.capture === capture) this.capture = null;
    capture.finishStart?.(capture.failure ?? new Error('录音已取消')); capture.finishStart = null;
    capture.finishFlush?.(capture.failure ?? undefined); capture.finishFlush = null;
    const source = capture.source, node = capture.node, stream = capture.stream, context = capture.context;
    capture.source = null; capture.node = null; capture.stream = null; capture.context = null;
    try { source?.disconnect(); } catch { /* Already disconnected. */ }
    if (node) {
      node.onprocessorerror = null; node.port.onmessage = null;
      try { node.disconnect(); node.port.close(); } catch { /* Already closed. */ }
    }
    stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    if (context) context.onstatechange = null;
    capture.closing = (async () => { try { await context?.close(); } catch { /* Already closed. */ } })();
    return capture.closing;
  }
}
