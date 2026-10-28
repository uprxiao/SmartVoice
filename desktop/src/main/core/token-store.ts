import { app, safeStorage } from 'electron';
import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { CredentialClearResult, DeviceIdentity } from './api-client';
import { hostname } from 'node:os';
import { randomUUID, createHash } from 'node:crypto';

interface StoredTokens {
  version: 1;
  access: string;
  refresh: string;
}

export interface TokenCipher {
  isEncryptionAvailable(): boolean;
  encryptString(value: string): Buffer;
  decryptString(value: Buffer): string;
}

/** Injectable only for fault tests; never accepts a credential serialization fallback. */
export interface TokenStorageIO {
  exists(path: string): boolean;
  read(path: string): Buffer;
  write(path: string, value: Buffer | string, options: { mode: number; flag: 'wx' }): void;
  rename(from: string, to: string): void;
  remove(path: string): void;
}
const storageIO: TokenStorageIO = {
  exists: existsSync,
  read: path => readFileSync(path),
  write: (path, value, options) => { writeFileSync(path, value, options); },
  rename: renameSync,
  remove: path => { rmSync(path, { force: true }); },
};

export class TokenStore {
  private readonly authFile: string;
  private readonly deviceFile: string;
  private readonly blockedFile: string;
  private encryptionFailed = false;
  private ioFailed = false;
  private restoreBlocked = false;
  private warning: string | null = null;
  private memoryTokens: { access: string; refresh: string } | null = null;

  constructor(
    private readonly cipher: TokenCipher = safeStorage,
    directory: string = app.getPath('userData'),
    serverIdentity?: string,
    private readonly io: TokenStorageIO = storageIO
  ) {
    this.authFile = join(directory, serverIdentity ? `auth-${createHash('sha256').update(new URL(serverIdentity).href).digest('hex')}.bin` : 'auth.bin');
    this.deviceFile = join(directory, 'device.json');
    this.blockedFile = `${this.authFile}.blocked`;
  }

  private encryptionAvailable(): boolean {
    try {
      return this.cipher.isEncryptionAvailable();
    } catch {
      return false;
    }
  }

  get canPersist(): boolean { return !this.encryptionFailed && !this.ioFailed && this.encryptionAvailable(); }
  get isMemoryOnly(): boolean { return !this.canPersist; }
  get persistenceWarning(): string | null { return this.warning; }

  private blockDiskRestore(): boolean {
    try {
      if (this.io.exists(this.blockedFile)) return true;
      // Metadata only. A failed deletion must not silently restore old ciphertext
      // in a new process. The marker is namespaced exactly like the auth file.
      this.io.write(this.blockedFile, JSON.stringify({ version: 1, restore_disabled: true }), { mode: 0o600, flag: 'wx' });
      return true;
    } catch (error) {
      return (error as NodeJS.ErrnoException).code === 'EEXIST';
    }
  }

  private clearPersisted(): CredentialClearResult {
    this.restoreBlocked = true;
    try {
      this.io.remove(this.authFile);
      return { persistedCredentialsMayRemain: false, restartBlocked: true };
    } catch {
      this.ioFailed = true;
      const restartBlocked = this.blockDiskRestore();
      this.warning = restartBlocked
        ? '本机旧凭据未能删除，已禁止自动恢复；仅本次内存登录'
        : '本机旧凭据和恢复禁用标记均无法处理；退出必须联网确认服务器撤销，不能保证重启后自动清理';
      // If the directory is unwritable AND revocation cannot reach the server,
      // no local-only mechanism can durably remember logout. Caller must report
      // this limitation, never claim the previous credentials were removed.
      return { persistedCredentialsMayRemain: true, restartBlocked };
    }
  }

  load(): { access: string; refresh: string } | null {
    // Explicit credentials from this process can continue in memory even when an
    // older disk record is blocked. Never consult that record as a fallback.
    if (this.memoryTokens) return { ...this.memoryTokens };
    if (this.restoreBlocked) return null;
    try {
      if (this.io.exists(this.blockedFile)) {
        this.restoreBlocked = true; this.ioFailed = true;
        this.warning = '本机旧凭据已禁止自动恢复，请重新登录';
        return null;
      }
      // A fresh profile has nothing to decrypt. Avoid a synchronous Keychain
      // query (and possible OS interaction) before its first login screen.
      if (!this.io.exists(this.authFile)) return null;
      if (!this.encryptionAvailable()) {
        this.clearPersisted();
        return null;
      }
      const parsed = JSON.parse(this.cipher.decryptString(this.io.read(this.authFile))) as StoredTokens;
      if (parsed.version !== 1 || typeof parsed.access !== 'string' || !parsed.access ||
          typeof parsed.refresh !== 'string' || !parsed.refresh) throw new Error('invalid stored credential');
      this.memoryTokens = { access: parsed.access, refresh: parsed.refresh };
      return { ...this.memoryTokens };
    } catch {
      this.ioFailed = true;
      this.warning = '安全凭据无法读取，仅支持本次内存登录';
      this.clearPersisted();
      return null;
    }
  }

  set(access: string, refresh: string): void {
    this.memoryTokens = { access, refresh };
    if (!this.encryptionAvailable()) {
      this.clearPersisted();
      return;
    }
    let bytes: Buffer;
    try {
      bytes = this.cipher.encryptString(JSON.stringify({ version: 1, access, refresh } satisfies StoredTokens));
    } catch {
      this.encryptionFailed = true;
      this.warning = '安全加密不可用，仅本次内存登录';
      this.clearPersisted();
      return;
    }
    const temporary = `${this.authFile}.${randomUUID()}.tmp`;
    let saved = false;
    try {
      this.io.write(temporary, bytes, { mode: 0o600, flag: 'wx' });
      this.io.rename(temporary, this.authFile);
      saved = true;
      // Remove a previous barrier only after this explicit login's new encrypted
      // record has atomically replaced the old file. No network callback does it.
      this.io.remove(this.blockedFile);
      this.restoreBlocked = false;
      this.encryptionFailed = false; this.ioFailed = false; this.warning = null;
    } catch {
      this.ioFailed = true;
      this.warning = '安全凭据无法持久保存，仅本次内存登录';
      this.clearPersisted();
    } finally {
      // A rename consumes the temp file; otherwise clean only that exact UUID
      // path. Cleanup errors remain observable and never reject a valid login.
      if (!saved) {
        try { this.io.remove(temporary); }
        catch {
          this.ioFailed = true;
          this.warning = `${this.warning ?? '仅本次内存登录'}；安全凭据临时文件无法清理，残留内容仍为密文`;
        }
      }
    }
  }

  clear(): CredentialClearResult {
    this.memoryTokens = null;
    return this.clearPersisted();
  }

  device(version: string): DeviceIdentity {
    try {
      if (existsSync(this.deviceFile)) {
        const parsed = JSON.parse(readFileSync(this.deviceFile, 'utf8')) as DeviceIdentity;
        if (parsed.installation_id) {
          return {
            ...parsed,
            platform: process.platform === 'darwin' ? 'macos' : 'windows',
            client_version: version,
          };
        }
      }
    } catch {
      // Fall through and create a fresh identity.
    }
    const identity: DeviceIdentity = {
      installation_id: randomUUID(),
      name: hostname().slice(0, 64) || 'Mac',
      platform: process.platform === 'darwin' ? 'macos' : 'windows',
      client_version: version,
    };
    writeFileSync(this.deviceFile, JSON.stringify(identity, null, 2), { mode: 0o600 });
    return identity;
  }
}
