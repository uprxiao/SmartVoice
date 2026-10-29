import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const f = vi.hoisted(() => ({ packaged: true, read: vi.fn(), write: vi.fn(), identity: vi.fn(), open: vi.fn(), microphone: vi.fn() }));
vi.mock('electron', () => ({ app: { get isPackaged() { return f.packaged; }, getLoginItemSettings: f.read, setLoginItemSettings: f.write, setAppUserModelId: f.identity },
  shell: { openExternal: f.open }, systemPreferences: { getMediaAccessStatus: f.microphone } }));
import { autoStartInfo, configureWindowsAppIdentity, microphoneInfo, openMicrophoneSettings, openStartupSettings, updateAutoStart } from '../../../native/system-settings';
const platform = Object.getOwnPropertyDescriptor(process, 'platform')!;
let registered: boolean, enabled: boolean;
beforeEach(() => {
  vi.resetAllMocks(); Object.defineProperty(process, 'platform', { ...platform, value: 'win32' });
  registered = false; enabled = false; f.packaged = true;
  f.read.mockImplementation(() => ({ openAtLogin: registered, executableWillLaunchAtLogin: true,
    launchItems: registered ? [{ name: 'com.smartvoiceinput.desktop', path: process.execPath, args: [], scope: 'user', enabled }] : [] }));
  f.write.mockImplementation((options: { openAtLogin: boolean; enabled: boolean }) => { registered = options.openAtLogin; enabled = options.enabled; });
});
afterEach(() => Object.defineProperty(process, 'platform', platform));
describe('Windows startup state is read back from its own exact entry', () => {
  it('sets the stable Windows app identity and leaves the Mac identity unchanged', () => {
    configureWindowsAppIdentity(); expect(f.identity).toHaveBeenCalledExactlyOnceWith('com.smartvoiceinput.desktop');
    Object.defineProperty(process, 'platform', { ...platform, value: 'darwin' });
    configureWindowsAppIdentity(); expect(f.identity).toHaveBeenCalledTimes(1);
  });
  it('defaults off even when another launch of the executable is enabled, and reads back a real toggle', () => {
    expect(autoStartInfo()).toMatchObject({ registered: false, enabled: false, error: false });
    expect(updateAutoStart(true)).toMatchObject({ registered: true, enabled: true });
    expect(f.read).toHaveBeenCalledWith({ path: `"${process.execPath}"`, args: ['--background'] });
    expect(f.write).toHaveBeenCalledWith(expect.objectContaining({ name: 'com.smartvoiceinput.desktop', args: ['--background'], path: process.execPath }));
    expect(updateAutoStart(false)).toMatchObject({ registered: false, enabled: false });
  });
  it('distinguishes system-disabled registration and never adopts another entry', () => {
    registered = true;
    expect(autoStartInfo()).toMatchObject({ registered: true, enabled: false, systemDisabled: true });
    f.read.mockReturnValue({ openAtLogin: true, launchItems: [{ name: 'Other', path: process.execPath, args: ['--background'], enabled: true }] });
    expect(autoStartInfo()).toMatchObject({ error: true, enabled: false });
  });
  it('requires exact command-line proof even though launchItems omits switch arguments', () => {
    f.read.mockReturnValue({ openAtLogin: true, launchItems: [{ name: 'com.smartvoiceinput.desktop', path: process.execPath, args: [], scope: 'user', enabled: true }] });
    expect(autoStartInfo()).toMatchObject({ registered: true, enabled: true, error: false });
    f.read.mockReturnValue({ openAtLogin: false, launchItems: [{ name: 'com.smartvoiceinput.desktop', path: process.execPath, args: [], scope: 'user', enabled: true }] });
    expect(autoStartInfo()).toMatchObject({ registered: true, enabled: false, error: true });
    expect(() => updateAutoStart(true)).toThrow('AUTOSTART_UNAVAILABLE');
    f.read.mockReturnValue({ openAtLogin: false, launchItems: [{ name: 'com.smartvoiceinput.desktop', path: process.execPath, args: [], scope: 'machine', enabled: true }] });
    expect(autoStartInfo()).toMatchObject({ registered: false, enabled: false });
  });
  it('does not acknowledge an ineffective write and verifies rollback', () => {
    f.write.mockImplementation(() => undefined);
    expect(() => updateAutoStart(true)).toThrow('AUTOSTART_SAVE_FAILED');
    expect(autoStartInfo()).toMatchObject({ registered: false, enabled: false });
    expect(f.write).toHaveBeenCalledTimes(2);
  });
  it('restores the system-disabled value when a write fails, rather than enabling it', () => {
    registered = true; enabled = false;
    f.write.mockImplementationOnce(() => { throw new Error(); });
    expect(() => updateAutoStart(false)).toThrow('AUTOSTART_SAVE_FAILED');
    expect(f.write).toHaveBeenLastCalledWith(expect.objectContaining({ openAtLogin: true, enabled: false }));
    expect(autoStartInfo().systemDisabled).toBe(true);
  });
  it('reports unknown after a failed rollback, and never configures the development shell or Mac', () => {
    f.write.mockImplementation(() => { throw new Error(); });
    expect(() => updateAutoStart(true)).toThrow('AUTOSTART_STATE_UNKNOWN');
    f.packaged = false; expect(autoStartInfo().configurable).toBe(false);
    Object.defineProperty(process, 'platform', { ...platform, value: 'darwin' });
    expect(autoStartInfo()).toMatchObject({ supported: false, configurable: false });
    expect(() => updateAutoStart(false)).toThrow('AUTOSTART_UNAVAILABLE');
  });
  it('fails closed on an unreadable system state and validates IPC values', () => {
    f.read.mockImplementation(() => { throw new Error(); });
    expect(autoStartInfo()).toMatchObject({ error: true });
    expect(() => updateAutoStart('yes' as unknown as boolean)).toThrow('AUTOSTART_INVALID');
    expect(f.write).not.toHaveBeenCalled();
  });
  it('opens only fixed platform settings and never invents microphone permission', async () => {
    f.microphone.mockImplementation(() => { throw new Error(); });
    expect(microphoneInfo()).toEqual({ status: 'unknown', error: true });
    await openMicrophoneSettings(); await openStartupSettings();
    expect(f.open.mock.calls).toEqual([['ms-settings:privacy-microphone'], ['ms-settings:startupapps']]);
  });
});
