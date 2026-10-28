import { describe, expect, it } from 'vitest';
import { AudioFlow } from './audio-flow';

describe('PCM backpressure and cumulative ACK boundaries (VOICE-16/17/23)', () => {
  it('accepts exactly two seconds in flight and refuses the next sample', () => {
    const flow = new AudioFlow();
    for (let i = 0; i < 8; i++) flow.frame(4000, i * 10);
    expect(flow.sentSamples).toBe(32000);
    expect(() => flow.frame(1, 80)).toThrow('AUDIO_BACKPRESSURE');
    expect(flow.sentSamples).toBe(32000);
  });
  it('a repeated ACK cannot extend the oldest outstanding deadline', () => {
    const flow = new AudioFlow(); flow.frame(4000, 0); flow.frame(4000, 250);
    expect(flow.acknowledge(0)).toBe(false);
    expect(flow.acknowledgementDeadline).toBe(2000);
    expect(() => flow.checkDeadline(2000)).toThrow('AUDIO_BACKPRESSURE');
  });
  it('an advancing ACK keeps the actual age of the next frame', () => {
    const flow = new AudioFlow(); flow.frame(4000, 0); flow.frame(4000, 250);
    flow.acknowledge(4000);
    expect(flow.acknowledgementDeadline).toBe(2250);
    expect(() => flow.checkDeadline(2249)).not.toThrow();
    expect(() => flow.checkDeadline(2250)).toThrow('AUDIO_BACKPRESSURE');
  });
  it('rejects backward, fractional, beyond-sent, and partial-frame ACKs', () => {
    const flow = new AudioFlow(); flow.frame(4000, 10); flow.frame(4000, 20); flow.acknowledge(4000);
    for (const ack of [0, 4000.5, 8001, 5000, Number.NaN]) {
      expect(() => flow.acknowledge(ack)).toThrow('AUDIO_PROTOCOL_ERROR');
    }
    expect(flow.acknowledgedSamples).toBe(4000);
  });
  it('coalesces ACKs and removes the timer once all samples are acknowledged', () => {
    const flow = new AudioFlow(); flow.frame(4000, 10); flow.frame(4000, 20); flow.frame(4000, 30);
    flow.acknowledge(12000); expect(flow.acknowledgementDeadline).toBeNull();
    expect(() => flow.checkDeadline(100000)).not.toThrow();
  });
  it('accepts a nonempty short final frame without inventing samples or a later full frame', () => {
    const flow = new AudioFlow(); flow.frame(4000, 0); const tail = flow.frame(123, 250);
    expect(tail).toEqual({ sequence: 1, samples: 123, totalSamples: 4123 });
    expect(() => flow.frame(4000, 500)).toThrow('AUDIO_PROTOCOL_ERROR');
    flow.acknowledge(4123); expect(flow.acknowledgementDeadline).toBeNull();
  });
  it('reset removes old accounting from a new session', () => {
    const flow = new AudioFlow(); flow.frame(123, 0); flow.reset();
    expect(flow.frame(4000, 3000)).toEqual({ sequence: 0, samples: 4000, totalSamples: 4000 });
  });
});
