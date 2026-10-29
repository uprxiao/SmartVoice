import { app, dialog, Menu, Tray, type BrowserWindow } from 'electron';
import { readFileSync, renameSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

/** Owns the tray for the lifetime of Main; hiding never destroys the audio renderer. */
export class WindowsTray {
  private tray: Tray | null = null;
  private acknowledged = false;
  private prompting = false;
  private disposed = false;
  private readonly stateFile: string;
  constructor(icon: string, private readonly show: () => Promise<void>, private readonly locale: () => string) {
    this.stateFile = join(app.getPath('userData'), 'tray-state.json');
    try { this.acknowledged = JSON.parse(readFileSync(this.stateFile, 'utf8')).closeHintAcknowledged === true; } catch { /* first close */ }
    const tray = new Tray(icon);
    if (tray.isDestroyed()) throw new Error('TRAY_UNAVAILABLE');
    this.tray = tray;
    tray.setToolTip('SmartVoiceInput');
    tray.on('double-click', () => { void this.show(); });
    this.refreshMenu();
  }
  refreshMenu(): void {
    const en = this.locale() === 'en';
    this.tray?.setContextMenu(Menu.buildFromTemplate([
      { label: en ? 'Open' : '打开', click: () => { void this.show(); } },
      { label: en ? 'Exit' : '退出', click: () => app.quit() },
    ]));
  }
  async close(win: BrowserWindow): Promise<void> {
    if (this.disposed || win.isDestroyed() || this.prompting) return;
    if (!this.acknowledged) {
      this.prompting = true;
      const en = this.locale() === 'en';
      try {
        await dialog.showMessageBox(win, { type: 'info', title: 'SmartVoiceInput',
          message: en ? 'SmartVoiceInput keeps running in the background. Open or exit it from the tray icon.'
            : 'SmartVoiceInput 仍在后台运行，可从托盘图标打开或退出。',
          buttons: [en ? 'Got it' : '知道了'], defaultId: 0, noLink: true });
        if (this.disposed || win.isDestroyed()) return;
        // Never mark acknowledged before the user has actually seen the notice.
        const temporary = `${this.stateFile}.tmp`;
        writeFileSync(temporary, JSON.stringify({ closeHintAcknowledged: true }), { mode: 0o600 });
        renameSync(temporary, this.stateFile);
        if (JSON.parse(readFileSync(this.stateFile, 'utf8')).closeHintAcknowledged !== true) throw new Error('TRAY_STATE_READBACK');
        this.acknowledged = true;
      } catch {
        // Keep a visible recovery path if the dialog or persistence failed.
        if (!this.disposed && !win.isDestroyed()) {
          win.show();
          await dialog.showMessageBox(win, { type: 'error', message: en ? 'Could not save the background notice. The window remains open.' : '未能保存后台运行提示，主窗口保持打开。' });
        }
        return;
      } finally { this.prompting = false; }
    }
    if (!this.disposed && !win.isDestroyed()) win.hide();
  }
  dispose(): void { this.disposed = true; this.tray?.destroy(); this.tray = null; }
}
