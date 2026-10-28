import { describe, expect, it, vi } from 'vitest';

const doubles = vi.hoisted(() => ({ historyList: vi.fn(), dictionaryList: vi.fn() }));
vi.mock('../lib/ipc', () => ({
  rpc: { history: { list: doubles.historyList }, dictionary: { list: doubles.dictionaryList } },
}));

import { DictionaryStore } from './dictionary-store';
import { HistoryStore } from './history-store';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}

describe('list stores deduplicate rapid retries', () => {
  it('issues one history request while the first reset is in flight', async () => {
    const pending = deferred<{ items: never[]; next_cursor: null }>();
    doubles.historyList.mockReset().mockReturnValue(pending.promise);
    const store = new HistoryStore();
    const first = store.load();
    const second = store.load();
    expect(doubles.historyList).toHaveBeenCalledTimes(1);
    pending.resolve({ items: [], next_cursor: null });
    await Promise.all([first, second]);
    expect(store.loading).toBe(false);
    expect(store.loaded).toBe(true);
  });

  it('continues to fetch after a query change invalidates the in-flight request', () => {
    const first = deferred<{ items: never[]; next_cursor: null; dictionary_version: number }>();
    const second = deferred<{ items: never[]; next_cursor: null; dictionary_version: number }>();
    doubles.dictionaryList.mockReset().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const store = new DictionaryStore();
    void store.load();
    expect(doubles.dictionaryList).toHaveBeenCalledTimes(1);
    store.setQuery('Claude');
    void store.load();
    expect(doubles.dictionaryList).toHaveBeenCalledTimes(2);
    first.resolve({ items: [], next_cursor: null, dictionary_version: 0 });
    second.resolve({ items: [], next_cursor: null, dictionary_version: 0 });
  });
});
