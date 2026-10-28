import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
// TC-V2-DESK-04: per-session limit (360 s) in the AudioWorklet. Synthetic tone only.
interface Message { type: string; samples?: number; total_samples?: number; code?: string; }
interface Processor { process(inputs: Float32Array[][]): boolean; port: { onmessage: (event: unknown) => void }; }

function worklet(rate: number, begin: Record<string, unknown>) {
  const sent: Message[] = [];
  let Registered!: new () => Processor;
  class Base { port = { postMessage: (value: Message) => { sent.push(value); }, onmessage: (_event: unknown) => undefined }; }
  const clock = { AudioWorkletProcessor: Base, registerProcessor: (_n: string, v: typeof Registered) => { Registered = v; },
    Float32Array, Int16Array, currentTime: 0, sampleRate: rate };
  runInNewContext(readFileSync(resolve('src/renderer/public/pcm-worklet.js'), 'utf8'), clock);
  const processor = new Registered();
  const message = (data: unknown) => processor.port.onmessage({ data });
  message({ type: 'prepare' });
  processor.process([[new Float32Array(128)]]); clock.currentTime = 128 / rate;
  processor.process([[new Float32Array(128)]]); clock.currentTime = 256 / rate;
  message({ type: 'begin', ...begin });
  let acknowledged = 0;
  const run = (seconds: number) => {
    const block = new Float32Array(128).fill(0.25);
    const quanta = Math.ceil(seconds * rate / 128);
    for (let n = 0; n < quanta; n++) {
      const active = processor.process([[block]]);
      clock.currentTime += 128 / rate;
      const pcm = sent.filter(m => m.type === 'pcm');
      const last = pcm[pcm.length - 1]?.total_samples ?? 0;
      if (last !== acknowledged) { message({ type: 'ack', total_samples: last }); acknowledged = last; }
      if (!active) return n;
    }
    return quanta;
  };
  return { sent, run };
}

describe('TC-V2-DESK-04 360 s session limit in the worklet', () => {
  // Resampling 360 s of 44.1/48 kHz input takes ~12 s here; allow it explicitly.
  it.each([16000, 44100, 48000])('emits exactly 5,760,000 samples with contiguous frames at %i Hz', { timeout: 60_000 }, rate => {
    const h = worklet(rate, { max_samples: 5_760_000 });
    h.run(361);
    const pcm = h.sent.filter(m => m.type === 'pcm');
    const totals = pcm.map(m => m.total_samples!);
    expect(totals.every(Number.isSafeInteger)).toBe(true);
    expect(totals.every((t, i) => i === 0 || t - totals[i - 1] === pcm[i].samples)).toBe(true);
    expect(totals[totals.length - 1]).toBe(5_760_000);
    expect(pcm.length).toBe(1440);
    expect(pcm.every(m => m.samples === 4000)).toBe(true); // 360 s divides into whole frames
    expect(h.sent.filter(m => m.type === 'limit')).toEqual([{ type: 'limit', total_samples: 5_760_000 }]);
  });

  // This also processes a full recording; hosted runners can exceed Vitest's
  // default 5 s. Keep the same execution allowance as the 360 s cases above.
  it('without an explicit limit keeps the V1 180 s ceiling', { timeout: 60_000 }, () => {
    const h = worklet(16000, {});
    h.run(181);
    expect(h.sent.filter(m => m.type === 'limit')).toEqual([{ type: 'limit', total_samples: 2_880_000 }]);
  });

  it.each([5_760_001, 6_400_000, 1000, 16001, -16000, 1.5])('rejects an invalid limit %s', value => {
    const h = worklet(16000, { max_samples: value });
    expect(h.sent.some(m => m.type === 'error' || m.code === 'AUDIO_PROTOCOL_ERROR')).toBe(true);
  });
});
