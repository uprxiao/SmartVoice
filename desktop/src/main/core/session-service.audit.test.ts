import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSnapshot, Session, SessionProgress, SessionResult } from '@shared/types';
import { ApiClient, ApiError, AuthContextChangedError } from './api-client';
import type { AuthService } from './auth-service';
import { NativeCallError, type NativeVoiceInput } from './native-input';
import { InsertionJournal } from './insertion-journal';
import { SessionService } from './session-service';

const doubles = vi.hoisted(() => ({ sockets: [] as unknown[], now: 100_000 }));
vi.mock('node:perf_hooks', () => ({ performance: { timeOrigin: 0, now: () => doubles.now } }));
vi.mock('electron', () => ({ clipboard: { writeText: vi.fn() } }));
vi.mock('ws', async () => {
  const { EventEmitter } = await import('node:events');
  class FakeSocket extends EventEmitter {
    static OPEN = 1;
    static CLOSED = 3;
    readyState = FakeSocket.OPEN;
    send = vi.fn((_data: unknown, callback?: (error?: Error) => void) => callback?.());
    constructor() { super(); doubles.sockets.push(this); }
    close(): void { this.readyState = FakeSocket.CLOSED; this.emit('close', 1000); }
    message(event: unknown): void { this.emit('message', Buffer.from(JSON.stringify(event))); }
  }
  return { default: FakeSocket };
});
interface Socket {
  readyState: number;
  send: ReturnType<typeof vi.fn>;
  close(): void;
  emit(name: string, ...args: unknown[]): boolean;
  message(event: unknown): void;
}
const USER = '11111111-1111-4111-8111-111111111111';
const SESSION = '22222222-2222-4222-8222-222222222222';
const RESULT = '33333333-3333-4333-8333-333333333333';
const INSERTION = '44444444-4444-4444-8444-444444444444';
const TEXT = '今天测试语音输入。';
const RESULT_VALUE = { result_id: RESULT, attempt_no: 1, attempt_kind: 'initial', raw_text: TEXT, refined_text: TEXT,
  review_required: false, review_reason: null, text_hash: createHash('sha256').update(TEXT).digest('hex'),
  dictionary_version: 0, stop_to_result_ms: 1, created_at: '2026-10-18T00:00:00Z' } satisfies SessionResult;
const session = (patch: Partial<Session> = {}): Session => ({ session_id: SESSION, mode: 'dictation', state: 'created', learning_enabled: false,
  current_attempt_no: 1, current_result_id: null, audio_complete: false, received_samples: 0, audio_ms: 0,
  created_at: '2026-10-18T00:00:00Z', first_terminal_at: null, content_expires_at: null, content_expired: false,
  error_code: null, result: null, retry_kind: 'none', retry_expires_at: null, retry_unavailable_reason: null,
  insertion_status: null, ...patch });
const pending = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let n = 0; n < 20; n++) await Promise.resolve(); };
const all: Array<{ directory: string; service: SessionService }> = [];
function fixture() {
  const directory = mkdtempSync(join(tmpdir(), 'svi-session-audit-'));
  const journal = new InsertionJournal(directory);
  let snapshot = { user: { id: USER }, settings: { learning_enabled: false } } as AuthSnapshot | null;
  const api = {
    accessTokenValue: 'test-token', ensureFreshAccess: vi.fn(async () => undefined),
    createSession: vi.fn(async () => session()), getSession: vi.fn(async () => session()),
    cancelSession: vi.fn(async () => session({ state: 'cancelled' })), clearTokens: vi.fn(),
    retrySession: vi.fn(async (_id: string, _kind: 'asr' | 'refine', _key: string) => session()),
    claimInsertion: vi.fn(async (sessionId: string, body: { insertion_id: string; result_id: string }) => ({
      replayed: false, insertion_id: body.insertion_id, session_id: sessionId, result_id: body.result_id,
      status: 'reserved', observable: false, error_code: null, claimed_at: '', completed_at: null,
    })),
    insertionReceipt: vi.fn(async (sessionId: string, insertionId: string, body: { status: string; observable: boolean; error_code: string | null }) => ({
      replayed: false, insertion_id: insertionId, session_id: sessionId, result_id: RESULT,
      ...body, claimed_at: '', completed_at: '',
    })),
  };
  let captures = 0;
  const native = { call: vi.fn(async (action: string, _payload?: Record<string, unknown>): Promise<Record<string, unknown>> => {
    if (action === 'permission') return { trusted: true };
    if (action === 'capture') return { target: `target-${++captures}`, app: 'TestEdit' };
    if (action === 'validate') return { valid: true, effect: 'none' };
    if (action === 'insert') return { status: 'inserted', effect: 'written', observable: false, message: '已输入' };
    if (action === 'insertCurrent') return { status: 'inserted', effect: 'written', observable: false,
      target: 'final-target', app: 'TestEdit', message: '已输入' };
    return {};
  }) };
  const events: SessionProgress[] = [];
  let service!: SessionService;
  const auth = {
    get current() { return snapshot; }, get learningEnabled() { return snapshot?.settings.learning_enabled ?? false; },
    ensure: () => { if (!snapshot) throw new Error('请登录'); return snapshot; },
    invalidate: vi.fn(() => { snapshot = null; service.invalidateAccount(); }),
  };
  service = new SessionService(api as unknown as ApiClient, native as unknown as NativeVoiceInput,
    auth as unknown as AuthService, journal, progress => events.push(progress), progress => events.push(progress));
  const value = { directory, journal, api, native, auth, service, events,
    setLearning: (enabled: boolean) => { if (snapshot) snapshot.settings.learning_enabled = enabled; } };
  all.push(value);
  return value;
}
async function streaming(f: ReturnType<typeof fixture>, learning = false): Promise<Socket> {
  const started = f.service.start(learning);
  await flush();
  const ws = doubles.sockets.at(-1) as Socket;
  expect(ws).toBeDefined();
  ws.message({ type: 'ready', session_id: SESSION, attempt_no: 1,
    audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1, chunk_samples: 4000, max_samples: 2_880_000 } });
  await started;
  return ws;
}
async function ready(f: ReturnType<typeof fixture>): Promise<Socket> {
  const ws = await streaming(f);
  f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
  ws.message({ type: 'audio.ack', seq: 0, total_samples: 4000 });
  const finished = f.service.finish();
  ws.message({ type: 'result.ready', session_id: SESSION, attempt_no: 1, result: RESULT_VALUE });
  await finished;
  await flush(); // Result is published before the independent native target query completes.
  return ws;
}
const sends = (ws: Socket) => ws.send.mock.calls.filter(call => Buffer.isBuffer(call[0]));

const HISTORY = '55555555-5555-4555-8555-555555555555';
const HISTORY_RESULT: SessionResult = { ...RESULT_VALUE, result_id: '66666666-6666-4666-8666-666666666666',
  review_required: true, review_reason: 'llm_unavailable' };
const historySession = (patch: Partial<Session> = {}): Session => session({
  session_id: HISTORY, state: 'ready', current_result_id: HISTORY_RESULT.result_id,
  audio_complete: true, received_samples: 4000, audio_ms: 250, result: HISTORY_RESULT,
  retry_kind: 'refine', learning_enabled: false, ...patch,
});

beforeEach(() => { vi.useFakeTimers(); doubles.sockets.length = 0; doubles.now = 100_000; });
afterEach(() => {
  for (const f of all.splice(0)) { f.service.dispose(); rmSync(f.directory, { recursive: true, force: true }); }
  vi.useRealTimers();
});

