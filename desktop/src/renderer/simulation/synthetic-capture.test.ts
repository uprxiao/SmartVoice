import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SyntheticCaptureSource } from './synthetic-capture';
import type { RecorderCallbacks } from '../audio/recorder-contract';

let elapsed = 0;
beforeEach(() => {
  vi.useFakeTimers();
  elapsed = 0;
  vi.spyOn(performance, 'now').mockImplementation(() => elapsed);
  vi.stubGlobal('navigator', { get mediaDevices() { throw new Error('Hardware must not be queried'); } });
  vi.stubGlobal('AudioContext', class { constructor() { throw new Error('Hardware must not be opened'); } });
  vi.stubGlobal('AudioWorkletNode', class { constructor() { throw new Error('Hardware must not be opened'); } });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

function fixture(acknowledge = true) {
  const source = new SyntheticCaptureSource();
  const frames: Array<{ bytes: Uint8Array; time: number }> = [];
  let total = 0;
  const callbacks: RecorderCallbacks = {
    onChunk: (bytes, time) => {
      frames.push({ bytes: new Uint8Array(bytes), time }); total += bytes.byteLength / 2;
      if (acknowledge) source.acknowledge(total);
    },
    onLevel: vi.fn(), onLimit: vi.fn(), onError: vi.fn(),
  };
  return { source, frames, callbacks };
}

describe('hardware-free capture contract', () => {
  it('immediately emits valid zero PCM, then follows the 250ms clock and ACK credit', async () => {
    const f = fixture(); await f.source.start(f.callbacks);
    expect(f.frames.map(frame => frame.bytes.length)).toEqual([8000]);
    expect(f.frames[0].bytes.every(byte => byte === 0)).toBe(true);
    elapsed = 250; await vi.advanceTimersByTimeAsync(250);
    expect(f.frames.map(frame => frame.bytes.length)).toEqual([8000, 8000]);
    expect(f.frames[1].time - f.frames[0].time).toBe(250);
    expect(f.callbacks.onError).not.toHaveBeenCalled();
    await f.source.stop({ discard: true });
  });

  it('normal stop sends one bounded tail and repeated stop has no duplicate effect', async () => {
    const f = fixture(); await f.source.start(f.callbacks); elapsed = 30;
    const stopped = f.source.stop(); expect(f.source.stop()).toBe(stopped); await stopped;
    expect(f.frames.map(frame => frame.bytes.length)).toEqual([8000, 960]);
    await f.source.stop(); await vi.advanceTimersByTimeAsync(1000);
    expect(f.frames.length).toBe(2); expect(f.source.active).toBe(false);
  });

  it('discard invalidates synchronously, including a pending normal tail, and a fresh capture stays separate', async () => {
    const f = fixture(); await f.source.start(f.callbacks);
    const stopped = f.source.stop(); const discarded = f.source.stop({ discard: true });
    expect(f.source.active).toBe(false);
    await Promise.all([stopped, discarded]);
    const next = vi.fn(); await f.source.start({ onChunk: next, onLevel: vi.fn() });
    await f.source.stop({ discard: true }); await vi.advanceTimersByTimeAsync(2000);
    expect(f.frames.length).toBe(1); expect(next).toHaveBeenCalledTimes(1);
  });

  it('one late timer emits one frame without replaying a suspended clock backlog', async () => {
    const f = fixture(); await f.source.start(f.callbacks);
    elapsed = 60_000; await vi.runOnlyPendingTimersAsync();
    expect(f.frames.length).toBe(2); expect(f.frames[1].time - f.frames[0].time).toBe(60_000);
    await f.source.stop({ discard: true });
  });

  it('bounds outstanding audio and stops after an invalid cumulative ACK', async () => {
    const f = fixture(false); await f.source.start(f.callbacks);
    for (let tick = 1; tick <= 8; tick++) { elapsed = tick * 250; await vi.advanceTimersByTimeAsync(250); }
    expect(f.frames.length).toBe(8);
    expect(f.callbacks.onError).toHaveBeenCalledWith(expect.objectContaining({ code: 'AUDIO_BACKPRESSURE' }));
    expect(f.source.active).toBe(false);
    const g = fixture(false); await g.source.start(g.callbacks); g.source.acknowledge(4001);
    expect(g.callbacks.onError).toHaveBeenCalledWith(expect.objectContaining({ code: 'AUDIO_PROTOCOL_ERROR' }));
    expect(g.source.active).toBe(false);
  });

  it('finishes at the server-granted sample limit exactly once', async () => {
    const f = fixture(); await f.source.start(f.callbacks, { maxSamples: 16_000 });
    for (let tick = 1; tick <= 5; tick++) { elapsed = tick * 250; await vi.advanceTimersByTimeAsync(250); }
    expect(f.frames.reduce((samples, frame) => samples + frame.bytes.length / 2, 0)).toBe(16_000);
    expect(f.callbacks.onLimit).toHaveBeenCalledTimes(1);
    await f.source.stop(); expect(f.frames.length).toBe(4);
  });
});
