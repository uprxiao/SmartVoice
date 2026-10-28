import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './api-client';

const device = { installation_id: '4e984426-3c53-49bc-8b32-30b176a59c38', name: 'test', platform: 'macos' as const, client_version: '0.0.1' };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const newClient = () => new ApiClient({ set() {}, clear() {} }, device, () => {});
type Seen = { path: string; init: RequestInit };

// A terminal fetch double: no DNS/socket or actual redirect is exercised here.
// The owned TLS 307/308 tests remain the separate transport-effect evidence.
function trap(replies: Array<Response | Error>): Seen[] {
  const seen: Seen[] = [];
  vi.stubGlobal('fetch', async (url: string | URL | Request, init?: RequestInit) => {
    if (!init) throw new Error('MISSING_REQUEST_INIT');
    seen.push({ path: new URL(String(url)).pathname, init });
    const reply = replies.shift();
    if (!reply) throw new Error('UNEXPECTED_EXTRA_FETCH');
    if (reply instanceof Error) throw reply;
    return reply;
  });
  return seen;
}
function expectRedirectBlocked(seen: Seen[]): void {
  expect(seen.length).toBeGreaterThan(0);
  for (const { init } of seen) {
    expect(init.redirect).toBe('error');
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.signal?.aborted).toBe(false);
  }
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('ApiClient central redirect policy (NFR05)', () => {
  it('rejects a login transport failure unchanged and does not retry or save credentials', async () => {
    const client = newClient(), failure = new TypeError('synthetic transport rejection');
    const seen = trap([failure]);
    await expect(client.login('fixture@example.test', 'SyntheticPass123')).rejects.toBe(failure);
    expect(seen.map(call => call.path)).toEqual(['/api/auth/login']);
    expectRedirectBlocked(seen);
    expect(client.hasSession()).toBe(false);
  });

  it('keeps redirects disabled across one authorized 401, refresh, and identical idempotent replay', async () => {
    const client = newClient(); client.setTokens('synthetic-old-access', 'synthetic-refresh');
    const seen = trap([
      json({ error: { code: 'AUTH_INVALID' } }, 401),
      json({ access_token: 'synthetic-new-access', refresh_token: 'synthetic-refresh', expires_in: 900 }),
      json({ id: 'synthetic-session', replayed: true }),
    ]);
    await expect(client.retrySession('synthetic-session', 'refine', 'synthetic-same-key')).resolves.toMatchObject({ replayed: true });
    expect(seen.map(call => call.path)).toEqual(['/api/v1/sessions/synthetic-session/retry', '/api/auth/refresh', '/api/v1/sessions/synthetic-session/retry']);
    expectRedirectBlocked(seen);
    const first = seen[0]!.init, replay = seen[2]!.init;
    expect(replay.method).toBe(first.method);
    expect(replay.body).toBe(first.body);
    expect(JSON.parse(String(first.body))).toEqual({ retry_kind: 'refine' });
    expect(new Headers(replay.headers).get('Idempotency-Key')).toBe('synthetic-same-key');
    expect(new Headers(first.headers).get('Idempotency-Key')).toBe('synthetic-same-key');
    expect(new Headers(first.headers).get('Authorization')).not.toBe(new Headers(replay.headers).get('Authorization'));
    expect(client.accessTokenValue).toBe('synthetic-new-access');
  });

  it('applies the same central policy to the multipart CSV path', async () => {
    const client = newClient(); client.setTokens('synthetic-access', 'synthetic-refresh');
    const seen = trap([json({ import_id: 'synthetic-import', created: 1 })]);
    await expect(client.importCsv('terms.csv', new TextEncoder().encode('term,aliases\nSQLite,SQL lite\n'), 'synthetic-csv-key')).resolves.toMatchObject({ created: 1 });
    expect(seen.map(call => call.path)).toEqual(['/api/v1/dictionary/import']);
    expectRedirectBlocked(seen);
    expect(seen[0]!.init.body).toBeInstanceOf(FormData);
    expect(new Headers(seen[0]!.init.headers).get('Idempotency-Key')).toBe('synthetic-csv-key');
  });

  it('also protects frozen logout credentials through its independent refresh/revoke path', async () => {
    const client = newClient(); client.setTokens('synthetic-old-access', 'synthetic-refresh');
    const seen = trap([
      json({ error: { code: 'AUTH_INVALID' } }, 401),
      json({ access_token: 'synthetic-renewed-access', refresh_token: 'synthetic-refresh', expires_in: 900 }),
      new Response(null, { status: 204 }),
    ]);
    await client.logout();
    expect(seen.map(call => call.path)).toEqual(['/api/auth/logout', '/api/auth/refresh', '/api/auth/logout']);
    expectRedirectBlocked(seen);
    expect(client.hasSession()).toBe(false);
    expect(client.accessTokenValue).toBeNull();
  });
});
