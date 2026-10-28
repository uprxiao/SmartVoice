import { afterEach, describe, expect, it, vi } from 'vitest';
import { existsSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }));
import { TokenStore, type TokenCipher, type TokenStorageIO } from './token-store';

const directories: string[] = [];
const directory = () => { const path = mkdtempSync(join(tmpdir(), 'svi-token-regression-')); directories.push(path); return path; };
const cipher = (available: boolean): TokenCipher => ({
  isEncryptionAvailable: () => available,
  encryptString: value => Buffer.from(`encrypted:${Buffer.from(value).toString('base64')}`),
  decryptString: bytes => {
    const value = bytes.toString();
    if (!value.startsWith('encrypted:')) throw new Error('invalid ciphertext');
    return Buffer.from(value.slice('encrypted:'.length), 'base64').toString();
  },
});
afterEach(() => { for (const path of directories.splice(0)) rmSync(path, { recursive: true, force: true }); });

describe('credential storage failure boundaries (AUTH-09/15)', () => {
  it('keeps unavailable encryption credentials in memory and requires login in a new process', () => {
    const path = directory(); const store = new TokenStore(cipher(false), path);
    store.set('access-canary', 'refresh-canary');
    expect(store.load()).toEqual({ access: 'access-canary', refresh: 'refresh-canary' });
    expect(existsSync(join(path, 'auth.bin'))).toBe(false);
    expect(new TokenStore(cipher(false), path).load()).toBeNull();
  });
  it('never recovers legacy plaintext when encryption is unavailable', () => {
    const path = directory(); writeFileSync(join(path, 'auth.bin'), JSON.stringify({ version: 1, access: 'unsafe', refresh: 'unsafe' }));
    expect(new TokenStore(cipher(false), path).load()).toBeNull();
    expect(existsSync(join(path, 'auth.bin'))).toBe(false);
  });
  it('never falls back to plaintext after decryption fails', () => {
    const path = directory(); writeFileSync(join(path, 'auth.bin'), JSON.stringify({ version: 1, access: 'unsafe', refresh: 'unsafe' }));
    expect(new TokenStore(cipher(true), path).load()).toBeNull();
  });
  it('persists ciphertext with private permissions and decrypts it on restoration', () => {
    const path = directory(); new TokenStore(cipher(true), path).set('access-canary', 'refresh-canary');
    const raw = readFileSync(join(path, 'auth.bin')).toString();
    expect(raw).not.toContain('access-canary'); expect(raw).not.toContain('refresh-canary');
    expect(statSync(join(path, 'auth.bin')).mode & 0o777).toBe(0o600);
    expect(new TokenStore(cipher(true), path).load()).toEqual({ access: 'access-canary', refresh: 'refresh-canary' });
  });
  it('encryption exceptions never cause plaintext persistence', () => {
    const path = directory(); const broken = cipher(true); broken.encryptString = () => { throw new Error('keychain unavailable'); };
    const store = new TokenStore(broken, path); store.set('access-canary', 'refresh-canary');
    expect(store.load()?.refresh).toBe('refresh-canary'); expect(existsSync(join(path, 'auth.bin'))).toBe(false);
  });
  it('does not restore or transmit credentials across configured API deployments', () => {
    const path = directory();
    new TokenStore(cipher(true), path, 'http://127.0.0.1:17863').set('old-access', 'old-refresh');
    expect(new TokenStore(cipher(true), path, 'https://voice.example.test').load()).toBeNull();
    expect(new TokenStore(cipher(true), path, 'http://127.0.0.1:17863/').load()?.access).toBe('old-access');
  });
  it('logout clears both in-memory and persisted credentials', () => {
    const path = directory(); const store = new TokenStore(cipher(true), path); store.set('a', 'r'); store.clear();
    expect(store.load()).toBeNull(); expect(new TokenStore(cipher(true), path).load()).toBeNull();
  });
});


const fileIO = (): TokenStorageIO => ({
  exists: existsSync, read: path => readFileSync(path),
  write: (path, value, options) => { writeFileSync(path, value, options); },
  rename: renameSync, remove: path => { rmSync(path, { force: true }); },
});
const denied = () => Object.assign(new Error('fault injected'), { code: 'EACCES' });

