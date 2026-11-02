import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './api-client';
import type { ServiceEndpoint } from './service-endpoint';

const endpoint: ServiceEndpoint = { origin: 'http://127.0.0.1:43219', storageIdentity: 'fictional-stable-service',
  headers: { 'X-SmartVoice-Simulation': 'fictional-private-boot-nonce' } };
const device = { installation_id: '11111111-1111-4111-8111-111111111111', name: 'fictional', platform: 'windows' as const, client_version: '0.0.2' };
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });
afterEach(() => { vi.unstubAllGlobals(); });

describe('single owned service endpoint', () => {
  it('uses the same endpoint and private headers for requests, refresh, multipart import and frozen logout', async () => {
    const seen: Array<{ url: string; headers: Headers; body: unknown }> = [];
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      seen.push({ url, headers: new Headers(init.headers), body: init.body });
      if (url.endsWith('/refresh')) return json({ access_token: 'fresh', refresh_token: 'refresh', expires_in: 900 });
      if (url.endsWith('/logout')) return new Response(null, { status: 204 });
      return json({ ok: true });
    });
    const client = new ApiClient({ set() {}, clear() {} }, device, () => {}, endpoint);
    client.setTokens('access', 'refresh', 0);
    await client.ensureFreshAccess(); await client.me();
    await client.importCsv('fictional.csv', new TextEncoder().encode('term,aliases\nReact,react'), 'fictional-import');
    await client.logout();
    expect(seen.map(call => call.url)).toEqual([
      'http://127.0.0.1:43219/api/auth/refresh', 'http://127.0.0.1:43219/api/users/me',
      'http://127.0.0.1:43219/api/v1/dictionary/import', 'http://127.0.0.1:43219/api/auth/logout',
    ]);
    expect(seen.every(call => call.headers.get('x-smartvoice-simulation') === 'fictional-private-boot-nonce')).toBe(true);
    expect(seen[2].body).toBeInstanceOf(FormData);
    expect(seen[3].headers.get('authorization')).toBe('Bearer fresh');
  });

  it('derives WS authentication and stable storage identity independently of a random port', () => {
    const client = new ApiClient({ set() {}, clear() {} }, device, () => {}, endpoint);
    client.setTokens('fictional-token', 'refresh');
    expect(client.audioSocket('/ws/v1/sessions/id/audio')).toEqual({
      url: 'ws://127.0.0.1:43219/ws/v1/sessions/id/audio',
      headers: { 'X-SmartVoice-Simulation': 'fictional-private-boot-nonce', Authorization: 'Bearer fictional-token' },
    });
    const restart = new ApiClient({ set() {}, clear() {} }, device, () => {}, { ...endpoint, origin: 'http://127.0.0.1:49173' });
    expect(restart.storageIdentity).toBe(client.storageIdentity);
    expect(restart.baseUrl).not.toBe(client.baseUrl);
  });
});
