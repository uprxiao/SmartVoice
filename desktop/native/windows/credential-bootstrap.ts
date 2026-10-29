import { app, safeStorage } from 'electron';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { win32 } from 'node:path';
import type { TokenCipher } from '../../src/main/core/token-store';

const FLAG = '--svi-credential-storage-bootstrap';
const PREFIX = 'SVI_CREDENTIAL_STORAGE_V1 ';
const CANARY = 'SmartVoiceInput credential storage initialization';
let profileProof: Buffer | null = null;
let profileConfirmed = false;
const ESSENTIAL_ENV = new Set([
  'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
  'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH',
]);

function profileIdentity(path: string): string {
  return createHash('sha256').update(win32.normalize(path).toLowerCase()).digest('hex');
}

export function isCredentialBootstrap(platform = process.platform, argv = process.argv): boolean {
  return platform === 'win32' && argv.includes(FLAG);
}

/** Internal child mode: no account, network, native input, RPC, tray or window. */
export async function completeCredentialBootstrap(
  write: (line: string, done: (error?: Error | null) => void) => void = (line, done) => { process.stdout.write(line, done); }
): Promise<void> {
  try {
    await app.whenReady();
    if (!safeStorage.isEncryptionAvailable()) throw new Error('unavailable');
    // Exercise the public API, then let Electron commit its own configuration
    // during normal shutdown. No key or private preference format is accessed.
    const proof = safeStorage.encryptString(CANARY);
    const result = {
      schema: 'svi-credential-storage-v1',
      user_data: profileIdentity(app.getPath('userData')),
      session_data: profileIdentity(app.getPath('sessionData')),
      encrypted_canary: proof.toString('base64'),
    };
    write(PREFIX + JSON.stringify(result) + '\n', error => {
      if (error) app.exit(2);
      else app.quit();
    });
  } catch {
    app.exit(2);
  }
}

/**
 * Call synchronously from the owning Main's initial script, before app.ready.
 * Electron initializes Local State after that script returns. An independently
 * completed normal exit therefore makes its key available before Main reads it.
 * The owning Main retains its PID and single-instance lock throughout.
 */
export function prepareCredentialStorage(platform = process.platform): boolean {
  if (platform !== 'win32') return true;
  profileProof?.fill(0);
  profileProof = null;
  profileConfirmed = false;
  try {
    const userData = app.getPath('userData');
    const sessionData = app.getPath('sessionData');
    const environment: NodeJS.ProcessEnv = {};
    for (const [name, value] of Object.entries(process.env)) {
      if (ESSENTIAL_ENV.has(name.toUpperCase())) environment[name] = value;
    }
    environment.SVI_USER_DATA = userData;
    const args = [
      ...(app.isPackaged ? [] : [app.getAppPath()]),
      FLAG,
      `--user-data-dir=${userData}`,
    ];
    const child = spawnSync(process.execPath, args, {
      env: environment,
      windowsHide: true,
      timeout: 8000,
      killSignal: 'SIGKILL',
      maxBuffer: 8192,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (child.error || child.signal || child.status !== 0) return false;
    const lines = child.stdout.toString('utf8').split(/\r?\n/).filter(line => line.startsWith(PREFIX));
    if (lines.length !== 1) return false;
    const result = JSON.parse(lines[0].slice(PREFIX.length));
    if (result.schema !== 'svi-credential-storage-v1'
      || result.user_data !== profileIdentity(userData)
      || result.session_data !== profileIdentity(sessionData)
      || typeof result.encrypted_canary !== 'string'
      || result.encrypted_canary.length === 0 || result.encrypted_canary.length > 4096) return false;
    const proof = Buffer.from(result.encrypted_canary, 'base64');
    if (proof.toString('base64') !== result.encrypted_canary) return false;
    profileProof = proof;
    return true;
  } catch {
    return false;
  }
}

/** A failed bootstrap can still sign in in memory; never persist with an unsealed key. */
export function credentialCipher(ready: boolean, platform = process.platform): TokenCipher | undefined {
  if (platform !== 'win32') return undefined;
  if (ready && !profileConfirmed && profileProof) {
    try {
      // Exit 0 cannot prove a configuration write succeeded. Decrypt the other
      // process's non-account challenge with this freshly initialized parent.
      profileConfirmed = app.isReady() && safeStorage.isEncryptionAvailable()
        && safeStorage.decryptString(profileProof) === CANARY;
    } catch { profileConfirmed = false; }
    finally { profileProof.fill(0); profileProof = null; }
  }
  if (ready && profileConfirmed) return undefined;
  return {
    isEncryptionAvailable: () => false,
    encryptString: () => { throw new Error('Credential storage initialization failed'); },
    decryptString: () => { throw new Error('Credential storage initialization failed'); },
  };
}
