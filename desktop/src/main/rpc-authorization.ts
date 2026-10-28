import type { BrowserWindow, IpcMainInvokeEvent } from 'electron';

type RpcWindow = Pick<BrowserWindow, 'webContents'>;
interface WindowSources {
  getMainWindow(): RpcWindow | null;
  getHudWindow(): RpcWindow | null;
}

/** Main retains its existing router; HUD only reads its initial session status. */
export function createRendererRpcAuthorizer(windows: WindowSources) {
  return (event: IpcMainInvokeEvent, channel: string): boolean => {
    const main = windows.getMainWindow();
    if (main && event.sender === main.webContents) return true;
    // HUD confirm/cancel use independently checked ipcMain.on event handlers.
    const hud = windows.getHudWindow();
    return Boolean(hud && event.sender === hud.webContents && channel === 'session.status');
  };
}
