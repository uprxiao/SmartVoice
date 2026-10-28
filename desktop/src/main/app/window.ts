import { join } from 'node:path';
import { app, BrowserWindow, screen } from 'electron';
import { hudWindowPlatformOptions } from '../../../native/hud-window-policy';

let mainWindow: BrowserWindow | null = null;
let hudWindow: BrowserWindow | null = null;
let appQuitting = false;

export function resolveAppResource(name: string): string {
  return app.isPackaged
    ? join(process.resourcesPath, name)
    : join(__dirname, '../../resources', name);
}

export function markAppQuitting(): void {
  appQuitting = true;
}

export function getMainWindow(): BrowserWindow | null {
  return mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
}

export function getHudWindow(): BrowserWindow | null {
  return hudWindow && !hudWindow.isDestroyed() ? hudWindow : null;
}

export async function createHudWindow(): Promise<BrowserWindow> {
  const existing = getHudWindow();
  if (existing) return existing;

  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const width = 300;
  // Transparent space above the 34px capsule holds the non-activating slow hint.
  const height = 104;
  const x = Math.round(display.workArea.x + (display.workArea.width - width) / 2);
  const y = Math.round(display.workArea.y + display.workArea.height - height - 72);

  const win = new BrowserWindow({
    width,
    height,
    x,
    y,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    focusable: false,
    ...hudWindowPlatformOptions(process.platform),
    alwaysOnTop: true,
    show: false,
    hasShadow: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  hudWindow = win;
  win.on('closed', () => {
    if (hudWindow === win) hudWindow = null;
  });

  if (process.env.ELECTRON_RENDERER_URL) {
    await win.loadURL(`${process.env.ELECTRON_RENDERER_URL.replace(/\/$/, '')}/hud.html`);
  } else {
    await win.loadFile(join(__dirname, '../renderer/hud.html'));
  }
  return win;
}

export function showHud(): void {
  const win = getHudWindow();
  if (win) win.showInactive();
}

export function hideHud(): void {
  const win = getHudWindow();
  if (win) win.hide();
}

export async function createMainWindow(): Promise<BrowserWindow> {
  const isMac = process.platform === 'darwin';
  const win = new BrowserWindow({
    width: 1080,
    height: 750,
    minWidth: 900,
    minHeight: 620,
    title: 'SmartVoiceInput',
    icon: resolveAppResource('icon.png'),
    show: false,
    backgroundColor: '#ffffff',
    ...(isMac
      ? {
          titleBarStyle: 'hiddenInset' as const,
          trafficLightPosition: { x: 14, y: 14 },
          acceptFirstMouse: true,
        }
      : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });
  mainWindow = win;
  win.on('close', (event) => {
    if (appQuitting || !isMac) return;
    event.preventDefault();
    win.hide();
  });
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });
  win.once('ready-to-show', () => {
    if (process.env.SVI_HIDDEN !== '1') win.show();
  });
  if (process.env.ELECTRON_RENDERER_URL) await win.loadURL(process.env.ELECTRON_RENDERER_URL);
  else await win.loadFile(join(__dirname, '../renderer/index.html'));
  return win;
}
