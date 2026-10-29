import { app, BrowserWindow, dialog, ipcMain, powerMonitor, screen, shell } from 'electron';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { API_BASE_URL, SERVICE_CA_SHA256 } from '@shared/config';
import { installServiceTrust } from './core/service-trust';
import {
  dictationLevelEvent,
  hotkeyToggleEvent,
  HUD_NOTICE_MS,
  hudCancelEvent,
  hudConfirmEvent,
  hudNoticeEvent,
  type HudNotice,
  learningLearnedEvent,
  learningSuggestedEvent,
  sessionProgressEvent,
  sessionResultEvent,
  sessionErrorEvent,
} from '@shared/events';
import { createEventEmitter } from '@shared/ipc/events';
import { registerRPCRouter } from '@shared/ipc/rpc';
import { buildAppMenu } from './app/menu';
import { HudVisibility } from './app/hud-visibility';
import { HudPosition, inputRectangle } from './app/hud-position';
import { WindowsTray } from './app/windows-tray';
import { inputGeometryToDip } from '../../native/input-geometry';
import {
  createHudWindow,
  createMainWindow,
  getMainWindow,
  getHudWindow,
  hideHud,
  markAppQuitting,
  resolveAppResource,
  showHud,
} from './app/window';
import { ApiClient } from './core/api-client';
import { AuthService } from './core/auth-service';
import { NativeVoiceInput } from './core/native-input';
import { SessionService } from './core/session-service';
import { ShortcutService } from './core/shortcut-service';
import { TokenStore } from './core/token-store';
import { InsertionJournal } from './core/insertion-journal';
import { createRpcRouter } from './rpc';
import { createRendererRpcAuthorizer } from './rpc-authorization';

app.setName('SmartVoiceInput');
if (process.env.SVI_USER_DATA) app.setPath('userData', process.env.SVI_USER_DATA);

const gotLock = app.requestSingleInstanceLock();
let showRequested = false;
let showMain: (() => Promise<void>) | null = null;
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    showRequested = true;
    void showMain?.();
  });
}

const events = createEventEmitter({
  emit: (channel, data) => {
    for (const win of BrowserWindow.getAllWindows()) win.webContents.send(channel, data);
  },
  on: (channel, cb) => {
    const listener = (_event: Electron.IpcMainEvent, data: unknown) => cb(data);
    ipcMain.on(channel, listener);
    return () => ipcMain.off(channel, listener);
  },
});

