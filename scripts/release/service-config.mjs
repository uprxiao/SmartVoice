import { readFileSync } from 'node:fs';
import { resolve, relative, join, isAbsolute } from 'node:path';
import { X509Certificate } from 'node:crypto';
import { isIP } from 'node:net';
import { readJson, root } from './common.mjs';

/** One committed service target feeds every native installer build. */
export function serviceBuildConfig(config = readJson(join(root, 'release.config.json')), repository = root) {
  const service = config.service;
  if (!service || typeof service.apiBaseUrl !== 'string') throw new Error('Release service.apiBaseUrl is required');
  const apiBaseUrl = service.apiBaseUrl.replace(/\/+$/, '');
  const url = new URL(apiBaseUrl);
  if (url.protocol !== 'https:' || url.origin !== apiBaseUrl || url.username || url.password
    || ['localhost', '127.0.0.1', '[::1]', '0.0.0.0'].includes(url.hostname)
    || (!isIP(url.hostname) && !url.hostname.includes('.'))) {
    throw new Error('Installer service must use an explicit HTTPS origin reachable by recipients');
  }
  if (typeof service.caCertificate !== 'string' || !/^[a-f0-9]{64}$/i.test(service.caSha256 ?? '')) {
    throw new Error('Release service public CA path and SHA256 fingerprint are required');
  }
  const certificateFile = resolve(repository, service.caCertificate);
  const within = relative(resolve(repository), certificateFile);
  if (within.startsWith('..') || isAbsolute(within)) throw new Error('Service CA must be inside the repository');
  const pem = readFileSync(certificateFile, 'utf8');
  if (!/^\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*$/.test(pem)) {
    throw new Error('Service CA file must contain exactly one public certificate');
  }
  const ca = new X509Certificate(pem);
  const caSha256 = ca.fingerprint256.replaceAll(':', '').toLowerCase();
  if (!ca.ca || !ca.verify(ca.publicKey) || caSha256 !== service.caSha256.toLowerCase()
    || Date.now() < Date.parse(ca.validFrom) || Date.now() >= Date.parse(ca.validTo)) {
    throw new Error('Service CA fingerprint, CA identity or validity mismatch');
  }
  return { apiBaseUrl, caSha256, certificateFile };
}

export function serviceBuildEnvironment(service, inherited = process.env) {
  if (inherited.VITE_API_BASE_URL && inherited.VITE_API_BASE_URL.replace(/\/+$/, '') !== service.apiBaseUrl) {
    throw new Error('Installer VITE_API_BASE_URL must match release.config.json');
  }
  return { ...inherited, VITE_API_BASE_URL: service.apiBaseUrl, VITE_SERVICE_CA_SHA256: service.caSha256 };
}

export function verifyServiceBuildOutput(desktopDirectory, service) {
  const main = readFileSync(join(desktopDirectory, 'out/main/index.js'), 'utf8');
  const html = readFileSync(join(desktopDirectory, 'out/renderer/index.html'), 'utf8');
  if (!main.includes(service.apiBaseUrl) || !main.includes(service.caSha256)
    || !html.includes(service.apiBaseUrl) || !html.includes(service.apiBaseUrl.replace(/^https:/, 'wss:'))) {
    throw new Error('Built Main/Renderer service address, WSS policy or CA pin is missing');
  }
}
