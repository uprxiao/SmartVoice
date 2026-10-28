import { API_BASE_URL, apiUrl } from '@shared/config';
import type {
  AuthSnapshot,
  DictionaryPage,
  Insertion,
  LearningEvent,
  Page,
  Session,
  SessionResult,
  Settings,
  Stats,
  Term,
  TokenPair,
  User,
} from '@shared/types';

export class AuthContextChangedError extends Error {
  constructor() { super('账号已变化，旧请求结果已忽略'); }
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly retryable: boolean;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    retryable = false
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryable = retryable;
  }
}

export interface CredentialClearResult {
  persistedCredentialsMayRemain: boolean;
  restartBlocked: boolean;
}

export class LogoutCleanupError extends Error {
  readonly code = 'LOGOUT_CLEANUP_INCOMPLETE';
  constructor(readonly serverRevoked: boolean, readonly restartBlocked: boolean) {
    super(serverRevoked
      ? '已撤销服务器登录，但本机旧凭据未能删除；旧凭据无法继续登录，请检查本机存储权限'
      : restartBlocked
        ? '已清除内存登录并禁止自动恢复，但本机旧凭据未能删除且服务器撤销未确认；请联网核验'
        : '本机旧凭据未能删除，服务器撤销也未确认；请联网核验，不能保证重启后不会恢复旧登录');
  }
}

interface TokenSink {
  set(access: string, refresh: string): void;
  clear(): CredentialClearResult | void;
}

export interface DeviceIdentity {
  installation_id: string;
  name: string;
  platform: 'macos' | 'windows';
  client_version: string;
}

export interface ImportResult {
  import_id: string;
  created: number;
  skipped: number;
  conflicts: number;
  invalid: number;
  dictionary_version: number;
  rows: Array<{ row: number; status: string; term_id?: string; error_code?: string }>;
  replayed: boolean;
}

export class ApiClient {
  private accessToken: string | null = null;
  private refreshToken: string | null = null;
  private accessExpiresAt = 0;
  private refreshing: Promise<boolean> | null = null;
  private authEpoch = 0;
  private readonly activeRequests = new Set<AbortController>();

  constructor(
    private readonly tokens: TokenSink,
    private readonly device: DeviceIdentity,
    private readonly onUnauthorized: () => void
  ) {}

  setTokens(access: string, refresh: string, expiresInSeconds = 900): void {
    if (refresh !== this.refreshToken) this.authEpoch++;
    this.accessToken = access;
    this.refreshToken = refresh;
    const declared = Date.now() + Math.max(0, expiresInSeconds) * 1000;
    let actual = declared;
    try {
      // Scheduling hint only: server remains responsible for signature/claims.
      const payload = JSON.parse(Buffer.from(access.split('.')[1] ?? '', 'base64url').toString());
      if (Number.isSafeInteger(payload.exp) && payload.exp > 0) actual = Math.min(declared, payload.exp * 1000);
    } catch { /* Opaque/malformed tokens are rejected by the server. */ }
    this.accessExpiresAt = actual;
    this.tokens.set(access, refresh);
  }

  clearTokens(): CredentialClearResult | void {
    this.authEpoch++;
    for (const controller of this.activeRequests) controller.abort();
    this.activeRequests.clear();
    this.refreshing = null;
    this.accessToken = null;
    this.refreshToken = null;
    this.accessExpiresAt = 0;
    return this.tokens.clear();
  }

  /** Main must refresh before starting a stream when less than 240s remain. */
  async ensureFreshAccess(): Promise<void> {
    const epoch = this.authEpoch;
    if (!this.accessToken || !this.refreshToken) throw new Error('请先登录');
    if (this.accessExpiresAt - Date.now() > 240_000) return;
    if (!(await this.tryRefresh())) throw new Error('登录已过期，请重新登录');
    this.assertEpoch(epoch);
  }

  hasSession(): boolean {
    return this.refreshToken !== null;
  }

  get accessTokenValue(): string | null {
    return this.accessToken;
  }

