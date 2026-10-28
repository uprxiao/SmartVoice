import { makeAutoObservable, runInAction } from 'mobx';
import type { Term } from '@shared/types';
import { errorMessage } from '../lib/error-message';
import { rpc } from '../lib/ipc';

export class DictionaryStore {
  items: Term[] = [];
  version = 0;
  cursor: string | null = null;
  query = '';
  source: 'all' | 'manual' | 'auto' = 'all';
  loading = false;
  busy = false;
  error: string | null = null;
  loaded = false;
  selected = new Map<string, { id: string; version: number; term: string }>();
  private deletionRequest: { signature: string; key: string } | null = null;
  private generation = 0;
  private request = 0;
  private loadTask: Promise<void> | null = null;
  private refreshQueued = false;

  constructor() {
    makeAutoObservable<DictionaryStore, 'loadTask'>(this, { loadTask: false });
  }

  get hasMore(): boolean {
    return this.cursor !== null;
  }

  get allLoadedSelected(): boolean {
    return this.items.length > 0 && this.items.every(item => this.selected.has(item.id));
  }

  clearSelection(): void {
    if (this.busy) return;
    this.selected.clear();
    this.deletionRequest = null;
  }

  toggleSelection(term: Term): void {
    if (this.busy || this.loading) return;
    if (this.selected.has(term.id)) this.selected.delete(term.id);
    else this.selected.set(term.id, { id: term.id, version: term.version, term: term.term });
  }

  toggleAllLoaded(): void {
    if (this.busy || this.loading) return;
    if (this.allLoadedSelected) this.clearSelection();
    else for (const term of this.items) {
      if (!this.selected.has(term.id)) this.selected.set(term.id, { id: term.id, version: term.version, term: term.term });
    }
  }

  clear(): void {
    this.generation++; this.request++; this.items = []; this.version = 0; this.cursor = null;
    this.loadTask = null; this.refreshQueued = false;
    this.query = ''; this.source = 'all'; this.loading = false; this.busy = false; this.error = null; this.loaded = false;
    this.selected.clear(); this.deletionRequest = null;
  }

  load(reset = true): Promise<void> {
    if (this.loading) {
      if (reset) this.refreshQueued = true;
      return this.loadTask ?? Promise.resolve();
    }
    const generation = this.generation;
    const request = ++this.request;
    const current = () => generation === this.generation && request === this.request;
    this.refreshQueued = false;
    const task = this.fetchPage(reset, current).then(async () => {
      if (current() && this.refreshQueued) await this.load(true);
    });
    this.loadTask = task;
    const clearTask = () => { if (this.loadTask === task) this.loadTask = null; };
    void task.then(clearTask, clearTask);
    return task;
  }

  private async fetchPage(reset: boolean, current: () => boolean): Promise<void> {
    this.loading = true;
    this.error = null;
    try {
      const page = await rpc.dictionary.list({
        q: this.query.trim() || undefined,
        source: this.source,
        cursor: reset ? null : this.cursor,
      });
      runInAction(() => {
        if (!current()) return;
        this.items = reset ? page.items : [...this.items, ...page.items];
        this.cursor = page.next_cursor;
        this.version = page.dictionary_version;
        this.loaded = true;
      });
    } catch (error) {
      runInAction(() => {
        if (current()) this.error = errorMessage(error);
      });
    } finally {
      runInAction(() => {
        if (current()) this.loading = false;
      });
    }
  }

  setQuery(query: string): void {
    if (this.busy) return;
    this.clearSelection();
    this.request++; this.loading = false; this.loadTask = null; this.refreshQueued = false; this.query = query;
  }

  setSource(source: 'all' | 'manual' | 'auto'): void {
    if (this.busy) return;
    this.clearSelection();
    this.request++; this.loading = false; this.loadTask = null; this.refreshQueued = false; this.source = source;
  }

  async create(term: string, aliases: string[]): Promise<void> {
    const generation = this.generation;
    this.busy = true;
    this.error = null;
    try {
      await rpc.dictionary.create(term, aliases);
      if (generation !== this.generation) return;
      await this.load(true);
    } catch (error) {
      runInAction(() => {
        if (generation === this.generation) this.error = errorMessage(error);
      });
      throw error;
    } finally {
      runInAction(() => {
        if (generation === this.generation) this.busy = false;
      });
    }
  }

  async update(
    term: Term,
    payload: { term?: string; aliases?: string[] }
  ): Promise<void> {
    const generation = this.generation;
    this.busy = true;
    this.error = null;
    try {
      await rpc.dictionary.update(term.id, { version: term.version, ...payload });
      if (generation !== this.generation) return;
      await this.load(true);
    } catch (error) {
      runInAction(() => {
        if (generation === this.generation) this.error = errorMessage(error);
      });
      throw error;
    } finally {
      runInAction(() => {
        if (generation === this.generation) this.busy = false;
      });
    }
  }

  async remove(term: Term): Promise<void> {
    const generation = this.generation;
    this.busy = true;
    this.error = null;
    try {
      await rpc.dictionary.remove(term.id, term.version);
      if (generation !== this.generation) return;
      await this.load(true);
    } catch (error) {
      runInAction(() => {
        if (generation === this.generation) this.error = errorMessage(error);
      });
      throw error;
    } finally {
      runInAction(() => {
        if (generation === this.generation) this.busy = false;
      });
    }
  }

  async removeSelected(): Promise<boolean> {
    if (this.busy || this.loading || this.selected.size === 0) return false;
    const generation = this.generation;
    const items = [...this.selected.values()].map(({ id, version }) => ({ id, version }))
      .sort((a, b) => a.id.localeCompare(b.id));
    const signature = JSON.stringify(items);
    if (this.deletionRequest?.signature !== signature) {
      this.deletionRequest = { signature, key: crypto.randomUUID() };
    }
    const key = this.deletionRequest.key;
    this.busy = true; this.error = null;
    try {
      const result = await rpc.dictionary.removeMany(items, key);
      if (generation !== this.generation) return false;
      if (JSON.stringify([...result.deleted_ids].sort()) !== JSON.stringify(items.map(item => item.id).sort())) {
        throw new Error('删除结果未能核对，请刷新列表后重试');
      }
      await this.load(true);
      if (generation !== this.generation) return false;
      if (this.error || this.items.some(item => items.some(target => target.id === item.id))) {
        throw new Error(this.error || '删除后列表尚未更新，请刷新后重试');
      }
      runInAction(() => { this.selected.clear(); this.deletionRequest = null; });
      return true;
    } catch (error) {
      if (generation !== this.generation) return false;
      runInAction(() => { this.error = errorMessage(error); });
      throw error;
    } finally {
      runInAction(() => { if (generation === this.generation) this.busy = false; });
    }
  }

  async importCsv(): Promise<string | null> {
    const generation = this.generation;
    this.busy = true;
    this.error = null;
    try {
      const result = await rpc.dictionary.importCsv();
      if (!result || generation !== this.generation) return null;
      await this.load(true);
      return `新增 ${result.created} · 跳过 ${result.skipped} · 冲突 ${result.conflicts} · 无效 ${result.invalid}`;
    } catch (error) {
      runInAction(() => {
        if (generation === this.generation) this.error = errorMessage(error);
      });
      throw error;
    } finally {
      runInAction(() => {
        if (generation === this.generation) this.busy = false;
      });
    }
  }
}
