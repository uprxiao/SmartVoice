import { createHash } from 'node:crypto';
import { readFileSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { readJson } from './common.mjs';

export const targets = [
  { platform: 'mac', arch: 'arm64', extension: 'dmg' },
  { platform: 'mac', arch: 'x64', extension: 'dmg' },
  { platform: 'win', arch: 'x64', extension: 'exe' },
];

export function installerName(version, target) {
  return `SmartVoiceInput-${version}-${target.platform}-${target.arch}.${target.extension}`;
}

export function manifestName(target) {
  return `manifest-${target.platform}-${target.arch}.json`;
}

const digest = (file) => createHash('sha256').update(readFileSync(file)).digest('hex');

export function writeManifest(
  dir,
  target,
  version,
  { commit, dirty, signed, verification, electron = null, service = null }
) {
  const file = installerName(version, target);
  const path = join(dir, file);
  const size = statSync(path).size;
  if (size === 0) throw new Error(`Installer is empty: ${file}`);
  const manifest = {
    schemaVersion: 2,
    version,
    commit,
    platform: target.platform,
    arch: target.arch,
    signed,
    dirty,
    verification,
    // Selected build input only, not a packaged app launch/signature claim.
    // Additive schema-2 evidence: old/missing/null remains unknown.
    electron,
    service,
    files: [{ file, size, sha256: digest(path) }],
  };
  writeFileSync(
    join(dir, manifestName(target)),
    `${JSON.stringify(manifest, null, 2)}\n`
  );
  return manifest;
}

export function verifyArtifacts(
  dir,
  version,
  commit,
  { requireMacSigning = true } = {}
) {
  const files = [];
  const checksums = [];
  for (const target of targets) {
    const manifestPath = join(dir, manifestName(target));
    const manifest = readJson(manifestPath);
    const expected = installerName(version, target);
    if (
      manifest.schemaVersion !== 2 ||
      manifest.version !== version ||
      manifest.commit !== commit ||
      manifest.platform !== target.platform ||
      manifest.arch !== target.arch ||
      manifest.dirty !== false ||
      typeof manifest.verification !== 'string' ||
      !manifest.verification ||
      !Array.isArray(manifest.files) ||
      manifest.files.length !== 1 ||
      manifest.files[0]?.file !== expected
    ) {
      throw new Error(`Artifact provenance mismatch: ${manifestName(target)}`);
    }
    if (requireMacSigning && target.platform === 'mac' && manifest.signed !== true) {
      throw new Error('Stable macOS releases require signing and notarization');
    }
    const entry = manifest.files[0];
    const file = join(dir, entry.file);
    const size = statSync(file).size;
    if (size === 0 || size !== entry.size || digest(file) !== entry.sha256) {
      throw new Error(`Artifact checksum mismatch: ${entry.file}`);
    }
    files.push(file);
    checksums.push(`${entry.sha256}  ${entry.file}`);
    files.push(manifestPath);
  }
  return { files, checksums: `${checksums.join('\n')}\n` };
}
