import { app, globalShortcut } from 'electron';
import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { shortcutPolicyError, shortcutSignature } from '@root/native/shortcut-policy';

interface LocalSettings {
  shortcut: string;
}

const DEFAULT_SHORTCUT = 'CommandOrControl+Shift+Space';

export class ShortcutService {
  private readonly file = join(app.getPath('userData'), 'settings.json');
  private shortcut = DEFAULT_SHORTCUT;
  private callback: (() => void) | null = null;

  constructor() {
    try {
      if (existsSync(this.file)) {
        const parsed = JSON.parse(readFileSync(this.file, 'utf8')) as Partial<LocalSettings>;
        if (typeof parsed.shortcut === 'string' && parsed.shortcut.trim()
            && !shortcutPolicyError(parsed.shortcut, process.platform)) {
          this.shortcut = parsed.shortcut.trim();
        }
      }
    } catch {
      this.shortcut = DEFAULT_SHORTCUT;
    }
  }

  get current(): string {
    return this.shortcut;
  }

  setCallback(callback: () => void): void {
    this.callback = callback;
    if (!globalShortcut.register(this.shortcut, callback)) {
      // Keep the default registered if the saved accelerator is now unavailable.
      this.shortcut = DEFAULT_SHORTCUT;
      globalShortcut.register(this.shortcut, callback);
    }
  }

  update(next: string): string {
    const accelerator = next.trim();
    if (!accelerator) throw new Error('快捷键不能为空');
    const policyError = shortcutPolicyError(accelerator, process.platform);
    if (policyError) throw new Error(policyError);
    if (shortcutSignature(accelerator, process.platform)
        === shortcutSignature(this.shortcut, process.platform)) return this.shortcut;
    const callback = this.callback ?? (() => undefined);
    const previous = this.shortcut;
    const temporary = `${this.file}.${randomUUID()}.tmp`;
    if (!globalShortcut.register(accelerator, callback)) {
      throw new Error(`快捷键 ${accelerator} 不可用，仍保留 ${previous}`);
    }
    let temporaryWriteReturned = false;
    try {
      const payload: LocalSettings = { shortcut: accelerator };
      writeFileSync(temporary, JSON.stringify(payload, null, 2), { mode: 0o600, flag: 'wx' });
      temporaryWriteReturned = true;
      renameSync(temporary, this.file);
    } catch (error) {
      // The old key and settings have not been changed. Remove only the key
      // and exact UUID temporary owned by this failed update; cleanup failure
      // also remains an error rather than an acknowledgement of saved state.
      globalShortcut.unregister(accelerator);
      // An exclusive write rejecting an existing path did not create it.
      // A rename failure happens after our own write and still needs cleanup.
      const writeFoundExisting = !temporaryWriteReturned && typeof error === 'object'
        && error !== null && 'code' in error && error.code === 'EEXIST';
      if (!writeFoundExisting) rmSync(temporary, { force: true });
      throw error;
    }
    globalShortcut.unregister(previous);
    this.shortcut = accelerator;
    return this.shortcut;
  }

  dispose(): void {
    globalShortcut.unregisterAll();
  }
}
