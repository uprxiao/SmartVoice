import { makeAutoObservable, runInAction } from 'mobx';
import type { AuthSnapshot, Settings } from '@shared/types';
import { errorMessage } from '../lib/error-message';
import { rpc } from '../lib/ipc';

export class AuthStore {
  snapshot: AuthSnapshot | null = null;
  booting = true;
  busy = false;
  error: string | null = null;
  permissionsGuidePending = false;
  private actionEpoch = 0;
  private bootVersion = 0;

  constructor() {
    makeAutoObservable(this);
  }

  get user() {
    return this.snapshot?.user ?? null;
  }

  get settings(): Settings | null {
    return this.snapshot?.settings ?? null;
  }

  async boot(): Promise<void> {
    const epoch = this.actionEpoch;
    const version = ++this.bootVersion;
    const current = () => epoch === this.actionEpoch && version === this.bootVersion;
    try {
      const snapshot = await rpc.auth.me();
      runInAction(() => { if (current()) this.snapshot = snapshot; });
    } catch {
      runInAction(() => { if (current()) this.snapshot = null; });
    } finally {
      runInAction(() => { if (current()) this.booting = false; });
    }
  }

  async login(email: string, password: string): Promise<void> {
    const epoch = ++this.actionEpoch;
    this.bootVersion++;
    this.snapshot = null;
    this.busy = true;
    this.error = null;
    try {
      const snapshot = await rpc.auth.login(email, password);
      runInAction(() => {
        if (epoch === this.actionEpoch) { this.snapshot = snapshot; this.permissionsGuidePending = true; }
      });
    } catch (error) {
      runInAction(() => {
        if (epoch === this.actionEpoch) this.error = errorMessage(error);
      });
      throw error;
    } finally {
      runInAction(() => {
        if (epoch === this.actionEpoch) { this.busy = false; this.booting = false; }
      });
    }
  }

  async register(email: string, password: string): Promise<void> {
    const epoch = ++this.actionEpoch;
    this.bootVersion++;
    this.snapshot = null;
    this.busy = true;
    this.error = null;
    try {
      const snapshot = await rpc.auth.register(email, password);
      runInAction(() => {
        if (epoch === this.actionEpoch) { this.snapshot = snapshot; this.permissionsGuidePending = true; }
      });
    } catch (error) {
      runInAction(() => {
        if (epoch === this.actionEpoch) this.error = errorMessage(error);
      });
      throw error;
    } finally {
      runInAction(() => {
        if (epoch === this.actionEpoch) { this.busy = false; this.booting = false; }
      });
    }
  }

  async logout(): Promise<void> {
    this.permissionsGuidePending = false;
    const epoch = ++this.actionEpoch;
    this.bootVersion++;
    this.snapshot = null;
    this.busy = false;
    this.error = null;
    try {
      await rpc.auth.logout();
    } catch (error) {
      runInAction(() => { if (epoch === this.actionEpoch) this.error = errorMessage(error); });
      throw error;
    } finally {
      runInAction(() => {
        if (epoch === this.actionEpoch) this.snapshot = null;
      });
    }
  }

  async updateSettings(patch: {
    version: number;
    history_retention_days?: number | null;
    learning_enabled?: boolean;
    locale?: 'zh-CN' | 'en';
  }): Promise<void> {
    const epoch = this.actionEpoch;
    const userId = this.snapshot?.user.id;
    const settings = await rpc.settings.update(patch);
    runInAction(() => {
      if (epoch === this.actionEpoch && this.snapshot?.user.id === userId && this.snapshot) this.snapshot = { ...this.snapshot, settings };
    });
  }

  clearError(): void {
    this.error = null;
  }
}
