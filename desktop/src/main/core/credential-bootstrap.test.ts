import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createHash } from 'node:crypto';
import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, win32 } from 'node:path';

const port = vi.hoisted(() => ({
  spawn: vi.fn(),
  app: {
    isPackaged: false,
    getPath: vi.fn(), getAppPath: vi.fn(), whenReady: vi.fn(), quit: vi.fn(), exit: vi.fn(),
  },
  cipher: { isEncryptionAvailable: vi.fn(), encryptString: vi.fn(), decryptString: vi.fn() },
}));
vi.mock('electron', () => ({ app: port.app, safeStorage: port.cipher }));
vi.mock('node:child_process', () => ({ spawnSync: port.spawn }));
import { completeCredentialBootstrap, credentialCipher, isCredentialBootstrap, prepareCredentialStorage } from '../../../native/windows/credential-bootstrap';
import { TokenStore } from './token-store';

const USER = 'C:\\Fictional profiles\\SmartVoiceInput';
const SESSION = USER;
const identity = (value: string) => createHash('sha256').update(win32.normalize(value).toLowerCase()).digest('hex');
const ack = (user = USER, session = SESSION) => 'SVI_CREDENTIAL_STORAGE_V1 ' + JSON.stringify({
  schema: 'svi-credential-storage-v1', user_data: identity(user), session_data: identity(session),
}) + '\n';
const exited = (stdout = ack()) => ({ status: 0, signal: null, stdout: Buffer.from(stdout) });

beforeEach(() => {
  vi.clearAllMocks();
  port.app.isPackaged = false;
  port.app.getPath.mockImplementation((name: string) => name === 'sessionData' ? SESSION : USER);
  port.app.getAppPath.mockReturnValue('C:\\Fictional source\\desktop');
  port.app.whenReady.mockResolvedValue(undefined);
  port.cipher.isEncryptionAvailable.mockReturnValue(true);
  port.cipher.encryptString.mockReturnValue(Buffer.from('encrypted fictional canary'));
  port.spawn.mockReturnValue(exited());
});
afterEach(() => { vi.unstubAllEnvs(); });

