import { makeAutoObservable, runInAction } from 'mobx';

export type RetentionDays = number | null;
export interface RetentionContext {
  userId: string;
  version: number;
  days: RetentionDays;
}
interface PendingChange extends RetentionContext {
  next: RetentionDays;
}
interface Ports {
  read: () => RetentionContext | null;
  save: (patch: { version: number; history_retention_days: RetentionDays }) => Promise<void>;
  clearHistory: () => void;
  reloadHistory: () => Promise<void>;
}

export function shortensRetention(current: RetentionDays, next: RetentionDays): boolean {
  return next !== null && (current === null || next < current);
}

/** Keeps the committed select value in AuthStore; only an explicit confirm saves a shorter period. */
export class RetentionChange {
  pending: PendingChange | null = null;
  busy = false;
  error: unknown = null;
  stale = false;

  constructor(private readonly ports: Ports) {
    makeAutoObservable<RetentionChange, 'ports'>(this, { ports: false });
  }

  get needsConfirmation(): boolean {
    return this.pending !== null && shortensRetention(this.pending.days, this.pending.next);
  }

  async request(next: RetentionDays): Promise<boolean> {
    if (this.busy) return false;
    const current = this.ports.read();
    if (!current || (next !== null && ![7, 30, 90].includes(next))) return false;
    this.error = null;
    this.stale = false;
    this.pending = current.days === next ? null : { ...current, next };
    if (!this.pending || this.needsConfirmation) return false;
    return this.savePending();
  }

  cancel(): void {
    if (this.busy) return;
    this.pending = null;
    this.error = null;
    this.stale = false;
  }

  async confirm(): Promise<boolean> {
    if (!this.needsConfirmation) return false;
    return this.savePending();
  }

  private async savePending(): Promise<boolean> {
    const pending = this.pending;
    if (!pending || this.busy) return false;
    const current = this.ports.read();
    if (!current || current.userId !== pending.userId || current.version !== pending.version || current.days !== pending.days) {
      this.pending = null;
      this.stale = true;
      return false;
    }
    // Synchronous guard: a second click before React renders cannot issue another PATCH.
    this.busy = true;
    this.error = null;
    try {
      await this.ports.save({ version: pending.version, history_retention_days: pending.next });
    } catch (error) {
      runInAction(() => { this.error = error; this.busy = false; });
      return false;
    }
    const stillThisAccount = this.ports.read()?.userId === pending.userId;
    runInAction(() => { this.pending = null; this.busy = false; });
    if (!stillThisAccount) return false;
    if (shortensRetention(pending.days, pending.next)) {
      // Invalidate the old generation before the first reload await. Failed reloads
      // must show HistoryStore.error with no previous body left in either view.
      this.ports.clearHistory();
      void this.ports.reloadHistory(); // HistoryStore.load handles and exposes API errors.
    }
    return true;
  }
}
