import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AppInfo } from '@shared/types';
import { watchAppInfo } from './app-info-refresh';
const info = (trusted: boolean, supported = true): AppInfo => ({
  nativeInputSupported:true, correctionObservationSupported:true, microphone:{status:'granted',error:false},
  autoStart:{supported:false,configurable:false,registered:false,enabled:false,systemDisabled:false,error:false},
  version: '0.0.1', electron: '42', platform: 'darwin', shortcut: 'CommandOrControl+Shift+Space',
  apiBaseUrl: 'http://127.0.0.1:17863', accessibilityTrusted: trusted, accessibilitySupported: supported,
});
const settle = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };
let win: EventTarget, doc: EventTarget & { visibilityState: string };
let stop: (() => void) | undefined;
beforeEach(() => {
  vi.useFakeTimers(); win = new EventTarget(); doc = Object.assign(new EventTarget(), { visibilityState: 'visible' });
  vi.stubGlobal('window', win); vi.stubGlobal('document', doc);
});
afterEach(() => { stop?.(); stop = undefined; vi.useRealTimers(); vi.unstubAllGlobals(); });
describe('Settings live permission read-back', () => {
  it('returning from System Settings updates denied to granted without remount or restart', async () => {
    const read = vi.fn().mockResolvedValueOnce(info(false)).mockResolvedValue(info(true));
    const publish = vi.fn(); const watcher = watchAppInfo(read, publish); stop = watcher.stop;
    await settle(); expect(publish).toHaveBeenLastCalledWith(info(false));
    win.dispatchEvent(new Event('focus')); await settle();
    expect(publish).toHaveBeenLastCalledWith(info(true));
    await vi.advanceTimersByTimeAsync(6000); expect(read).toHaveBeenCalledTimes(2);
  });
  it('while visible and denied polls actual state; never grants merely by opening settings', async () => {
    const read = vi.fn().mockResolvedValue(info(false)); const publish = vi.fn();
    stop = watchAppInfo(read, publish).stop; await settle();
    await vi.advanceTimersByTimeAsync(3000); expect(read).toHaveBeenCalledTimes(3);
    expect(publish.mock.calls.every(([value]) => value.accessibilityTrusted === false)).toBe(true);
  });
  it('does not poll a hidden window, resumes when visible, and observes later revocation on focus', async () => {
    const read = vi.fn().mockResolvedValue(info(false)); const publish = vi.fn();
    stop = watchAppInfo(read, publish).stop; await settle(); doc.visibilityState = 'hidden';
    await vi.advanceTimersByTimeAsync(3000); expect(read).toHaveBeenCalledOnce();
    read.mockResolvedValue(info(true)); doc.visibilityState = 'visible'; doc.dispatchEvent(new Event('visibilitychange')); await settle();
    expect(publish).toHaveBeenLastCalledWith(info(true));
    read.mockResolvedValue(info(false)); win.dispatchEvent(new Event('focus')); await settle();
    expect(publish).toHaveBeenLastCalledWith(info(false));
  });
  it('coalesces overlapping focus reads and never publishes an obsolete denial', async () => {
    let resolve!: (value: AppInfo) => void;
    const read = vi.fn().mockReturnValueOnce(new Promise<AppInfo>(done => { resolve = done; })).mockResolvedValue(info(true));
    const publish = vi.fn(); stop = watchAppInfo(read, publish).stop;
    win.dispatchEvent(new Event('focus')); win.dispatchEvent(new Event('focus'));
    expect(read).toHaveBeenCalledOnce(); resolve(info(false)); await settle();
    expect(read).toHaveBeenCalledTimes(2); expect(publish).toHaveBeenCalledExactlyOnceWith(info(true));
  });
  it('cleans up focus/timer listeners and ignores late completion after Settings closes', async () => {
    let resolve!: (value: AppInfo) => void;
    const read = vi.fn().mockReturnValue(new Promise<AppInfo>(done => { resolve = done; })); const publish = vi.fn();
    const watcher = watchAppInfo(read, publish); stop = watcher.stop; watcher.stop();
    resolve(info(true)); win.dispatchEvent(new Event('focus')); doc.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(3000); expect(read).toHaveBeenCalledOnce(); expect(publish).not.toHaveBeenCalled();
  });
  it('keeps a failed read unknown and does not poll unsupported platforms', async () => {
    const read = vi.fn().mockRejectedValueOnce(new Error('probe failed')).mockResolvedValue(info(false, false));
    const publish = vi.fn(); stop = watchAppInfo(read, publish).stop; await settle();
    expect(publish).toHaveBeenLastCalledWith(null);
    win.dispatchEvent(new Event('focus')); await settle();
    await vi.advanceTimersByTimeAsync(3000); expect(read).toHaveBeenCalledTimes(2);
    expect(publish).toHaveBeenLastCalledWith(info(false, false));
  });
});