describe('Windows credential initialization before Main profile creation', () => {
  it('waits for the exact same profile child to exit normally without reading the parent cipher', () => {
    vi.stubEnv('SVI_JWT_SECRET', 'fictional-must-not-inherit');
    vi.stubEnv('NODE_OPTIONS', '--trace-warnings');
    vi.stubEnv('ELECTRON_RUN_AS_NODE', '1');
    vi.stubEnv('ELECTRON_RENDERER_URL', 'http://fictional.invalid');
    expect(prepareCredentialStorage('win32')).toBe(true);
    expect(port.spawn).toHaveBeenCalledExactlyOnceWith(process.execPath, [
      'C:\\Fictional source\\desktop', '--svi-credential-storage-bootstrap', `--user-data-dir=${USER}`,
    ], expect.objectContaining({ windowsHide: true, timeout: 8000, killSignal: 'SIGKILL', maxBuffer: 8192, stdio: ['ignore', 'pipe', 'pipe'] }));
    const options = port.spawn.mock.calls[0][2];
    expect(options.env.SVI_USER_DATA).toBe(USER);
    for (const key of ['SVI_JWT_SECRET', 'NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE', 'ELECTRON_RENDERER_URL']) expect(options.env[key]).toBeUndefined();
    expect(port.app.whenReady).not.toHaveBeenCalled();
    expect(port.cipher.encryptString).not.toHaveBeenCalled();
    expect(port.cipher.decryptString).not.toHaveBeenCalled();
  });

  it('uses the packaged executable directly without treating app.asar as a script', () => {
    port.app.isPackaged = true;
    expect(prepareCredentialStorage('win32')).toBe(true);
    expect(port.spawn.mock.calls[0][1]).toEqual(['--svi-credential-storage-bootstrap', `--user-data-dir=${USER}`]);
    expect(port.app.getAppPath).not.toHaveBeenCalled();
  });

  it.each([
    ['timeout', { ...exited(), error: Object.assign(new Error('fixture timeout'), { code: 'ETIMEDOUT' }), status: null }],
    ['signal', { ...exited(), signal: 'SIGKILL' }],
    ['nonzero exit after an acknowledgement', { ...exited(), status: 2 }],
    ['missing acknowledgement', exited('')],
    ['ambiguous acknowledgement', exited(ack() + ack())],
    ['another user profile', exited(ack('C:\\Other fictional profile'))],
    ['another session profile', exited(ack(USER, 'C:\\Other fictional session'))],
    ['malformed acknowledgement', exited('SVI_CREDENTIAL_STORAGE_V1 {bad\n')],
  ])('fails closed on %s and never persists credentials with that key', (_name, result) => {
    port.spawn.mockReturnValue(result);
    const ready = prepareCredentialStorage('win32');
    expect(ready).toBe(false);
    const directory = mkdtempSync(join(tmpdir(), 'svi-credential-failure-'));
    try {
      const tokens = new TokenStore(credentialCipher(ready, 'win32'), directory, 'http://127.0.0.1:17863');
      tokens.set('fictional-access', 'fictional-refresh');
      expect(tokens.isMemoryOnly).toBe(true);
      expect(tokens.load()).toEqual({ access: 'fictional-access', refresh: 'fictional-refresh' });
      expect(readdirSync(directory)).toEqual([]);
      expect(port.cipher.encryptString).not.toHaveBeenCalled();
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });

  it('contains process-launch exceptions without a fallback key or retry', () => {
    port.spawn.mockImplementation(() => { throw new Error('fixture process launch failure'); });
    expect(prepareCredentialStorage('win32')).toBe(false);
    expect(port.spawn).toHaveBeenCalledTimes(1);
    expect(credentialCipher(false, 'win32')!.isEncryptionAvailable()).toBe(false);
    expect(() => credentialCipher(false, 'win32')!.encryptString('fictional')).toThrow('initialization failed');
  });

  it('does not launch children, touch Keychain or replace the Mac cipher', () => {
    expect(prepareCredentialStorage('darwin')).toBe(true);
    expect(credentialCipher(false, 'darwin')).toBeUndefined();
    expect(port.spawn).not.toHaveBeenCalled();
    expect(port.app.getPath).not.toHaveBeenCalled();
    expect(port.cipher.isEncryptionAvailable).not.toHaveBeenCalled();
  });

  it('recognizes only the fixed Windows internal mode', () => {
    expect(isCredentialBootstrap('win32', ['app', '--svi-credential-storage-bootstrap'])).toBe(true);
    expect(isCredentialBootstrap('darwin', ['app', '--svi-credential-storage-bootstrap'])).toBe(false);
    expect(isCredentialBootstrap('win32', ['app', '--svi-credential-storage-bootstrap=arbitrary'])).toBe(false);
  });
});

describe('internal child completes an ordinary exit before a parent may persist', () => {
  it('waits for ready and flushes its acknowledgement before normal quit', async () => {
    let ready!: () => void, finish!: (error?: Error | null) => void;
    port.app.whenReady.mockReturnValue(new Promise<void>(resolve => { ready = resolve; }));
    let line = '';
    const completion = completeCredentialBootstrap((value, done) => { line = value; finish = done; });
    expect(port.cipher.encryptString).not.toHaveBeenCalled();
    ready(); await completion;
    expect(line).toBe(ack());
    expect(port.app.quit).not.toHaveBeenCalled();
    finish();
    expect(port.app.quit).toHaveBeenCalledTimes(1);
    expect(port.app.exit).not.toHaveBeenCalled();
  });

  it.each(['unavailable', 'encrypt-error', 'output-error'])('does not claim completion on %s', async fault => {
    if (fault === 'unavailable') port.cipher.isEncryptionAvailable.mockReturnValue(false);
    if (fault === 'encrypt-error') port.cipher.encryptString.mockImplementation(() => { throw new Error('fixture'); });
    let line = '';
    await completeCredentialBootstrap((value, done) => { line = value; done(new Error('fixture output failed')); });
    expect(port.app.exit).toHaveBeenCalledExactlyOnceWith(2);
    expect(port.app.quit).not.toHaveBeenCalled();
    if (fault !== 'output-error') expect(line).toBe('');
  });
});
