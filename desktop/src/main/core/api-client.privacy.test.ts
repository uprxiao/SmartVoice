import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './api-client';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('malformed API response privacy', () => {
  it.each([200, 503])('keeps the response body out of debug logs for HTTP %i', async status => {
    const sensitive = ['private-password', 'private-access-token', 'private-refresh-token', 'private-dictated-text'];
    const response = `malformed:${sensitive.join('|')}`;
    vi.stubEnv('SVI_API_DEBUG', '1');
    vi.stubGlobal('fetch', vi.fn(async () => new Response(response, { status })));
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const client = new ApiClient({ set() {}, clear() {} }, {
      installation_id: 'privacy-owned-installation', name: 'Synthetic privacy test',
      platform: 'macos', client_version: '0.0.1',
    }, () => {});

    await expect(client.request('GET', '/privacy-malformed')).rejects.toMatchObject({
      status, code: 'INTERNAL_ERROR', message: '服务端返回了无效数据',
    });
    expect(warning).toHaveBeenCalledTimes(1);
    expect(warning.mock.calls[0]).toContain(status);
    const logged = warning.mock.calls.flat().map(String).join('\n');
    for (const value of sensitive) expect(logged).not.toContain(value);
    expect(logged).not.toContain(response);
  });
});