async function main(): Promise<void> {
  const debug = (step: string) => {
    if (process.env.SVI_STARTUP_DEBUG) console.log('[startup]', step);
  };
  debug('waiting-for-ready');
  await app.whenReady();
  installServiceTrust(resolveAppResource('service-ca.crt'), SERVICE_CA_SHA256);
  debug('ready');
  if (process.platform === 'darwin' && app.dock) {
    app.dock.setIcon(resolveAppResource('icon.png'));
  }
  debug('dock-icon');
  buildAppMenu();
  debug('menu');

  const serverIdentity = new URL(API_BASE_URL).href;
  const serverKey = createHash('sha256').update(serverIdentity).digest('hex');
  const tokens = new TokenStore(undefined, app.getPath('userData'), serverIdentity);
  const device = tokens.device(app.getVersion());
  debug('tokens');
  let api!: ApiClient;
  let auth!: AuthService;
  let sessions: SessionService | null = null;
  let accountId: string | null = null;
  let tray: WindowsTray | null = null;
  let quitting = false;
  const native = new NativeVoiceInput();
  // Geometry cannot queue in front of an insertion or consume its helper's targets.
  const hudNative = new NativeVoiceInput();
  let nextHudPermissionCheck = 0;
  const toDip = (geometry: Record<string, unknown>) => inputGeometryToDip(geometry, inputRectangle(geometry),
    process.platform, rect => screen.screenToDipRect(null, rect));
  const hudPosition = new HudPosition(screen, async () => {
    const geometry = await hudNative.call('inputBounds');
    if (geometry.available !== true && Date.now() >= nextHudPermissionCheck) {
      nextHudPermissionCheck = Date.now() + 1500;
      const permission = await hudNative.call('permission', { prompt: false });
      if (permission.trusted) return toDip(await hudNative.call('inputBounds'));
    }
    return toDip(geometry);
  },
    bounds => getHudWindow()?.setBounds(bounds, false), showHud);
  const hudVisibility = new HudVisibility(() => hudPosition.start(), () => {
    hudPosition.stop(); hideHud();
  }, () => events.emit(hudNoticeEvent, null));
  const notifyHud = (notice: HudNotice | null) => {
    events.emit(hudNoticeEvent, notice);
    if (notice?.kind === 'insert-failed') hudVisibility.notice(HUD_NOTICE_MS);
  };
  // macOS shows its own Accessibility prompt at most once per app process after sign-in.
  let accessibilityPrompted = false;
  debug('native');
  api = new ApiClient(tokens, device, () => auth.invalidate());
  auth = new AuthService(api, tokens, () => {
    const nextId = auth.current?.user.id ?? null;
    if (nextId !== accountId) {
      accountId = nextId;
      sessions?.invalidateAccount();
      native.invalidate();
      hudNative.invalidate();
      hudVisibility.reset();
      if (nextId && process.platform === 'darwin' && !accessibilityPrompted) {
        accessibilityPrompted = true;
        void native.call('permission', { prompt: true }).catch(() => undefined);
      }
      if (nextId && sessions) {
        void sessions.recoverReceipts().catch(() => {
          if (auth.current?.user.id === nextId) events.emit(sessionErrorEvent, '上次输入的回执尚未确认，请检查目标；不会重复输入');
        });
      }
    }
    tray?.refreshMenu();
    events.emit({ name: 'auth.changed' }, null);
  });
  const journal = new InsertionJournal(join(app.getPath('userData'), 'insertion-marks', serverKey));
  sessions = new SessionService(
    api,
    native,
    auth,
    journal,
    (progress) => {
      events.emit(sessionProgressEvent, progress);
      hudVisibility.update(progress, sessions?.isBusy() ?? false);
    },
    (progress) => events.emit(sessionResultEvent, progress),
    (suggestion) => {
      events.emit(
        suggestion.status === 'suggested' ? learningSuggestedEvent : learningLearnedEvent,
        suggestion
      );
    },
    (message) => events.emit(sessionErrorEvent, message)
  );

  const sessionService = sessions;
  const shortcuts = new ShortcutService();
  debug('services');
  await auth.restore().catch(() => null);
  debug('auth-restore');

  // Register IPC handlers before any window starts loading, otherwise a fast
  // renderer can call session.status/auth.me before the router exists.
  const router = createRpcRouter({ api, auth, sessions: sessionService, native, shortcuts, notifyHud });
  registerRPCRouter(router, ipcMain, createRendererRpcAuthorizer({ getMainWindow, getHudWindow }));
  ipcMain.on('session.audio', (event, data: unknown) => {
    const win = getMainWindow();
    if (!win || event.sender !== win.webContents || !data || typeof data !== 'object') return;
    const frame = data as { session_id?: unknown; buffer?: unknown; captured_at_ms?: unknown };
    if (typeof frame.session_id !== 'string' || !(frame.buffer instanceof ArrayBuffer) || typeof frame.captured_at_ms !== 'number' || !Number.isFinite(frame.captured_at_ms)) {
      if (process.env.SVI_AUDIO_DEBUG) console.warn('[audio] rejected ipc frame', {
        session_id: typeof frame.session_id, buffer: frame.buffer instanceof ArrayBuffer ? 'arraybuffer' : typeof frame.buffer,
        captured_at_ms: typeof frame.captured_at_ms,
      });
      return;
    }
    sessionService.sendAudio(frame.buffer, frame.session_id, frame.captured_at_ms);
  });
  const hudSender = (event: Electron.IpcMainEvent) => {
    const win = getHudWindow();
    return Boolean(win && event.sender === win.webContents);
  };
  ipcMain.on('hud.confirm', event => { if (hudSender(event)) events.emit(hudConfirmEvent, null); });
  ipcMain.on('hud.cancel', event => { if (hudSender(event)) events.emit(hudCancelEvent, null); });
  ipcMain.on('hud.copy', event => {
    if (!hudSender(event)) return;
    const text = sessionService.status.result?.refined_text;
    if (typeof text === 'string' && text) sessionService.copy(text);
    hudVisibility.dismiss();
  });
  ipcMain.on('hud.open-accessibility', event => {
    if (!hudSender(event) || process.platform !== 'darwin') return;
    void shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility');
    hudVisibility.dismiss();
  });
  ipcMain.on('hud.dismiss', event => { if (hudSender(event)) hudVisibility.dismiss(); });
  ipcMain.on('dictation.level', (event, data: unknown) => {
    const win = getMainWindow();
    if (!win || event.sender !== win.webContents || !data || typeof data !== 'object') return;
    const sample = data as { session_id?: unknown; level?: unknown };
    if (sample.session_id !== sessionService.status.session_id || typeof sample.level !== 'number' || !Number.isFinite(sample.level)) return;
    events.emit(dictationLevelEvent, Math.max(0, Math.min(1, sample.level)));
  });
  debug('ipc');

  const interrupt = () => {
    if (sessionService.isBusy() && ['created', 'streaming', 'finalizing', 'refining'].includes(sessionService.status.state)) {
      void sessionService.cancel().catch(() => sessionService.reset());
    }
    native.invalidate();
    hudNative.invalidate();
    hudVisibility.reset();
  };
  const boundWindows = new WeakSet<BrowserWindow>();
  const background = process.platform === 'win32' && process.argv.includes('--background');
  const ensureMain = async () => {
    const win = await createMainWindow({ hidden: background && !showRequested && Boolean(tray),
      ...(process.platform === 'win32' ? { closeToTray: (window: BrowserWindow) => { if (tray) void tray.close(window); else window.show(); } } : {}) });
    if (!boundWindows.has(win)) {
      boundWindows.add(win);
      win.webContents.on('render-process-gone', interrupt);
      win.on('closed', interrupt);
    }
    return win;
  };
  showMain = async () => {
    if (quitting) return;
    try {
      const win = await ensureMain();
      if (quitting || win.isDestroyed()) return;
      if (win.isMinimized()) win.restore();
      win.show(); win.focus();
      tray?.refreshMenu();
    } catch {
      if (!quitting) await dialog.showMessageBox({ type: 'error', title: 'SmartVoiceInput',
        message: auth.current?.settings.locale === 'en' ? 'Could not open the main window. Try again from the tray or exit the app.' : '无法打开主窗口，请从托盘重试或退出应用。' });
    }
  };
  if (process.platform === 'win32') {
    try { tray = new WindowsTray(resolveAppResource('icon.png'), showMain, () => auth.current?.settings.locale ?? 'zh-CN'); }
    catch {
      // Initial window remains visible and the application menu has an Exit command.
      void dialog.showMessageBox({ type: 'error', title: 'SmartVoiceInput', message: auth.current?.settings.locale === 'en'
        ? 'The tray could not be created. Keep the window open or exit from the application menu.'
        : '托盘创建失败，请保留主窗口，或从应用菜单退出。' });
    }
  }
  await ensureMain();
  if (showRequested) await showMain();
  debug('main-window');
  await createHudWindow();
  debug('hud-window');
  powerMonitor.on('suspend', interrupt);
  shortcuts.setCallback(() => {
    // Toggle capture without changing the main window's visibility or
    // minimized state. The non-activating HUD owns recording feedback.
    events.emit(hotkeyToggleEvent, null);
  });

  app.on('activate', () => {
    showRequested = true;
    void showMain?.();
  });
  app.on('before-quit', () => {
    quitting = true;
    markAppQuitting();
    tray?.dispose();
    showMain = null;
    hudVisibility.reset();
    shortcuts.dispose();
    powerMonitor.off('suspend', interrupt);
    sessionService.dispose();
    native.dispose();
    hudNative.dispose();
  });
}

if (gotLock) app.whenReady().then(main).catch((error) => {
  console.error('[startup]', error);
  app.exit(1);
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
