import { readFileSync } from 'node:fs';
import { X509Certificate } from 'node:crypto';
import { getCACertificates, setDefaultCACertificates } from 'node:tls';

/** Run in Main before HTTP/WS connections; leaves the OS trust store untouched. */
export function installServiceTrust(certificateFile: string, fingerprint: string): boolean {
  if (!fingerprint) return false; // Local development uses normal runtime trust.
  if (!/^[a-f0-9]{64}$/i.test(fingerprint)) throw new Error('内网服务证书指纹无效，请重新安装客户端');
  const pem = readFileSync(certificateFile, 'utf8');
  if (Buffer.byteLength(pem) > 64_000
    || !/^\s*-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*$/.test(pem)) {
    throw new Error('内网服务证书文件无效，请重新安装客户端');
  }
  const certificate = new X509Certificate(pem);
  const actual = certificate.fingerprint256.replaceAll(':', '').toLowerCase();
  if (actual !== fingerprint.toLowerCase() || !certificate.ca || !certificate.verify(certificate.publicKey)) {
    throw new Error('内网服务证书不匹配，请重新安装客户端');
  }
  const now = Date.now();
  if (now < Date.parse(certificate.validFrom) || now >= Date.parse(certificate.validTo)) {
    throw new Error('内网服务证书不在有效期内，请联系管理员');
  }
  if (typeof setDefaultCACertificates !== 'function') throw new Error('客户端运行时不支持内网证书，请更新客户端');
  // Preserve standard roots. HTTPS and WSS still validate their peer's IP/name,
  // chain and lifetime; only this Main thread gains the pinned private root.
  setDefaultCACertificates([...getCACertificates('default'), pem]);
  return true;
}
