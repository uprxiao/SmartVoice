import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
interface Message { type: string; buffer?: ArrayBuffer; samples?: number; total_samples?: number; context_time?: number; code?: string; }
interface Processor {
  process(inputs: Float32Array[][]): boolean;
  port: { onmessage: (event: unknown) => void };
  totalSamples: number;
  inputSamples: number;
  capturedOutputSamples(): number;
}
function preparedWorklet(rate = 16000) {
  const sent: Message[] = [];
  let latestTotal = 0, lastAcknowledged = 0;
  let Registered!: new () => Processor;
  class Base { port = { postMessage: (value: Message) => {
    sent.push(value); if (value.type === 'pcm') latestTotal = value.total_samples!;
  }, onmessage: (_event: unknown) => undefined }; }
  const clock = { AudioWorkletProcessor: Base,
    registerProcessor: (_name: string, value: typeof Registered) => { Registered = value; },
    Float32Array, Int16Array, currentTime: 0, sampleRate: rate };
  runInNewContext(readFileSync(resolve('src/renderer/public/pcm-worklet.js'), 'utf8'), clock);
  const processor = new Registered();
  const message = (data: unknown) => processor.port.onmessage({ data });
  const frames = () => sent.filter(event => event.type === 'pcm');
  const acknowledgeLatest = () => {
    if (latestTotal !== lastAcknowledged) {
      message({ type: 'ack', total_samples: latestTotal }); lastAcknowledged = latestTotal;
    }
  };
  const feed = (samples: number, acknowledge = false) => {
    let remaining = samples;
    while (remaining > 0) {
      const size = Math.min(128, remaining);
      const active = processor.process([[new Float32Array(size).fill(0.5)]]);
      remaining -= size; clock.currentTime += size / rate;
      if (acknowledge) acknowledgeLatest();
      if (!active) return false;
    }
    return true;
  };
  const feedChannels = (channels: Float32Array[], block = 128, acknowledge = true) => {
    for (let start = 0; start < channels[0].length; start += block) {
      const size = Math.min(block, channels[0].length - start);
      const active = processor.process([channels.map(channel => channel.subarray(start, start + size))]);
      clock.currentTime += size / rate;
      if (acknowledge) acknowledgeLatest();
      if (!active) return false;
    }
    return true;
  };
  // Existing DSP/credit tests explicitly prepare two silent quanta, then arm
  // begin. Only their first real feed quantum starts capture. Raw startup
  // counterexamples live in pcm-worklet.startup.test.ts and bypass this helper.
  message({ type: 'prepare' });
  processor.process([[new Float32Array(128)]]); clock.currentTime = 128 / rate;
  processor.process([[new Float32Array(128)]]); clock.currentTime = 256 / rate;
  message({ type: 'begin' });
  const captureOrigin = clock.currentTime;
  return { processor, sent, clock, message, frames, feed, feedChannels, captureOrigin };
}
const events = (messages: Message[]) => messages.filter(value => !['level', 'ready', 'begun'].includes(value.type));

