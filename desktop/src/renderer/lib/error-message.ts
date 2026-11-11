/**
 * Renderer-side presentation of errors crossing the typed RPC bridge.
 *
 * Electron wraps main-process rejections as
 * "Error invoking remote method 'history.list': TypeError: fetch failed".
 * Users should see the actionable part instead of the transport wrapper.
 */
export function errorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const unwrapped = raw
    .replace(/^Error invoking remote method '[^']+':\s*/, '')
    .replace(/^Error:\s*/, '');
  if (/fetch failed|ECONNREFUSED|ECONNRESET|ENOTFOUND|ETIMEDOUT|network error/i.test(unwrapped)) {
    return '无法连接服务，请检查网络后重试';
  }
  return unwrapped;
}

/** Localize shortcut-policy errors for the English settings UI without changing the RPC contract. */
export function shortcutErrorMessage(message: string, locale: 'zh-CN' | 'en'): string {
  if (locale !== 'en') return message;
  if (message === '快捷键不能为空') return 'Shortcut cannot be empty.';
  if (message === 'Escape 用于取消操作，不能设为全局快捷键') {
    return 'Escape is used to cancel. Choose another shortcut.';
  }
  if (message === '此快捷键为系统或常用操作保留，请选择其他组合') {
    return 'This shortcut is reserved by the system or a common action. Choose another combination.';
  }
  const unavailable = message.match(/^快捷键 (.+) 不可用，仍保留 (.+)$/);
  if (unavailable) return `Shortcut ${unavailable[1]} is unavailable; keeping ${unavailable[2]}.`;
  return message;
}
