/** Bounded PCM/ACK accounting. No audio is retained by this controller. */
export class AudioFlowError extends Error {
  constructor(readonly code: 'AUDIO_BACKPRESSURE' | 'AUDIO_PROTOCOL_ERROR' | 'AUDIO_LIMIT_EXCEEDED') {
    super(code);
  }
}

interface Flight { samplesThrough: number; sentAt: number; }
export class AudioFlow {
  private frames: Flight[] = [];
  private sent = 0;
  private acknowledged = 0;
  private sequence = -1;
  private shortFrame = false;
  readonly maxUnacknowledgedSamples = 32_000;
  readonly maxUnacknowledgedMs = 2000;

  /** `maxSamples`: this session's limit (V2: server-granted; V1 default 180 s). */
  constructor(readonly maxSamples = 2_880_000) {}

  get sentSamples(): number { return this.sent; }
  get acknowledgedSamples(): number { return this.acknowledged; }
  get lastSequence(): number { return this.sequence; }
  get oldestUnacknowledgedAt(): number | null { return this.frames[0]?.sentAt ?? null; }
  get acknowledgementDeadline(): number | null {
    const oldest = this.oldestUnacknowledgedAt;
    return oldest === null ? null : oldest + this.maxUnacknowledgedMs;
  }

  checkDeadline(now: number): void {
    const deadline = this.acknowledgementDeadline;
    if (deadline !== null && now >= deadline) throw new AudioFlowError('AUDIO_BACKPRESSURE');
  }

  frame(samples: number, now: number): { sequence: number; samples: number; totalSamples: number } {
    this.checkDeadline(now);
    if (!Number.isInteger(samples) || samples < 1 || samples > 4000 || this.shortFrame) {
      throw new AudioFlowError('AUDIO_PROTOCOL_ERROR');
    }
    if (this.sent + samples > this.maxSamples) throw new AudioFlowError('AUDIO_LIMIT_EXCEEDED');
    if (this.sent - this.acknowledged + samples > this.maxUnacknowledgedSamples) {
      throw new AudioFlowError('AUDIO_BACKPRESSURE');
    }
    this.sent += samples;
    this.sequence++;
    this.shortFrame = samples < 4000;
    this.frames.push({ samplesThrough: this.sent, sentAt: now });
    return { sequence: this.sequence, samples, totalSamples: this.sent };
  }

  acknowledge(totalSamples: number): boolean {
    if (!Number.isInteger(totalSamples) || totalSamples < this.acknowledged || totalSamples > this.sent) {
      throw new AudioFlowError('AUDIO_PROTOCOL_ERROR');
    }
    if (totalSamples === this.acknowledged) return false;
    // The protocol ACKs fully checked PCM frames; cumulative ACKs may skip
    // intermediate frames but cannot acknowledge only a fraction of one.
    if (!this.frames.some(frame => frame.samplesThrough === totalSamples)) {
      throw new AudioFlowError('AUDIO_PROTOCOL_ERROR');
    }
    this.acknowledged = totalSamples;
    this.frames = this.frames.filter(frame => frame.samplesThrough > totalSamples);
    return true;
  }

  reset(): void {
    this.frames = [];
    this.sent = 0;
    this.acknowledged = 0;
    this.sequence = -1;
    this.shortFrame = false;
  }
}