describe('server-granted recording limit handshake', () => {
  it.each([undefined, 10, 180, 360])('accepts ready for the created session limit %s', async seconds => {
    const f = fixture();
    f.api.createSession.mockResolvedValueOnce(session({ max_record_seconds: seconds }));
    const started = f.service.start(false);
    await flush();
    const maxSamples = (seconds ?? 180) * 16000;
    const ws = doubles.sockets.at(-1) as Socket;
    ws.message({ type: 'ready', session_id: SESSION, attempt_no: 1,
      audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1,
        chunk_samples: 4000, max_samples: maxSamples } });
    await expect(started).resolves.toMatchObject({ max_samples: maxSamples });
    expect(f.service.current()).toMatchObject({ state: 'streaming', max_audio_ms: (seconds ?? 180) * 1000 });
    expect(f.api.cancelSession).not.toHaveBeenCalled();
  });

  it.each([[180, 360], [360, 180], [360, undefined]])(
    'rejects a ready limit %s that disagrees with the created session %s', async (seconds, readySeconds) => {
      const f = fixture();
      f.api.createSession.mockResolvedValueOnce(session({ max_record_seconds: seconds }));
      const started = f.service.start(false);
      const rejected = expect(started).rejects.toThrow('AUDIO_PROTOCOL_ERROR');
      await flush();
      const ws = doubles.sockets.at(-1) as Socket;
      ws.message({ type: 'ready', session_id: SESSION, attempt_no: 1,
        audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1,
          chunk_samples: 4000, max_samples: readySeconds === undefined ? undefined : readySeconds * 16000 } });
      await rejected;
      expect(f.service.current()).toMatchObject({ state: 'failed', error_code: 'AUDIO_PROTOCOL_ERROR' });
      expect(f.api.cancelSession).toHaveBeenCalledWith(SESSION);
    });
});

describe('result-time focus selection', () => {
  it('keeps the passed A/B/C scenario and uses final C once; duplicates never write again', async () => {
    const f = fixture(); let focus = 'WPS';
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation((action, body) => action === 'capture'
      ? Promise.resolve({ target: focus, app: focus }) : action === 'insertCurrent'
        ? Promise.resolve({ target: focus, app: focus, status: 'inserted', effect: 'written', observable: false })
        : original(action, body));
    const ws = await streaming(f);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'capture')).toHaveLength(0);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 4000 });
    focus = 'Code'; const finished = f.service.finish();
    expect(f.native.call.mock.calls.filter(([action]) => action === 'capture')).toHaveLength(0);
    focus = 'Chrome';
    const event = { type: 'result.ready', session_id: SESSION, attempt_no: 1, result: RESULT_VALUE };
    ws.message(event); await finished;
    expect((await f.service.insert(false, false)).status).toBe('inserted');
    focus = 'WPS'; ws.message(event); await f.service.insert(false, false);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'capture')).toHaveLength(1);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insertCurrent')).toEqual([
      ['insertCurrent', expect.objectContaining({ text: TEXT, restoreFocus: false })],
    ]);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(0);
    expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
    expect(f.service.current().target).toMatchObject({ target_app: 'Chrome', insertion_available: true });
  });

  it('does not wait for an early preview; a late preview is released without replacing the final target', async () => {
    const f = fixture(), capture = pending<Record<string, unknown>>();
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation((action, body) => action === 'capture' ? capture.promise : original(action, body));
    await ready(f);
    expect(f.service.current()).toMatchObject({ state: 'ready', result: RESULT_VALUE });
    const inserting = f.service.insert(false, false); await flush();
    expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
    expect((await inserting).status).toBe('inserted');
    capture.resolve({ target: 'result-target', app: 'Chrome' });
    await flush();
    expect(f.native.call).toHaveBeenCalledWith('release', { target: 'result-target' });
    expect(f.native.call.mock.calls.filter(([action]) => action === 'validate')).toHaveLength(0);
    expect(f.service.current().target).toMatchObject({ target_app: 'TestEdit', insertion_available: true });
  });

  it.each(['cancel', 'account', 'new-dictation'] as const)('releases late result-time captures after %s without using a new target', async action => {
    const f = fixture(), capture = pending<Record<string, unknown>>();
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation((name, body) => name === 'capture' ? capture.promise : original(name, body));
    await ready(f);
    if (action === 'cancel') await f.service.cancel();
    else if (action === 'account') f.auth.invalidate();
    else await streaming(f);
    capture.resolve({ target: 'late-old-result', app: 'Chrome' }); await flush();
    expect(f.native.call).toHaveBeenCalledWith('release', { target: 'late-old-result' });
    expect(f.api.claimInsertion).not.toHaveBeenCalled();
    expect(f.native.call.mock.calls.filter(([name]) => name === 'insert')).toHaveLength(0);
    expect(f.service.current().target?.insertion_available).toBe(false);
  });

  it('keeps history/copy and permits another recording when the final target is unavailable', async () => {
    const f = fixture(), original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation((name, body) => name === 'capture'
      ? Promise.reject(new Error('没有当前输入位置')) : name === 'insertCurrent'
        ? Promise.reject(new NativeCallError('没有当前输入位置', 'TARGET_UNSUPPORTED', 'none', 'insertCurrent'))
        : original(name, body));
    await ready(f);
    expect(await f.service.insert(false, false)).toMatchObject({ status: 'failed', observable: false, error_code: 'TARGET_UNSUPPORTED' });
    expect(f.service.current()).toMatchObject({ state: 'ready', result: RESULT_VALUE });
    expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
    expect(f.native.call.mock.calls.filter(([name]) => name === 'insert')).toHaveLength(0);
    await streaming(f);
    expect(f.api.createSession).toHaveBeenCalledTimes(2);
  });

  // TC-INSERT-22: a missing Accessibility grant is reported as PERMISSION_DENIED, not a generic ZERO_WRITE.
  it('maps the native PERMISSION_REQUIRED zero-write to PERMISSION_DENIED in the outcome and receipt', async () => {
    const f = fixture(), original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation((name, body) => name === 'insertCurrent'
      ? Promise.reject(new NativeCallError('外部输入需要辅助功能权限，请在语音设置中授权', 'PERMISSION_REQUIRED', 'none', 'insertCurrent'))
      : original(name, body));
    await ready(f);
    const outcome = await f.service.insert(false, false);
    expect(outcome).toMatchObject({ status: 'failed', observable: false, error_code: 'PERMISSION_DENIED',
      message: '外部输入需要辅助功能权限，请在语音设置中授权' });
    await vi.waitFor(() => expect(f.api.insertionReceipt).toHaveBeenCalledWith(SESSION, outcome.insertion_id,
      { status: 'failed', observable: false, error_code: 'PERMISSION_DENIED' }));
    expect(f.service.current()).toMatchObject({ state: 'ready', result: RESULT_VALUE });
  });

  it('does not reject a final target because the earlier preview has changed', async () => {
    const f = fixture(); await ready(f);
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation((name, body) => name === 'validate'
      ? Promise.resolve({ valid: false, code: 'TARGET_CHANGED' }) : original(name, body));
    expect(await f.service.insert(false, false)).toMatchObject({ status: 'inserted' });
    expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
    expect(f.native.call.mock.calls.filter(([name]) => name === 'validate')).toHaveLength(0);
    expect(f.native.call.mock.calls.filter(([name]) => name === 'capture')).toHaveLength(1);
    expect(f.native.call.mock.calls.filter(([name]) => name === 'insert')).toHaveLength(0);
  });
});

