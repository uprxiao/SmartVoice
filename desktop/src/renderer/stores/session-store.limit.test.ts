import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSnapshot, SessionProgress, SessionStartResult } from '@shared/types';
import { AUDIO, sessionMaxSamples } from '@shared/config';
import { AudioFlow, AudioFlowError } from '../../main/core/audio-flow';
// TC-V2-DESK-01..03: stop at the session's own server-granted limit.
const doubles = vi.hoisted(() => ({ listeners: new Map<string, (value: unknown) => void>(),
  start: vi.fn().mockResolvedValue(undefined), stop: vi.fn().mockResolvedValue(undefined), acknowledge: vi.fn() }));
vi.mock('../audio/recorder', async importOriginal => ({ ...await importOriginal<typeof import('../audio/recorder')>(),
  PcmRecorder: class { start = doubles.start; stop = doubles.stop; acknowledge = doubles.acknowledge; } }));
vi.mock('../lib/ipc', () => ({
  events: { on: (event: { name: string }, callback: (value: unknown) => void) => { doubles.listeners.set(event.name, callback); return () => doubles.listeners.delete(event.name); }, emit: vi.fn() },
  rpc: { session: { start: vi.fn(), captureReady: vi.fn(), cancelStart: vi.fn().mockResolvedValue(undefined), cancel: vi.fn(), status: vi.fn(),
    finish: vi.fn(), insert: vi.fn(), retry: vi.fn(), reset: vi.fn().mockResolvedValue(undefined) }, history: { selectForRetry: vi.fn() }, auth: {}, settings: {} },
}));
import { rpc } from '../lib/ipc';
import { AuthStore } from './auth-store';
import { SessionStore } from './session-store';
import type { UiStore } from './ui-store';

const USER = '11111111-1111-4111-8111-111111111111';
const progress = (session: string, patch: Partial<SessionProgress> = {}): SessionProgress => ({ acknowledged_samples: 0, session_id: session,
  attempt_no: 1, revision: 1, auto_insert_allowed: false, capture_ready: true, state: 'streaming', audio_ms: 0, stable_text: '',
  provisional_text: '', error_code: null, result: null, ...patch });
const started = (session: string, maxSamples?: number): SessionStartResult => ({ session_id: session, attempt_no: 1,
  insertion_available: false, target_app: null, target_error: null, ...(maxSamples ? { max_samples: maxSamples } : {}) });
const stores: SessionStore[] = [];
function fixture() {
  const auth = new AuthStore();
  auth.snapshot = { user: { id: USER }, settings: { learning_enabled: true, locale: 'zh-CN' } } as AuthSnapshot;
  const store = new SessionStore(auth, { toast: vi.fn() } as unknown as UiStore);
  stores.push(store); store.init(); return store;
}
const emit = (value: SessionProgress) => doubles.listeners.get('session.progress')?.(value);

beforeEach(() => {
  vi.clearAllMocks(); doubles.listeners.clear();
  vi.mocked(rpc.session.status).mockResolvedValue(progress('', { session_id: null, attempt_no: null, state: 'created', revision: 0 }));
  vi.mocked(rpc.session.captureReady).mockImplementation(async (sessionId, attemptNo) => progress(sessionId, { attempt_no: attemptNo, revision: 2 }));
  vi.mocked(rpc.session.finish).mockResolvedValue(progress('', { state: 'finalizing', revision: 50 }));
});
afterEach(() => { for (const store of stores.splice(0)) store.invalidateLocal(); });

describe('TC-V2-DESK-01..03 per-session recording limit', () => {
  const A = '22222222-2222-4222-8222-222222222222', B = '33333333-3333-4333-8333-333333333333';

  it('DESK-01: a 360 s session stops at 360 s, not at 180 s; recorder gets the session limit', async () => {
    const store = fixture();
    vi.mocked(rpc.session.start).mockResolvedValueOnce(started(A, 5_760_000));
    await store.start();
    expect(doubles.start.mock.calls[0][1]).toEqual({ maxSamples: 5_760_000 });
    emit(progress(A, { revision: 10, audio_ms: 180_000, max_audio_ms: 360_000 }));
    emit(progress(A, { revision: 11, audio_ms: 359_750, max_audio_ms: 360_000 }));
    expect(rpc.session.finish).not.toHaveBeenCalled();
    emit(progress(A, { revision: 12, audio_ms: 360_000, max_audio_ms: 360_000 }));
    await Promise.resolve();
    expect(rpc.session.finish).toHaveBeenCalledTimes(1);
  });

  it('DESK-02: an older server without the field falls back to 180 s', async () => {
    const store = fixture();
    vi.mocked(rpc.session.start).mockResolvedValueOnce(started(A));
    await store.start();
    expect(doubles.start.mock.calls[0][1]).toEqual({ maxSamples: undefined });
    emit(progress(A, { revision: 10, audio_ms: 179_750 }));
    expect(rpc.session.finish).not.toHaveBeenCalled();
    emit(progress(A, { revision: 11, audio_ms: 180_000 }));
    await Promise.resolve();
    expect(rpc.session.finish).toHaveBeenCalledTimes(1);
  });

  it('DESK-03: consecutive sessions use their own limits, not the previous one', async () => {
    const store = fixture();
    vi.mocked(rpc.session.start).mockResolvedValueOnce(started(A, 5_760_000));
    await store.start();
    emit(progress(A, { revision: 10, state: 'cancelled', max_audio_ms: 360_000 }));
    vi.mocked(rpc.session.start).mockResolvedValueOnce(started(B, 2_880_000));
    await store.start();
    expect(doubles.start.mock.calls[1][1]).toEqual({ maxSamples: 2_880_000 });
    emit(progress(B, { revision: 3, audio_ms: 180_000, max_audio_ms: 180_000 }));
    await Promise.resolve();
    expect(rpc.session.finish).toHaveBeenCalledTimes(1);
  });

  it('Main helpers: the creation response sets the flow limit; config.ts is only the fallback', () => {
    expect(sessionMaxSamples(360)).toBe(5_760_000);
    expect(sessionMaxSamples(180)).toBe(2_880_000);
    for (const invalid of [undefined, null, 0, 361, 12.5, '360']) expect(sessionMaxSamples(invalid)).toBe(AUDIO.maxSamples);
    const flow = new AudioFlow(sessionMaxSamples(360));
    let total = 0;
    for (let n = 0; n < 1440; n++) { total = flow.frame(4000, n * 250).totalSamples; flow.acknowledge(total); }
    expect(total).toBe(5_760_000);
    expect(() => flow.frame(1, 1440 * 250)).toThrowError(AudioFlowError);
    expect(new AudioFlow().maxSamples).toBe(2_880_000);
  });
});
