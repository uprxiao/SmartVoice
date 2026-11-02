import { SERVICE_CA_SHA256 } from '@shared/config';
import { resolveAppResource } from '../app/window';
import { configuredServiceEndpoint } from '../core/service-endpoint';
import { installServiceTrust } from '../core/service-trust';

export async function startProductService(_directory: string) {
  installServiceTrust(resolveAppResource('service-ca.crt'), SERVICE_CA_SHA256);
  return { endpoint: configuredServiceEndpoint(), close: async () => {} };
}
