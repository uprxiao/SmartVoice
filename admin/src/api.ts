import { apiUrl } from './config';
import { ApiError, requestJson } from './http';
export { ApiError } from './http';
import type {
  AdminUserItem,
  CapacityView,
  ModelUsage,
  Overview,
  Page,
  TokenPair,
  UsageItem,
  User,
} from './types';
import type { ActiveView, AuditItem, ProviderItem, ProviderKind } from './providers';

interface LoginContext {
  accessToken: string;
  refreshToken: string | null;
  userId: string | null;
  refreshTask: Promise<void> | null;
  invalidated: { error: ApiError; epoch: number } | null;
}
let context: LoginContext | null = null;
let epoch = 0;
let authFailureEpoch: number | null = null;
const errorEpochs = new WeakMap<ApiError, number>();
const REQUEST_MS = 15_000;
let authFailureSubscription: { listener: (error: ApiError) => void } | null = null;

/** The mounted App owns this subscription; an old cleanup cannot remove a new one. */
export function onAuthInvalidated(listener: (error: ApiError) => void): () => void {
  const subscription = { listener };
  authFailureSubscription = subscription;
  return () => {
    if (authFailureSubscription === subscription) authFailureSubscription = null;
  };
}

function notifyAuthInvalidated(error: ApiError): void {
  if (isCurrentAuthError(error)) authFailureSubscription?.listener(error);
}

export function setToken(token: string | null): void {
  epoch++;
  authFailureEpoch = null;
  context = token === null ? null : { accessToken: token, refreshToken: null, userId: null, refreshTask: null, invalidated: null };
}

/** Old callbacks must not clear a newer login or show its predecessor's error. */
export function isCurrentAuthError(error: unknown): boolean {
  return !(error instanceof ApiError) || (error.code !== 'AUTH_CONTEXT_CHANGED'
    && (!errorEpochs.has(error) || errorEpochs.get(error) === epoch));
}

const contextChanged = () => new ApiError(0, 'AUTH_CONTEXT_CHANGED', '登录状态已变化，请重新操作');
const deadline = () => performance.now() + REQUEST_MS;
function remaining(until: number): number {
  const ms = until - performance.now();
  if (ms <= 0) throw new ApiError(0, 'REQUEST_TIMEOUT', '请求超时，请检查连接后重试');
  return ms;
}
function assertCurrent(captured: LoginContext | null, generation: number): void {
  if (context === captured && epoch === generation) return;
  // Preserve this login's terminal auth failure until App handles it. A filter
  // change can discard its first caller; another in-flight read must still
  // carry the reason to App. Explicit clear/new login advances epoch again.
  if (context === null && captured?.invalidated?.epoch === epoch) {
    throw captured.invalidated.error;
  }
  throw contextChanged();
}
async function within<T>(task: Promise<T>, until: number): Promise<T> {
  const ms = remaining(until);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([task, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new ApiError(0, 'REQUEST_TIMEOUT', '请求超时，请检查连接后重试')), ms);
    })]);
  } finally { if (timer !== undefined) clearTimeout(timer); }
}

/** Updates only the captured login object, even when logout has detached it. */
async function refresh(captured: LoginContext, until: number): Promise<void> {
  if (!captured.refreshToken) throw new ApiError(401, 'AUTH_INVALID', '登录已过期，请重新登录');
  if (!captured.refreshTask) {
    const task = (async () => {
      const pair = await requestJson<TokenPair>({ url: apiUrl('/api/auth/refresh'), method: 'POST',
        body: { refresh_token: captured.refreshToken }, timeoutMs: remaining(until) });
      if (!pair || !pair.user || typeof pair.user.id !== 'string'
          || typeof pair.access_token !== 'string' || !pair.access_token
          || typeof pair.refresh_token !== 'string') {
        throw new ApiError(401, 'AUTH_INVALID', '登录状态校验失败，请重新登录');
      }
      if (pair.user.role !== 'admin') throw new ApiError(403, 'FORBIDDEN', '该账号没有管理员权限');
      if (pair.user.id !== captured.userId || pair.refresh_token !== captured.refreshToken) {
        throw new ApiError(401, 'AUTH_INVALID', '登录状态校验失败，请重新登录');
      }
      captured.accessToken = pair.access_token;
    })();
    captured.refreshTask = task;
    const clear = () => { if (captured.refreshTask === task) captured.refreshTask = null; };
    // A failed login context must not repeatedly retry its refresh credentials.
    void task.then(clear, () => undefined);
  }
  await within(captured.refreshTask, until);
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  extraHeaders: Record<string, string> = {}
): Promise<T> {
  const captured = context, generation = epoch, until = deadline();
  // Freeze caller-owned objects before the first await; a replay must be exact.
  const frozenBody: unknown = body === undefined ? undefined : JSON.parse(JSON.stringify(body));
  const headers = { ...extraHeaders };
  const send = (token: string | undefined) => requestJson<T>({ url: apiUrl(path), method,
    body: frozenBody, headers, token, timeoutMs: remaining(until) });
  const originalToken = captured?.accessToken;
  try {
    let result: T;
    try { result = await send(originalToken); }
    catch (error) {
      assertCurrent(captured, generation);
      if (!(error instanceof ApiError) || error.status !== 401 || !captured?.refreshToken) throw error;
      // A concurrent request may have already refreshed this same login.
      if (captured.accessToken === originalToken) {
        try { await refresh(captured, until); }
        catch (refreshError) {
          assertCurrent(captured, generation);
          if (refreshError instanceof ApiError && [401, 403].includes(refreshError.status)) throw refreshError;
          throw new ApiError(401, 'AUTH_INVALID', '登录刷新失败，请重新登录', {
            cause_code: refreshError instanceof ApiError ? refreshError.code : 'INVALID_RESPONSE',
          });
        }
      }
      assertCurrent(captured, generation);
      result = await send(captured.accessToken); // One replay only, including mutations.
    }
    assertCurrent(captured, generation);
    return result;
  } catch (error) {
    assertCurrent(captured, generation);
    if (error instanceof ApiError) {
      if ([401, 403].includes(error.status) && !(captured === null && authFailureEpoch === epoch)) {
        setToken(null);
        authFailureEpoch = epoch;
        if (captured) captured.invalidated = { error, epoch };
      }
      errorEpochs.set(error, epoch);
      if ([401, 403].includes(error.status)) notifyAuthInvalidated(error);
    }
    throw error;
  }
}

