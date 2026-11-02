import { API_BASE_URL, validateServiceBase } from '@shared/config';

export interface ServiceEndpoint {
  readonly origin: string;
  readonly storageIdentity: string;
  readonly headers?: Readonly<Record<string, string>>;
}

export function configuredServiceEndpoint(): ServiceEndpoint {
  return Object.freeze({ origin: API_BASE_URL, storageIdentity: new URL(API_BASE_URL).href });
}

export function endpointUrl(endpoint: ServiceEndpoint, path: string, websocket = false): string {
  validateServiceBase(endpoint.origin);
  const origin = endpoint.origin.replace(/\/+$/, '');
  return `${websocket ? origin.replace(/^http/i, 'ws') : origin}${path}`;
}