  /** Bound both headers and response-body consumption; abort also follows account/observer cancellation. */
  private async fetchBounded(url: string, init: RequestInit, signal?: AbortSignal, accountBound = true): Promise<Response> {
    const epoch = this.authEpoch;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) throw new Error('请求已取消');
    signal?.addEventListener('abort', abort, { once: true });
    if (accountBound) this.activeRequests.add(controller);
    const timer = setTimeout(abort, 15_000);
    try {
      const response = await fetch(url, { ...init, signal: controller.signal, redirect: 'error' });
      const body = response.status === 204 ? null : await response.arrayBuffer();
      if (controller.signal.aborted) throw new Error('请求已取消或超时');
      return new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers });
    } catch (error) {
      if (accountBound) this.assertEpoch(epoch);
      throw error;
    } finally {
      clearTimeout(timer); if (accountBound) this.activeRequests.delete(controller);
      signal?.removeEventListener('abort', abort);
    }
  }

  private async parse<T>(response: Response): Promise<T> {
    if (response.status === 204) return undefined as T;
    const raw = await response.text();
    let body: unknown = null;
    if (raw) {
      try {
        body = JSON.parse(raw);
      } catch {
        // Invalid responses can still contain credentials or dictated text.
        if (process.env.SVI_API_DEBUG) console.warn('[api] invalid json response', response.status);
        throw new ApiError(response.status, 'INTERNAL_ERROR', '服务端返回了无效数据');
      }
    }
    if (!response.ok) {
      const error = (body as { error?: Record<string, unknown> } | null)?.error ?? {};
      throw new ApiError(
        response.status,
        String(error.code ?? 'INTERNAL_ERROR'),
        String(error.message ?? `请求失败（${response.status}）`),
        (error.details as Record<string, unknown>) ?? {},
        Boolean(error.retryable)
      );
    }
    return body as T;
  }

  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    headers: Record<string, string> = {},
    retryAuth = true,
    signal?: AbortSignal
  ): Promise<T> {
    const epoch = this.authEpoch;
    const finalHeaders: Record<string, string> = { ...headers };
    if (body !== undefined) finalHeaders['Content-Type'] = 'application/json';
    if (this.accessToken) finalHeaders['Authorization'] = `Bearer ${this.accessToken}`;
    const response = await this.fetchBounded(apiUrl(path), {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    }, signal);
    this.assertEpoch(epoch);
    if (response.status === 401 && retryAuth && this.refreshToken) {
      if (await this.tryRefresh()) {
        this.assertEpoch(epoch);
        return this.request<T>(method, path, body, headers, false, signal);
      }
    }
    const result = await this.parse<T>(response);
    this.assertEpoch(epoch);
    return result;
  }

  private assertEpoch(epoch: number): void {
    if (epoch !== this.authEpoch) throw new AuthContextChangedError();
  }

  private async tryRefresh(): Promise<boolean> {
    if (this.refreshing) return this.refreshing;
    const epoch = this.authEpoch;
    const refresh = this.refreshToken;
    if (!refresh) return false;
    const operation = (async () => {
      try {
        const response = await this.fetchBounded(apiUrl('/api/auth/refresh'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refresh }),
        });
        if (epoch !== this.authEpoch || this.refreshToken !== refresh) return false;
        if (!response.ok) {
          this.clearTokens();
          this.onUnauthorized();
          return false;
        }
        const pair = (await response.json()) as TokenPair;
        if (epoch !== this.authEpoch || this.refreshToken !== refresh) return false;
        if (typeof pair.access_token !== 'string' || !pair.access_token || pair.refresh_token !== refresh || !Number.isFinite(pair.expires_in) || pair.expires_in <= 0) {
          this.clearTokens(); this.onUnauthorized(); return false;
        }
        this.setTokens(pair.access_token, pair.refresh_token, pair.expires_in);
        return true;
      } catch {
        return false;
      }
    })();
    this.refreshing = operation;
    void operation.finally(() => { if (this.refreshing === operation) this.refreshing = null; });
    return operation;
  }

  register(email: string, password: string): Promise<User> {
    return this.request('POST', '/api/auth/register', { email, password }, {}, false);
  }

  async login(email: string, password: string): Promise<TokenPair> {
    this.clearTokens();
    const epoch = this.authEpoch;
    const pair = await this.request<TokenPair>(
      'POST',
      '/api/auth/login',
      {
        email,
        password,
        client_type: 'desktop',
        device: this.device,
      },
      {},
      false
    );
    this.assertEpoch(epoch);
    this.setTokens(pair.access_token, pair.refresh_token, pair.expires_in);
    return pair;
  }

  async logout(): Promise<void> {
    const token = this.accessToken, refresh = this.refreshToken;
    let cleanup: CredentialClearResult | void = undefined;
    let cleanupFailed = false;
    let revoked = false;
    let revocationError: unknown;
    try {
      cleanup = this.clearTokens();
      cleanupFailed = cleanup?.persistedCredentialsMayRemain === true;
    } catch {
      // Even a nonstandard TokenSink throwing cannot skip remote revocation.
      // clearTokens has already invalidated API memory/generation before sink I/O.
      cleanupFailed = true;
    } finally {
      if (token) {
        try {
          await this.revokeFrozenSession(token, refresh);
          revoked = true;
        } catch (error) { revocationError = error; }
      }
    }
    if (cleanupFailed) throw new LogoutCleanupError(revoked, cleanup?.restartBlocked === true);
    if (revocationError) throw revocationError;
  }

  private async revokeFrozenSession(access: string, refresh: string | null): Promise<void> {
    const revoke = (token: string) => this.fetchBounded(apiUrl('/api/auth/logout'), {
      method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: '{}',
    }, undefined, false);
    let response = await revoke(access);
    if (response.status === 401 && refresh) {
      // An expired access token cannot revoke its sid. Renew the FROZEN old
      // session once solely for revocation; never setTokens or touch a new login.
      const fresh = await this.fetchBounded(apiUrl('/api/auth/refresh'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      }, undefined, false);
      const pair = await this.parse<TokenPair>(fresh);
      if (typeof pair.access_token !== 'string' || !pair.access_token || pair.refresh_token !== refresh) {
        throw new ApiError(502, 'INTERNAL_ERROR', '旧登录的撤销响应无法核验，请联网后重试');
      }
      response = await revoke(pair.access_token);
    }
    await this.parse<void>(response);
    if (response.status !== 204) throw new ApiError(502, 'INTERNAL_ERROR', '服务器未确认退出，请联网后重试');
  }

  me(): Promise<AuthSnapshot> {
    return this.request('GET', '/api/users/me');
  }

  capabilities(): Promise<{
    api_version: string;
    ready: boolean;
    degraded: boolean;
    unavailable_reason: string | null;
    asr: { provider: string; model_id: string; stable_partial: boolean };
    llm: { available: boolean; provider: string; model_id: string };
    limits: { global_sessions: number; active_sessions_per_user: number };
  }> {
    return this.request('GET', '/api/v1/capabilities');
  }

  getSettings(): Promise<Settings> {
    return this.request('GET', '/api/v1/settings');
  }

  patchSettings(payload: Partial<Settings> & { version: number }): Promise<Settings> {
    return this.request('PATCH', '/api/v1/settings', payload);
  }

  createSession(clientRequestId: string, learningEnabled: boolean): Promise<Session> {
    return this.request(
      'POST',
      '/api/v1/sessions',
      {
        client_request_id: clientRequestId,
        mode: 'dictation',
        learning_enabled: learningEnabled,
      },
      { 'Idempotency-Key': clientRequestId }
    );
  }

  getSession(sessionId: string): Promise<Session> {
    return this.request('GET', `/api/v1/sessions/${sessionId}`);
  }

  cancelSession(sessionId: string): Promise<Session> {
    return this.request('POST', `/api/v1/sessions/${sessionId}/cancel`, {});
  }

  retrySession(
    sessionId: string,
    retryKind: 'asr' | 'refine',
    idempotencyKey: string
  ): Promise<Session> {
    return this.request(
      'POST',
      `/api/v1/sessions/${sessionId}/retry`,
      { retry_kind: retryKind },
      { 'Idempotency-Key': idempotencyKey }
    );
  }

  claimInsertion(
    sessionId: string,
    payload: {
      result_id: string;
      insertion_id: string;
      text_hash: string;
      review_confirmed: boolean;
    }
  ): Promise<Insertion> {
    return this.request(
      'POST',
      `/api/v1/sessions/${sessionId}/insertions/claim`,
      payload
    );
  }

  insertionReceipt(
    sessionId: string,
    insertionId: string,
    payload: { status: 'inserted' | 'failed' | 'unverified'; observable: boolean; error_code: string | null }
  ): Promise<Insertion> {
    return this.request(
      'POST',
      `/api/v1/sessions/${sessionId}/insertions/${insertionId}/receipt`,
      payload
    );
  }

  submitCorrection(payload: {
    session_id: string;
    insertion_id: string;
    correction_id: string;
    offset: number;
    before: string;
    after: string;
    left: string;
    right: string;
  }, signal?: AbortSignal): Promise<LearningEvent> {
    return this.request('POST', '/api/v1/corrections', payload, {}, true, signal);
  }

  getLearningEvent(eventId: string): Promise<LearningEvent> {
    return this.request('GET', `/api/v1/learning-events/${eventId}`);
  }

  confirmLearningEvent(eventId: string, key: string): Promise<LearningEvent> {
    return this.request('POST', `/api/v1/learning-events/${eventId}/confirm`, undefined, {
      'Idempotency-Key': key,
    });
  }

  undoLearningEvent(eventId: string, key: string): Promise<LearningEvent> {
    return this.request('POST', `/api/v1/learning-events/${eventId}/undo`, undefined, {
      'Idempotency-Key': key,
    });
  }

  history(cursor?: string | null, limit = 50): Promise<Page<Session>> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set('cursor', cursor);
    return this.request('GET', `/api/v1/history?${params.toString()}`);
  }

  stats(dateFrom: string, dateTo: string): Promise<Stats> {
    const params = new URLSearchParams({ date_from: dateFrom, date_to: dateTo });
    return this.request('GET', `/api/v1/stats/me?${params.toString()}`);
  }

  dictionary(
    params: { q?: string; source?: 'all' | 'manual' | 'auto'; cursor?: string | null; limit?: number }
  ): Promise<DictionaryPage> {
    const query = new URLSearchParams();
    if (params.q) query.set('q', params.q);
    if (params.source) query.set('source', params.source);
    if (params.cursor) query.set('cursor', params.cursor);
    query.set('limit', String(params.limit ?? 50));
    return this.request('GET', `/api/v1/dictionary/terms?${query.toString()}`);
  }

  createTerm(
    term: string,
    aliases: string[],
    idempotencyKey: string
  ): Promise<Term & { replayed: boolean }> {
    return this.request(
      'POST',
      '/api/v1/dictionary/terms',
      { term, aliases, scope: 'default' },
      { 'Idempotency-Key': idempotencyKey }
    );
  }

  updateTerm(
    id: string,
    payload: { version: number; term?: string; aliases?: string[] },
    idempotencyKey: string
  ): Promise<Term & { replayed: boolean }> {
    return this.request(
      'PATCH',
      `/api/v1/dictionary/terms/${id}`,
      payload,
      { 'Idempotency-Key': idempotencyKey }
    );
  }

  deleteTerm(id: string, version: number, idempotencyKey: string): Promise<{
    id: string;
    deleted: boolean;
    version: number;
    dictionary_version: number;
    replayed: boolean;
  }> {
    return this.request(
      'DELETE',
      `/api/v1/dictionary/terms/${id}?version=${version}`,
      undefined,
      { 'Idempotency-Key': idempotencyKey }
    );
  }

  deleteTerms(items: { id: string; version: number }[], idempotencyKey: string): Promise<{
    deleted_ids: string[]; dictionary_version: number; replayed: boolean;
  }> {
    return this.request('POST', '/api/v1/dictionary/terms/batch-delete', { items },
      { 'Idempotency-Key': idempotencyKey });
  }

  async importCsv(filename: string, bytes: Uint8Array, idempotencyKey: string): Promise<ImportResult> {
    const epoch = this.authEpoch;
    // Blob snapshots the bytes once. Authorization retries keep the same payload
    // and idempotency key, and never share a consumed Request/body stream.
    const file = new Blob([bytes as BlobPart], { type: 'text/csv' });
    for (let attempt = 0; attempt < 2; attempt++) {
      this.assertEpoch(epoch);
      const form = new FormData();
      form.append('file', file, filename);
      const response = await this.fetchBounded(apiUrl('/api/v1/dictionary/import'), {
        method: 'POST',
        headers: { Authorization: `Bearer ${this.accessToken ?? ''}`, 'Idempotency-Key': idempotencyKey },
        body: form,
      });
      this.assertEpoch(epoch);
      if (response.status === 401 && attempt === 0 && this.refreshToken) {
        const refreshed = await this.tryRefresh();
        this.assertEpoch(epoch);
        if (refreshed) continue;
      }
      const result = await this.parse<ImportResult>(response);
      this.assertEpoch(epoch);
      return result;
    }
    throw new ApiError(401, 'AUTH_INVALID', '登录已过期，请重新登录');
  }

  get baseUrl(): string {
    return API_BASE_URL;
  }
}