export const api = {
  async login(email: string, password: string): Promise<TokenPair> {
    setToken(null);
    const generation = epoch;
    try {
      // Login never sends an old account's access token or refreshes that account.
      const pair = await requestJson<TokenPair>({ url: apiUrl('/api/auth/login'), method: 'POST',
        body: { email, password, client_type: 'admin_web' } });
      assertCurrent(null, generation);
      if (pair.user.role !== 'admin') throw new ApiError(403, 'FORBIDDEN', '该账号没有管理员权限');
      if (!pair.user.id || !pair.access_token || !pair.refresh_token) {
        throw new ApiError(0, 'INVALID_RESPONSE', '服务端返回了无效数据');
      }
      context = { accessToken: pair.access_token, refreshToken: pair.refresh_token,
        userId: pair.user.id, refreshTask: null, invalidated: null };
      return pair;
    } catch (error) {
      if (epoch !== generation) throw contextChanged();
      throw error;
    }
  },
  async logout(): Promise<void> {
    const captured = context, until = deadline();
    const originalToken = captured?.accessToken;
    const send = (token: string | undefined) => requestJson<void>({ url: apiUrl('/api/auth/logout'),
      method: 'POST', token, timeoutMs: remaining(until) });
    try { await send(originalToken); }
    catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401 || !captured?.refreshToken) throw error;
      if (captured.accessToken === originalToken) await refresh(captured, until);
      // Detached old credentials are only used to revoke their own SID. This
      // completion never restores credentials or clears a later login.
      await send(captured.accessToken);
    }
  },
  users(params: { q?: string; status?: string; cursor?: string | null }): Promise<Page<AdminUserItem>> {
    const query = new URLSearchParams();
    if (params.q) query.set('q', params.q);
    if (params.status) query.set('status', params.status);
    if (params.cursor) query.set('cursor', params.cursor);
    query.set('limit', '20');
    return request('GET', `/api/admin/users?${query.toString()}`);
  },
  createUser(payload: { email: string; password: string; display_name?: string }, idempotencyKey: string = crypto.randomUUID()): Promise<{
    user: User;
    replayed: boolean;
  }> {
    return request('POST', '/api/admin/users', payload, {
      'Idempotency-Key': idempotencyKey,
    });
  },
  updateUser(id: string, status: 'active' | 'disabled'): Promise<{ user: User }> {
    return request('PATCH', `/api/admin/users/${id}`, { status });
  },
  overview(dateFrom: string, dateTo: string): Promise<Overview> {
    return request('GET', `/api/admin/stats/overview?date_from=${dateFrom}&date_to=${dateTo}`);
  },
  usage(dateFrom: string, dateTo: string, userId?: string, cursor?: string): Promise<{ timezone: string; items: UsageItem[]; next_cursor: string | null }> {
    const query = new URLSearchParams({ date_from: dateFrom, date_to: dateTo });
    if (userId) query.set('user_id', userId);
    if (cursor) query.set('cursor', cursor);
    return request('GET', `/api/admin/stats/usage?${query.toString()}`);
  },
  models(dateFrom: string, dateTo: string, userId?: string): Promise<{ timezone: string; items: ModelUsage[] }> {
    const query = new URLSearchParams({ date_from: dateFrom, date_to: dateTo });
    if (userId) query.set('user_id', userId);
    return request('GET', `/api/admin/stats/models?${query.toString()}`);
  },
  providers(kind?: ProviderKind): Promise<{ items: ProviderItem[] }> {
    return request('GET', `/api/admin/providers${kind ? `?kind=${kind}` : ''}`);
  },
  providersActive(): Promise<ActiveView> {
    return request('GET', '/api/admin/providers/active');
  },
  providerAudit(cursor?: string | null): Promise<{ items: AuditItem[]; next_cursor: string | null }> {
    return request('GET', `/api/admin/providers/audit?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
  },
  createProvider(body: Record<string, unknown>): Promise<ProviderItem> {
    return request('POST', '/api/admin/providers', body);
  },
  updateProvider(id: string, body: Record<string, unknown>): Promise<ProviderItem> {
    return request('PATCH', `/api/admin/providers/${encodeURIComponent(id)}`, body);
  },
  deleteProvider(id: string): Promise<{ deleted: boolean }> {
    return request('DELETE', `/api/admin/providers/${encodeURIComponent(id)}`);
  },
  testProvider(id: string): Promise<ProviderItem> {
    return request('POST', `/api/admin/providers/${encodeURIComponent(id)}/test`);
  },
  activateProvider(body: { kind: ProviderKind; provider_id: string; version: number; confirm_cloud?: boolean }): Promise<ActiveView> {
    return request('PUT', '/api/admin/providers/active', body);
  },
  capacity(): Promise<CapacityView> {
    return request('GET', '/api/admin/capacity');
  },
  updateCapacity(version: number, maxActiveSessions: number): Promise<CapacityView> {
    return request('PATCH', '/api/admin/capacity', {
      version,
      max_active_sessions: maxActiveSessions,
    });
  },
};
