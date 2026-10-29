import type { AppInfo } from '@shared/types';

/** Settings permission read-back; never prompts or treats opening Settings as a grant. */
export function watchAppInfo(read: () => Promise<AppInfo>, publish: (info: AppInfo | null) => void) {
  let disposed = false;
  let inFlight = false;
  let repeat = false;
  let revision = 0;
  let pollMissingPermission = true;
  const refresh = () => {
    if (disposed) return;
    const current = ++revision;
    if (inFlight) { repeat = true; return; }
    inFlight = true;
    void read().then(info => {
      if (disposed || current !== revision) return;
      pollMissingPermission = (info.accessibilitySupported && !info.accessibilityTrusted)
        || (info.platform === 'win32' && (info.microphone.status !== 'granted' || info.autoStart.error || info.autoStart.systemDisabled));
      publish(info);
    }).catch(() => {
      if (!disposed && current === revision) publish(null);
    }).finally(() => {
      inFlight = false;
      if (!disposed && repeat) { repeat = false; refresh(); }
    });
  };
  const visible = () => { if (document.visibilityState !== 'hidden') refresh(); };
  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', visible);
  const timer = setInterval(() => {
    if (pollMissingPermission && document.visibilityState !== 'hidden') refresh();
  }, 1500);
  refresh();
  return {
    refresh,
    stop: () => {
      disposed = true;
      revision++;
      clearInterval(timer);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', visible);
    },
  };
}