describe('final focus after insertion claim', () => {
  it.each(['cancel', 'account'] as const)(
    'never dispatches the final write when %s happens while claim is pending', async change => {
      const f = fixture(); await ready(f);
      const gate = pending<Awaited<ReturnType<typeof f.api.claimInsertion>>>();
      const claim = f.api.claimInsertion.getMockImplementation()!;
      let committed!: Awaited<ReturnType<typeof claim>>;
      f.api.claimInsertion.mockImplementation(async (sid, body) => {
        committed = await claim(sid, body); return gate.promise;
      });
      const inserting = f.service.insert(false, false).catch(error => error);
      await flush();
      expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
      if (change === 'cancel') await f.service.cancel();
      else f.auth.invalidate();
      gate.resolve(committed); await flush();
      expect(await inserting).toBeInstanceOf(AuthContextChangedError);
      expect(f.native.call.mock.calls.filter(([action]) => action === 'insertCurrent' || action === 'insert')).toHaveLength(0);
    });

  it('keeps a new dictation out of the single in-flight insertion instead of overlapping writes', async () => {
    const f = fixture(); await ready(f);
    const gate = pending<Awaited<ReturnType<typeof f.api.claimInsertion>>>();
    const claim = f.api.claimInsertion.getMockImplementation()!;
    let committed!: Awaited<ReturnType<typeof claim>>;
    f.api.claimInsertion.mockImplementation(async (sid, body) => {
      committed = await claim(sid, body); return gate.promise;
    });
    const inserting = f.service.insert(false, false); await flush();
    await expect(f.service.start(false)).rejects.toThrow('已有进行中的语音会话');
    expect(f.api.createSession).toHaveBeenCalledTimes(1);
    gate.resolve(committed);
    expect((await inserting).status).toBe('inserted');
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insertCurrent')).toHaveLength(1);
  });

  it.each(['application', 'selection', 'value'] as const)(
    'uses the final %s after a delayed claim, writes once and ignores the early snapshot', async change => {
      const f = fixture();
      let focus = { target: 'first-field', app: 'Code', value: 'left OLD right', start: 5, length: 3 };
      let captured = { ...focus };
      const writes: typeof focus[] = [];
      const original = f.native.call.getMockImplementation()!;
      const write = () => {
        const current = { ...focus };
        writes.push(current);
        focus.value = current.value.slice(0, current.start) + TEXT + current.value.slice(current.start + current.length);
        return { status: 'inserted', effect: 'written', observable: true, target: current.target, app: current.app };
      };
      f.native.call.mockImplementation(async (action, body) => {
        if (action === 'capture') { captured = { ...focus }; return { target: captured.target, app: captured.app }; }
        if (action === 'insertCurrent') return write();
        if (action === 'insert') {
          if (JSON.stringify(focus) !== JSON.stringify(captured)) {
            return { status: 'failed', effect: 'none', observable: false, code: 'TARGET_CHANGED' };
          }
          return write();
        }
        return original(action, body);
      });
      await ready(f);
      const gate = pending<Awaited<ReturnType<typeof f.api.claimInsertion>>>();
      const claim = f.api.claimInsertion.getMockImplementation()!;
      let committed!: Awaited<ReturnType<typeof claim>>;
      f.api.claimInsertion.mockImplementation(async (sid, body) => {
        committed = await claim(sid, body);
        return gate.promise;
      });
      const inserting = f.service.insert(false, false); await flush();
      expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
      expect(writes).toHaveLength(0);
      if (change === 'application') focus = { target: 'last-field', app: 'Chrome', value: 'new target', start: 4, length: 0 };
      else if (change === 'selection') focus = { ...focus, start: 0, length: 4 };
      else focus = { ...focus, value: 'user added text', start: 15, length: 0 };
      const final = { ...focus };
      gate.resolve(committed);
      expect(await inserting).toMatchObject({ status: 'inserted', observable: true });
      expect(writes).toEqual([final]);
      expect(focus.value).toBe(final.value.slice(0, final.start) + TEXT + final.value.slice(final.start + final.length));
      await f.service.insert(false, false);
      expect(writes).toHaveLength(1);
      expect(f.api.claimInsertion).toHaveBeenCalledTimes(1);
      expect(f.native.call.mock.calls.filter(([action]) => action === 'insertCurrent')).toHaveLength(1);
    });
});

