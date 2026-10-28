import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Session, Stats, Term } from '@shared/types';
vi.mock('../lib/ipc', () => ({rpc: {history: {list: vi.fn()}, stats: {me: vi.fn()}, dictionary: {list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), importCsv: vi.fn()}, session: {copy: vi.fn()}}}));
import { rpc } from '../lib/ipc';
import { HistoryStore } from './history-store';
import { StatsStore } from './stats-store';
import { DictionaryStore } from './dictionary-store';
const deferred = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });
describe('cleared account caches never publish previous responses (AUTH-16)', () => {
  it('old history cannot overwrite the new account or its loading state', async () => {
    const old = deferred<{items: Session[]; next_cursor: string | null}>();
    vi.mocked(rpc.history.list).mockReturnValueOnce(old.promise).mockResolvedValueOnce({items: [{session_id: 'new', content_expires_at: null, content_expired: false} as Session], next_cursor: null});
    const store = new HistoryStore(); const pending = store.load(); store.clear(); await store.load();
    old.resolve({items: [{session_id: 'old', content_expires_at: null, content_expired: false} as Session], next_cursor: 'old-cursor'}); await pending;
    expect(store.items.map(item => item.session_id)).toEqual(['new']); expect(store.cursor).toBeNull(); expect(store.loading).toBe(false);
  });
  it('old statistics remain absent after a logout cache reset', async () => {
    const old = deferred<Stats>(); vi.mocked(rpc.stats.me).mockReturnValueOnce(old.promise);
    const store = new StatsStore(); const pending = store.load(); store.clear(); old.resolve({timezone: 'old'} as unknown as Stats); await pending;
    expect(store.stats).toBeNull(); expect(store.loading).toBe(false);
  });
  it('stats date boundaries use Shanghai even near UTC midnight', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-17T20:00:00Z')); vi.mocked(rpc.stats.me).mockResolvedValueOnce({} as Stats);
    await new StatsStore().load(2); expect(rpc.stats.me).toHaveBeenCalledWith('2026-10-17', '2026-10-18');
  });
  it('a previous dictionary query cannot overwrite the latest query', async () => {
    const old = deferred<{items: Term[]; next_cursor: string | null; dictionary_version: number}>();
    vi.mocked(rpc.dictionary.list).mockReturnValueOnce(old.promise).mockResolvedValueOnce({items: [{id: 'new'} as Term], next_cursor: null, dictionary_version: 2});
    const store = new DictionaryStore(); store.setQuery('old'); const pending = store.load(); store.setQuery('new'); await store.load();
    old.resolve({items: [{id: 'old'} as Term], next_cursor: 'old-cursor', dictionary_version: 1}); await pending;
    expect(store.items.map(item => item.id)).toEqual(['new']); expect(store.version).toBe(2); expect(store.loading).toBe(false);
  });
  it('a delayed old-account write does not trigger a read under the new account', async () => {
    const old = deferred<Term>(); vi.mocked(rpc.dictionary.create).mockReturnValueOnce(old.promise as never);
    const store = new DictionaryStore(); const pending = store.create('old-term', []); store.clear(); old.resolve({id: 'old'} as Term); await pending;
    expect(rpc.dictionary.list).not.toHaveBeenCalled(); expect(store.items).toEqual([]); expect(store.busy).toBe(false);
  });
});
