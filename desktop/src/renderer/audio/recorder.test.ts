import { afterEach, describe, expect, it, vi } from 'vitest';
import { PcmRecorder, RecorderError } from './recorder';
const deferred = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
interface FakeNode {
  port: { onmessage: ((event: { data: unknown }) => void) | null; postMessage: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn> };
  disconnect: ReturnType<typeof vi.fn>;
  onprocessorerror: (() => void) | null;
}
function environment(autoFlush = true, sourceRate = 16000, preparationHandshake = false) {
  const stopped = vi.fn(); const track = { stop: stopped, onended: null as (() => void) | null, readyState: 'live' };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
  const getUserMedia = vi.fn().mockResolvedValue(stream);
  const timeline: string[] = [], nodes: FakeNode[] = [], contexts: Context[] = [];
  const contextOptions: AudioContextOptions[] = [], nodeOptions: AudioWorkletNodeOptions[] = [];
  const clock = { now: 5000 };
  let emitted = 0, nextFrameTime = 10, tail = 3;
  let resumeGate: Promise<void> = Promise.resolve();
  const source = { connect: vi.fn(() => { timeline.push('connect'); }), disconnect: vi.fn() };
  class Context {
    sampleRate = sourceRate; currentTime = 10; state = 'running'; onstatechange: (() => void) | null = null;
    audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
    constructor(options?: AudioContextOptions) { contexts.push(this); contextOptions.push(options ?? {}); }
    createMediaStreamSource() { return source; }
    resume = vi.fn(async () => { timeline.push('resume'); await resumeGate; timeline.push('resumed'); });
    close = vi.fn().mockResolvedValue(undefined);
  }
  const emit = (data: unknown, node = nodes.at(-1)!) => node.port.onmessage?.({ data });
  const pcm = (samples = 4000, time = nextFrameTime, overrides: Record<string, unknown> = {}) => {
    emitted += samples; nextFrameTime = time + samples / 16000;
    emit({ type: 'pcm', buffer: new ArrayBuffer(samples * 2), samples, total_samples: emitted, context_time: time, ...overrides });
  };
  class Node implements FakeNode {
    port = { onmessage: null as ((event: { data: unknown }) => void) | null, close: vi.fn(),
      postMessage: vi.fn((message: { type: string }) => {
        if (message.type === 'prepare' && preparationHandshake) queueMicrotask(() => emit({ type: 'ready' }, this));
        if (message.type === 'begin' && preparationHandshake) queueMicrotask(() => emit({ type: 'begun', context_time: contexts.at(-1)!.currentTime }, this));
        if (message.type === 'stop' && autoFlush) queueMicrotask(() => {
          if (tail > 0) pcm(tail);
          emit({ type: 'flushed', total_samples: emitted }, this);
        });
      }) };
    disconnect = vi.fn(); onprocessorerror: (() => void) | null = null;
    constructor(_context: unknown, _name: string, options?: AudioWorkletNodeOptions) { nodes.push(this); nodeOptions.push(options ?? {}); }
  }
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } });
  vi.stubGlobal('window', { location: { href: 'http://127.0.0.1/recorder.html' } });
  vi.stubGlobal('performance', { timeOrigin: 100_000, now: () => clock.now });
  vi.stubGlobal('AudioContext', Context); vi.stubGlobal('AudioWorkletNode', Node);
  return { stream, track, stopped, getUserMedia, source, timeline, clock, nodes, contexts, contextOptions, nodeOptions, pcm, emit,
    setResume(promise: Promise<void>) { resumeGate = promise; }, setTail(samples: number) { tail = samples; },
    get node() { return nodes.at(-1)!; }, get context() { return contexts.at(-1)!; } };
}
// Legacy Recorder tests explicitly use a port fixture that replies ready and
// begun; the preparation/cancellation tests below use the manual environment.
const environmentWithReadyBegin = (autoFlush = true, sourceRate = 16000) => environment(autoFlush, sourceRate, true);
const callbacks = () => ({ onChunk: vi.fn(), onLevel: vi.fn(), onLimit: vi.fn(), onError: vi.fn() });
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('capture lifetime, original sample time and final-frame handshake', () => {
  it('identifies a denied microphone without creating audio resources and permits an explicit retry', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(), cb = callbacks();
    env.getUserMedia.mockRejectedValueOnce(new DOMException('Permission denied', 'NotAllowedError'));
    await expect(recorder.start(cb)).rejects.toMatchObject({ code: 'MICROPHONE_PERMISSION_DENIED' });
    expect(recorder.active).toBe(false); expect(env.contexts).toHaveLength(0);
    expect(cb.onChunk).not.toHaveBeenCalled(); expect(env.getUserMedia).toHaveBeenCalledTimes(1);
    await recorder.start(cb);
    expect(recorder.active).toBe(true); expect(env.getUserMedia).toHaveBeenCalledTimes(2);
    await recorder.stop({ discard: true });
  });

  it('preserves non-permission device errors without misreporting microphone denial', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder();
    const error = new DOMException('Device busy', 'NotReadableError');
    env.getUserMedia.mockRejectedValueOnce(error);
    await expect(recorder.start(callbacks())).rejects.toBe(error);
    expect(recorder.active).toBe(false); expect(env.contexts).toHaveLength(0);
  });

  it.each([44100, 48000])('accepts actual %i Hz hardware without forcing 16 kHz or enabling speech suppression', async rate => {
    const env = environmentWithReadyBegin(true, rate), recorder = new PcmRecorder(), cb = callbacks();
    await recorder.start(cb);
    expect(env.contextOptions[0].sampleRate).toBeUndefined();
    expect(env.getUserMedia).toHaveBeenCalledWith({ audio: { channelCount: 1,
      echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    expect(env.nodeOptions[0]).toMatchObject({ channelCountMode: 'max', channelInterpretation: 'discrete' });
    env.pcm(4000, 10);
    expect(cb.onChunk.mock.calls[0][1]).toBe(105_000);
    expect(cb.onError).not.toHaveBeenCalled();
    await recorder.stop({ discard: true });
  });

  it('releases an unsupported-rate device with an explicit error', async () => {
    const env = environmentWithReadyBegin(true, 96000), recorder = new PcmRecorder();
    await expect(recorder.start(callbacks())).rejects.toThrow('采样率');
    expect(recorder.active).toBe(false); expect(env.stopped).toHaveBeenCalledOnce();
  });

  it('releases a stream acquired after cancellation without resurrecting the recorder', async () => {
    const env = environmentWithReadyBegin(), pending = deferred<MediaStream>(), cb = callbacks();
    env.getUserMedia.mockReturnValueOnce(pending.promise);
    const recorder = new PcmRecorder();
    const starting = recorder.start(cb), rejected = expect(starting).rejects.toThrow('取消');
    await recorder.stop({ discard: true }); pending.resolve(env.stream); await rejected;
    expect(recorder.active).toBe(false); expect(env.stopped).toHaveBeenCalled(); expect(cb.onChunk).not.toHaveBeenCalled();
  });

  it('a cancelled permission request cannot close a newer successful capture', async () => {
    const env = environmentWithReadyBegin(), pending = deferred<MediaStream>(), recorder = new PcmRecorder();
    env.getUserMedia.mockReturnValueOnce(pending.promise);
    const old = recorder.start(callbacks()).catch(error => error);
    await recorder.stop({ discard: true });
    await recorder.start(callbacks());
    const newContext = env.context, newNode = env.node;
    const lateStop = vi.fn();
    pending.resolve({ getTracks: () => [{ stop: lateStop }] } as unknown as MediaStream); await old;
    expect(recorder.active).toBe(true);
    expect(newContext.close).not.toHaveBeenCalled(); expect(newNode.port.close).not.toHaveBeenCalled();
    expect(lateStop).toHaveBeenCalledOnce(); expect(env.stopped).not.toHaveBeenCalled();
    await recorder.stop({ discard: true });
  });

  it('connects the graph before resume and maps the sampling clock once after resume', async () => {
    const env = environmentWithReadyBegin(), resumed = deferred<void>(), recorder = new PcmRecorder(), cb = callbacks();
    env.setResume(resumed.promise);
    const start = recorder.start(cb); await flush();
    expect(env.source.connect).toHaveBeenCalledOnce();
    env.context.currentTime = 20; env.clock.now = 9000;
    resumed.resolve(); await start;
    expect(env.timeline).toEqual(['connect', 'resume', 'resumed']);
    env.clock.now = 19_000; // Port delivery may be late; do not restamp its frame.
    env.pcm(4000, 20);
    expect(cb.onChunk.mock.calls[0][1]).toBe(109_000);
    await recorder.stop({ discard: true });
  });

  it('awaits flushed after the final PCM and preserves the tail timestamp', async () => {
    const env = environmentWithReadyBegin(), order: string[] = [], recorder = new PcmRecorder(), times: number[] = [];
    await recorder.start({ ...callbacks(), onChunk: (_buffer, at) => { order.push('pcm'); times.push(at); } });
    env.pcm(4000, 10); env.clock.now = 25_000;
    await recorder.stop(); order.push('end');
    expect(order).toEqual(['pcm', 'pcm', 'end']); expect(times).toEqual([105_000, 105_250]);
    expect(env.node.port.postMessage).toHaveBeenCalledWith({ type: 'stop' }); expect(env.stopped).toHaveBeenCalled();
  });

  it('only increasing cumulative server ACK is passed to the worklet', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(); await recorder.start(callbacks());
    env.pcm(); env.pcm();
    recorder.acknowledge(4000); recorder.acknowledge(4000); recorder.acknowledge(8000);
    expect(env.node.port.postMessage.mock.calls.filter(([message]) => message.type === 'ack')).toEqual([
      [{ type: 'ack', total_samples: 4000 }], [{ type: 'ack', total_samples: 8000 }],
    ]);
    await recorder.stop({ discard: true });
  });

  it.each([8001, -1, 1.5, Number.NaN])('invalid ACK %s fails closed', async total => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb); env.pcm();
    recorder.acknowledge(total); await flush();
    expect(cb.onError).toHaveBeenCalledOnce();
    expect(cb.onError.mock.calls[0][0]).toBeInstanceOf(RecorderError);
    expect(recorder.active).toBe(false); expect(env.stopped).toHaveBeenCalled();
  });

  it('a backwards ACK fails rather than replenishing credit twice', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb);
    env.pcm(); env.pcm(); recorder.acknowledge(8000); recorder.acknowledge(4000); await flush();
    expect(cb.onError).toHaveBeenCalledOnce(); expect(recorder.active).toBe(false);
  });

  it('a missing flushed acknowledgement rejects and releases the microphone', async () => {
    vi.useFakeTimers(); const env = environmentWithReadyBegin(false), recorder = new PcmRecorder();
    await recorder.start(callbacks());
    const stopping = recorder.stop(), rejected = expect(stopping).rejects.toThrow('尾帧未确认');
    await vi.advanceTimersByTimeAsync(2000); await rejected;
    expect(env.stopped).toHaveBeenCalled(); expect(recorder.active).toBe(false);
  });

  it('discard cancellation never flushes pending audio into the server', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb);
    await recorder.stop({ discard: true });
    expect(env.node.port.postMessage).toHaveBeenCalledWith({ type: 'discard' });
    expect(env.node.port.postMessage).not.toHaveBeenCalledWith({ type: 'stop' });
    expect(cb.onChunk).not.toHaveBeenCalled(); expect(env.stopped).toHaveBeenCalled();
  });

  it('cancel overtaking normal stop suppresses queued PCM and settles the existing flush', async () => {
    const env = environmentWithReadyBegin(false), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb);
    const normalStop = recorder.stop(); const cancelled = recorder.stop({ discard: true });
    env.pcm(3); env.emit({ type: 'flushed', total_samples: 3 });
    await Promise.all([normalStop, cancelled]);
    expect(cb.onChunk).not.toHaveBeenCalled(); expect(recorder.active).toBe(false); expect(env.stopped).toHaveBeenCalled();
  });

  it('worklet backpressure while flushing rejects the finish instead of sending an incomplete result', async () => {
    const env = environmentWithReadyBegin(false), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb);
    const stop = recorder.stop(), rejected = expect(stop).rejects.toThrow('窗口已满');
    env.emit({ type: 'error', code: 'AUDIO_BACKPRESSURE', message: '窗口已满' });
    await rejected;
    expect(cb.onError).toHaveBeenCalledOnce(); expect(recorder.active).toBe(false);
  });

  it('a premature completion or wrong sample total is rejected before onChunk', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb);
    env.pcm(4000, 10, { total_samples: 8000 }); await flush();
    expect(cb.onChunk).not.toHaveBeenCalled(); expect(cb.onError).toHaveBeenCalledOnce();
  });

  it('limit and an already received flushed event let a later stop finish without a new tail', async () => {
    const env = environmentWithReadyBegin(false), recorder = new PcmRecorder(), cb = callbacks(); await recorder.start(cb);
    for (let n = 0; n < 720; n++) { env.pcm(4000, 10 + n * 0.25); recorder.acknowledge((n + 1) * 4000); }
    env.emit({ type: 'limit', total_samples: 2_880_000 });
    env.emit({ type: 'flushed', total_samples: 2_880_000 });
    await flush();
    expect(recorder.active).toBe(false); // Local close does not depend on a UI callback.
    await recorder.stop();
    expect(cb.onLimit).toHaveBeenCalledOnce(); expect(cb.onChunk).toHaveBeenCalledTimes(720);
    expect(env.node.port.postMessage).not.toHaveBeenCalledWith({ type: 'stop' });
    expect(recorder.active).toBe(false);
  });

  it('missing completion at the hard limit releases the mic and reports failure', async () => {
    vi.useFakeTimers(); const env = environmentWithReadyBegin(false), recorder = new PcmRecorder(), cb = callbacks();
    await recorder.start(cb);
    for (let n = 0; n < 720; n++) { env.pcm(4000, 10 + n * 0.25); recorder.acknowledge((n + 1) * 4000); }
    env.emit({ type: 'limit', total_samples: 2_880_000 });
    await vi.advanceTimersByTimeAsync(2000);
    expect(cb.onLimit).toHaveBeenCalledOnce(); expect(cb.onError).toHaveBeenCalledOnce();
    expect(recorder.active).toBe(false); expect(env.stopped).toHaveBeenCalled();
  });

  it('onLimit may synchronously request normal stop before the queued flushed event', async () => {
    const env = environmentWithReadyBegin(false), recorder = new PcmRecorder(), cb = callbacks(); let stopping: Promise<void> | undefined;
    cb.onLimit.mockImplementation(() => { stopping = recorder.stop(); });
    await recorder.start(cb);
    for (let n = 0; n < 720; n++) { env.pcm(4000, 10 + n * 0.25); recorder.acknowledge((n + 1) * 4000); }
    env.emit({ type: 'limit', total_samples: 2_880_000 }); env.emit({ type: 'flushed', total_samples: 2_880_000 });
    await stopping;
    expect(cb.onChunk).toHaveBeenCalledTimes(720); expect(cb.onError).not.toHaveBeenCalled(); expect(recorder.active).toBe(false);
  });

  it('events from an old node cannot write to a new recording', async () => {
    const env = environmentWithReadyBegin(), recorder = new PcmRecorder(), first = callbacks(), second = callbacks();
    await recorder.start(first); const oldHandler = env.node.port.onmessage!;
    await recorder.stop({ discard: true }); await recorder.start(second);
    oldHandler({ data: { type: 'pcm', buffer: new ArrayBuffer(8000), samples: 4000, total_samples: 4000, context_time: 10 } });
    expect(first.onChunk).not.toHaveBeenCalled(); expect(second.onChunk).not.toHaveBeenCalled();
    await recorder.stop({ discard: true });
  });
});