describe('SessionService lifecycle and insertion boundaries', () => {
  it('starts the next dictation without handling the saved result or reusing its target', async () => {
    const f=fixture(), old=await ready(f);
    const next='88888888-8888-4888-8888-888888888888';
    f.api.createSession.mockResolvedValueOnce(session({session_id:next}));
    f.native.call.mockImplementation(async action => {
      if(action==='permission')return {trusted:true};
      if(action==='capture')throw new Error('没有可读取的普通文本输入目标');
      return {};
    });
    const starting=f.service.start(false);
    await flush();
    const ws=doubles.sockets.at(-1) as Socket;
    // Start must reach a fresh connection; the old ready result is already persisted by the server.
    expect.soft(f.api.createSession).toHaveBeenCalledTimes(2);
    expect.soft(ws).not.toBe(old);
    if(ws!==old)ws.message({type:'ready',session_id:next,attempt_no:1,audio:{encoding:'pcm_s16le',sample_rate_hz:16000,channels:1,chunk_samples:4000,max_samples:2_880_000}});
    await expect(starting).resolves.toMatchObject({session_id:next,insertion_available:false,target_app:null});
    expect(f.service.current()).toMatchObject({session_id:next,state:'streaming',result:null});
    old.message({type:'result.ready',session_id:SESSION,attempt_no:1,result:RESULT_VALUE});
    expect(f.service.current()).toMatchObject({session_id:next,result:null});
    expect(f.native.call).toHaveBeenCalledWith('release',{target:'target-1'});
    expect(f.api.cancelSession).not.toHaveBeenCalled();
    expect(f.api.claimInsertion).not.toHaveBeenCalled();
  });
  it('rejects the ninth 4000-sample outstanding frame and ignores a late result', async () => {
    const f = fixture(), ws = await streaming(f);
    for (let n = 0; n < 9; n++) f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    expect(sends(ws)).toHaveLength(8);
    expect(f.service.current()).toMatchObject({ state: 'failed', error_code: 'AUDIO_BACKPRESSURE' });
    ws.message({ type: 'result.ready', session_id: SESSION, attempt_no: 1, result: RESULT_VALUE });
    expect(f.service.current().state).toBe('failed');
    expect(f.api.cancelSession).toHaveBeenCalledWith(SESSION);
  });

  it('charges IPC delay against the same two-second frame budget', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now - 2000);
    expect(sends(ws)).toHaveLength(0);
    expect(f.service.current()).toMatchObject({ state: 'failed', error_code: 'AUDIO_BACKPRESSURE' });
    expect(f.api.cancelSession).toHaveBeenCalledWith(SESSION);
  });

  it('spends only the remaining 500 ms after a 1500 ms local delay', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now - 1500);
    expect(sends(ws)).toHaveLength(1);
    doubles.now += 499; vi.advanceTimersByTime(499);
    expect(f.service.current().state).toBe('streaming');
    doubles.now += 1; vi.advanceTimersByTime(1);
    expect(f.service.current().error_code).toBe('AUDIO_BACKPRESSURE');
  });

  it('cannot use a late ACK to rescue a frame whose total budget has expired', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now - 1500);
    doubles.now += 501; // Deliberately deliver ACK before the overdue timer callback.
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 4000 });
    expect(f.service.current()).toMatchObject({ state: 'failed', error_code: 'AUDIO_BACKPRESSURE' });
  });

  it('rejects a delayed short tail after the worklet has already stopped', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(800), SESSION, doubles.now - 2000);
    expect(sends(ws)).toHaveLength(0);
    expect(f.service.current().error_code).toBe('AUDIO_BACKPRESSURE');
    await f.service.finish();
    expect(ws.send.mock.calls.some(call => typeof call[0] === 'string'
      && JSON.parse(call[0]).type === 'audio.end')).toBe(false);
  });

  it.each([1, 1001])('rejects an uncalibrated capture timestamp %i ms in the future', async ahead => {
    const f = fixture();
    await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now + ahead);
    expect(f.service.current().error_code).toBe('AUDIO_PROTOCOL_ERROR');
  });

  it('a partial cumulative ACK does not reset the next frame original deadline', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    doubles.now += 250;
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    doubles.now += 750;
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 4000 });
    doubles.now = 102_251;
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    expect(sends(ws)).toHaveLength(2);
    expect(f.service.current().error_code).toBe('AUDIO_BACKPRESSURE');
  });

  it('rejects an ACK whose seq and total disagree', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 3999 });
    expect(f.service.current().error_code).toBe('AUDIO_PROTOCOL_ERROR');
  });

  it('recovers lost end ACK from audio_complete and requires an explicit target afterwards', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 4000 });
    f.api.getSession.mockResolvedValue(session({ state: 'ready', audio_complete: true, result: RESULT_VALUE }));
    const finished = f.service.finish();
    ws.close();
    expect((await finished).state).toBe('ready');
    expect(f.service.current().auto_insert_allowed).toBe(false);
    expect((await f.service.insert(false)).status).toBe('unavailable');
    expect(f.api.getSession).toHaveBeenCalledWith(SESSION);
    expect(sends(ws)).toHaveLength(1);
  });

  it('reset settles an outstanding finish and old WS events cannot recreate its result', async () => {
    const f = fixture(), ws = await streaming(f);
    const finished = f.service.finish();
    f.service.reset();
    expect((await finished).state).toBe('cancelled');
    ws.message({ type: 'result.ready', session_id: SESSION, attempt_no: 1, result: RESULT_VALUE });
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    expect(f.service.current()).toMatchObject({ session_id: null, result: null, state: 'created' });
    expect(sends(ws)).toHaveLength(0);
  });

  it('a create response arriving after reset is cancelled without adopting its session', async () => {
    const f = fixture(), creation = pending<Session>();
    f.api.createSession.mockReturnValue(creation.promise);
    const started = f.service.start(false).catch(error => error);
    await flush();
    expect(f.api.createSession).toHaveBeenCalledOnce();
    f.service.reset(); creation.resolve(session());
    expect(await started).toBeInstanceOf(AuthContextChangedError);
    await flush();
    expect(f.service.currentSessionId()).toBeNull();
    expect(f.api.cancelSession).toHaveBeenCalledWith(SESSION);
  });

  it('retarget keeps the new captured target alive even though the result is already ready', async () => {
    const f = fixture(); await ready(f);
    expect(await f.service.retarget(0)).toMatchObject({ insertion_available: true });
    expect(f.native.call.mock.calls.some(([action, body]) => action === 'release' && body?.target === 'target-2')).toBe(false);
    await f.service.insert(false);
    expect(f.native.call.mock.calls.find(([action]) => action === 'insert')?.[1]?.target).toBe('target-2');
  });

  it('a replayed reserved claim writes an uncertain journal but never calls native insert', async () => {
    const f = fixture(); await ready(f);
    f.api.claimInsertion.mockImplementation(async (sessionId, body) => ({ replayed: true,
      insertion_id: body.insertion_id, session_id: sessionId, result_id: body.result_id,
      status: 'reserved', observable: false, error_code: null, claimed_at: '', completed_at: null }));
    const outcome = await f.service.insert(false);
    expect(outcome.status).toBe('unverified');
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(0);
    expect(f.journal.read(USER, outcome.insertion_id!)?.phase).toBe('unverified');
  });

  it('opaque compatibility paste stays unverified, sends one receipt, never observes or repeats', async () => {
    const f = fixture(); f.setLearning(true);
    f.api.createSession.mockResolvedValueOnce(session({ learning_enabled: true }));
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation(async (action, body) => {
      if (action === 'capture') return { target: 'opaque-document', app: 'WPS', observable: false };
      if (action === 'insert') return { status: 'unverified', effect: 'unknown', observable: false,
        code: 'WRITE_UNVERIFIED', message: '已尝试粘贴，未核验；请检查目标，不会自动重试' };
      return original(action, body);
    });
    await ready(f);
    const outcome = await f.service.insert(false);
    await flush();
    expect(outcome).toMatchObject({ status: 'unverified', observable: false, error_code: 'VERIFY_FAILED' });
    expect(outcome.message).toContain('已尝试粘贴');
    expect(f.api.insertionReceipt).toHaveBeenCalledWith(SESSION, outcome.insertion_id,
      { status: 'unverified', observable: false, error_code: 'VERIFY_FAILED' });
    expect(f.journal.read(USER, outcome.insertion_id!)?.phase).toBe('unverified');
    await f.service.insert(false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(1);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'observe')).toHaveLength(0);
    expect(f.native.call.mock.calls.some(([action]) => action === 'release')).toBe(true);
  });

  it('native timeout is unverified; a lost receipt only retries the identical payload', async () => {
    const f = fixture(); await ready(f);
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation(async (action, body) => {
      if (action === 'insert') throw new NativeCallError('helper timed out', 'NATIVE_TIMEOUT', 'unknown', 'insert');
      return original(action, body);
    });
    f.api.insertionReceipt.mockRejectedValueOnce(new ApiError(0, 'CLIENT_TIMEOUT', 'lost reply'));
    const outcome = await f.service.insert(false);
    expect(outcome.status).toBe('unverified');
    await vi.advanceTimersByTimeAsync(250);
    expect(f.api.insertionReceipt).toHaveBeenCalledTimes(2);
    expect(f.api.insertionReceipt.mock.calls[1]).toEqual(f.api.insertionReceipt.mock.calls[0]);
    expect(f.journal.read(USER, outcome.insertion_id!)?.receipt_confirmed).toBe(true);
    await f.service.insert(false);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(1);
  });

  it('only a structured effect=none error records a confirmed zero-write failure', async () => {
    const f = fixture(); await ready(f);
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation(async (action, body) => {
      if (action === 'insert') throw new NativeCallError('target changed', 'TARGET_CHANGED', 'none', 'insert');
      return original(action, body);
    });
    const outcome = await f.service.insert(false);
    expect(outcome).toMatchObject({ status: 'failed', observable: false, error_code: 'TARGET_CHANGED' });
    expect(f.journal.read(USER, outcome.insertion_id!)?.phase).toBe('failed');
  });

  it('reset during native insertion preserves the old journal but cannot repopulate the new UI', async () => {
    const f = fixture(); await ready(f);
    const inserted = pending<Record<string, unknown>>();
    const original = f.native.call.getMockImplementation()!;
    f.native.call.mockImplementation(async (action, body) => action === 'insert' ? inserted.promise : original(action, body));
    const result = f.service.insert(false).catch(error => error);
    await flush();
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(1);
    const insertionId = f.api.claimInsertion.mock.calls[0][1].insertion_id;
    expect(f.journal.read(USER, insertionId)?.phase).toBe('started');
    f.service.reset();
    inserted.resolve({ status: 'inserted', effect: 'written', observable: false });
    expect(await result).toBeInstanceOf(AuthContextChangedError);
    expect(f.journal.read(USER, insertionId)?.phase).toBe('inserted');
    expect(f.service.current()).toMatchObject({ session_id: null, result: null, state: 'created' });
  });

  it('startup turns an unfinished durable intent into a receipt without any native action', async () => {
    const f = fixture();
    f.journal.begin({ user_id: USER, session_id: SESSION, result_id: RESULT, insertion_id: INSERTION, text_hash: RESULT_VALUE.text_hash });
    await f.service.recoverReceipts();
    expect(f.native.call).not.toHaveBeenCalled();
    expect(f.api.insertionReceipt).toHaveBeenCalledWith(SESSION, INSERTION,
      { status: 'unverified', observable: false, error_code: 'CLIENT_INTERRUPTED' });
    expect(f.journal.read(USER, INSERTION)?.receipt_confirmed).toBe(true);
  });

  it('a definite WS authentication failure clears credentials and invalidates the account', async () => {
    const f = fixture(), ws = await streaming(f);
    ws.emit('close', 4401);
    expect(f.api.clearTokens).toHaveBeenCalledOnce();
    expect(f.auth.invalidate).toHaveBeenCalledOnce();
    expect(f.service.currentSessionId()).toBeNull();
  });
});

