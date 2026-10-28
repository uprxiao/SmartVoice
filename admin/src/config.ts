/** Deployed Admin uses its own HTTPS origin; Vite proxies local development. */
const browserLocation = typeof globalThis.location === 'undefined' ? undefined : globalThis.location;
export const API_BASE_URL = browserLocation && ['http:', 'https:'].includes(browserLocation.protocol)
  ? browserLocation.origin : 'http://127.0.0.1:17863';

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}
