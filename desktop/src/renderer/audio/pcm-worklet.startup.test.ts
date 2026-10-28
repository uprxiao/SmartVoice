import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';

interface PortMessage { type: string; code?: string; total_samples?: number; context_time?: number; }
interface RawProcessor {
  process(inputs: Float32Array[][]): boolean;
  port: { onmessage: (event: { data: unknown }) => void };
  inputSamples: number; totalSamples: number; sentSamples: number; acknowledgedSamples: number;
  frames: unknown[]; filled: number; lastInputTime: number | null;
}
/** Raw constructor only: no automatic prepare/begin and no synthetic capture state. */
function rawProcessor(rate = 16000) {
  const sent: PortMessage[] = [];
  let Registered!: new () => RawProcessor;
  class Base { port = { onmessage: (_event: { data: unknown }) => undefined,
    postMessage: (message: PortMessage) => { sent.push(message); } }; }
  const clock = { AudioWorkletProcessor: Base, Float32Array, Float64Array, Int16Array,
    registerProcessor: (_name: string, value: typeof Registered) => { Registered = value; },
    currentTime: 0, sampleRate: rate };
  runInNewContext(readFileSync(resolve('src/renderer/public/pcm-worklet.js'), 'utf8'), clock);
  const processor = new Registered();
  const command = (type: string, extra: Record<string, unknown> = {}) => processor.port.onmessage({ data: { type, ...extra } });
  const quantum = (frame: number, samples = 128, amplitude = 0) => {
    clock.currentTime = frame / rate;
    return processor.process([[new Float32Array(samples).fill(amplitude)]]);
  };
  const errors = () => sent.filter(message => message.type === 'error');
  const output = () => sent.filter(message => ['pcm', 'level', 'limit', 'flushed'].includes(message.type));
  return { processor, sent, clock, command, quantum, errors, output };
}

describe('explicit capture boundary, without any automatic startup helper', () => {
  it('raw initial frame 0 then 256 captures nothing before prepare/begin', () => {
    const h = rawProcessor();
    expect(h.quantum(0)).toBe(true);
    expect(h.quantum(256)).toBe(true);
    expect(h.processor.inputSamples).toBe(0);
    expect(h.errors()).toEqual([]); expect(h.output()).toEqual([]);
    h.command('discard');
  });

  it('preparing resets a discontinuous prefix, then digital silence becomes ready without audio or credit', () => {
    const h = rawProcessor(); h.command('prepare');
    h.quantum(0); h.quantum(256);
    expect(h.sent).toEqual([]);
    h.quantum(384);
    expect(h.sent).toEqual([{ type: 'ready' }]);
    expect(h.output()).toEqual([]);
    expect(h.processor.inputSamples).toBe(0); expect(h.processor.totalSamples).toBe(0);
    expect(h.processor.sentSamples).toBe(0); expect(h.processor.acknowledgedSamples).toBe(0);
    expect(h.processor.filled).toBe(0); expect(h.processor.frames).toEqual([]);
    h.command('discard'); expect(h.output()).toEqual([]); expect(h.errors()).toEqual([]);
  });

  it('preparation consumes neither the capture duration nor the ACK age budget', () => {
    const h = rawProcessor(); h.command('prepare'); h.quantum(0);
    expect(h.quantum(181 * 16000)).toBe(true); // Still preparing; restart its streak only.
    expect(h.quantum(181 * 16000 + 128)).toBe(true);
    expect(h.sent).toEqual([{ type: 'ready' }]);
    expect(h.processor.inputSamples).toBe(0); expect(h.processor.totalSamples).toBe(0);
    expect(h.processor.sentSamples).toBe(0); expect(h.processor.acknowledgedSamples).toBe(0);
    expect(h.output()).toEqual([]); expect(h.errors()).toEqual([]); h.command('discard');
  });

  it('ready keeps the most recent boundary until begin, and begun starts at that real quantum', () => {
    const h = rawProcessor(); h.command('prepare'); h.quantum(0); h.quantum(128);
    h.quantum(256); h.quantum(384); // Ready is not a stale one-time timestamp.
    expect(h.processor.inputSamples).toBe(0); expect(h.output()).toEqual([]);
    h.command('begin'); h.quantum(512);
    expect(h.sent).toEqual([{ type: 'ready' }, { type: 'begun', context_time: 512 / 16000 }]);
    expect(h.processor.inputSamples).toBe(128);
    h.command('stop');
    expect(h.sent.at(-1)).toEqual({ type: 'flushed', total_samples: 128 });
    expect(h.sent.find(message => message.type === 'pcm')?.context_time).toBe(512 / 16000);
  });

  it.each(['ready', 'begin_pending'])('a missing quantum after %s is rejected rather than rebased', state => {
    const h = rawProcessor(); h.command('prepare'); h.quantum(0); h.quantum(128);
    if (state === 'begin_pending') h.command('begin');
    expect(h.quantum(384)).toBe(false);
    expect(h.errors()).toEqual([expect.objectContaining({ code: 'AUDIO_INTERRUPTED' })]);
    expect(h.sent.some(message => message.type === 'begun')).toBe(false);
    expect(h.processor.inputSamples).toBe(0); expect(h.output()).toEqual([]);
  });

  it('the same gap after begun still fails the original continuity guard', () => {
    const h = rawProcessor(); h.command('prepare'); h.quantum(0); h.quantum(128);
    h.command('begin'); h.quantum(256);
    expect(h.processor.inputSamples).toBe(128);
    expect(h.quantum(512)).toBe(false);
    expect(h.errors()).toEqual([expect.objectContaining({ code: 'AUDIO_INTERRUPTED' })]);
    expect(h.processor.inputSamples).toBe(128);
  });

  it.each(['unprepared', 'preparing', 'ready', 'begin_pending'])('stop during %s emits no tail or completion and cannot be revived', state => {
    const h = rawProcessor();
    if (state !== 'unprepared') h.command('prepare');
    if (state === 'ready' || state === 'begin_pending') { h.quantum(0); h.quantum(128); }
    if (state === 'begin_pending') h.command('begin');
    h.command('stop'); h.command('prepare'); h.command('begin');
    expect(h.quantum(256)).toBe(false); expect(h.output()).toEqual([]);
    expect(h.processor.inputSamples).toBe(0); expect(h.errors()).toEqual([]);
  });

  it('non-finite preparation input fails rather than qualifying as silence', () => {
    const h = rawProcessor(); h.command('prepare');
    expect(h.quantum(0, 128, Number.NaN)).toBe(false);
    expect(h.errors()).toEqual([expect.objectContaining({ code: 'AUDIO_PROTOCOL_ERROR' })]);
    expect(h.processor.inputSamples).toBe(0); expect(h.output()).toEqual([]);
  });
});