// Real public SessionService methods; API/WS/native adapters here are test doubles.
// No OS write, real IPC or provider acceptance is claimed by these unit tests.
describe('history adoption keeps the existing operation and learning boundaries', () => {
  it('rejects selection while streaming and before an existing result is handled', async () => {
    const active = fixture(); await streaming(active);
    await expect(active.service.selectForRetry(HISTORY, 'refine')).rejects.toThrow('进行中');
    expect(active.api.getSession).not.toHaveBeenCalled();
    expect(active.service.current()).toMatchObject({ session_id: SESSION, state: 'streaming' });
    const done = fixture(); await ready(done);
    const before = done.service.current();
    await expect(done.service.selectForRetry(HISTORY, 'refine')).rejects.toThrow('处理或关闭');
    expect(done.api.getSession).not.toHaveBeenCalled();
    expect(done.service.current()).toEqual(before);
    expect((await done.service.insert(false)).status).toBe('inserted');
    expect(done.native.call.mock.calls.find(([action]) => action === 'insert')?.[1]?.target).toBe('target-1');
  });

  it('a failed history read preserves the old result and target, and blocks competing writes while pending', async () => {
    const f = fixture(); await ready(f); f.service.copy(TEXT);
    const before = f.service.current(), read = pending<Session>();
    f.api.getSession.mockReturnValueOnce(read.promise);
    const selecting = f.service.selectForRetry(HISTORY, 'refine').catch(error => error);
    expect(f.service.isBusy()).toBe(true);
    await expect(f.service.insert(false)).rejects.toThrow('读取历史');
    await expect(f.service.retarget(0)).rejects.toThrow('读取历史');
    await expect(f.service.selectForRetry(HISTORY, 'refine')).rejects.toThrow('进行中');
    expect(f.api.getSession).toHaveBeenCalledTimes(1);
    read.reject(new ApiError(404, 'NOT_FOUND', '不存在'));
    expect(await selecting).toBeInstanceOf(ApiError);
    expect(f.service.current()).toEqual(before);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'release')).toHaveLength(0);
    expect((await f.service.insert(false)).status).toBe('inserted');
    expect(f.native.call.mock.calls.find(([action]) => action === 'insert')?.[1]?.target).toBe('target-1');
  });

  it.each([
    ['no capability', { retry_kind: 'none' }],
    ['expired content', { content_expired: true }],
    ['reserved insertion', { insertion_status: 'reserved' }],
    ['mismatched result', { current_result_id: RESULT }],
    ['missing frozen learning', { learning_enabled: undefined }],
  ] as Array<[string, Partial<Session>]>)('does not replace the current result on %s', async (_name, patch) => {
    const f = fixture(); await ready(f); f.service.copy(TEXT);
    const before = f.service.current();
    f.api.getSession.mockResolvedValueOnce(historySession(patch));
    await expect(f.service.selectForRetry(HISTORY, 'refine')).rejects.toThrow();
    expect(f.service.current()).toEqual(before);
    expect(f.api.retrySession).not.toHaveBeenCalled();
    expect(f.native.call.mock.calls.filter(([action]) => action === 'release')).toHaveLength(0);
  });

  it('cannot adopt a delayed history response after the account is invalidated', async () => {
    const f = fixture(), read = pending<Session>();
    f.api.getSession.mockReturnValueOnce(read.promise);
    const selecting = f.service.selectForRetry(HISTORY, 'refine').catch(error => error);
    f.auth.invalidate(); read.resolve(historySession());
    expect(await selecting).toBeInstanceOf(AuthContextChangedError);
    expect(f.service.currentSessionId()).toBeNull();
    expect(f.api.retrySession).not.toHaveBeenCalled();
    expect(f.native.call).not.toHaveBeenCalled();
  });

  it('a late selection finally cannot clear a newer start busy state', async () => {
    const f = fixture(), read = pending<Session>(), creation = pending<Session>();
    f.api.getSession.mockReturnValueOnce(read.promise);
    const selecting = f.service.selectForRetry(HISTORY, 'refine').catch(error => error);
    f.service.reset(); f.api.createSession.mockReturnValueOnce(creation.promise);
    const starting = f.service.start(false);
    await flush(); read.resolve(historySession());
    expect(await selecting).toBeInstanceOf(AuthContextChangedError);
    expect(f.service.isBusy()).toBe(true);
    expect(f.service.currentSessionId()).toBeNull();
    creation.resolve(session()); await flush();
    const ws = doubles.sockets.at(-1) as Socket;
    ws.message({ type: 'ready', session_id: SESSION, attempt_no: 1,
      audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1, chunk_samples: 4000, max_samples: 2_880_000 } });
    await starting;
    expect(f.service.current()).toMatchObject({ session_id: SESSION, state: 'streaming' });
  });

  it('adopts history without a target or automatic insert and uses the existing retry path', async () => {
    const f = fixture(); await ready(f); f.service.copy(TEXT);
    f.api.getSession.mockResolvedValueOnce(historySession());
    const selected = await f.service.selectForRetry(HISTORY, 'refine');
    expect(selected).toMatchObject({ session_id: HISTORY, attempt_no: 1, state: 'ready', auto_insert_allowed: false, result: HISTORY_RESULT });
    expect(f.native.call.mock.calls.filter(([action]) => action === 'release')).toEqual([['release', { target: 'target-1' }]]);
    expect(f.api.claimInsertion).not.toHaveBeenCalled();
    await expect(f.service.insert(true, false)).rejects.toThrow('明确确认');
    expect((await f.service.insert(true, true)).status).toBe('unavailable');
    expect(f.api.claimInsertion).not.toHaveBeenCalled();
    const next = { ...HISTORY_RESULT, result_id: '77777777-7777-4777-8777-777777777777', attempt_no: 2,
      attempt_kind: 'refine' as const, review_required: false, review_reason: null, stop_to_result_ms: 300 };
    f.api.retrySession.mockResolvedValueOnce(historySession({ current_attempt_no: 2, current_result_id: next.result_id, result: next, retry_kind: 'none' }));
    expect(await f.service.retry('refine')).toMatchObject({ session_id: HISTORY, attempt_no: 2, result: next, auto_insert_allowed: false });
    expect(f.api.retrySession).toHaveBeenCalledWith(HISTORY, 'refine', expect.stringMatching(/^[0-9a-f-]{36}$/));
    expect(f.api.createSession).toHaveBeenCalledTimes(1); // Only the earlier real public start helper.
    expect(f.native.call.mock.calls.filter(([action]) => action === 'capture')).toHaveLength(1);
    expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(0);
  });

  it.each([[false, true, 0], [true, false, 0], [true, true, 1]] as const)(
    'observes only when the frozen history permission=%s and current permission=%s both allow it',
    async (frozen, current, expectedPolls) => {
      const f = fixture(); f.setLearning(current);
      f.api.getSession.mockResolvedValueOnce(historySession({ learning_enabled: frozen }));
      await f.service.selectForRetry(HISTORY, 'refine');
      const original = f.native.call.getMockImplementation()!;
      f.native.call.mockImplementation(async (action, body) => action === 'insert'
        ? { status: 'inserted', effect: 'written', observable: true, message: 'synthetic adapter only' }
        : action === 'observe' ? { valid: true, text: TEXT, composition: 'committed' } : original(action, body));
      f.api.insertionReceipt.mockImplementation(async (sessionId, insertionId, body) => ({
        replayed: false, session_id: sessionId, insertion_id: insertionId, result_id: HISTORY_RESULT.result_id,
        ...body, claimed_at: '', completed_at: '',
      }));
      await f.service.retarget(0);
      expect((await f.service.insert(true)).status).toBe('inserted');
      await vi.advanceTimersByTimeAsync(300);
      expect(f.native.call.mock.calls.filter(([action]) => action === 'observe')).toHaveLength(expectedPolls);
    });
});

