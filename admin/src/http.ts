export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** One request only: mutations are never automatically retried. */
export async function requestJson<T>(options: {
  url: string;
  method: string;
  token?: string | null;
  body?: unknown;
  headers?: Record<string, string>;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeoutMs ?? 15_000);
  try {
    let target: URL;
    try {
      target = new URL(options.url);
    } catch {
      throw new ApiError(0, 'INSECURE_TRANSPORT', '服务地址必须使用 HTTPS（本机开发除外）');
    }
    const localHttp = target.protocol === 'http:'
      && ['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname);
    if (target.username || target.password || target.href.includes('#')
        || (target.protocol !== 'https:' && !localHttp)) {
      throw new ApiError(0, 'INSECURE_TRANSPORT', '服务地址必须使用 HTTPS（本机开发除外）');
    }
    const headers: Record<string, string> = { ...options.headers };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.token) headers.Authorization = `Bearer ${options.token}`;
    const response = await (options.fetchImpl ?? fetch)(options.url, {
      method: options.method,
      redirect: 'error',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new ApiError(response.status, 'INVALID_RESPONSE', '服务端返回了无效数据');
      }
    }
    if (!response.ok) {
      const payload = (parsed as { error?: Record<string, unknown> } | null)?.error;
      const error = payload && typeof payload === 'object' ? payload : {};
      throw new ApiError(
        response.status,
        String(error.code ?? 'INTERNAL_ERROR'),
        String(error.message ?? `请求失败（${response.status}）`),
        error.details && typeof error.details === 'object'
          ? error.details as Record<string, unknown>
          : {}
      );
    }
    return parsed as T;
  } catch (error) {
    if (timedOut) throw new ApiError(0, 'REQUEST_TIMEOUT', '请求超时，请检查连接后重试');
    if (error instanceof ApiError) throw error;
    throw new ApiError(0, 'NETWORK_ERROR', '无法连接服务，请检查连接后重试');
  } finally {
    clearTimeout(timeout);
  }
}