describe('manual ready/begin startup cancellation and capture identity', () => {
  it('does not resolve start or emit samples/levels before the actual begun acknowledgement', async () => {
    const env = environment(), recorder = new PcmRecorder(), cb = callbacks();
    let resolved = false;
    const start = recorder.start(cb).then(() => { resolved = true; }); await flush();
    expect(env.node.port.postMessage).toHaveBeenCalledWith({ type: 'prepare' });
    expect(resolved).toBe(false); expect(cb.onChunk).not.toHaveBeenCalled(); expect(cb.onLevel).not.toHaveBeenCalled();
    env.emit({ type: 'ready' }); await flush();
    expect(env.node.port.postMessage).toHaveBeenCalledWith({ type: 'begin' }); expect(resolved).toBe(false);
    env.emit({ type: 'begun', context_time: 10 }); await start;
    expect(resolved).toBe(true); env.pcm(4000, 10);
    expect(cb.onChunk.mock.calls[0][1]).toBe(105_000);
    await recorder.stop({ discard: true });
  });

  it.each(['ready', 'begun'])('cancellation while awaiting %s settles start and ignores late startup events', async waitingFor => {
    const env = environment(), recorder = new PcmRecorder(), cb = callbacks();
    const starting = recorder.start(cb), rejected = expect(starting).rejects.toThrow('取消'); await flush();
    if (waitingFor === 'begun') env.emit({ type: 'ready' });
    const stale = env.node.port.onmessage!;
    await recorder.stop({ discard: true }); await rejected;
    stale({ data: { type: 'ready' } }); stale({ data: { type: 'begun', context_time: 10 } });
    expect(recorder.active).toBe(false); expect(cb.onChunk).not.toHaveBeenCalled(); expect(cb.onLevel).not.toHaveBeenCalled();
    expect(env.node.port.postMessage).toHaveBeenCalledWith({ type: 'discard' });
    expect(env.node.port.postMessage).not.toHaveBeenCalledWith({ type: 'stop' });
    expect(env.context.close).toHaveBeenCalledOnce(); expect(env.stopped).toHaveBeenCalledOnce();
  });

  it('a track interruption while preparing rejects the pending start and releases the graph', async () => {
    const env = environment(), recorder = new PcmRecorder(), cb = callbacks();
    const starting = recorder.start(cb), rejected = expect(starting).rejects.toMatchObject({ code: 'AUDIO_INTERRUPTED' });
    await flush(); env.track.onended?.(); await rejected; await flush();
    expect(cb.onError).toHaveBeenCalledOnce(); expect(recorder.active).toBe(false);
    expect(env.context.close).toHaveBeenCalledOnce(); expect(cb.onChunk).not.toHaveBeenCalled();
  });

  it('a late ready/begun from a cancelled capture cannot complete or close a newer startup', async () => {
    const env = environment(), recorder = new PcmRecorder();
    const oldStart = recorder.start(callbacks()).catch(error => error); await flush();
    const oldHandler = env.node.port.onmessage!; await recorder.stop({ discard: true }); await oldStart;
    let newerResolved = false;
    const newer = recorder.start(callbacks()).then(() => { newerResolved = true; }); await flush();
    const context = env.context;
    oldHandler({ data: { type: 'ready' } }); oldHandler({ data: { type: 'begun', context_time: 10 } }); await flush();
    expect(newerResolved).toBe(false); expect(context.close).not.toHaveBeenCalled();
    env.emit({ type: 'ready' }); env.emit({ type: 'begun', context_time: 10 }); await newer;
    expect(newerResolved).toBe(true); await recorder.stop({ discard: true });
  });

  it.each([undefined, -1, Number.NaN])('an invalid begun context time %s fails rather than inventing a capture epoch', async context_time => {
    const env = environment(), recorder = new PcmRecorder(), cb = callbacks();
    const starting = recorder.start(cb), rejected = expect(starting).rejects.toMatchObject({ code: 'AUDIO_PROTOCOL_ERROR' });
    await flush(); env.emit({ type: 'ready' }); env.emit({ type: 'begun', context_time }); await rejected;
    expect(cb.onChunk).not.toHaveBeenCalled(); expect(recorder.active).toBe(false);
  });
});
