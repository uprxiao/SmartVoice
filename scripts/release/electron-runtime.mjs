// Selection policy only. Importing this module performs no I/O or subprocesses.
// probe(directory) is injected by build.mjs; unit tests use finite data fixtures.
export function selectElectronRuntime({
  overrideDir = null,
  installedDir,
  expectedVersion,
  platform,
  arch,
  lockfileSha256,
}, probe) {
  const base = {
    scope: 'build-input',
    expectedVersion,
    lockfileSha256,
  };
  function select(directory, source) {
    const info = probe(directory);
    if (!info || typeof info !== 'object' || Array.isArray(info)
        || info.version !== expectedVersion || info.platform !== platform || info.arch !== arch) {
      const actual = info && typeof info === 'object'
        ? `${info.version ?? 'unknown'}/${info.platform ?? 'unknown'}/${info.arch ?? 'unknown'}`
        : 'unknown';
      throw new Error(`Electron ${source} runtime mismatch: expected ${expectedVersion}/${platform}/${arch}, got ${actual}`);
    }
    if (typeof info.executableSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(info.executableSha256)) {
      throw new Error(`Electron ${source} runtime has no verified executable SHA256`);
    }
    return {
      electronDist: directory,
      electron: {
        ...base,
        source,
        verification: 'runtime-probe',
        actualVersion: info.version,
        actualPlatform: info.platform,
        actualArch: info.arch,
        executableSha256: info.executableSha256,
      },
      warning: null,
    };
  }
  // Explicit choice is authoritative: never probe or overwrite it with the
  // installed runtime, and never turn an invalid explicit choice into fallback.
  if (overrideDir) return select(overrideDir, 'override');
  try {
    return select(installedDir, 'installed');
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return {
      electronDist: null,
      electron: {
        ...base,
        source: 'builder-download',
        verification: 'not-probed',
        actualVersion: null,
        actualPlatform: null,
        actualArch: null,
        executableSha256: null,
        fallbackReason: reason,
      },
      warning: `Falling back to electron-builder downloader; selected runtime is unverified: ${reason}`,
    };
  }
}
