import { makeAutoObservable, runInAction } from 'mobx';
import type { Session } from '@shared/types';
import { errorMessage } from '../lib/error-message';
import { historyContentExpired } from '../lib/history-retention';
import { rpc } from '../lib/ipc';

export class HistoryStore {
  items: Session[] = [];
  cursor: string | null = null;
  loading = false;
  error: string | null = null;
  loaded = false;
  private generation = 0;
  private request = 0;
  private loadTask: Promise<void> | null = null;
  private refreshQueued = false;
  private expiryTimer: ReturnType<typeof setTimeout> | null = null;

  constructor() {
    makeAutoObservable<HistoryStore, 'loadTask' | 'expiryTimer'>(this, { loadTask: false, expiryTimer: false });
  }

  get hasMore(): boolean {
    return this.cursor !== null;
  }

  clear(): void {
    if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
    this.expiryTimer = null;
    this.generation++; this.request++; this.items = []; this.cursor = null; this.loading = false; this.error = null; this.loaded = false;
    this.loadTask = null; this.refreshQueued = false;
  }

  load(reset = true): Promise<void> {
    if (this.loading) {
      if (reset) this.refreshQueued = true;
      return this.loadTask ?? Promise.resolve();
    }
    const generation = this.generation, request = ++this.request;
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
      const page = await rpc.history.list(reset ? null : this.cursor);
      runInAction(() => {
        if (!current()) return;
        this.items = reset ? page.items : [...this.items, ...page.items];
        this.expireCachedContent();
        this.cursor = page.next_cursor;
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

  private expireCachedContent(): void {
    if (this.expiryTimer !== null) clearTimeout(this.expiryTimer);
    this.expiryTimer = null;
    const now = Date.now();
    // Preserve time/state/duration metadata in both views. Clearing result
    // also unmounts open details; component state holds no separate body copy.
    this.items = this.items.map((item): Session => historyContentExpired(item, now)
      ? { ...item, result: null, content_expired: true, retry_kind: 'none',
          retry_expires_at: null, retry_unavailable_reason: 'content_expired' }
      : item);
    const next = this.items.reduce((nearest, item) => item.content_expired || item.content_expires_at === null
      ? nearest : Math.min(nearest, Date.parse(item.content_expires_at)), Infinity);
    if (Number.isFinite(next)) {
      const generation = this.generation;
      this.expiryTimer = setTimeout(() => {
        if (generation !== this.generation) return;
        this.expiryTimer = null;
        this.expireCachedContent();
      }, Math.max(1, Math.min(next - now, 2_147_483_647)));
    }
  }

  async copy(sessionId: string, resultId: string, variant: 'raw' | 'refined'): Promise<'copied' | 'unavailable' | 'stale'> {
    // Re-check at the click, even if an expiry callback has not run yet.
    this.expireCachedContent();
    const item = this.items.find(row => row.session_id === sessionId);
    if (!item?.result || item.state !== 'ready' || item.current_result_id !== resultId
      || item.result.result_id !== resultId || item.result.attempt_no !== item.current_attempt_no
      || (variant !== 'raw' && variant !== 'refined')) return 'unavailable';
    const generation = this.generation;
    try {
      await rpc.session.copy(variant === 'raw' ? item.result.raw_text : item.result.refined_text);
    } catch (error) {
      if (generation !== this.generation) return 'stale';
      throw error;
    }
    return generation === this.generation ? 'copied' : 'stale';
  }
}
