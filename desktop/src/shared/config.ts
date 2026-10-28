/**
 * The desktop client talks to the self-hosted backend. The address is defined
 * once here; a deployment can override it with VITE_API_BASE_URL at build time
 * without touching business code. HTTPS deployments automatically use WSS.
 */
const buildEnv =
  (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env ??
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ??
  {};

export const API_BASE_URL = (buildEnv.VITE_API_BASE_URL ?? 'http://127.0.0.1:17863').replace(
  /\/+$/,
  ''
);
export const WS_BASE_URL = API_BASE_URL.replace(/^https:/i, 'wss:').replace(/^http:/i, 'ws:');
/** DER SHA256 of the public internal CA, injected by the installer build. */
export const SERVICE_CA_SHA256 = buildEnv.VITE_SERVICE_CA_SHA256 ?? '';
export const APP_NAME = 'SmartVoiceInput';

export const AUDIO = {
  encoding: 'pcm_s16le',
  sampleRate: 16000,
  channels: 1,
  chunkSamples: 4000,
  chunkBytes: 8000,
  /** V1 default/fallback (180 s); a session's own limit comes from the server. */
  maxSamples: 2_880_000,
  maxDurationSeconds: 180,
  /** Largest limit any server may grant (V2 cloud ASR, 360 s). */
  ceilingSamples: 5_760_000,
} as const;

/**
 * Recording limit of one session (V2 §4.1): the creation response's
 * `max_record_seconds`, else the 180 s default for older servers.
 */
export function sessionMaxSamples(maxRecordSeconds: unknown): number {
  if (typeof maxRecordSeconds === 'number' && Number.isInteger(maxRecordSeconds)
      && maxRecordSeconds >= 1 && maxRecordSeconds * AUDIO.sampleRate <= AUDIO.ceilingSamples) {
    return maxRecordSeconds * AUDIO.sampleRate;
  }
  return AUDIO.maxSamples;
}

/** Validate lazily: a bad deployment address must not prevent the UI from loading. */
export function validateServiceBase(address: string = API_BASE_URL): void {
  let base: URL;
  try {
    base = new URL(address);
  } catch {
    throw new Error('服务地址无效，请配置 HTTP 或 HTTPS 服务根地址');
  }
  if (!['http:', 'https:'].includes(base.protocol)
    || base.username || base.password || /[?#]/.test(address)) {
    throw new Error('服务地址无效，请使用不含凭据、查询参数或片段的 HTTP/HTTPS 服务根地址');
  }
  if (base.protocol === 'http:' && !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname)) {
    throw new Error('远程服务必须使用 HTTPS，音频连接必须使用 WSS');
  }
}

export function apiUrl(path: string): string {
  validateServiceBase();
  return `${API_BASE_URL}${path}`;
}

export function wsUrl(path: string): string {
  validateServiceBase();
  return `${WS_BASE_URL}${path}`;
}