describe('filesystem failures and durable restore barrier', () => {
  it.each(['write', 'rename'] as const)('%s failure preserves usable memory credentials without claiming persistence', operation => {
    const path = directory(), io = fileIO();
    if (operation === 'write') io.write = () => { throw denied(); };
    else io.rename = () => { throw denied(); };
    const store = new TokenStore(cipher(true), path, undefined, io);
    expect(() => store.set('access-canary', 'refresh-canary')).not.toThrow();
    expect(store.load()).toEqual({ access: 'access-canary', refresh: 'refresh-canary' });
    expect(store.canPersist).toBe(false); expect(store.isMemoryOnly).toBe(true);
    expect(store.persistenceWarning).toBeTruthy();
    expect(new TokenStore(cipher(true), path).load()).toBeNull();
  });

  it('failed temp cleanup is observable and never converts ciphertext to plaintext', () => {
    const path = directory(), io = fileIO();
    let leftover = '';
    io.rename = from => { leftover = from; throw denied(); };
    const remove = io.remove;
    io.remove = file => { if (file.endsWith('.tmp')) throw denied(); remove(file); };
    const store = new TokenStore(cipher(true), path, undefined, io);
    expect(() => store.set('access-canary', 'refresh-canary')).not.toThrow();
    expect(store.isMemoryOnly).toBe(true); expect(store.persistenceWarning).toContain('临时文件');
    expect(readFileSync(leftover, 'utf8')).not.toContain('refresh-canary');
    expect(new TokenStore(cipher(true), path).load()).toBeNull();
  });

  it('a failed credential deletion blocks restoration in this and a fresh process', () => {
    const path = directory(), io = fileIO();
    const store = new TokenStore(cipher(true), path, undefined, io);
    store.set('old-access', 'old-refresh');
    const remove = io.remove;
    io.remove = file => { if (file === join(path, 'auth.bin')) throw denied(); remove(file); };
    expect(store.clear()).toEqual({ persistedCredentialsMayRemain: true, restartBlocked: true });
    expect(store.load()).toBeNull(); expect(store.isMemoryOnly).toBe(true);
    expect(existsSync(join(path, 'auth.bin'))).toBe(true);
    expect(new TokenStore(cipher(true), path).load()).toBeNull();
    const barrier = readFileSync(join(path, 'auth.bin.blocked'), 'utf8');
    expect(barrier).not.toContain('old-access'); expect(barrier).not.toContain('old-refresh');
  });

  it('checks a restore barrier before decrypting any stored token', () => {
    const path = directory();
    new TokenStore(cipher(true), path).set('old-a', 'old-r');
    writeFileSync(join(path, 'auth.bin.blocked'), '{}');
    const crypto = cipher(true); crypto.decryptString = vi.fn(crypto.decryptString);
    expect(new TokenStore(crypto, path).load()).toBeNull();
    expect(crypto.decryptString).not.toHaveBeenCalled();
  });

  it('does not lift a barrier until a new encrypted login atomically succeeds', () => {
    const path = directory(), io = fileIO();
    new TokenStore(cipher(true), path).set('old-a', 'old-r');
    writeFileSync(join(path, 'auth.bin.blocked'), '{}');
    io.rename = () => { throw denied(); };
    const store = new TokenStore(cipher(true), path, undefined, io);
    store.set('new-a', 'new-r');
    expect(existsSync(join(path, 'auth.bin.blocked'))).toBe(true);
    expect(new TokenStore(cipher(true), path).load()).toBeNull();
    io.rename = renameSync;
    store.set('new-a', 'new-r');
    expect(store.canPersist).toBe(true); expect(existsSync(join(path, 'auth.bin.blocked'))).toBe(false);
    expect(new TokenStore(cipher(true), path).load()).toEqual({ access: 'new-a', refresh: 'new-r' });
  });

  it('reports the unfixable local boundary when both deletion and barrier writing fail', () => {
    const path = directory(), io = fileIO();
    new TokenStore(cipher(true), path).set('old-a', 'old-r');
    io.remove = () => { throw denied(); }; io.write = () => { throw denied(); };
    const store = new TokenStore(cipher(true), path, undefined, io);
    expect(store.clear()).toEqual({ persistedCredentialsMayRemain: true, restartBlocked: false });
    expect(store.load()).toBeNull(); expect(store.persistenceWarning).toContain('联网');
    expect(existsSync(join(path, 'auth.bin'))).toBe(true);
    // A fresh process has no durable marker in this fault mode. API logout must
    // prove server revocation or report failure; do not assert safe disk cleanup.
  });

  it('isolates a failed-deletion barrier by configured API deployment', () => {
    const path = directory(), io = fileIO();
    const a = new TokenStore(cipher(true), path, 'https://a.example.test', io);
    const b = new TokenStore(cipher(true), path, 'https://b.example.test');
    a.set('a', 'ra'); b.set('b', 'rb');
    io.remove = () => { throw denied(); };
    expect(a.clear().restartBlocked).toBe(true);
    expect(new TokenStore(cipher(true), path, 'https://a.example.test').load()).toBeNull();
    expect(new TokenStore(cipher(true), path, 'https://b.example.test').load()?.access).toBe('b');
  });
});
