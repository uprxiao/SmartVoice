import { makeAutoObservable, runInAction } from 'mobx';
import type { AppInfo } from '@shared/types';
import { rpc } from '../lib/ipc';

export interface ToastItem {
  id: number;
  message: string;
  tone: 'info' | 'error' | 'warning';
}

export class UiStore {
  toasts: ToastItem[] = [];
  // Device-local shortcut, independent of account settings. Null means not yet read.
  localShortcut: string | null = null;
  appInfo: AppInfo | null = null;
  autoStartBusy = false;
  private appInfoRevision = 0;
  private shortcutRevision = 0;
  private nextId = 1;

  constructor() {
    makeAutoObservable(this);
  }

  async loadAppInfo(): Promise<AppInfo> {
    const revision = ++this.shortcutRevision;
    const infoRevision = ++this.appInfoRevision;
    const info = await rpc.app.info();
    runInAction(() => {
      if (revision === this.shortcutRevision) this.localShortcut = info.shortcut || null;
      if (infoRevision === this.appInfoRevision) this.appInfo = info;
    });
    return info;
  }

  async updateAutoStart(enabled: boolean): Promise<void> {
    if (this.autoStartBusy) return;
    this.autoStartBusy = true;
    const revision = ++this.appInfoRevision;
    try {
      const autoStart = await rpc.app.updateAutoStart(enabled);
      runInAction(() => {
        if (revision === this.appInfoRevision && this.appInfo) this.appInfo = { ...this.appInfo, autoStart };
      });
    } finally {
      // Keep the control disabled through read-back, including failed writes/rollbacks.
      await this.loadAppInfo().catch(() => {
        runInAction(() => { if (this.appInfo) this.appInfo = { ...this.appInfo, autoStart: { ...this.appInfo.autoStart, error: true } }; });
      });
      runInAction(() => { this.autoStartBusy = false; });
    }
  }

  async updateShortcut(next: string): Promise<string> {
    const applied = await rpc.app.updateShortcut(next);
    runInAction(() => {
      // A successful Main acknowledgement supersedes every already-started info read.
      this.shortcutRevision += 1;
      this.localShortcut = applied;
    });
    return applied;
  }

  toast(message: string, tone: ToastItem['tone'] = 'info'): void {
    const id = this.nextId++;
    this.toasts = [...this.toasts, { id, message, tone }];
    setTimeout(() => this.dismiss(id), tone === 'error' ? 5000 : 3000);
  }

  clear(): void { this.toasts = []; }

  dismiss(id: number): void {
    runInAction(() => {
      this.toasts = this.toasts.filter((item) => item.id !== id);
    });
  }
}