it('history selection cannot interrupt an in-flight retarget capture, including its cancellation cleanup', async () => {
  const f = fixture(); await ready(f); f.service.copy(TEXT);
  const capture = pending<Record<string, unknown>>(), original = f.native.call.getMockImplementation()!;
  f.native.call.mockImplementation((action, body) => action === 'capture' ? capture.promise : original(action, body));
  const retargeting = f.service.retarget(0).catch(error => error);
  await flush();
  expect(f.service.isBusy()).toBe(true);
  await expect(f.service.selectForRetry(HISTORY, 'refine')).rejects.toThrow('进行中');
  expect(f.api.getSession).not.toHaveBeenCalled();
  f.service.reset();
  expect(await retargeting).toBeInstanceOf(AuthContextChangedError);
  capture.resolve({ target: 'late-history-target', app: 'TestEdit' });
  await flush();
  expect(f.service.isBusy()).toBe(false);
  expect(f.service.currentSessionId()).toBeNull();
  expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(0);
  f.api.getSession.mockResolvedValueOnce(historySession());
  expect(await f.service.selectForRetry(HISTORY, 'refine')).toMatchObject({ session_id: HISTORY, auto_insert_allowed: false });
});

it('history selection cannot replace a result while its native insertion is awaiting confirmation', async () => {
  const f = fixture(); await ready(f);
  const write = pending<Record<string, unknown>>(), original = f.native.call.getMockImplementation()!;
  f.native.call.mockImplementation((action, body) => action === 'insert' ? write.promise : original(action, body));
  const inserting = f.service.insert(false);
  await flush();
  expect(f.native.call.mock.calls.filter(([action]) => action === 'insert')).toHaveLength(1);
  await expect(f.service.selectForRetry(HISTORY, 'refine')).rejects.toThrow('进行中');
  expect(f.api.getSession).not.toHaveBeenCalled();
  write.resolve({ status: 'inserted', effect: 'written', observable: false, message: 'synthetic adapter only' });
  expect((await inserting).status).toBe('inserted');
  expect(f.service.currentSessionId()).toBe(SESSION);
});

describe('TC-VOICE-17 local send completion', () => {
  it('waits for the successful short-tail send callback before audio.end', async () => {
    const f = fixture(), ws = await streaming(f);
    const delegate = ws.send.getMockImplementation() as ((data: unknown, callback?: (error?: Error) => void) => void) | undefined;
    expect(delegate).toBeTypeOf('function');
    const events: string[] = [];
    const held: { callback?: (error?: Error) => void; delegated: number; released: number } = {
      delegated: 0, released: 0,
    };
    const ends = () => ws.send.mock.calls
      .filter(call => typeof call[0] === 'string')
      .map(call => JSON.parse(call[0] as string) as Record<string, unknown>)
      .filter(event => event.type === 'audio.end');
    const releaseTail = () => {
      const callback = held.callback;
      if (!callback) return;
      held.callback = undefined;
      held.released++;
      events.push('tail:callback-delivered');
      callback();
    };
    ws.send.mockImplementation((data: unknown, callback?: (error?: Error) => void) => {
      if (Buffer.isBuffer(data)) {
        const seq = data.readUInt32LE(0), samples = data.readUInt32LE(4);
        events.push(`pcm:${seq}:${samples}`);
        if (seq === 1 && samples === 137) {
          // Delegate the actual fixture send successfully; hold only delivery of
          // its completion callback to production SessionService, not server ACK.
          return delegate!(data, (error?: Error) => {
            expect(error).toBeUndefined();
            expect(held.callback).toBeUndefined();
            expect(callback).toBeTypeOf('function');
            held.delegated++;
            held.callback = callback;
            events.push('tail:delegate-success');
          });
        }
      } else if (typeof data === 'string' && JSON.parse(data).type === 'audio.end') {
        const end = JSON.parse(data) as { last_seq: number; total_samples: number };
        events.push(`audio.end:${end.last_seq}:${end.total_samples}`);
      }
      return delegate!(data, callback);
    });
    try {
      f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
      ws.message({ type: 'audio.ack', seq: 0, total_samples: 4000 });
      doubles.now += 250;
      f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
      expect(sends(ws).map(call => {
        const frame = call[0] as Buffer;
        return [frame.readUInt32LE(0), frame.readUInt32LE(4), frame.byteLength - 8];
      })).toEqual([[0, 4000, 8000], [1, 137, 274]]);
      expect(held.delegated).toBe(1);
      expect(held.released).toBe(0);
      expect(held.callback).toBeTypeOf('function');
      events.push('finish:called');
      const finishing = f.service.finish();
      await flush();
      // Soft only so the old-source negative also releases the gate and proves
      // the exact resumed sequence. This remains a failing contract assertion.
      expect.soft(ends(), 'audio.end must not precede the held tail completion').toEqual([]);
      releaseTail();
      await flush();
      expect(held.released).toBe(1);
      expect(ends()).toEqual([{ type: 'audio.end', last_seq: 1, total_samples: 4137 }]);
      expect.soft(events).toEqual([
        'pcm:0:4000', 'pcm:1:137', 'tail:delegate-success', 'finish:called',
        'tail:callback-delivered', 'audio.end:1:4137',
      ]);
      // ACK is deliberately delivered only AFTER the completion/end assertions;
      // delaying ACK cannot satisfy this distinct local-write-completion gate.
      ws.message({ type: 'audio.ack', seq: 1, total_samples: 4137 });
      ws.message({ type: 'audio.end_ack', session_id: SESSION, attempt_no: 1,
        last_seq: 1, total_samples: 4137 });
      ws.message({ type: 'result.ready', session_id: SESSION, attempt_no: 1, result: RESULT_VALUE });
      expect(await finishing).toMatchObject({ state: 'ready', result: RESULT_VALUE });
      expect(f.api.getSession).not.toHaveBeenCalled();
      expect(f.api.cancelSession).not.toHaveBeenCalled();
      expect(f.native.call.mock.calls.some(call => call[0] === 'insert')).toBe(false);
    } finally {
      releaseTail();
      f.service.dispose();
    }
  });
});

