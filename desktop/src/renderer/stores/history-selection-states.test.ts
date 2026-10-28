import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { runInAction } from 'mobx';
import type { AuthSnapshot, SessionProgress, SessionResult } from '@shared/types';

const doubles = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn().mockResolvedValue(undefined), listeners: new Map<string, (value: unknown) => void>() }));
vi.mock('../audio/recorder', () => ({ PcmRecorder: class { start = doubles.start; stop = doubles.stop; acknowledge = vi.fn(); } }));
vi.mock('../lib/ipc', () => ({
  events: { on: (event: { name: string }, handler: (value: unknown) => void) => { doubles.listeners.set(event.name, handler); return () => doubles.listeners.delete(event.name); }, emit: vi.fn() },
  rpc: { history: { selectForRetry: vi.fn() }, session: { status: vi.fn(), reset: vi.fn().mockResolvedValue(undefined),
    cancel: vi.fn(), retry: vi.fn(), insert: vi.fn(), cancelStart: vi.fn().mockResolvedValue(undefined) }, auth: {}, settings: {} },
}));
import { rpc } from '../lib/ipc';
import { AuthStore } from './auth-store';
import { SessionStore } from './session-store';
import type { UiStore } from './ui-store';

const USER = '11111111-1111-4111-8111-111111111111';
const OLD = '22222222-2222-4222-8222-222222222222';
const FIRST = '33333333-3333-4333-8333-333333333333';
const NEXT = '44444444-4444-4444-8444-444444444444';
const result = { result_id: '55555555-5555-4555-8555-555555555555', attempt_no: 1, attempt_kind: 'initial',
  raw_text: '虚构保留原文', refined_text: '虚构保留整理稿', review_required: true, review_reason: 'llm_unavailable',
  text_hash: 'a'.repeat(64), dictionary_version: 0, stop_to_result_ms: 9000, created_at: '2026-10-18T00:00:00Z' } satisfies SessionResult;
