import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { X509Certificate } from 'node:crypto';
import { createServer, get } from 'node:https';
import { getCACertificates, setDefaultCACertificates } from 'node:tls';
import type { AddressInfo } from 'node:net';
import WebSocket, { WebSocketServer } from 'ws';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { installServiceTrust } from './service-trust';

const roots = getCACertificates('default');
const fingerprints = (certificates: string[]) => certificates.map(pem => new X509Certificate(pem).fingerprint256).sort();
let directory: string;
let ca: string;
let fingerprint: string;
let server: ReturnType<typeof createServer>;
let wss: WebSocketServer;
let port: number;

function request(servername?: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const req = get(`https://127.0.0.1:${port}/health`, { agent: false, ...(servername ? { servername } : {}) }, response => {
      let body = '';
      response.on('data', data => { body += String(data); });
      response.on('end', () => resolve(body));
    });
    req.on('error', reject);
  });
}

beforeAll(async () => {
  directory = mkdtempSync(join(tmpdir(), 'svi-service-tls-'));
  const run = (...args: string[]) => execFileSync('openssl', args, { cwd: directory, stdio: 'pipe' });
  writeFileSync(join(directory, 'root.cnf'), '[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=ca\n[dn]\nCN=SVI Fictional Test Root\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n');
  writeFileSync(join(directory, 'leaf.cnf'), 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n');
  run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-sha256', '-config', 'root.cnf', '-keyout', 'ca.key', '-out', 'ca.crt');
  run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=127.0.0.1', '-keyout', 'leaf.key', '-out', 'leaf.csr');
  run('x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial', '-days', '1', '-sha256', '-extfile', 'leaf.cnf', '-out', 'leaf.crt');
  ca = join(directory, 'ca.crt');
  fingerprint = new X509Certificate(readFileSync(ca)).fingerprint256.replaceAll(':', '').toLowerCase();
  server = createServer({ key: readFileSync(join(directory, 'leaf.key')), cert: readFileSync(join(directory, 'leaf.crt')) }, (_req, res) => res.end('fictional-ok'));
  wss = new WebSocketServer({ server });
  wss.on('connection', socket => socket.send('fictional-wss-ok'));
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  port = (server.address() as AddressInfo).port;
}, 20_000);

afterEach(() => { vi.restoreAllMocks(); setDefaultCACertificates(roots); });
afterAll(async () => {
  for (const client of wss.clients) client.terminate();
  await new Promise<void>(resolve => wss.close(() => resolve()));
  server.closeAllConnections();
  await new Promise<void>(resolve => server.close(() => resolve()));
  rmSync(directory, { recursive: true, force: true });
});

describe('pinned internal CA with normal TLS identity checks', () => {
  it('rejects an untrusted root, then permits native HTTPS, Fetch and WSS after startup trust', async () => {
    await expect(request()).rejects.toThrow(/certificate|verify/i);
    expect(installServiceTrust(ca, fingerprint)).toBe(true);
    // Node normalizes PEM formatting when installing roots; compare identities.
    expect(fingerprints(getCACertificates('default'))).toEqual(expect.arrayContaining(fingerprints(roots)));
    expect(await request()).toBe('fictional-ok');
    expect(await (await fetch(`https://127.0.0.1:${port}/health`)).text()).toBe('fictional-ok');
    const message = await new Promise<string>((resolve, reject) => {
      const ws = new WebSocket(`wss://127.0.0.1:${port}`);
      ws.once('error', reject);
      ws.once('message', data => { resolve(String(data)); ws.close(); });
    });
    expect(message).toBe('fictional-wss-ok');
  });

  it('continues rejecting a wrong server identity even with the trusted root', async () => {
    installServiceTrust(ca, fingerprint);
    await expect(request('different.example.invalid')).rejects.toMatchObject({ code: 'ERR_TLS_CERT_ALTNAME_INVALID' });
  });

  it('rejects a substituted certificate before changing runtime trust', () => {
    expect(() => installServiceTrust(ca, '0'.repeat(64))).toThrow('不匹配');
    expect(fingerprints(getCACertificates('default'))).toEqual(fingerprints(roots));
  });

  it('rejects a leaf masquerading as the configured CA', () => {
    const leaf = join(directory, 'leaf.crt');
    const hash = new X509Certificate(readFileSync(leaf)).fingerprint256.replaceAll(':', '');
    expect(() => installServiceTrust(leaf, hash)).toThrow('不匹配');
  });

  it('rejects an expired CA', () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse(new X509Certificate(readFileSync(ca)).validTo) + 1);
    expect(() => installServiceTrust(ca, fingerprint)).toThrow('有效期');
  });

  it('does not load any private CA for a development build without a pin', () => {
    expect(installServiceTrust('/nonexistent-svi-certificate.crt', '')).toBe(false);
    expect(fingerprints(getCACertificates('default'))).toEqual(fingerprints(roots));
  });
});
