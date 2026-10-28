import { describe, expect, it, vi } from 'vitest';
import type { BrowserWindow, IpcMain, IpcMainInvokeEvent, WebContents } from 'electron';

// Only construct the real router's handler inventory. No Electron API or
// business handler executes; the boundary is exercised using dispatch spies.
vi.mock('electron', () => ({ app: {}, dialog: {}, shell: {}, systemPreferences: {} }));
import { registerRPCRouter } from '@shared/ipc/rpc';
import { createRpcRouter, type RpcDeps } from './rpc';
import { createRendererRpcAuthorizer } from './rpc-authorization';

type Handler = (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown;
type TestWindow = Pick<BrowserWindow, 'webContents'>;
const windowFixture = (id: number): TestWindow => ({ webContents: { id } as WebContents });
const event = (sender: WebContents): IpcMainInvokeEvent => ({ sender }) as IpcMainInvokeEvent;

function fixture() {
  const windows: { main: TestWindow | null; hud: TestWindow | null } = {
    main: windowFixture(1), hud: windowFixture(2),
  };
  const originalMain = windows.main!, originalHud = windows.hud!;
  const authorize = vi.fn(createRendererRpcAuthorizer({
    getMainWindow: () => windows.main,
    getHudWindow: () => windows.hud,
  }));
  const registered = new Map<string, Handler>();
  const calls = new Map<string, ReturnType<typeof vi.fn>>();
  const inventory = createRpcRouter({} as RpcDeps);
  const router = Object.fromEntries(Object.entries(inventory).map(([namespace, procedures]) => [
    namespace,
    Object.fromEntries(Object.keys(procedures).map(procedure => {
      const channel = `${namespace}.${procedure}`;
      const handler = vi.fn((...args: unknown[]) => ({ channel, args }));
      calls.set(channel, handler);
      return [procedure, handler];
    })),
  ]));
  const ipc = { handle: (channel: string, handler: Handler) => {
    if (registered.has(channel)) throw new Error(`Duplicate channel: ${channel}`);
    registered.set(channel, handler);
  } } as unknown as IpcMain;
  registerRPCRouter(router, ipc, authorize);
  return { windows, originalMain, originalHud, authorize, registered, calls };
}

describe('renderer RPC authorization by current window and registered channel', () => {
  it('keeps every existing registered RPC available to the main window with unchanged arguments', () => {
    const f = fixture(), sender = event(f.originalMain.webContents);
    const args = ['synthetic-only', { version: 1 }];
    expect(f.registered.size).toBe(f.calls.size);
    expect(f.registered.has('session.status')).toBe(true);
    expect(f.registered.has('dictionary.importCsv')).toBe(true);
    for (const [channel, invoke] of f.registered) {
      expect(invoke(sender, ...args)).toEqual({ channel, args });
      expect(f.calls.get(channel)).toHaveBeenCalledExactlyOnceWith(...args);
      expect(f.authorize).toHaveBeenCalledWith(sender, channel);
    }
  });

  it('lets HUD read session.status and denies every other registered RPC before dispatch', () => {
    const f = fixture(), sender = event(f.originalHud.webContents);
    for (const [channel, invoke] of f.registered) {
      if (channel === 'session.status') {
        expect(invoke(sender)).toEqual({ channel, args: [] });
        expect(f.calls.get(channel)).toHaveBeenCalledExactlyOnceWith();
      } else {
        expect(() => invoke(sender, 'synthetic-only')).toThrow('Untrusted IPC sender');
        expect(f.calls.get(channel)).not.toHaveBeenCalled();
      }
    }
    // The HUD UI does not read settings; retain the strict single-channel scope.
    expect(f.calls.get('settings.get')).not.toHaveBeenCalled();
    expect(f.calls.get('settings.update')).not.toHaveBeenCalled();
    expect(f.calls.get('auth.logout')).not.toHaveBeenCalled();
    expect(f.calls.get('session.insert')).not.toHaveBeenCalled();
  });

  it('denies all registered channels from an unknown window, including an identical numeric ID', () => {
    const f = fixture(), sender = event(windowFixture(f.originalMain.webContents.id).webContents);
    for (const [channel, invoke] of f.registered) {
      expect(() => invoke(sender)).toThrow('Untrusted IPC sender');
      expect(f.calls.get(channel)).not.toHaveBeenCalled();
    }
  });

  it('uses the registered channel, so a payload cannot impersonate session.status', () => {
    const f = fixture(), sender = event(f.originalHud.webContents);
    expect(() => f.registered.get('session.insert')!(sender, 'session.status')).toThrow('Untrusted IPC sender');
    expect(f.authorize).toHaveBeenCalledExactlyOnceWith(sender, 'session.insert');
    expect(f.calls.get('session.insert')).not.toHaveBeenCalled();
    expect(f.authorize(sender, 'session.status.extra')).toBe(false);
    expect(f.registered.has('session.status.extra')).toBe(false);
  });

  it('denies stale windows after current-window replacement and permits the new owners', () => {
    const f = fixture();
    f.windows.main = windowFixture(3); f.windows.hud = windowFixture(4);
    const status = f.registered.get('session.status')!;
    for (const stale of [f.originalMain, f.originalHud]) {
      expect(() => status(event(stale.webContents))).toThrow('Untrusted IPC sender');
    }
    expect(status(event(f.windows.hud.webContents))).toEqual({ channel: 'session.status', args: [] });
    expect(f.registered.get('auth.logout')!(event(f.windows.main.webContents)))
      .toEqual({ channel: 'auth.logout', args: [] });
  });

  it('denies every registered channel when neither current window exists', () => {
    const f = fixture();
    f.windows.main = null; f.windows.hud = null;
    for (const [channel, invoke] of f.registered) {
      for (const former of [f.originalMain, f.originalHud]) {
        expect(() => invoke(event(former.webContents))).toThrow('Untrusted IPC sender');
      }
      expect(f.calls.get(channel)).not.toHaveBeenCalled();
    }
  });
});
