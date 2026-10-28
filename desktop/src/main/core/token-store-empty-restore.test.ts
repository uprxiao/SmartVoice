import { describe, expect, it, vi } from 'vitest';
import { createHash } from 'node:crypto';
import { join } from 'node:path';

vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }));
import { TokenStore, type TokenCipher, type TokenStorageIO } from './token-store';
import { AuthService } from './auth-service';
import type { ApiClient } from './api-client';

// Synthetic boundary objects only. Actual TokenStore/AuthService production
// methods are imported; this test never invokes Keychain, fs or a live API.
const directory = '/synthetic-owned-profile';
const address = 'https://voice.example.test';
const pathsFor = (serverIdentity?: string) => {
  const name = serverIdentity
    ? `auth-${createHash('sha256').update(new URL(serverIdentity).href).digest('hex')}.bin`
    : 'auth.bin';
  const auth = join(directory, name);
  return { auth, blocked: `${auth}.blocked` };
};

function storage() {
  const files = new Map<string, Buffer>();
  const io = {
    exists: vi.fn((path: string) => files.has(path)),
    read: vi.fn((path: string) => {
      const bytes = files.get(path);
      if (!bytes) throw new Error('synthetic missing file');
      return Buffer.from(bytes);
    }),
    write: vi.fn((path: string, value: Buffer | string, _options: { mode: number; flag: 'wx' }) => {
      if (files.has(path)) throw Object.assign(new Error('synthetic duplicate'), { code: 'EEXIST' });
      files.set(path, typeof value === 'string' ? Buffer.from(value) : Buffer.from(value));
    }),
    rename: vi.fn((from: string, to: string) => {
      const bytes = files.get(from);
      if (!bytes) throw new Error('synthetic missing source');
      files.set(to, bytes); files.delete(from);
    }),
    remove: vi.fn((path: string) => { files.delete(path); }),
  } satisfies TokenStorageIO;
  return { files, io };
}

function failingCipher(kind: 'unavailable' | 'throws' = 'throws') {
  return {
    isEncryptionAvailable: vi.fn(() => {
      if (kind === 'throws') throw new Error('synthetic Keychain probe failure');
      return false;
    }),
    encryptString: vi.fn((_value: string): Buffer => { throw new Error('unexpected encryption'); }),
    decryptString: vi.fn((_bytes: Buffer): string => { throw new Error('unexpected decryption'); }),
  } satisfies TokenCipher;
}

describe('fresh profile restore must not enter the Keychain boundary', () => {
  it.each([
    ['legacy pathname', undefined],
    ['API namespace', address],
  ] as const)('returns null without crypto or filesystem mutation for %s', (_label, serverIdentity) => {
    const { files, io } = storage();
    const crypto = failingCipher();
    const store = new TokenStore(crypto, directory, serverIdentity, io);

    expect(store.load()).toBeNull();

    expect(crypto.isEncryptionAvailable).not.toHaveBeenCalled();
    expect(crypto.encryptString).not.toHaveBeenCalled();
    expect(crypto.decryptString).not.toHaveBeenCalled();
    expect(io.read).not.toHaveBeenCalled();
    expect(io.write).not.toHaveBeenCalled();
    expect(io.rename).not.toHaveBeenCalled();
    expect(io.remove).not.toHaveBeenCalled();
    expect([...files.keys()]).toEqual([]);
    expect(store.persistenceWarning).toBeNull();
    // Do not read canPersist/isMemoryOnly: their explicit capability inquiry
    // remains permitted and is outside the absent-credential load contract.
  });

  it.each(['unavailable', 'throws'] as const)('keeps existing ciphertext fail-closed when crypto is %s', kind => {
    const { files, io } = storage();
    const path = pathsFor(address);
    files.set(path.auth, Buffer.from('synthetic-ciphertext-not-a-token'));
    const crypto = failingCipher(kind);
    const store = new TokenStore(crypto, directory, address, io);

    expect(store.load()).toBeNull();

    expect(crypto.isEncryptionAvailable).toHaveBeenCalledTimes(1);
    expect(crypto.encryptString).not.toHaveBeenCalled();
    expect(crypto.decryptString).not.toHaveBeenCalled();
    expect(io.read).not.toHaveBeenCalled();
    expect(io.remove).toHaveBeenCalledExactlyOnceWith(path.auth);
    expect(files.has(path.auth)).toBe(false);
    // A confirmed deletion does not require a restoration barrier or a new
    // file. Failure-to-delete/barrier faults remain covered in token-store.test.
    expect(files.has(path.blocked)).toBe(false);
    expect(io.write).not.toHaveBeenCalled();
    expect(io.rename).not.toHaveBeenCalled();
  });

  it('honors an existing restore barrier even when its ciphertext is absent', () => {
    const { files, io } = storage();
    const path = pathsFor(address);
    const barrier = Buffer.from('{"version":1,"restore_disabled":true}');
    files.set(path.blocked, barrier);
    const crypto = failingCipher();
    const store = new TokenStore(crypto, directory, address, io);

    expect(store.load()).toBeNull();

    expect(crypto.isEncryptionAvailable).not.toHaveBeenCalled();
    expect(crypto.decryptString).not.toHaveBeenCalled();
    expect(store.persistenceWarning).toContain('禁止自动恢复');
    expect(files.get(path.blocked)).toEqual(barrier);
    expect(io.read).not.toHaveBeenCalled();
    expect(io.remove).not.toHaveBeenCalled();
    expect(io.write).not.toHaveBeenCalled();
    expect(io.rename).not.toHaveBeenCalled();
  });

  it('actual AuthService.restore publishes signed-out state without a fresh-profile capability probe', async () => {
    const { files, io } = storage();
    const crypto = failingCipher();
    const tokens = new TokenStore(crypto, directory, address, io);
    const api = { me: vi.fn(), setTokens: vi.fn(), clearTokens: vi.fn() };
    const changed = vi.fn();
    const auth = new AuthService(api as unknown as ApiClient, tokens, changed);

    await expect(auth.restore()).resolves.toBeNull();

    expect(auth.current).toBeNull();
    expect(changed).toHaveBeenCalledTimes(1);
    expect(api.me).not.toHaveBeenCalled();
    expect(api.setTokens).not.toHaveBeenCalled();
    expect(api.clearTokens).not.toHaveBeenCalled();
    expect(crypto.isEncryptionAvailable).not.toHaveBeenCalled();
    expect(crypto.encryptString).not.toHaveBeenCalled();
    expect(crypto.decryptString).not.toHaveBeenCalled();
    expect(io.read).not.toHaveBeenCalled();
    expect(io.write).not.toHaveBeenCalled();
    expect(io.rename).not.toHaveBeenCalled();
    expect(io.remove).not.toHaveBeenCalled();
    expect([...files.keys()]).toEqual([]);
  });
});
