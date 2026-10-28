import { dateRangeError } from './date-range.ts';
import { ApiError } from './http.ts';
import type { UsageItem } from './types.ts';

export type UsageQuery = { from: string; to: string; userId?: string };
export type UsagePage = { timezone: string; items: UsageItem[]; next_cursor: string | null };
export type UsageState = {
  items: UsageItem[]; nextCursor: string | null;
  loading: boolean; loaded: boolean; error: unknown | null;
};
type UsageClient = {
  usage(from: string, to: string, userId?: string, cursor?: string): Promise<UsagePage>;
};

export function emptyUsage(): UsageState {
  return { items: [], nextCursor: null, loading: false, loaded: false, error: null };
}

/** One page at a time; a new range/user or unmount invalidates older responses. */
export class UsageReader {
  private revision = 0;
  private lifetime = 0;
  private query: UsageQuery | null = null;
  private state: UsageState = emptyUsage();
  private client: UsageClient;
  private onChange: (state: UsageState) => void;
  private onError: (error: unknown) => void;

  constructor(client: UsageClient, onChange: (state: UsageState) => void, onError: (error: unknown) => void) {
    this.client = client;
    this.onChange = onChange;
    this.onError = onError;
  }

  private publish(): void {
    this.onChange({ ...this.state, items: [...this.state.items] });
  }

  async replace(query: UsageQuery): Promise<void> {
    const revision = ++this.revision;
    this.query = { ...query };
    this.state = emptyUsage();
    const invalid = dateRangeError(query.from, query.to);
    if (invalid) {
      this.state.error = new Error(invalid);
      this.publish();
      return;
    }
    await this.read(revision, { ...query });
  }

  async more(): Promise<void> {
    if (this.state.loading || !this.state.nextCursor || !this.query) return;
    await this.read(this.revision, { ...this.query }, this.state.nextCursor);
  }

  invalidate(): void {
    this.lifetime += 1;
    this.revision += 1;
    this.query = null;
    this.state = emptyUsage();
  }

  private async read(revision: number, query: UsageQuery, cursor?: string): Promise<void> {
    const lifetime = this.lifetime;
    this.state = { ...this.state, loading: true, error: null };
    this.publish();
    try {
      const page = await this.client.usage(query.from, query.to, query.userId, cursor);
      if (revision !== this.revision) return;
      this.state = {
        items: cursor ? [...this.state.items, ...page.items] : [...page.items],
        nextCursor: page.next_cursor, loading: false, loaded: true, error: null,
      };
      this.publish();
    } catch (error) {
      if (lifetime !== this.lifetime) return;
      const authorizationFailed = error instanceof ApiError && [401, 403].includes(error.status);
      // A different filter cannot make this account's authorization valid.
      // Only unmount/account invalidation may suppress its late auth failure.
      if (authorizationFailed) this.onError(error);
      if (revision !== this.revision) return;
      this.state = { ...this.state, loading: false, error };
      this.publish();
      // Preserve structured 401/403 and non-network failures for the caller.
      if (!authorizationFailed) this.onError(error);
    }
  }
}
