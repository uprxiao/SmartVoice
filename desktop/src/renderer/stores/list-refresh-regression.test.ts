import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DictionaryPage, Page, Session, Term } from '@shared/types';

// Same IPC mock boundary as list-loading.test.ts. Stores are actual production
// imports, not copied implementations; no Electron, server or model is involved.
const doubles = vi.hoisted(() => ({ historyList: vi.fn(), dictionaryList: vi.fn(), createTerm: vi.fn() }));
vi.mock('../lib/ipc', () => ({
  rpc: {
    history: { list: doubles.historyList },
    dictionary: { list: doubles.dictionaryList, create: doubles.createTerm },
  },
}));

import { DictionaryStore } from './dictionary-store';
import { HistoryStore } from './history-store';
import { errorMessage } from '../lib/error-message';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}
const term = (id: string, version: number): Term => ({
  id, term: id, norm: id.toLowerCase(), aliases: [], contexts: [], scope: 'default', source: 'manual',
  version: 1, dictionary_version: version, created_at: '2026-10-18T00:00:00Z',
  updated_at: '2026-10-18T00:00:00Z', ambiguous_aliases: [],
});
const dictionaryPage = (ids: string[], version: number): DictionaryPage => ({
  items: ids.map(id => term(id, version)), next_cursor: null, dictionary_version: version,
});
const historyPage = (ids: string[]): Page<Session> => ({
  // The store transports session records unchanged; only identity is relevant
  // to this paging/refresh oracle (same fixture style as account-cache.test.ts).
  items: ids.map(session_id => ({ session_id, content_expires_at: null, content_expired: false }) as Session), next_cursor: null,
});

beforeEach(() => { Object.values(doubles).forEach(mock => mock.mockReset()); });

describe('a completed change is eventually visible despite an older list request', () => {
  it('reloads dictionary after a completed create even if a pre-write list snapshot is still in flight', async () => {
    const beforeWrite = deferred<DictionaryPage>();
    const write = deferred<Term>();
    doubles.dictionaryList.mockReturnValueOnce(beforeWrite.promise)
      .mockResolvedValueOnce(dictionaryPage(['Existing', 'Added'], 2));
    doubles.createTerm.mockReturnValueOnce(write.promise);
    const store = new DictionaryStore();
    const initialRead = store.load(true);
    const mutation = store.create('Added', []);

    write.resolve(term('Added', 2));
    // Resume the real create() continuation so its post-write load(true) occurs
    // before the old read completes. This is controlled promise order, not a timer.
    await Promise.resolve();
    expect(doubles.dictionaryList).toHaveBeenCalledTimes(1);
    beforeWrite.resolve(dictionaryPage(['Existing'], 1));
    await Promise.all([initialRead, mutation]);

    expect(doubles.dictionaryList).toHaveBeenCalledTimes(2);
    expect(doubles.dictionaryList.mock.calls.map(([args]) => args.cursor)).toEqual([null, null]);
    expect(store.items.map(item => item.id)).toEqual(['Existing', 'Added']);
    expect(store.version).toBe(2);
    expect(store.loading).toBe(false);
    expect(store.busy).toBe(false);
    expect(store.error).toBeNull();
  });

  it('coalesces terminal-result refreshes into one fresh history read after the in-flight read', async () => {
    const beforeResult = deferred<Page<Session>>();
    doubles.historyList.mockReturnValueOnce(beforeResult.promise)
      .mockResolvedValueOnce(historyPage(['NewlyFinished', 'Earlier']));
    const store = new HistoryStore();
    const initialRead = store.load(true);
    // App's sessionResultEvent handler uses this exact public store call.
    const terminalRefresh = store.load(true);
    const repeatedTerminalRefresh = store.load(true);
    expect(doubles.historyList).toHaveBeenCalledTimes(1);
    beforeResult.resolve(historyPage(['Earlier']));
    await Promise.all([initialRead, terminalRefresh, repeatedTerminalRefresh]);

    expect(doubles.historyList).toHaveBeenCalledTimes(2);
    expect(doubles.historyList.mock.calls.map(([cursor]) => cursor)).toEqual([null, null]);
    expect(store.items.map(item => item.session_id)).toEqual(['NewlyFinished', 'Earlier']);
    expect(store.loading).toBe(false);
    expect(store.loaded).toBe(true);
    expect(store.error).toBeNull();
  });

  it('drops a queued old-account refresh when clear starts a new account generation', async () => {
    const oldRead = deferred<Page<Session>>();
    const newRead = deferred<Page<Session>>();
    doubles.historyList.mockReturnValueOnce(oldRead.promise).mockReturnValueOnce(newRead.promise);
    const store = new HistoryStore();
    const oldInitial = store.load(true);
    const oldRefresh = store.load(true);
    store.clear();
    const currentRead = store.load(true);
    newRead.resolve(historyPage(['NewAccount']));
    await currentRead;
    oldRead.resolve(historyPage(['OldAccount']));
    await Promise.all([oldInitial, oldRefresh]);

    expect(doubles.historyList).toHaveBeenCalledTimes(2);
    expect(store.items.map(item => item.session_id)).toEqual(['NewAccount']);
    expect(store.loading).toBe(false);
    expect(store.error).toBeNull();
  });
});

describe('error presentation keeps business errors distinct from network errors', () => {
  it('preserves an actionable conflict while mapping only a matching network error', () => {
    expect(errorMessage(new Error("Error invoking remote method 'dictionary.update': Error: 词条已被修改，请刷新后重试")))
      .toBe('词条已被修改，请刷新后重试');
    expect(errorMessage(new Error("Error invoking remote method 'history.list': TypeError: fetch failed")))
      .toBe('无法连接服务，请检查网络后重试');
  });
});
