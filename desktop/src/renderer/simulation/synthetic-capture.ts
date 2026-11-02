import { AUDIO } from '@shared/config';
import { RecorderError, type RecorderCallbacks } from '../audio/recorder-contract';

interface Capture {
  callbacks: RecorderCallbacks;
  timer: ReturnType<typeof setTimeout> | null;
  emittedSamples: number;
  acknowledgedSamples: number;
  maxSamples: number;
  lastFrameAt: number;
  limitReached: boolean;
}
type SourceState = { phase: 'idle' }
  | { phase: 'running'; capture: Capture }
  | { phase: 'stopping'; capture: Capture; completion: Promise<void> };
const now = () => performance.timeOrigin + performance.now();

export class SyntheticCaptureSource {
  private state: SourceState = { phase: 'idle' };
  get active(): boolean { return this.state.phase !== 'idle'; }

  async start(callbacks: RecorderCallbacks, options: { maxSamples?: number } = {}): Promise<void> {
    if (this.state.phase === 'stopping') await this.state.completion;
    if (this.state.phase !== 'idle') throw new Error('模拟输入正在使用');
    const maxSamples = options.maxSamples ?? AUDIO.maxSamples;
    if (!Number.isSafeInteger(maxSamples) || maxSamples < AUDIO.chunkSamples || maxSamples > AUDIO.ceilingSamples) {
      throw new RecorderError('AUDIO_PROTOCOL_ERROR', '模拟输入的会话上限无效');
    }
    const capture: Capture = { callbacks, timer: null, emittedSamples: 0, acknowledgedSamples: 0,
      maxSamples, lastFrameAt: now(), limitReached: false };
    this.state = { phase: 'running', capture };
    try {
      this.emit(capture, AUDIO.chunkSamples, capture.lastFrameAt);
      if (this.current(capture) && !capture.limitReached) this.schedule(capture);
    } catch (error) {
      this.clear(capture);
      throw error;
    }
  }

  acknowledge(totalSamples: number): void {
    if (this.state.phase === 'idle') return;
    const capture = this.state.capture;
    if (!Number.isSafeInteger(totalSamples) || totalSamples < capture.acknowledgedSamples || totalSamples > capture.emittedSamples) {
      this.fail(capture, new RecorderError('AUDIO_PROTOCOL_ERROR', '模拟输入累计确认不匹配'));
      return;
    }
    capture.acknowledgedSamples = totalSamples;
  }

  stop(options: { discard?: boolean } = {}): Promise<void> {
    if (this.state.phase === 'idle') return Promise.resolve();
    const capture = this.state.capture;
    if (options.discard) {
      this.clear(capture);
      return Promise.resolve();
    }
    if (this.state.phase === 'stopping') return this.state.completion;
    if (capture.timer) clearTimeout(capture.timer);
    capture.timer = null;
    const completion = Promise.resolve().then(() => {
      if (this.state.phase !== 'stopping' || this.state.capture !== capture) return;
      try {
        if (!capture.limitReached) {
          const timestamp = Math.max(capture.lastFrameAt, now());
          const tail = Math.min(3999, Math.max(1, Math.floor((timestamp - capture.lastFrameAt) * 16)),
            capture.maxSamples - capture.emittedSamples);
          if (tail > 0) this.emit(capture, tail, timestamp);
        }
      } finally { this.clear(capture); }
    });
    this.state = { phase: 'stopping', capture, completion };
    return completion;
  }

  private current(capture: Capture): boolean {
    return this.state.phase !== 'idle' && this.state.capture === capture;
  }
  private schedule(capture: Capture): void {
    capture.timer = setTimeout(() => {
      capture.timer = null;
      if (this.state.phase !== 'running' || this.state.capture !== capture) return;
      try {
        const samples = Math.min(AUDIO.chunkSamples, capture.maxSamples - capture.emittedSamples);
        this.emit(capture, samples, Math.max(capture.lastFrameAt, now()));
        if (this.state.phase === 'running' && this.state.capture === capture && !capture.limitReached) this.schedule(capture);
      } catch (error) {
        this.fail(capture, error instanceof Error ? error : new Error('模拟输入中断'));
      }
    }, 250);
  }
  private emit(capture: Capture, samples: number, timestamp: number): void {
    if (!this.current(capture)) return;
    if (capture.emittedSamples + samples - capture.acknowledgedSamples > 32_000) {
      throw new RecorderError('AUDIO_BACKPRESSURE', '模拟音频未及时确认，已停止本轮输入');
    }
    capture.emittedSamples += samples;
    capture.lastFrameAt = timestamp;
    capture.callbacks.onChunk(new ArrayBuffer(samples * 2), timestamp);
    if (!this.current(capture)) return;
    capture.callbacks.onLevel(0.18);
    if (capture.emittedSamples === capture.maxSamples && !capture.limitReached) {
      capture.limitReached = true;
      if (capture.timer) clearTimeout(capture.timer);
      capture.timer = null;
      capture.callbacks.onLimit?.();
    }
  }
  private clear(capture: Capture): void {
    if (capture.timer) clearTimeout(capture.timer);
    capture.timer = null;
    if (this.current(capture)) this.state = { phase: 'idle' };
  }
  private fail(capture: Capture, error: Error): void {
    if (!this.current(capture)) return;
    this.clear(capture);
    capture.callbacks.onError?.(error);
  }
}

export { SyntheticCaptureSource as CaptureSource };
