import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '@shared/types';
vi.mock('./ipc', () => ({ rpc: { history: { list: vi.fn() }, session: { copy: vi.fn() } } }));
import { rpc } from './ipc';
import { HistoryStore } from '../stores/history-store';
import { RetentionChange, shortensRetention, type RetentionContext, type RetentionDays } from './retention-change';

const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function fixture(days: RetentionDays = null) {
  let context: RetentionContext | null = { userId: 'account-a', version: 1, days };
  const save = vi.fn(async (patch: { version: number; history_retention_days: RetentionDays }) => {
    if (context) context = { ...context, version: patch.version + 1, days: patch.history_retention_days };
  });
  const clearHistory = vi.fn();
  const reloadHistory = vi.fn(async () => {});
  const change = new RetentionChange({ read: () => context, save, clearHistory, reloadHistory });
  return { change, save, clearHistory, reloadHistory, read: () => context,
    setContext: (next: RetentionContext | null) => { context = next; } };
}

beforeEach(() => { vi.clearAllMocks(); });

describe('retention confirmation (TC-HIST-15)', () => {
  it.each([
    [null, 7, true], [null, 30, true], [null, 90, true],
    [90, 30, true], [90, 7, true], [30, 7, true],
    [7, 30, false], [30, 90, false], [90, null, false],
    [null, null, false], [7, 7, false],
  ] as const)('shortening %s to %s requires confirmation: %s', (before, next, expected) => {
    expect(shortensRetention(before, next)).toBe(expected);
  });

  it('selecting a shorter period then cancelling leaves settings/version and API untouched', async () => {
    const f = fixture();
    await f.change.request(7);
    expect(f.change.needsConfirmation).toBe(true);
    expect(f.save).not.toHaveBeenCalled();
    expect(f.read()).toEqual({ userId: 'account-a', days: null, version: 1 });
    f.change.cancel();
    expect(f.change.needsConfirmation).toBe(false);
    expect(await f.change.confirm()).toBe(false);
    expect(f.save).not.toHaveBeenCalled();
    expect(f.clearHistory).not.toHaveBeenCalled();
    expect(f.read()?.version).toBe(1);
  });

  it('a double confirm and another selection during the write issue one PATCH only', async () => {
    const f = fixture(); const write = deferred<void>();
    f.save.mockImplementationOnce(async (patch) => {
      await write.promise;
      f.setContext({ userId: 'account-a', version: patch.version + 1, days: patch.history_retention_days });
    });
    await f.change.request(7);
    const pending = f.change.confirm();
    expect(f.change.busy).toBe(true);
    expect(await f.change.confirm()).toBe(false);
    expect(await f.change.request(30)).toBe(false);
    f.change.cancel();
    expect(f.change.needsConfirmation).toBe(true);
    expect(f.read()?.days).toBeNull();
    write.resolve(); expect(await pending).toBe(true);
    expect(f.save.mock.calls).toEqual([[{ version: 1, history_retention_days: 7 }]]);
    expect(f.read()).toEqual({ userId: 'account-a', version: 2, days: 7 });
    expect(f.clearHistory).toHaveBeenCalledTimes(1);
    expect(f.reloadHistory).toHaveBeenCalledTimes(1);
    expect(f.change.pending).toBeNull();
  });

  it('a failed save keeps the old committed value/version and allows explicit retry', async () => {
    const f = fixture(); const failure = new Error('temporary offline');
    f.save.mockRejectedValueOnce(failure);
    await f.change.request(7); expect(await f.change.confirm()).toBe(false);
    expect(f.read()?.days).toBeNull(); expect(f.read()?.version).toBe(1);
    expect(f.change.error).toBe(failure); expect(f.change.busy).toBe(false);
    expect(f.change.needsConfirmation).toBe(true); expect(f.clearHistory).not.toHaveBeenCalled();
    expect(await f.change.confirm()).toBe(true);
    expect(f.save.mock.calls).toEqual([
      [{ version: 1, history_retention_days: 7 }], [{ version: 1, history_retention_days: 7 }],
    ]);
    expect(f.read()?.days).toBe(7);
  });

  it('extending retention saves directly and does not repopulate any cleared history', async () => {
    const f = fixture(7);
    expect(await f.change.request(30)).toBe(true);
    expect(f.change.needsConfirmation).toBe(false);
    expect(f.save).toHaveBeenCalledWith({ version: 1, history_retention_days: 30 });
    expect(f.clearHistory).not.toHaveBeenCalled(); expect(f.reloadHistory).not.toHaveBeenCalled();
    expect(await f.change.request(30)).toBe(false); expect(f.save).toHaveBeenCalledTimes(1);
  });

  it.each(['account', 'version', 'unmount'] as const)('an old confirmation cannot write after %s changes', async (kind) => {
    const f = fixture(); await f.change.request(7);
    f.setContext(kind === 'unmount' ? null : {
      userId: kind === 'account' ? 'account-b' : 'account-a',
      version: kind === 'version' ? 2 : 1, days: null,
    });
    expect(await f.change.confirm()).toBe(false);
    expect(f.change.stale).toBe(true); expect(f.change.pending).toBeNull();
    expect(f.save).not.toHaveBeenCalled(); expect(f.clearHistory).not.toHaveBeenCalled();
  });

  it('a late successful write from a previous account cannot clear the new account history', async () => {
    const f = fixture(); const write = deferred<void>(); f.save.mockReturnValueOnce(write.promise);
    await f.change.request(7); const pending = f.change.confirm();
    f.setContext({ userId: 'account-b', version: 1, days: null });
    write.resolve(); expect(await pending).toBe(false);
    expect(f.clearHistory).not.toHaveBeenCalled(); expect(f.reloadHistory).not.toHaveBeenCalled();
  });

  it('successful shortening clears real HistoryStore immediately and fences a late old response even when reload fails', async () => {
    const history = new HistoryStore();
    const oldBody = { session_id: 'old-session', result: { refined_text: 'synthetic expired body' } } as unknown as Session;
    history.items = [oldBody];
    const oldRequest = deferred<{ items: Session[]; next_cursor: string | null }>();
    const freshRequest = deferred<{ items: Session[]; next_cursor: string | null }>();
    vi.mocked(rpc.history.list).mockReturnValueOnce(oldRequest.promise).mockReturnValueOnce(freshRequest.promise);
    const oldLoad = history.load(true);
    let context = { userId: 'account-a', version: 1, days: null as RetentionDays };
    let freshLoad!: Promise<void>;
    const change = new RetentionChange({ read: () => context,
      save: async (patch) => { context = { ...context, version: 2, days: patch.history_retention_days }; },
      clearHistory: () => history.clear(),
      reloadHistory: () => (freshLoad = history.load(true)),
    });
    await change.request(7); expect(await change.confirm()).toBe(true);
    expect(history.items).toEqual([]); expect(rpc.history.list).toHaveBeenCalledTimes(2);
    oldRequest.resolve({ items: [oldBody], next_cursor: 'old-cursor' }); await oldLoad;
    expect(history.items).toEqual([]); expect(history.cursor).toBeNull();
    freshRequest.reject(new Error('synthetic reload failure')); await freshLoad;
    expect(history.items).toEqual([]); expect(history.error).toContain('synthetic reload failure');
    expect(history.loading).toBe(false);
  });
});
