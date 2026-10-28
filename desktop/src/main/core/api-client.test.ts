import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, AuthContextChangedError, LogoutCleanupError } from './api-client';
const device = { installation_id: '4e984426-3c53-49bc-8b32-30b176a59c38', name: 'test', platform: 'macos' as const, client_version: '0.0.1' };
const sink = () => ({ set: vi.fn(), clear: vi.fn() });
const deferred = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe('account generation and stream token refresh boundaries (AUTH-16/19)', () => {
  it.each([239, 240])('refreshes before opening a stream at %i seconds remaining', async seconds => {
    const store = sink(); const client = new ApiClient(store, device, vi.fn()); client.setTokens('a', 'r', seconds);
    const fetch = vi.fn(async () => json({ access_token: 'new-a', refresh_token: 'r', expires_in: 900 })); vi.stubGlobal('fetch', fetch);
    await client.ensureFreshAccess(); expect(fetch).toHaveBeenCalledTimes(1); expect(client.accessTokenValue).toBe('new-a');
  });
  it('does not refresh with 241 seconds remaining', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('a', 'r', 241); const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    await client.ensureFreshAccess(); expect(fetch).not.toHaveBeenCalled();
  });
  it('restored JWT expiry limits the synthetic 900-second default', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-18T00:00:00Z'));
    const payload = Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + 30 })).toString('base64url');
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens(`x.${payload}.x`, 'r');
    const fetch = vi.fn(async () => json({ access_token: 'fresh', refresh_token: 'r', expires_in: 900 })); vi.stubGlobal('fetch', fetch);
    await client.ensureFreshAccess(); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('an old account response cannot populate the new account', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('a1', 'r1');
    const pending = deferred<Response>(); vi.stubGlobal('fetch', vi.fn(() => pending.promise));
    const request = client.me(); client.setTokens('a2', 'r2'); pending.resolve(json({ user: { id: 'old' } }));
    await expect(request).rejects.toBeInstanceOf(AuthContextChangedError); expect(client.accessTokenValue).toBe('a2');
  });
  it('logout clears credentials before waiting for the network', async () => {
    const store = sink(); const client = new ApiClient(store, device, vi.fn()); client.setTokens('a', 'r');
    const pending = deferred<Response>(); vi.stubGlobal('fetch', vi.fn(() => pending.promise));
    const request = client.logout(); expect(client.hasSession()).toBe(false); expect(store.clear).toHaveBeenCalled();
    pending.resolve(new Response(null, { status: 204 })); await request;
  });
  it('late refresh cannot resurrect a logged-out account', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('a', 'r', 239);
    const pending = deferred<Response>(); vi.stubGlobal('fetch', vi.fn(() => pending.promise)); const refreshing = client.ensureFreshAccess();
    client.clearTokens(); pending.resolve(json({ access_token: 'late-a', refresh_token: 'r', expires_in: 900 }));
    await expect(refreshing).rejects.toThrow('重新登录'); expect(client.accessTokenValue).toBeNull(); expect(client.hasSession()).toBe(false);
  });
  it('late login cannot overwrite a subsequent login', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); const first = deferred<Response>(); const second = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise));
    const loginA = client.login('a@example.test', 'DemoPass123'); const loginB = client.login('b@example.test', 'DemoPass123');
    second.resolve(json({ access_token: 'b', refresh_token: 'rb', expires_in: 900 })); await loginB;
    first.resolve(json({ access_token: 'a', refresh_token: 'ra', expires_in: 900 })); await expect(loginA).rejects.toBeInstanceOf(AuthContextChangedError);
    expect(client.accessTokenValue).toBe('b');
  });
});


describe('logout revokes frozen credentials even when persistence fails', () => {
  it('attempts remote logout even when the sink throws after memory has been cleared', async () => {
    const store = sink(), client = new ApiClient(store, device, vi.fn()); client.setTokens('old-a', 'old-r');
    store.clear.mockImplementation(() => { throw new Error('disk denied'); });
    const fetch = vi.fn(async (_url: string, _init: RequestInit) => new Response(null, { status: 204 })); vi.stubGlobal('fetch', fetch);
    await expect(client.logout()).rejects.toMatchObject({ code: 'LOGOUT_CLEANUP_INCOMPLETE', serverRevoked: true });
    expect(client.hasSession()).toBe(false); expect(client.accessTokenValue).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[1]?.headers).toMatchObject({ Authorization: 'Bearer old-a' });
  });

  it.each([true, false])('reports unconfirmed remote revocation with restartBlocked=%s', async restartBlocked => {
    const store = { set: vi.fn(), clear: vi.fn(() => ({ persistedCredentialsMayRemain: true, restartBlocked })) };
    const client = new ApiClient(store, device, vi.fn()); client.setTokens('old-a', 'old-r');
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline'); }));
    await expect(client.logout()).rejects.toMatchObject({ code: 'LOGOUT_CLEANUP_INCOMPLETE', serverRevoked: false, restartBlocked });
    expect(client.hasSession()).toBe(false);
  });

  it('does not claim server revocation when there was no frozen token to revoke', async () => {
    const client = new ApiClient({ set: vi.fn(), clear: () => ({ persistedCredentialsMayRemain: true, restartBlocked: false }) }, device, vi.fn());
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    await expect(client.logout()).rejects.toBeInstanceOf(LogoutCleanupError);
    expect(fetch).not.toHaveBeenCalled();
  });

  it('renews an expired old access solely to revoke that old sid, never installing renewed tokens', async () => {
    const store = sink(), client = new ApiClient(store, device, vi.fn()); client.setTokens('expired-a', 'old-r');
    const fetch = vi.fn().mockResolvedValueOnce(json({ error: { code: 'AUTH_INVALID' } }, 401))
      .mockResolvedValueOnce(json({ access_token: 'renewed-old-a', refresh_token: 'old-r', expires_in: 900 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetch); await client.logout();
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ refresh_token: 'old-r' });
    expect(fetch.mock.calls[2][1].headers.Authorization).toBe('Bearer renewed-old-a');
    expect(store.set).toHaveBeenCalledTimes(1); expect(client.hasSession()).toBe(false);
  });

  it('late logout neither cancels nor clears a new login', async () => {
    const store = sink(), client = new ApiClient(store, device, vi.fn()); client.setTokens('old-a', 'old-r');
    const pending = deferred<Response>();
    const fetch = vi.fn().mockImplementationOnce(() => pending.promise)
      .mockResolvedValueOnce(json({ access_token: 'new-a', refresh_token: 'new-r', expires_in: 900 }));
    vi.stubGlobal('fetch', fetch);
    const loggingOut = client.logout();
    await client.login('new@example.test', 'DemoPass123');
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(false);
    pending.resolve(new Response(null, { status: 204 })); await loggingOut;
    expect(client.accessTokenValue).toBe('new-a'); expect(client.hasSession()).toBe(true);
    expect(store.set).toHaveBeenLastCalledWith('new-a', 'new-r');
  });
});

