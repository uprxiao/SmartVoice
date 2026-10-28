import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Term } from '@shared/types';

const doubles = vi.hoisted(() => ({ list: vi.fn(), removeMany: vi.fn() }));
vi.mock('../lib/ipc', () => ({ rpc: { dictionary: doubles } }));
import { DictionaryStore } from './dictionary-store';

const term = (id: string, version = 1): Term => ({
  id, term: id, norm: id, aliases: [], contexts: [], scope: 'default', source: 'manual', version,
  dictionary_version: 1, ambiguous_aliases: [], created_at: '', updated_at: '',
});
const page = (ids: string[]) => ({ items: ids.map(id => term(id)), next_cursor: null, dictionary_version: 2 });
const store = () => { const value = new DictionaryStore(); value.items = [term('a'), term('b')]; value.loaded = true; return value; };
beforeEach(() => { doubles.list.mockReset(); doubles.removeMany.mockReset(); });

describe('selection snapshots and deletion outcome', () => {
  it('selects loaded rows only, preserves versions, and does not select a newly loaded page', () => {
    const value = store(); value.toggleAllLoaded();
    expect(value.selected.size).toBe(2); expect(value.allLoadedSelected).toBe(true);
    value.items = [term('a', 2), term('b'), term('c')];
    expect(value.selected.get('a')?.version).toBe(1); expect(value.allLoadedSelected).toBe(false);
    value.toggleAllLoaded(); expect(value.selected.size).toBe(3); expect(value.selected.get('a')?.version).toBe(1);
    value.toggleAllLoaded(); expect(value.selected.size).toBe(0);
    value.toggleSelection(value.items[0]); value.setSource('auto'); expect(value.selected.size).toBe(0);
    value.toggleSelection(value.items[0]); value.setQuery('different'); expect(value.selected.size).toBe(0);
  });

  it('retains selected snapshots and the same key after a lost response, then reads back before clearing', async () => {
    const value = store(); value.toggleAllLoaded();
    doubles.removeMany.mockRejectedValueOnce(new Error('fetch failed')).mockResolvedValueOnce({ deleted_ids: ['b', 'a'] });
    doubles.list.mockResolvedValueOnce(page([]));
    await expect(value.removeSelected()).rejects.toThrow();
    expect(value.selected.size).toBe(2); expect(doubles.list).not.toHaveBeenCalled();
    expect(await value.removeSelected()).toBe(true);
    expect(doubles.removeMany.mock.calls[0]).toEqual(doubles.removeMany.mock.calls[1]);
    expect(value.selected.size).toBe(0); expect(value.items).toEqual([]);
  });

  it('does not announce completion or forget the key when the post-delete refresh fails', async () => {
    const value = store(); value.toggleAllLoaded();
    doubles.removeMany.mockResolvedValue({ deleted_ids: ['a', 'b'] });
    doubles.list.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page([]));
    await expect(value.removeSelected()).rejects.toThrow();
    expect(value.selected.size).toBe(2);
    expect(await value.removeSelected()).toBe(true);
    expect(doubles.removeMany.mock.calls[0][1]).toBe(doubles.removeMany.mock.calls[1][1]);
  });

  it('rejects mismatched responses and stale list readback without clearing selection', async () => {
    const value = store(); value.toggleAllLoaded();
    doubles.removeMany.mockResolvedValueOnce({ deleted_ids: ['a'] }).mockResolvedValueOnce({ deleted_ids: ['a', 'b'] });
    await expect(value.removeSelected()).rejects.toThrow('核对');
    expect(doubles.list).not.toHaveBeenCalled();
    doubles.list.mockResolvedValueOnce(page(['a']));
    await expect(value.removeSelected()).rejects.toThrow('尚未更新');
    expect(value.selected.size).toBe(2);
  });

  it('rejects duplicate submissions and drops an old-account completion without changing new selection', async () => {
    const value = store(); value.toggleAllLoaded();
    let finish!: (result: { deleted_ids: string[] }) => void;
    doubles.removeMany.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const pending = value.removeSelected(); expect(value.busy).toBe(true);
    expect(await value.removeSelected()).toBe(false); expect(doubles.removeMany).toHaveBeenCalledTimes(1);
    value.clear(); value.items = [term('new-account')]; value.toggleAllLoaded();
    finish({ deleted_ids: ['a', 'b'] });
    expect(await pending).toBe(false); expect(value.selected.has('new-account')).toBe(true);
    expect(value.busy).toBe(false); expect(doubles.list).not.toHaveBeenCalled();
  });

  it('does not dispatch an empty selection or change snapshots while loading/busy', async () => {
    const value = store(); expect(await value.removeSelected()).toBe(false);
    value.loading = true; value.toggleSelection(value.items[0]); expect(value.selected.size).toBe(0);
    value.loading = false; value.toggleAllLoaded(); value.busy = true;
    value.clearSelection(); value.setSource('auto'); value.setQuery('hidden');
    expect(value.selected.size).toBe(2); expect(value.source).toBe('all'); expect(value.query).toBe('');
    expect(doubles.removeMany).not.toHaveBeenCalled();
  });
});
