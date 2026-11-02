import { systemPreferences } from 'electron';
export { microphoneInfo, openMicrophoneSettings } from '../../../native/system-settings';

export function prepareCaptureAccess(isCurrent: () => boolean): void | Promise<void> {
  if (process.platform !== 'darwin' && process.platform !== 'win32') return;
  const query = () => {
    try { return systemPreferences.getMediaAccessStatus('microphone'); }
    catch { throw new Error('MICROPHONE_PERMISSION_UNAVAILABLE'); }
  };
  const status = query();
  if (status === 'not-determined' && process.platform === 'darwin') {
    return (async () => {
      let granted: boolean;
      try { granted = await systemPreferences.askForMediaAccess('microphone'); }
      catch { throw new Error('MICROPHONE_PERMISSION_UNAVAILABLE'); }
      if (!isCurrent()) throw new Error('录音操作已取消');
      if (!granted) throw new Error('MICROPHONE_PERMISSION_DENIED');
      ensureGranted(query());
    })();
  }
  ensureGranted(status);
}

function ensureGranted(status: string): void {
  if (status === 'denied' || status === 'restricted') throw new Error('MICROPHONE_PERMISSION_DENIED');
  if (status !== 'granted') throw new Error('MICROPHONE_PERMISSION_UNAVAILABLE');
}