const progress = (id: string | null, patch: Partial<SessionProgress> = {}): SessionProgress => ({
  session_id: id, attempt_no: id ? 1 : null, revision: id ? 10 : 0, acknowledged_samples: 0, auto_insert_allowed: false,
  state: id ? 'ready' : 'created', audio_ms: 1000, stable_text: '', provisional_text: '', error_code: null,
  result: id ? result : null, ...patch,
});
const snapshot = (id = USER) => ({ user: { id }, settings: { locale: 'zh-CN', learning_enabled: false } } as AuthSnapshot);
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };
function pending<T>() { let resolve!: (value: T) => void, reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
// Optional structural observation keeps these tests typecheckable on old production.
function selection(store: SessionStore) { return (store as SessionStore & { historySelection?: {
  sessionId: string; kind: 'asr' | 'refine'; loading: boolean; error: string | null;
} | null }).historySelection; }
const fixtures: SessionStore[] = [];
async function fixture(restored: string | null = null) {
  vi.mocked(rpc.session.status).mockResolvedValueOnce(progress(restored));
  const auth = new AuthStore(); auth.snapshot = snapshot();
  const ui = { toast: vi.fn() }; const store = new SessionStore(auth, ui as unknown as UiStore);
  fixtures.push(store); store.init(); await flush(); return { auth, store, ui };
}
beforeEach(() => {
  vi.resetAllMocks(); doubles.stop.mockResolvedValue(undefined); doubles.listeners.clear();
  vi.mocked(rpc.session.status).mockResolvedValue(progress(null));
  vi.mocked(rpc.session.reset).mockResolvedValue(undefined);
  vi.mocked(rpc.session.cancel).mockResolvedValue(progress(null, { state: 'cancelled' }));
});
afterEach(() => { for (const store of fixtures.splice(0)) store.invalidateLocal(); });

describe('history GET selection state through public Store and controlled IPC', () => {
  it.each(['选择读取失败', 'CONTENT_EXPIRED'])('preserves the current result on %s and retries GET only after a second explicit call', async (message) => {
    const { store } = await fixture(OLD); const before = JSON.parse(JSON.stringify(store.progress));
    const first = pending<SessionProgress>(); vi.mocked(rpc.history.selectForRetry).mockReturnValueOnce(first.promise);
    const selecting = store.selectHistoryForRetry(FIRST, 'refine');
    try {
      expect(selection(store)).toEqual({ sessionId: FIRST, kind: 'refine', loading: true, error: null });
      expect(store.busy).toBe(true); expect(store.progress).toEqual(before);
      expect(await store.selectHistoryForRetry(FIRST, 'refine')).toBe(false);
      expect(await store.selectHistoryForRetry(NEXT, 'refine')).toBe(false);
      expect(rpc.history.selectForRetry).toHaveBeenCalledTimes(1);
      first.reject(new Error(message)); expect(await selecting).toBe(false);
      expect(selection(store)).toEqual({ sessionId: FIRST, kind: 'refine', loading: false, error: message });
      expect(store.progress).toEqual(before); expect(store.error).toBeNull(); expect(store.busy).toBe(false);
      await flush(); expect(rpc.history.selectForRetry).toHaveBeenCalledTimes(1);
      expect(rpc.session.retry).not.toHaveBeenCalled(); expect(rpc.session.insert).not.toHaveBeenCalled();
      // A new explicit click still goes through Main's fresh authoritative GET;
      // a transient failure can recover, an expired record still cannot.
      if (message === 'CONTENT_EXPIRED') {
        vi.mocked(rpc.history.selectForRetry).mockRejectedValueOnce(new Error(message));
        expect(await store.selectHistoryForRetry(FIRST, 'refine')).toBe(false);
        expect(store.progress).toEqual(before);
      } else {
        vi.mocked(rpc.history.selectForRetry).mockResolvedValueOnce(progress(FIRST));
        expect(await store.selectHistoryForRetry(FIRST, 'refine')).toBe(true);
        expect(selection(store)).toBeNull(); expect(store.busy).toBe(false);
        expect(store.progress.session_id).toBe(FIRST); expect(store.progress.auto_insert_allowed).toBe(false);
        expect(store.targetApp).toBeNull(); expect(store.insertionAvailable).toBe(false);
      }
      expect(rpc.history.selectForRetry).toHaveBeenCalledTimes(2);
      expect(rpc.session.retry).not.toHaveBeenCalled(); expect(doubles.start).not.toHaveBeenCalled();
    } finally { first.reject(new Error('cleanup')); await selecting; }
  });

  it.each(['cancel', 'reset', 'clear'] as const)('%s clears pending state; a late failure cannot clear a newer selection', async (operation) => {
    const { store } = await fixture(); const first = pending<SessionProgress>(), next = pending<SessionProgress>();
    vi.mocked(rpc.history.selectForRetry).mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
    const old = store.selectHistoryForRetry(FIRST, 'refine'); let newer: Promise<boolean> | undefined;
    try {
      if (operation === 'clear') store.invalidateLocal(); else await store[operation]();
      expect(selection(store)).toBeNull(); expect(store.busy).toBe(false);
      newer = store.selectHistoryForRetry(NEXT, 'refine');
      first.reject(new Error('旧请求错误')); expect(await old).toBe(false);
      expect(selection(store)).toEqual({ sessionId: NEXT, kind: 'refine', loading: true, error: null });
      expect(store.busy).toBe(true);
      next.resolve(progress(NEXT)); expect(await newer).toBe(true);
      expect(selection(store)).toBeNull(); expect(store.busy).toBe(false);
      expect(store.progress.session_id).toBe(NEXT); expect(rpc.session.retry).not.toHaveBeenCalled();
    } finally { first.reject(new Error('cleanup')); next.resolve(progress(NEXT)); await Promise.all([old, newer]); }
  });

  it.each(['success', 'failure'] as const)('account switch fences an old GET %s and its finally', async (outcome) => {
    const { auth, store, ui } = await fixture(); const first = pending<SessionProgress>(), next = pending<SessionProgress>();
    vi.mocked(rpc.history.selectForRetry).mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
    const old = store.selectHistoryForRetry(FIRST, 'refine'); let newer: Promise<boolean> | undefined;
    try {
      runInAction(() => { auth.snapshot = null; }); expect(selection(store)).toBeNull();
      runInAction(() => { auth.snapshot = snapshot('66666666-6666-4666-8666-666666666666'); }); await flush();
      newer = store.selectHistoryForRetry(NEXT, 'refine');
      if (outcome === 'success') first.resolve(progress(FIRST)); else first.reject(new Error('旧账号错误'));
      expect(await old).toBe(false);
      expect(selection(store)).toMatchObject({ sessionId: NEXT, loading: true, error: null });
      expect(store.busy).toBe(true); expect(store.result).toBeNull(); expect(ui.toast).not.toHaveBeenCalled();
      next.resolve(progress(NEXT)); expect(await newer).toBe(true);
      expect(selection(store)).toBeNull(); expect(store.progress.session_id).toBe(NEXT);
      expect(rpc.session.retry).not.toHaveBeenCalled(); expect(rpc.session.insert).not.toHaveBeenCalled();
    } finally { first.reject(new Error('cleanup')); next.resolve(progress(NEXT)); await Promise.all([old, newer]); }
  });

  it('an unsafe snapshot remains a persistent selection error, not an adopted result', async () => {
    const { store } = await fixture(OLD); const before = JSON.parse(JSON.stringify(store.progress));
    vi.mocked(rpc.history.selectForRetry).mockResolvedValueOnce(progress(FIRST, { auto_insert_allowed: true }));
    expect(await store.selectHistoryForRetry(FIRST, 'refine')).toBe(false);
    expect(selection(store)).toMatchObject({ sessionId: FIRST, loading: false, error: '历史会话快照无效' });
    expect(store.progress).toEqual(before); expect(store.busy).toBe(false);
    expect(rpc.session.retry).not.toHaveBeenCalled(); expect(rpc.session.insert).not.toHaveBeenCalled();
  });
});