const imported = { import_id: 'example-import', created: 1, skipped: 0, conflicts: 0, invalid: 0,
  dictionary_version: 2, rows: [], replayed: false };
describe('CSV authorization retry boundaries', () => {
  it('performs one serial refresh and replays the same file and idempotency key', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('old-a', 'r');
    const fetch = vi.fn().mockResolvedValueOnce(json({ error: { code: 'AUTH_INVALID' } }, 401))
      .mockResolvedValueOnce(json({ access_token: 'new-a', refresh_token: 'r', expires_in: 900 }))
      .mockResolvedValueOnce(json(imported));
    vi.stubGlobal('fetch', fetch);
    const bytes = Buffer.from('term,aliases\nSQLite,SQL lite\n');
    await expect(client.importCsv('terms.csv', bytes, 'same-key')).resolves.toEqual(imported);
    expect(fetch).toHaveBeenCalledTimes(3);
    const first = fetch.mock.calls[0][1], second = fetch.mock.calls[2][1];
    expect(first.headers.Authorization).toBe('Bearer old-a'); expect(second.headers.Authorization).toBe('Bearer new-a');
    expect(first.headers['Idempotency-Key']).toBe('same-key'); expect(second.headers['Idempotency-Key']).toBe('same-key');
    expect(await first.body.get('file').text()).toBe(bytes.toString());
    expect(await second.body.get('file').text()).toBe(bytes.toString());
    expect(second.body).not.toBe(first.body);
  });

  it('does not start a second refresh or third upload after the retried 401', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('a', 'r');
    const fetch = vi.fn().mockResolvedValueOnce(json({ error: { code: 'AUTH_INVALID' } }, 401))
      .mockResolvedValueOnce(json({ access_token: 'new-a', refresh_token: 'r', expires_in: 900 }))
      .mockResolvedValueOnce(json({ error: { code: 'AUTH_INVALID' } }, 401));
    vi.stubGlobal('fetch', fetch);
    await expect(client.importCsv('terms.csv', new Uint8Array([65]), 'same-key')).rejects.toMatchObject({ status: 401 });
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('never retransmits the old file under a new account after refresh completes late', async () => {
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('old-a', 'old-r');
    const refresh = deferred<Response>(), began = deferred<void>();
    const fetch = vi.fn().mockResolvedValueOnce(json({ error: { code: 'AUTH_INVALID' } }, 401))
      .mockImplementationOnce(() => { began.resolve(); return refresh.promise; });
    vi.stubGlobal('fetch', fetch);
    const pending = client.importCsv('old.csv', new Uint8Array([65]), 'old-key');
    const rejected = expect(pending).rejects.toBeInstanceOf(AuthContextChangedError);
    await began.promise;
    client.setTokens('new-a', 'new-r');
    refresh.resolve(json({ access_token: 'late-old-a', refresh_token: 'old-r', expires_in: 900 }));
    await rejected;
    expect(fetch).toHaveBeenCalledTimes(2); expect(client.accessTokenValue).toBe('new-a');
  });

  it('keeps the 15 second bound while consuming the CSV response body', async () => {
    vi.useFakeTimers();
    const client = new ApiClient(sink(), device, vi.fn()); client.setTokens('a', 'r');
    const bodyStarted = deferred<void>();
    const fetch = vi.fn(async (_url: string, init: RequestInit) => ({
      status: 200, statusText: 'OK', headers: new Headers(),
      arrayBuffer: () => new Promise<ArrayBuffer>((_resolve, reject) => {
        bodyStarted.resolve();
        init.signal!.addEventListener('abort', () => reject(new Error('body aborted')), { once: true });
      }),
    } as Response));
    vi.stubGlobal('fetch', fetch);
    const pending = client.importCsv('terms.csv', new Uint8Array([65]), 'same-key');
    const rejected = expect(pending).rejects.toThrow('body aborted');
    await bodyStarted.promise; await vi.advanceTimersByTimeAsync(15_000); await rejected;
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
