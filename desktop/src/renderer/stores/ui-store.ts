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
  private shortcutRevision = 0;
  private nextId = 1;

  constructor() {
    makeAutoObservable(this);
  }

  async loadAppInfo(): Promise<AppInfo> {
    const revision = ++this.shortcutRevision;
    const info = await rpc.app.info();
    runInAction(() => {
      if (revision === this.shortcutRevision) this.localShortcut = info.shortcut || null;
    });
    return info;
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
