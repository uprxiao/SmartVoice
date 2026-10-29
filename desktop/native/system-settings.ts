import { app, shell, systemPreferences } from 'electron';
import { win32 } from 'node:path';
import type { AppInfo, AutoStartInfo } from '../src/shared/types';

const ENTRY = 'SmartVoiceInput';
const ARGS = ['--background'];
export function microphoneInfo(): AppInfo['microphone'] {
  try {
    const status = systemPreferences.getMediaAccessStatus('microphone');
    return { status, error: status === 'unknown' };
  } catch { return { status: 'unknown', error: true }; }
}

export function autoStartInfo(): AutoStartInfo {
  const supported = process.platform === 'win32';
  const configurable = supported && app.isPackaged;
  const empty = { supported, configurable, registered: false, enabled: false, systemDisabled: false, error: false };
  if (!configurable) return empty;
  try {
    const info = app.getLoginItemSettings({ path: process.execPath, args: ARGS });
    const items = info.launchItems.filter(item => item.name === ENTRY
      && win32.normalize(item.path).toLowerCase() === win32.normalize(process.execPath).toLowerCase()
      && JSON.stringify(item.args) === JSON.stringify(ARGS));
    const registered = info.openAtLogin || items.length > 0;
    // Do not use executableWillLaunchAtLogin: that ignores the launch arguments.
    if (registered && items.length === 0) return { ...empty, registered, error: true };
    const enabled = items.some(item => item.enabled);
    return { ...empty, registered, enabled, systemDisabled: registered && !enabled };
  } catch { return { ...empty, error: true }; }
}

export function updateAutoStart(enabled: boolean): AutoStartInfo {
  if (typeof enabled !== 'boolean') throw new Error('AUTOSTART_INVALID');
  const before = autoStartInfo();
  if (!before.configurable || before.error) throw new Error('AUTOSTART_UNAVAILABLE');
  const write = (value: boolean, launchEnabled = value) => app.setLoginItemSettings({ name: ENTRY, path: process.execPath, args: ARGS, openAtLogin: value, enabled: launchEnabled });
  try {
    write(enabled);
    const after = autoStartInfo();
    if (after.error || after.registered !== enabled || (enabled && !after.enabled)) throw new Error('AUTOSTART_READBACK_FAILED');
    return after;
  } catch {
    try {
      write(before.registered, before.enabled);
      const rollback = autoStartInfo();
      if (rollback.error || rollback.registered !== before.registered || rollback.enabled !== before.enabled) throw new Error();
    } catch { throw new Error('AUTOSTART_STATE_UNKNOWN'); }
    throw new Error('AUTOSTART_SAVE_FAILED');
  }
}

export async function openMicrophoneSettings(): Promise<void> {
  const uri = process.platform === 'win32' ? 'ms-settings:privacy-microphone'
    : process.platform === 'darwin' ? 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone' : null;
  if (!uri) throw new Error('SYSTEM_SETTINGS_UNAVAILABLE');
  await shell.openExternal(uri);
}
export async function openStartupSettings(): Promise<void> {
  if (process.platform !== 'win32') throw new Error('SYSTEM_SETTINGS_UNAVAILABLE');
  await shell.openExternal('ms-settings:startupapps');
}
