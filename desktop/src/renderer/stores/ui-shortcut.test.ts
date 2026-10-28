import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AppInfo } from '@shared/types';
vi.mock('../lib/ipc', () => ({ rpc: { app: { info: vi.fn(), updateShortcut: vi.fn() } } }));
import { rpc } from '../lib/ipc';
import { UiStore } from './ui-store';

const info = (shortcut: string): AppInfo => ({
  shortcut, version: '0.0.1', electron: '42.11.15', platform: 'darwin',
  accessibilityTrusted: false, accessibilitySupported: true, apiBaseUrl: 'http://127.0.0.1:17863',
});
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};
afterEach(() => { vi.resetAllMocks(); });

describe('device shortcut acknowledgement and read generations', () => {
  it('starts unknown and uses the exact Main restored shortcut after the read', async () => {
    const read = deferred<AppInfo>();
    vi.mocked(rpc.app.info).mockReturnValueOnce(read.promise);
    const store = new UiStore();
    const pending = store.loadAppInfo();
    expect(store.localShortcut).toBeNull();
    read.resolve(info('F19'));
    expect(await pending).toEqual(info('F19'));
    expect(store.localShortcut).toBe('F19');
    expect(rpc.app.info).toHaveBeenCalledTimes(1);
  });

  it('a late App/Settings info read cannot overwrite a completed save', async () => {
    const read = deferred<AppInfo>();
    const save = deferred<string>();
    vi.mocked(rpc.app.info).mockReturnValueOnce(read.promise);
    vi.mocked(rpc.app.updateShortcut).mockReturnValueOnce(save.promise);
    const store = new UiStore();
    const pendingRead = store.loadAppInfo();
    const pendingSave = store.updateShortcut('F19');
    expect(store.localShortcut).toBeNull(); // No optimistic success before Main.
    save.resolve('F19');
    expect(await pendingSave).toBe('F19');
    read.resolve(info('CommandOrControl+Shift+Space'));
    await pendingRead;
    expect(store.localShortcut).toBe('F19');
    expect(rpc.app.updateShortcut).toHaveBeenCalledExactlyOnceWith('F19');
    store.clear(); // Account changes clear toasts, not device-local configuration.
    expect(store.localShortcut).toBe('F19');
  });

  it('two overlapping reads publish only the newest response', async () => {
    const older = deferred<AppInfo>();
    const newer = deferred<AppInfo>();
    vi.mocked(rpc.app.info).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    const store = new UiStore();
    const first = store.loadAppInfo();
    const second = store.loadAppInfo();
    newer.resolve(info('Control+Alt+K')); await second;
    older.resolve(info('F19')); await first;
    expect(store.localShortcut).toBe('Control+Alt+K');
  });

  it('a failed read preserves a known value and preserves the actual rejection', async () => {
    const error = new Error('controlled IPC failure');
    vi.mocked(rpc.app.info).mockResolvedValueOnce(info('F19')).mockRejectedValueOnce(error);
    const store = new UiStore(); await store.loadAppInfo();
    await expect(store.loadAppInfo()).rejects.toBe(error);
    expect(store.localShortcut).toBe('F19');
  });

  it('a rejected shortcut save does not replace the last acknowledged value', async () => {
    const error = new Error('controlled shortcut conflict');
    vi.mocked(rpc.app.info).mockResolvedValueOnce(info('F19'));
    vi.mocked(rpc.app.updateShortcut).mockRejectedValueOnce(error);
    const store = new UiStore(); await store.loadAppInfo();
    await expect(store.updateShortcut('Control+Alt+K')).rejects.toBe(error);
    expect(store.localShortcut).toBe('F19');
  });
});