describe('TC-VOICE-17 send drain ownership', () => {
  type Completion = (error?: Error) => void;
  const ends = (ws: Socket) => ws.send.mock.calls
    .filter(call => typeof call[0] === 'string')
    .map(call => JSON.parse(call[0] as string) as Record<string, unknown>)
    .filter(event => event.type === 'audio.end');
  function holdPcm(ws: Socket): Completion[] {
    const callbacks: Completion[] = [];
    const delegate = ws.send.getMockImplementation() as ((data: unknown, callback?: Completion) => void) | undefined;
    expect(delegate).toBeTypeOf('function');
    ws.send.mockImplementation((data: unknown, callback?: Completion) => {
      if (!Buffer.isBuffer(data)) return delegate!(data, callback);
      return delegate!(data, (error?: Error) => {
        expect(error).toBeUndefined();
        expect(callback).toBeTypeOf('function');
        callbacks.push(callback!);
      });
    });
    return callbacks;
  }
  function result(ws: Socket): void {
    ws.message({ type: 'result.ready', session_id: SESSION, attempt_no: 1, result: RESULT_VALUE });
  }

  it('waits for every PCM completion and freezes new audio while finishing', async () => {
    const f = fixture(), ws = await streaming(f), callbacks = holdPcm(ws);
    f.service.sendAudio(new ArrayBuffer(8000), SESSION, doubles.now);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    expect(callbacks).toHaveLength(2);
    const first = f.service.finish(), duplicate = f.service.finish();
    f.service.sendAudio(new ArrayBuffer(2), SESSION, doubles.now);
    expect(sends(ws)).toHaveLength(2);
    callbacks[1](); // Last frame callback alone cannot discharge an earlier write.
    expect(ends(ws)).toEqual([]);
    callbacks[0]();
    expect(ends(ws)).toEqual([{ type: 'audio.end', last_seq: 1, total_samples: 4137 }]);
    callbacks[1](); // Even a duplicate callback cannot emit a second end.
    result(ws);
    expect((await first).state).toBe('ready');
    expect((await duplicate).state).toBe('ready');
    expect(ends(ws)).toHaveLength(1);
  });

  it('keeps synchronous completion and repeated finish to one immediate end', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    const first = f.service.finish(), duplicate = f.service.finish();
    // No microtask flush or ACK is required for locally completed writes.
    expect(ends(ws)).toEqual([{ type: 'audio.end', last_seq: 0, total_samples: 137 }]);
    result(ws);
    expect((await first).state).toBe('ready');
    expect((await duplicate).state).toBe('ready');
  });

  it('settles a failed pending write through existing recovery without any late end', async () => {
    const f = fixture(), ws = await streaming(f), callbacks = holdPcm(ws);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    const finishing = f.service.finish();
    expect(ends(ws)).toEqual([]);
    callbacks[0](new Error('synthetic local write failure'));
    await flush();
    expect(await finishing).toMatchObject({ state: 'failed', error_code: 'AUDIO_INTERRUPTED' });
    expect(f.api.getSession).toHaveBeenCalledOnce();
    callbacks[0]();
    expect(ends(ws)).toEqual([]);
    expect(f.service.current().result).toBeNull();
  });

  it('cancels a pending write without awaiting its callback or sending a late end', async () => {
    const f = fixture(), ws = await streaming(f), callbacks = holdPcm(ws);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    const finishing = f.service.finish();
    expect((await f.service.cancel()).state).toBe('cancelled');
    expect((await finishing).state).toBe('cancelled');
    callbacks[0]();
    expect(ends(ws)).toEqual([]);
    expect(f.api.cancelSession).toHaveBeenCalledOnce();
    expect(f.service.current().result).toBeNull();
  });

  it.each(['reset', 'invalidateAccount'] as const)('%s fences old callbacks from a same-id new operation', async action => {
    const f = fixture(), oldWs = await streaming(f), oldCallbacks = holdPcm(oldWs);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    const oldFinish = f.service.finish();
    f.service[action]();
    expect((await oldFinish).state).toBe('cancelled');
    const newWs = await streaming(f), newCallbacks = holdPcm(newWs);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    const newFinish = f.service.finish();
    oldCallbacks[0](action === 'invalidateAccount' ? new Error('stale write error') : undefined);
    expect(ends(oldWs)).toEqual([]);
    expect(ends(newWs)).toEqual([]);
    expect(f.api.getSession).not.toHaveBeenCalled();
    newCallbacks[0]();
    expect(ends(newWs)).toEqual([{ type: 'audio.end', last_seq: 0, total_samples: 137 }]);
    result(newWs);
    expect((await newFinish).state).toBe('ready');
  });

  it('retains the original two-second unacknowledged deadline while a callback is held', async () => {
    const f = fixture(), ws = await streaming(f), callbacks = holdPcm(ws);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    const finishing = f.service.finish();
    doubles.now += 1999; vi.advanceTimersByTime(1999);
    expect(f.service.current().state).toBe('finalizing');
    doubles.now += 1; vi.advanceTimersByTime(1);
    expect(await finishing).toMatchObject({ state: 'failed', error_code: 'AUDIO_BACKPRESSURE' });
    callbacks[0]();
    expect(ends(ws)).toEqual([]);
    expect(f.api.cancelSession).toHaveBeenCalledOnce();
  });

  it('does not create a new two-second deadline for an already acknowledged completion ticket', async () => {
    const f = fixture(), ws = await streaming(f), callbacks = holdPcm(ws);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 137 });
    const finishing = f.service.finish();
    doubles.now += 2001; vi.advanceTimersByTime(2001);
    expect(f.service.current()).toMatchObject({ state: 'finalizing', error_code: null });
    expect(ends(ws)).toEqual([]);
    callbacks[0]();
    expect(ends(ws)).toEqual([{ type: 'audio.end', last_seq: 0, total_samples: 137 }]);
    result(ws);
    expect((await finishing).state).toBe('ready');
    expect(f.api.cancelSession).not.toHaveBeenCalled();
  });

  it('keeps the original finish timeout even when ACK cleared the audio deadline', async () => {
    const f = fixture(), ws = await streaming(f), callbacks = holdPcm(ws);
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 137 });
    const finishing = f.service.finish();
    doubles.now += 34_999; vi.advanceTimersByTime(34_999);
    expect(f.service.current().state).toBe('finalizing');
    expect(ends(ws)).toEqual([]);
    doubles.now += 1; vi.advanceTimersByTime(1);
    expect(await finishing).toMatchObject({ state: 'failed', error_code: 'RESULT_TIMEOUT' });
    callbacks[0]();
    expect(ends(ws)).toEqual([]);
    expect(f.api.cancelSession).toHaveBeenCalledOnce();
  });
});

it('TC-VOICE-17 rejects a completion delivered at the finish deadline before timers run', async () => {
  const f = fixture(), ws = await streaming(f);
  const delegate = ws.send.getMockImplementation() as ((data: unknown, callback?: (error?: Error) => void) => void) | undefined;
  expect(delegate).toBeTypeOf('function');
  const held: { callback?: (error?: Error) => void } = {};
  ws.send.mockImplementation((data: unknown, callback?: (error?: Error) => void) => {
    if (!Buffer.isBuffer(data)) return delegate!(data, callback);
    return delegate!(data, (error?: Error) => {
      expect(error).toBeUndefined();
      expect(callback).toBeTypeOf('function');
      held.callback = callback;
    });
  });
  const ends = () => ws.send.mock.calls
    .filter(call => typeof call[0] === 'string')
    .map(call => JSON.parse(call[0] as string) as Record<string, unknown>)
    .filter(event => event.type === 'audio.end');
  try {
    f.service.sendAudio(new ArrayBuffer(274), SESSION, doubles.now);
    ws.message({ type: 'audio.ack', seq: 0, total_samples: 137 });
    const finishing = f.service.finish();
    let settled: SessionProgress | undefined;
    void finishing.then(value => { settled = value; });
    expect(held.callback).toBeTypeOf('function');
    expect(ends()).toEqual([]);
    doubles.now += 35_000; // Deliberately do NOT advance/run any timer callbacks.
    held.callback!();
    await flush();
    expect.soft(ends(), 'an overdue timer must not permit late audio.end').toEqual([]);
    expect.soft(f.service.current()).toMatchObject({ state: 'failed', error_code: 'RESULT_TIMEOUT' });
    expect.soft(settled).toMatchObject({ state: 'failed', error_code: 'RESULT_TIMEOUT' });
    expect.soft(f.api.cancelSession).toHaveBeenCalledOnce();
    expect(f.api.getSession).not.toHaveBeenCalled();
    // Prevent an old-source negative from waiting for its intentionally unrun timer.
    // Root retains the soft failures above; disposing cannot make them pass.
    f.service.dispose();
    await finishing;
    held.callback!();
    expect(f.service.current()).toMatchObject({ session_id: null, result: null });
  } finally {
    f.service.dispose();
  }
});


// Main-local capture readiness is independent from the server's WS ready.
describe('capture readiness identity and lifecycle', () => {
  it('keeps WS-ready preparing until the matching local capture acknowledges', async () => {
    const f = fixture(); await streaming(f);
    expect(f.service.current()).toMatchObject({ state: 'streaming', capture_ready: false });
    const revision = f.service.current().revision;
    for (const [sid, attempt] of [[SESSION, 2], [RESULT, 1], [SESSION, NaN]] as const) {
      expect(() => f.service.captureReady(sid, attempt)).toThrow();
      expect(f.service.current()).toMatchObject({ capture_ready: false, revision });
    }
    const ready = f.service.captureReady(SESSION, 1);
    expect(ready).toMatchObject({ session_id: SESSION, attempt_no: 1, state: 'streaming', capture_ready: true });
    expect(f.service.captureReady(SESSION, 1).revision).toBe(ready.revision);
  });

  it('clears readiness at terminal and refuses a late startup acknowledgement', async () => {
    const f = fixture(), ws = await streaming(f);
    f.service.captureReady(SESSION, 1);
    ws.message({ type: 'error', error: { code: 'AUDIO_INTERRUPTED' } });
    expect(f.service.current()).toMatchObject({ state: 'failed', capture_ready: false });
    expect(() => f.service.captureReady(SESSION, 1)).toThrow();
    expect(f.service.current().capture_ready).toBe(false);
  });

  it('does not reuse readiness across reset, another SID or an account boundary', async () => {
    const f = fixture(); await streaming(f); f.service.captureReady(SESSION, 1);
    f.service.reset();
    expect(f.service.current()).toMatchObject({ state: 'created', session_id: null, capture_ready: false });
    expect(() => f.service.captureReady(SESSION, 1)).toThrow();
    const next = '77777777-7777-4777-8777-777777777777';
    f.api.createSession.mockResolvedValueOnce(session({ session_id: next }));
    const started = f.service.start(false); await flush();
    const ws = doubles.sockets.at(-1) as Socket;
    ws.message({ type: 'ready', session_id: next, attempt_no: 1,
      audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1, chunk_samples: 4000, max_samples: 2_880_000 } });
    await started;
    expect(() => f.service.captureReady(SESSION, 1)).toThrow();
    expect(f.service.captureReady(next, 1).capture_ready).toBe(true);
    f.auth.invalidate();
    expect(f.service.current()).toMatchObject({ session_id: null, capture_ready: false });
    expect(() => f.service.captureReady(next, 1)).toThrow();
  });
});


