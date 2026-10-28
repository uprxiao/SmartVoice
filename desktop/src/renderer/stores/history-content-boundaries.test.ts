import { createHash } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@shared/types';

const port = vi.hoisted(() => ({ list: vi.fn(), copy: vi.fn() }));
vi.mock('../lib/ipc', () => ({ rpc: { history: { list: port.list }, session: { copy: port.copy } } }));
import { HistoryStore } from './history-store';

const START = Date.parse('2026-10-18T04:00:00.000Z');
const TIMER_MAX = 2_147_483_647;
const live: HistoryStore[] = [];
type CopyResult = 'copied' | 'unavailable' | 'stale';

// Invoke the real public Store method. This narrow type bridge permits a
// tests-only old-source run: old copy(text) still dispatches and fails the
// behavioral assertions; no new production module is imported at collection.
function copy(store: HistoryStore, sessionId: string, resultId: string, variant: 'raw' | 'refined') {
  return (store.copy as unknown as (s: string, r: string, v: 'raw' | 'refined') => Promise<CopyResult>)
    .call(store, sessionId, resultId, variant);
}
function deferred() {
  let resolve!: () => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const sid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const rid = (n: number) => `10000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
function row(n: number, expires: string | null, patch: Partial<Session> = {}): Session {
  const raw = `  虚构原文 ${n} 🙂\n保留前后空格  `;
  const refined = `整理稿 ${n}：\n保留换行。`;
  return {
    session_id: sid(n), mode: 'dictation', state: 'ready', current_attempt_no: 1,
    learning_enabled: false, current_result_id: rid(n), audio_complete: true,
    received_samples: 48000, audio_ms: 3000, created_at: '2026-10-18T03:00:00Z',
    first_terminal_at: '2026-10-18T03:00:01Z', content_expires_at: expires,
    content_expired: false, error_code: null, retry_kind: 'refine',
    retry_expires_at: null, retry_unavailable_reason: null, insertion_status: 'failed',
    result: { result_id: rid(n), attempt_no: 1, attempt_kind: 'initial', raw_text: raw,
      refined_text: refined, review_required: true, review_reason: 'llm_unavailable',
      text_hash: createHash('sha256').update(refined).digest('hex'), dictionary_version: 2,
      stop_to_result_ms: 125, created_at: '2026-10-18T03:00:01Z' }, ...patch,
  };
}
function metadata(item: Session) {
  const { result: _result, content_expired: _expired, retry_kind: _retry,
    retry_expires_at: _retryDeadline, retry_unavailable_reason: _reason, ...kept } = item;
  return kept;
}
async function loaded(items: Session[]): Promise<HistoryStore> {
  const store = new HistoryStore(); live.push(store);
  port.list.mockResolvedValueOnce({ items, next_cursor: null });
  await store.load();
  return store;
}
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(START);
  port.list.mockReset(); port.copy.mockReset().mockResolvedValue(undefined);
});
afterEach(() => {
  for (const store of live.splice(0)) store.clear();
  try { expect(vi.getTimerCount()).toBe(0); } finally { vi.useRealTimers(); }
});

// Actual HistoryStore + declared IPC doubles, fake clock and controlled
// promises. No Electron, real clipboard, HTTP, model or natural retention claim.
describe('history cached-body deadline and copy ownership', () => {
  it('copies each exact field before D without normalizing text or rereading history', async () => {
    const item = row(1, new Date(START + 1000).toISOString());
    const store = await loaded([item]);
    await vi.advanceTimersByTimeAsync(999);
    expect(await copy(store, item.session_id, rid(1), 'raw')).toBe('copied');
    expect(await copy(store, item.session_id, rid(1), 'refined')).toBe('copied');
    expect(port.copy.mock.calls).toEqual([[item.result!.raw_text], [item.result!.refined_text]]);
    expect(port.list).toHaveBeenCalledTimes(1);
    expect(store.items).toEqual([item]);
  });

  it('at D keeps row order and all Session metadata while clearing only body and retry permission', async () => {
    const items = [row(1, null), row(2, new Date(START + 1000).toISOString()),
      row(3, new Date(START - 1).toISOString(), { result: null, content_expired: true,
        retry_kind: 'none', retry_unavailable_reason: 'content_expired' })];
    const expected = items.map(metadata);
    const store = await loaded(items);
    expect(store.items.map(item => item.session_id)).toEqual(items.map(item => item.session_id));
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(store.items.map(metadata)).toEqual(expected);
    expect(store.items[0]).toEqual(items[0]);
    for (const item of store.items.slice(1)) {
      expect(item).toMatchObject({ result: null, content_expired: true, retry_kind: 'none',
        retry_expires_at: null, retry_unavailable_reason: 'content_expired' });
    }
    expect(vi.getTimerCount()).toBe(0);
    expect(port.copy).not.toHaveBeenCalled();
    expect(port.list).toHaveBeenCalledTimes(1);
  });

  it('rejects a retained old item at D even when the expiry timer has not run', async () => {
    const item = row(1, new Date(START + 1000).toISOString());
    const store = await loaded([item]);
    vi.setSystemTime(START + 1000); // Changes wall clock; deliberately does not execute timers.
    expect(vi.getTimerCount()).toBe(1);
    expect(store.items[0].result).not.toBeNull();
    expect(await copy(store, item.session_id, item.result!.result_id, 'raw')).toBe('unavailable');
    expect(port.copy).not.toHaveBeenCalled();
    expect(store.items.map(metadata)).toEqual([metadata(item)]);
    expect(store.items[0].result).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(['success', 'failure'] as const)('clear fences old copy %s and cancels the old account timer', async outcome => {
    const old = row(1, new Date(START + 1000).toISOString());
    const current = row(2, new Date(START + 5000).toISOString());
    const store = await loaded([old]);
    const pending = deferred(); port.copy.mockReturnValueOnce(pending.promise);
    const copying = copy(store, old.session_id, rid(1), 'raw');
    expect(port.copy.mock.calls).toEqual([[old.result!.raw_text]]);
    store.clear();
    expect(vi.getTimerCount()).toBe(0);
    port.list.mockResolvedValueOnce({ items: [current], next_cursor: null });
    await store.load();
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(store.items).toEqual([current]);
    expect(vi.getTimerCount()).toBe(1);
    if (outcome === 'success') pending.resolve(); else pending.reject(new Error('synthetic clipboard rejection'));
    expect(await copying).toBe('stale');
    expect(store.items).toEqual([current]);
    expect(port.copy).toHaveBeenCalledTimes(1);
    expect(port.list).toHaveBeenCalledTimes(2);
  });

  it('splits a deadline beyond the JS timer maximum without clearing early or adding a second timer', async () => {
    const item = row(1, new Date(START + TIMER_MAX + 5000).toISOString());
    const store = await loaded([item]);
    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(TIMER_MAX);
    expect(store.items).toEqual([item]); expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(4999);
    expect(store.items).toEqual([item]);
    await vi.advanceTimersByTimeAsync(1);
    expect(store.items.map(metadata)).toEqual([metadata(item)]);
    expect(store.items[0].result).toBeNull(); expect(store.items[0].content_expired).toBe(true);
    expect(vi.getTimerCount()).toBe(0); expect(port.copy).not.toHaveBeenCalled();
  });

  it('invalid finite deadlines remove body permission but keep metadata and never dispatch copy', async () => {
    const item = row(1, 'invalid-deadline');
    const store = await loaded([item]);
    expect(store.items.map(metadata)).toEqual([metadata(item)]);
    expect(store.items[0]).toMatchObject({ result: null, content_expired: true, retry_kind: 'none' });
    expect(await copy(store, item.session_id, rid(1), 'refined')).toBe('unavailable');
    expect(port.copy).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0);
  });

  it('rejects unknown sessions, old result IDs and inconsistent cached result/attempt identities', async () => {
    const first = row(1, null);
    const wrongResult = row(2, null); wrongResult.result = { ...wrongResult.result!, result_id: rid(20) };
    const wrongAttempt = row(3, null, { current_attempt_no: 2 });
    const items = [first, wrongResult, wrongAttempt]; const store = await loaded(items);
    expect(await copy(store, sid(99), rid(1), 'raw')).toBe('unavailable');
    expect(await copy(store, sid(1), rid(99), 'raw')).toBe('unavailable');
    expect(await copy(store, sid(2), rid(2), 'raw')).toBe('unavailable');
    expect(await copy(store, sid(3), rid(3), 'raw')).toBe('unavailable');
    expect(store.items).toEqual(items); expect(port.copy).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
