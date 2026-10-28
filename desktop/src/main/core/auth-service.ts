import type { AuthSnapshot, Settings } from '@shared/types';
import { ApiClient, AuthContextChangedError } from './api-client';
import { TokenStore } from './token-store';

export class AuthService {
  private snapshot: AuthSnapshot | null = null;
  private epoch = 0;

  constructor(
    private readonly api: ApiClient,
    private readonly tokens: TokenStore,
    private readonly onChanged: () => void
  ) {}

  get current(): AuthSnapshot | null { return this.snapshot; }
  get contextVersion(): number { return this.epoch; }
  private assertEpoch(epoch: number): void {
    if (epoch !== this.epoch) throw new AuthContextChangedError();
  }
  private publish(snapshot: AuthSnapshot | null): void {
    this.snapshot = snapshot ? { ...snapshot, memory_only: this.tokens.isMemoryOnly } : null;
    this.onChanged();
  }

  async restore(): Promise<AuthSnapshot | null> {
    const epoch = ++this.epoch;
    try {
      const stored = this.tokens.load();
      if (!stored) { this.publish(null); return null; }
      this.api.setTokens(stored.access, stored.refresh);
      const snapshot = await this.api.me();
      this.assertEpoch(epoch);
      this.publish(snapshot);
      return this.ensure();
    } catch {
      if (epoch === this.epoch) {
        this.api.clearTokens();
        this.publish(null);
      }
      return null;
    }
  }

  async login(email: string, password: string): Promise<AuthSnapshot> {
    const epoch = ++this.epoch;
    this.publish(null);
    await this.api.login(email.trim().toLowerCase(), password);
    this.assertEpoch(epoch);
    const snapshot = await this.api.me();
    this.assertEpoch(epoch);
    this.publish(snapshot);
    return this.ensure();
  }

  async register(email: string, password: string): Promise<AuthSnapshot> {
    const epoch = ++this.epoch;
    this.api.clearTokens();
    this.publish(null);
    await this.api.register(email.trim().toLowerCase(), password);
    this.assertEpoch(epoch);
    return this.login(email, password);
  }

  async logout(): Promise<void> {
    const epoch = ++this.epoch;
    this.publish(null);
    try { await this.api.logout(); }
    finally { if (epoch === this.epoch) this.publish(null); }
  }

  invalidate(): void { this.epoch++; this.publish(null); }

  async refreshSnapshot(): Promise<AuthSnapshot | null> {
    const epoch = this.epoch;
    if (!this.api.hasSession()) { this.invalidate(); return null; }
    const snapshot = await this.api.me();
    this.assertEpoch(epoch);
    this.publish(snapshot);
    return this.ensure();
  }

  async updateSettings(patch: Partial<Settings> & { version: number }): Promise<Settings> {
    const epoch = this.epoch;
    const userId = this.ensure().user.id;
    const settings = await this.api.patchSettings(patch);
    this.assertEpoch(epoch);
    if (this.snapshot?.user.id !== userId) throw new AuthContextChangedError();
    this.publish({ ...this.snapshot, settings });
    return settings;
  }

  ensure(): AuthSnapshot {
    if (!this.snapshot) throw new Error('请先登录');
    return this.snapshot;
  }
  get learningEnabled(): boolean { return Boolean(this.snapshot?.settings.learning_enabled); }
}