describe('worklet end-to-end server ACK credit and original capture time', () => {
  it('emits the short tail before flushed, preserving its sample time even after delayed stop', () => {
    const h = preparedWorklet(); h.feed(4123);
    h.clock.currentTime = h.captureOrigin + 1.5;
    h.message({ type: 'stop' });
    expect(events(h.sent).map(event => event.type)).toEqual(['pcm', 'pcm', 'flushed']);
    expect(h.frames().map(event => event.buffer!.byteLength)).toEqual([8000, 246]);
    expect(h.frames()[0].context_time).toBe(h.captureOrigin);
    expect(h.frames()[1].context_time).toBeCloseTo(h.captureOrigin + 0.25, 8);
    expect(events(h.sent).at(-1)).toEqual({ type: 'flushed', total_samples: 4123 });
    const count = h.sent.length;
    expect(h.processor.process([[new Float32Array(128)]])).toBe(false);
    h.message({ type: 'stop' }); expect(h.sent).toHaveLength(count);
  });

  it('stop before the first begun quantum discards preparation without PCM or flushed', () => {
    const h = preparedWorklet(); h.message({ type: 'stop' });
    expect(events(h.sent)).toEqual([]);
    expect(h.processor.process([])).toBe(false);
  });

  it('never captures more than 32000 outstanding samples, including a partial frame', () => {
    const h = preparedWorklet();
    expect(h.feed(31_999)).toBe(true);
    expect(h.processor.totalSamples).toBe(31_999);
    expect(h.frames()).toHaveLength(7);
    expect(h.feed(2)).toBe(false);
    expect(h.processor.totalSamples).toBe(32_000);
    expect(h.frames()).toHaveLength(8);
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_BACKPRESSURE' });
    expect(h.processor.process([[new Float32Array(4000)]])).toBe(false);
    expect(h.frames()).toHaveLength(8);
  });

  it('only cumulative server ACK replenishes credit, allowing a long recording with bounded backlog', () => {
    const h = preparedWorklet();
    expect(h.feed(160_000, true)).toBe(true);
    expect(h.processor.totalSamples).toBe(160_000);
    expect(h.sent.some(event => event.type === 'error')).toBe(false);
    h.message({ type: 'stop' });
    expect(h.frames()).toHaveLength(40);
  });

  it('cumulative ACK does not reset the next frame original two-second deadline', () => {
    const h = preparedWorklet(); h.feed(8000);
    h.clock.currentTime = h.captureOrigin + 1; h.message({ type: 'ack', total_samples: 4000 });
    h.clock.currentTime = h.captureOrigin + 2.249;
    expect(h.processor.process([])).toBe(true);
    h.clock.currentTime = h.captureOrigin + 2.251;
    expect(h.processor.process([])).toBe(false);
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_BACKPRESSURE' });
  });

  it('an unsent short partial frame also expires when input stops arriving', () => {
    const h = preparedWorklet(); h.feed(128);
    h.clock.currentTime = h.captureOrigin + 2;
    expect(h.processor.process([])).toBe(false);
    expect(h.frames()).toHaveLength(0);
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_BACKPRESSURE' });
    h.message({ type: 'stop' });
    expect(h.sent.some(event => event.type === 'flushed')).toBe(false);
  });

  it('an overdue ACK cannot retroactively erase a timeout', () => {
    const h = preparedWorklet(); h.feed(4000);
    h.clock.currentTime = h.captureOrigin + 2; h.message({ type: 'ack', total_samples: 4000 });
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_BACKPRESSURE' });
  });

  it.each([3999, 8001, -1, 0.5, Number.NaN])('rejects invalid cumulative ACK %s', total => {
    const h = preparedWorklet(); h.feed(8000); h.message({ type: 'ack', total_samples: total });
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_PROTOCOL_ERROR' });
    expect(h.processor.process([])).toBe(false);
  });

  it('rejects a backwards ACK while accepting an identical replay', () => {
    const h = preparedWorklet(); h.feed(8000);
    h.message({ type: 'ack', total_samples: 8000 }); h.message({ type: 'ack', total_samples: 8000 });
    expect(h.sent.some(event => event.type === 'error')).toBe(false);
    h.message({ type: 'ack', total_samples: 4000 });
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_PROTOCOL_ERROR' });
  });

  it('cuts precisely at 2880000 samples and sends the last PCM before one limit/flushed pair', () => {
    const h = preparedWorklet();
    expect(h.feed(2_880_128, true)).toBe(false);
    expect(h.processor.totalSamples).toBe(2_880_000);
    expect(h.frames()).toHaveLength(720);
    expect(h.frames().reduce((sum, event) => sum + event.samples!, 0)).toBe(2_880_000);
    expect(h.frames().every(event => event.buffer!.byteLength === 8000)).toBe(true);
    expect(events(h.sent).slice(-3).map(event => event.type)).toEqual(['pcm', 'limit', 'flushed']);
    expect(events(h.sent).slice(-2)).toEqual([
      { type: 'limit', total_samples: 2_880_000 }, { type: 'flushed', total_samples: 2_880_000 },
    ]);
    const count = h.sent.length; h.message({ type: 'stop' }); h.processor.process([[new Float32Array(128)]]);
    expect(h.sent).toHaveLength(count);
  }, 60_000);

  it('discard drops the pending tail and never flushes it later', () => {
    const h = preparedWorklet(); h.feed(4123); const count = h.frames().length;
    h.message({ type: 'discard' }); h.message({ type: 'stop' });
    expect(h.processor.process([])).toBe(false);
    expect(h.frames()).toHaveLength(count);
    expect(h.sent.some(event => event.type === 'flushed')).toBe(false);
  });

  it('fails closed on a wrong sample rate or non-finite input', () => {
    const rate = preparedWorklet(96000); rate.feed(128);
    expect(rate.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_PROTOCOL_ERROR' });
    const h = preparedWorklet(); h.processor.process([[new Float32Array([Number.NaN])]]);
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_PROTOCOL_ERROR' });
    expect(h.processor.totalSamples).toBe(0);
  });
});

function encode(rate: number, channels: Float32Array[], block: number) {
  const h = preparedWorklet(rate); h.feedChannels(channels, block); h.message({ type: 'stop' });
  expect(h.sent.some(event => event.type === 'error')).toBe(false);
  const pcm = Int16Array.from(h.frames().flatMap(frame => Array.from(new Int16Array(frame.buffer!))));
  return { ...h, pcm };
}
const tone = (rate: number, hz: number, count = rate, amplitude = 0.5) =>
  Float32Array.from({ length: count }, (_, i) => amplitude * Math.sin(2 * Math.PI * hz * i / rate));
const rms = (pcm: Int16Array) => {
  const middle = pcm.subarray(160, pcm.length - 160);
  return Math.sqrt(middle.reduce((sum, value) => sum + (value / 32768) ** 2, 0) / middle.length);
};

describe('hardware-rate resampling inherited from SmartDD windowed-sinc', () => {
  it.each([16000, 44100, 48000])('%i Hz phase is identical across 1/127/128/129/large input blocks', rate => {
    const source = tone(rate, 731, rate + 123);
    const whole = encode(rate, [source], source.length);
    expect(whole.pcm.length).toBe(Math.ceil(source.length * 16000 / rate));
    for (const block of [1, 127, 128, 129]) {
      const split = encode(rate, [source], block);
      expect(split.pcm).toEqual(whole.pcm);
      expect(split.frames().map(frame => frame.total_samples)).toEqual(whole.frames().map(frame => frame.total_samples));
      split.frames().forEach((frame, index) => expect(frame.context_time).toBeCloseTo(split.captureOrigin + index * 0.25, 7));
    }
  });

  it.each([16000, 44100, 48000])('%i Hz short tails use ceil(input*16000/rate), including sub-quantum recordings', rate => {
    for (const count of [0, 1, 2, 127, 128, 129, 1001]) {
      const source = new Float32Array(count).fill(0.5);
      const h = encode(rate, [source], 128);
      expect(h.pcm.length).toBe(Math.ceil(count * 16000 / rate));
      expect(h.pcm.every(sample => Math.abs(sample - 16384) <= 1)).toBe(true);
      expect(h.frames().every(frame => frame.samples! > 0 && frame.samples! <= 4000)).toBe(true);
      if (count === 0) expect(events(h.sent)).toEqual([]);
      else expect(events(h.sent).at(-1)).toEqual({ type: 'flushed', total_samples: h.pcm.length });
    }
  });

  it.each([44100, 48000])('%i Hz stop drains filter lookahead, retaining a final short-tail impulse', rate => {
    const input = new Float32Array(1001); input[input.length - 1] = 1;
    const h = preparedWorklet(rate); h.feedChannels([input]);
    const before = h.processor.totalSamples;
    h.clock.currentTime = h.captureOrigin + 1; // Waiting to stop must not change sample timestamps.
    h.message({ type: 'stop' });
    const pcm = Int16Array.from(h.frames().flatMap(frame => Array.from(new Int16Array(frame.buffer!))));
    expect(pcm.length).toBe(Math.ceil(input.length * 16000 / rate));
    expect(pcm.length).toBeGreaterThan(before);
    expect(pcm.subarray(pcm.length - 12).some(value => value !== 0)).toBe(true);
    expect(h.frames()[0].context_time).toBe(h.captureOrigin);
  });

  it.each([16000, 44100, 48000])('%i Hz preserves 1 kHz pitch and low-band gain', rate => {
    const { pcm } = encode(rate, [tone(rate, 1000)], 128);
    expect(rms(pcm)).toBeGreaterThan(0.345); expect(rms(pcm)).toBeLessThan(0.362);
    const data = pcm.subarray(160, pcm.length - 160);
    let sine = 0, cosine = 0;
    data.forEach((sample, n) => {
      sine += sample / 32768 * Math.sin(2 * Math.PI * 1000 * n / 16000);
      cosine += sample / 32768 * Math.cos(2 * Math.PI * 1000 * n / 16000);
    });
    expect(2 * Math.hypot(sine, cosine) / data.length).toBeGreaterThan(0.485);
  });

  it.each([44100, 48000])('%i Hz attenuates 12 kHz energy before 16 kHz decimation', rate => {
    const low = encode(rate, [tone(rate, 1000)], 128);
    const high = encode(rate, [tone(rate, 12000)], 128);
    expect(rms(high.pcm)).toBeLessThan(rms(low.pcm) * 0.1);
  });

  it.each([16000, 44100, 48000])('%i Hz stereo is the arithmetic mean of its channels', rate => {
    const left = tone(rate, 1000, rate, 0.6), right = tone(rate, 2000, rate, 0.2);
    const mean = Float32Array.from(left, (value, index) => (value + right[index]) / 2);
    const stereo = encode(rate, [left, right], 128), mono = encode(rate, [mean], 129);
    expect(stereo.pcm.length).toBe(mono.pcm.length);
    expect(stereo.pcm.every((sample, index) => Math.abs(sample - mono.pcm[index]) <= 1)).toBe(true);
    const cancelled = encode(rate, [left, Float32Array.from(left, value => -value)], 128);
    expect(cancelled.pcm.every(sample => sample === 0)).toBe(true);
  });

  it.each([44100, 48000])('%i Hz filter lookahead also consumes the 32000-sample credit', rate => {
    const h = preparedWorklet(rate);
    expect(h.feed(rate * 2 - 1)).toBe(true);
    expect(h.processor.capturedOutputSamples()).toBe(32000);
    expect(h.processor.totalSamples).toBeLessThan(32000);
    expect(h.feed(129)).toBe(false);
    expect(h.processor.inputSamples).toBeLessThanOrEqual(rate * 2);
    expect(h.sent.at(-1)).toMatchObject({ type: 'error', code: 'AUDIO_BACKPRESSURE' });
  });

  it.each([44100, 48000])('%i Hz captures exactly 180 seconds before the output limit handshake', rate => {
    const h = preparedWorklet(rate);
    expect(h.feed(rate * 180 + 128, true)).toBe(false);
    expect(h.processor.inputSamples).toBe(rate * 180);
    expect(h.frames().reduce((sum, frame) => sum + frame.samples!, 0)).toBe(2_880_000);
    expect(events(h.sent).slice(-3).map(event => event.type)).toEqual(['pcm', 'limit', 'flushed']);
    expect(h.sent.some(event => event.type === 'error')).toBe(false);
  }, 60_000);
});
