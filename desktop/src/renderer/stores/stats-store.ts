import { makeAutoObservable, runInAction } from 'mobx';
import type { Stats } from '@shared/types';
import { errorMessage } from '../lib/error-message';
import { rpc } from '../lib/ipc';

function isoDate(date: Date): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date);
  const get = (name: string) => parts.find(part => part.type === name)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

export class StatsStore {
  stats: Stats | null = null;
  loading = false;
  error: string | null = null;
  private generation = 0;
  private request = 0;

  constructor() {
    makeAutoObservable(this);
  }

  clear(): void {
    this.generation++; this.request++; this.stats = null; this.loading = false; this.error = null;
  }

  async load(days = 28): Promise<void> {
    const generation = this.generation;
    const request = ++this.request;
    const current = () => generation === this.generation && request === this.request;
    this.loading = true;
    this.error = null;
    try {
      const end = new Date();
      const start = new Date(end.getTime() - (days - 1) * 86_400_000);
      const stats = await rpc.stats.me(isoDate(start), isoDate(end));
      runInAction(() => {
        if (current()) this.stats = stats;
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
}
