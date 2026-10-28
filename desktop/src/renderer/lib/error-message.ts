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
