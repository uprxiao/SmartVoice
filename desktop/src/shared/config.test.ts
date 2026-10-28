import { describe, expect, it } from 'vitest';
import { API_BASE_URL, WS_BASE_URL, apiUrl, validateServiceBase, wsUrl } from './config';

// Pure address validation: this table does not mutate Vite's build-time env.
describe('Desktop service-root transport policy (NFR05)', () => {
  it.each([
    'http://127.0.0.1:17863',
    'http://localhost:17863',
    'http://[::1]:17863',
    'http://localhost:17863/prefix',
    'https://api.example.test',
    'https://127.0.0.1:17863',
    'https://localhost:17863',
    'https://[::1]:17863',
    'HTTPS://API.EXAMPLE.TEST/prefix',
    'HTTP://LOCALHOST:17863/prefix',
  ])('allows an explicitly safe service root: %s', address => {
    expect(() => validateServiceBase(address)).not.toThrow();
  });

  it.each([
    'http://192.0.2.1:17863',
    'http://api.example.test',
    'http://127.0.0.1.evil.example.test',
    'http://localhost.evil.example.test',
    'http://127.0.0.2:17863',
    'http://[::ffff:127.0.0.1]:17863',
    'ftp://api.example.test',
    'file:///tmp/service',
    'ws://localhost:17863',
    'wss://api.example.test',
    '/relative',
    '//localhost:17863',
    'https://user:synthetic-password@api.example.test',
    'http://synthetic-user@localhost:17863',
    'https://api.example.test/prefix?mode=test',
    'https://api.example.test/prefix?',
    'https://api.example.test/prefix#section',
    'https://api.example.test/prefix#',
  ])('rejects an unsafe or ambiguous service root: %s', address => {
    expect(() => validateServiceBase(address)).toThrow(/服务地址|HTTPS/);
  });

  it('keeps a business query and prefix as a concatenated endpoint, outside root validation', () => {
    const endpoint = '/api/v1/sessions?limit=20&cursor=a%2Fb';
    expect(apiUrl(endpoint)).toBe(`${API_BASE_URL}${endpoint}`);
    expect(new URL(apiUrl(endpoint)).search).toBe('?limit=20&cursor=a%2Fb');
  });

  it('uses the configured root for WS, preserving endpoint query and secure protocol', () => {
    const endpoint = '/api/v1/sessions/test/stream?probe=synthetic';
    const actual = wsUrl(endpoint);
    expect(actual).toBe(`${WS_BASE_URL}${endpoint}`);
    expect(new URL(actual).protocol).toBe(new URL(API_BASE_URL).protocol === 'https:' ? 'wss:' : 'ws:');
    expect(new URL(actual).search).toBe('?probe=synthetic');
  });
});