describe('cloud retry observation window', () => {
  async function prepared(kind: 'asr' | 'refine' = 'asr', audioMs = 85_000) {
    const f = fixture();
    f.api.getSession.mockResolvedValueOnce(historySession({
      state: kind === 'asr' ? 'failed' : 'ready', retry_kind: kind,
      ...(kind === 'asr' ? { current_result_id: null, result: null, error_code: 'ASR_FAILED' } : {}),
      audio_ms: audioMs, received_samples: audioMs * 16,
    }));
    await f.service.selectForRetry(HISTORY, kind);
    const active = historySession({ state: kind === 'asr' ? 'finalizing' : 'refining',
      current_attempt_no: 2, current_result_id: null, result: null, retry_kind: 'none',
      audio_ms: audioMs, received_samples: audioMs * 16 });
    f.api.retrySession.mockResolvedValueOnce(active);
    f.api.getSession.mockResolvedValue(active);
    return { f, active };
  }
  const successful = (active: Session): Session => ({ ...active, state: 'ready',
    result: { ...RESULT_VALUE, attempt_no: 2, attempt_kind: 'asr' }, current_result_id: RESULT });
  async function step(ms: number) { doubles.now += ms; await vi.advanceTimersByTimeAsync(1000); await flush(); }

  it.each([85_000, 360_000])('keeps a legal long retry running after 35 seconds, then adopts its result (%s ms audio)', async audioMs => {
    const { f, active } = await prepared('asr', audioMs);
    const task = f.service.retry('asr'); await flush();
    await step(36_000);
    expect(f.service.current()).toMatchObject({ state: 'finalizing', error_code: null });
    expect(f.api.cancelSession).not.toHaveBeenCalled();
    f.api.getSession.mockResolvedValue(successful(active));
    await step(audioMs === 360_000 ? 184_000 : 24_000);
    expect(await task).toMatchObject({ state: 'ready', attempt_no: 2, auto_insert_allowed: false });
    expect(f.api.claimInsertion).not.toHaveBeenCalled();
  });

  it('ends immediately on the server deadline failure without renewing or repeating the retry', async () => {
    const { f, active } = await prepared();
    const task = f.service.retry('asr'); await flush();
    f.api.getSession.mockResolvedValue({ ...active, state: 'failed', error_code: 'ASR_TIMEOUT' });
    await step(12_000);
    expect(await task).toMatchObject({ state: 'failed', error_code: 'ASR_TIMEOUT' });
    expect(f.api.retrySession).toHaveBeenCalledOnce();
    expect(f.api.cancelSession).not.toHaveBeenCalled();
  });

  it('keeps long-retry polling below the authenticated HTTP rate limit', async () => {
    const { f, active } = await prepared(); f.api.getSession.mockClear();
    const task = f.service.retry('asr'); await flush();
    for (let second = 0; second < 60; second++) await step(1000);
    expect(f.service.current().state).toBe('finalizing');
    expect(f.api.getSession.mock.calls.length).toBeLessThanOrEqual(61);
    f.api.getSession.mockResolvedValue(successful(active)); await step(1000);
    expect((await task).state).toBe('ready');
  });

  it('keeps the refinement-only retry window at 35 seconds', async () => {
    const { f } = await prepared('refine');
    const task = f.service.retry('refine'); await flush();
    await step(35_000);
    expect(await task).toMatchObject({ state: 'failed', error_code: 'RESULT_TIMEOUT' });
    expect(f.api.cancelSession).toHaveBeenCalledOnce();
  });

  it('caps the wait and rejects a late GET even when its timeout callback has not run', async () => {
    const { f, active } = await prepared('asr', 360_000);
    const held = pending<Session>(); f.api.getSession.mockReturnValueOnce(held.promise);
    const task = f.service.retry('asr'); await flush();
    doubles.now += 395_000; // No timer callbacks run before the late response.
    held.resolve(successful(active)); await flush();
    expect(await task).toMatchObject({ state: 'failed', error_code: 'RESULT_TIMEOUT', result: null });
    expect(f.api.cancelSession).toHaveBeenCalledOnce();
  });

  it.each(['cancel', 'account'] as const)('does not adopt an old result after %s during a long retry', async ending => {
    const { f, active } = await prepared();
    const held = pending<Session>(); f.api.getSession.mockReturnValueOnce(held.promise);
    const task = f.service.retry('asr'); const rejected = expect(task).rejects.toBeInstanceOf(AuthContextChangedError);
    await flush(); doubles.now += 36_000;
    if (ending === 'cancel') await f.service.cancel(); else f.auth.invalidate();
    held.resolve(successful(active)); await rejected;
    expect(f.service.current().result).toBeNull();
    expect(f.api.claimInsertion).not.toHaveBeenCalled();
  });
});

it('Windows disables observation per session while strict inserted/observable and account preference remain intact', async () => {
  const f=fixture();f.setLearning(true);Object.defineProperty(f.native,'supportsObservation',{value:false});
  f.api.createSession.mockResolvedValueOnce(session({learning_enabled:true}));
  const ws=await streaming(f,true);
  expect(f.api.createSession).toHaveBeenCalledWith(expect.any(String),false);
  f.service.sendAudio(new ArrayBuffer(8000),SESSION,doubles.now);ws.message({type:'audio.ack',seq:0,total_samples:4000});
  const finished=f.service.finish();ws.message({type:'result.ready',session_id:SESSION,attempt_no:1,result:RESULT_VALUE});await finished;await flush();
  const original=f.native.call.getMockImplementation()!;
  f.native.call.mockImplementation(async(action,body)=>action==='insertCurrent'?{status:'inserted',effect:'written',observable:true,target:'windows-target'}:original(action,body));
  const outcome=await f.service.insert(false,false);await flush();
  expect(outcome).toMatchObject({status:'inserted',observable:true});expect(f.auth.learningEnabled).toBe(true);
  expect(f.native.call.mock.calls.some(([action])=>action==='observe')).toBe(false);
  expect(f.native.call).toHaveBeenCalledWith('release',{target:'windows-target'});
});

it('preserves a native clipboard cleanup warning independently of an inserted receipt', async () => {
  const f=fixture();await ready(f);const original=f.native.call.getMockImplementation()!;
  f.native.call.mockImplementation(async(action,body)=>action==='insertCurrent'?{status:'inserted',effect:'written',observable:true,target:'native-target',warning:'剪贴板恢复失败，请检查剪贴板；本次不会再次输入'}:original(action,body));
  const outcome=await f.service.insert(false,false);await flush();
  expect(outcome).toMatchObject({status:'inserted',observable:true,warning:'剪贴板恢复失败，请检查剪贴板；本次不会再次输入'});
  expect(f.api.insertionReceipt).toHaveBeenCalledWith(SESSION,outcome.insertion_id,{status:'inserted',observable:true,error_code:null});
});
