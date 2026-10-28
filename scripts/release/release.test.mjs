import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import {
  installerName,
  manifestName,
  targets,
  verifyArtifacts,
  writeManifest,
} from './artifacts.mjs';
import { root } from './common.mjs';
import { selectElectronRuntime } from './electron-runtime.mjs';
import { releaseNotes, verifyTagCommit } from './metadata.mjs';
import { releasePolicy } from './policy.mjs';
import { nextTag, nextVersion, parseVersion } from './version.mjs';
// Keep native identity regression in the existing test:release / CI entry.
import './mac-identity.checks.mjs';
import './service-config.test.mjs';

test('custom version carry and numeric tag ordering', () => {
  for (const [before, after] of [
    ['0.0.1', '0.0.2'],
    ['0.0.8', '0.0.9'],
    ['0.0.9', '0.1.0'],
    ['0.9.9', '0.10.0'],
    ['0.99.8', '0.99.9'],
    ['0.99.9', '1.0.0'],
    ['1.99.9', '2.0.0'],
  ]) {
    assert.equal(nextVersion(before), after);
  }
  assert.equal(nextTag([]), 'v0.0.1');
  assert.equal(nextTag(['v0.9.9', 'v0.10.0', 'v0.1.0', 'v9.0.0-beta.1']), 'v0.10.1');
  assert.equal(nextTag(['v1.99.9', 'v0.99.9', 'v1.9.9']), 'v2.0.0');
});

test('rejects versions outside the agreed ranges and malformed tags', () => {
  for (const version of [
    '0.0.10',
    '0.100.0',
    '01.0.1',
    '0.01.0',
    '-1.0.0',
    '0.0',
    '0.0.1-beta.1',
    '0.0.1\n',
  ]) {
    assert.throws(() => parseVersion(version), /version|Version/i);
  }
  assert.throws(() => nextTag(['v0.0.10']), /Version/);
});

test('metadata requires matching version and one nonempty changelog section', () => {
  const changelog =
    '# Changelog\r\n\r\n## 0.0.2\r\n\r\nNew release.\r\n\r\n## 0.0.1\r\nOld release.\r\n';
  assert.equal(releaseNotes('v0.0.2', '0.0.2', changelog), 'New release.\n');
  assert.throws(() => releaseNotes('v0.0.2', '0.0.1', changelog), /does not match/);
  assert.throws(() => releaseNotes('0.0.2', '0.0.2', changelog), /start with v/);
  assert.throws(() => releaseNotes('v0.0.3', '0.0.3', changelog), /exactly one/);
  assert.throws(
    () => releaseNotes('v0.0.2', '0.0.2', '## 0.0.2\n\n## 0.0.1\nOld'),
    /empty/
  );
  assert.throws(
    () => releaseNotes('v0.0.2', '0.0.2', '## 0.0.2\nA\n## 0.0.2\nB'),
    /exactly one/
  );
});

test('release tag must be annotated, checked out, sequential and already in main', (t) => {
  const cwd = mkdtempSync(join(tmpdir(), 'svi-release-git-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const git = (...args) =>
    execFileSync(
      'git',
      [
        '-c',
        'user.name=SmartVoiceInput Test',
        '-c',
        'user.email=svi-test@example.invalid',
        '-c',
        'commit.gpgsign=false',
        '-c',
        'tag.gpgsign=false',
        ...args,
      ],
      { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }
    ).trim();
  git('init', '-b', 'main');
  git('commit', '--allow-empty', '-m', 'base');
  git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  git('tag', 'v0.0.1');
  assert.throws(() => verifyTagCommit('v0.0.1', cwd), /annotated/);
  git('tag', '-d', 'v0.0.1');
  git('switch', '-c', 'release/0.0.1');
  git('commit', '--allow-empty', '-m', 'release');
  git('tag', '-a', 'v0.0.1', '-m', 'SmartVoiceInput 0.0.1');
  assert.throws(() => verifyTagCommit('v0.0.1', cwd), /merged into/);
  git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  assert.equal(verifyTagCommit('v0.0.1', cwd), git('rev-parse', 'HEAD'));
  git('commit', '--allow-empty', '-m', 'later');
  assert.throws(() => verifyTagCommit('v0.0.1', cwd), /Checkout does not match/);
  git('tag', '-a', 'v0.0.3', '-m', 'skipped version');
  git('update-ref', 'refs/remotes/origin/main', 'HEAD');
  assert.throws(() => verifyTagCommit('v0.0.3', cwd), /expected v0.0.2/);
});

function artifactsFixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'svi-release-assets-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  for (const target of targets) {
    writeFileSync(
      join(dir, installerName('0.0.1', target)),
      `fixture ${target.platform} ${target.arch}`
    );
    writeManifest(dir, target, '0.0.1', {
      commit: 'commit-a',
      dirty: false,
      signed: false,
      verification: target.platform === 'mac' ? 'dmg-mounted' : 'nsis-installed',
    });
  }
  return dir;
}

test('installer filenames follow the three release targets', () => {
  assert.deepEqual(
    targets.map((target) => installerName('0.0.1', target)),
    [
      'SmartVoiceInput-0.0.1-mac-arm64.dmg',
      'SmartVoiceInput-0.0.1-mac-x64.dmg',
      'SmartVoiceInput-0.0.1-win-x64.exe',
    ]
  );
});

test('all three installers must match their recorded checksums and commit', (t) => {
  const dir = artifactsFixture(t);
  const result = verifyArtifacts(dir, '0.0.1', 'commit-a', releasePolicy({ channel: 'preview' }));
  assert.equal(result.files.length, 6);
  assert.equal(result.checksums.trim().split('\n').length, 3);
  assert.throws(
    () => verifyArtifacts(dir, '0.0.1', 'another-commit', releasePolicy({ channel: 'preview' })),
    /provenance/
  );
  const installer = join(dir, installerName('0.0.1', targets[2]));
  writeFileSync(installer, 'changed after verification');
  assert.throws(
    () => verifyArtifacts(dir, '0.0.1', 'commit-a', releasePolicy({ channel: 'preview' })),
    /checksum/
  );
});

test('dirty builds and missing assets block publication', (t) => {
  const dir = artifactsFixture(t);
  const file = join(dir, manifestName(targets[0]));
  const manifest = JSON.parse(readFileSync(file, 'utf8'));
  writeFileSync(file, JSON.stringify({ ...manifest, dirty: true }));
  assert.throws(
    () => verifyArtifacts(dir, '0.0.1', 'commit-a', releasePolicy({ channel: 'preview' })),
    /provenance/
  );
  writeFileSync(file, JSON.stringify(manifest));
  rmSync(join(dir, installerName('0.0.1', targets[1])));
  assert.throws(
    () => verifyArtifacts(dir, '0.0.1', 'commit-a', releasePolicy({ channel: 'preview' })),
    /ENOENT/
  );
});

test('preview permits unsigned assets while stable still requires signing', (t) => {
  assert.throws(() => releasePolicy({}), /channel/);
  assert.throws(() => releasePolicy({ channel: 'unknown' }), /channel/);
  const preview = releasePolicy({ channel: 'preview' });
  const stable = releasePolicy({ channel: 'stable' });
  assert.equal(preview.prerelease, true);
  assert.equal(stable.prerelease, false);
  const dir = artifactsFixture(t);
  assert.equal(verifyArtifacts(dir, '0.0.1', 'commit-a', preview).files.length, 6);
  assert.throws(
    () => verifyArtifacts(dir, '0.0.1', 'commit-a', stable),
    /signing/
  );
});

test('release workflows keep publication dependent on all platform builds', () => {
  const ci = readFileSync(join(root, '.github', 'workflows', 'ci.yml'), 'utf8');
  const build = readFileSync(join(root, '.github', 'workflows', 'build.yml'), 'utf8');
  const release = readFileSync(join(root, '.github', 'workflows', 'release.yml'), 'utf8');
  assert.match(ci, /workflow_call:/);
  assert.match(build, /workflow_call:/);
  assert.match(build, /platform: mac\n\s+arch: arm64/);
  assert.match(build, /platform: mac\n\s+arch: x64/);
  assert.match(build, /platform: win\n\s+arch: x64/);
  assert.match(release, /tags: \['v\*'\]/);
  assert.match(release, /needs: \[metadata, build\]/);
  assert.match(release, /contents: write/);
});


// Pure policy fixtures: these tests do not launch Electron or build/publish.
const runtimeOptions = {
  installedDir: '/fixtures/installed', expectedVersion: '42.11.15',
  platform: 'darwin', arch: 'arm64', lockfileSha256: 'b'.repeat(64),
};
const observedRuntime = {
  version: '42.11.15', platform: 'darwin', arch: 'arm64', executableSha256: 'a'.repeat(64),
};

test('explicit Electron distribution wins without inspecting matching installed runtime', () => {
  const probes = [];
  const result = selectElectronRuntime({ ...runtimeOptions, overrideDir: '/fixtures/verified-override' }, (dir) => {
    probes.push(dir);
    return observedRuntime;
  });
  assert.deepEqual(probes, ['/fixtures/verified-override']);
  assert.equal(result.electronDist, '/fixtures/verified-override');
  assert.equal(result.electron.source, 'override');
  assert.equal(result.electron.actualVersion, '42.11.15');
});

test('explicit wrong version, architecture or platform aborts without installed fallback', () => {
  for (const actual of [
    { ...observedRuntime, version: '42.11.14' },
    { ...observedRuntime, arch: 'x64' },
    { ...observedRuntime, platform: 'win32' },
  ]) {
    const probes = [];
    assert.throws(() => selectElectronRuntime({ ...runtimeOptions, overrideDir: '/fixtures/bad' }, (dir) => {
      probes.push(dir);
      return actual;
    }), /runtime mismatch/);
    assert.deepEqual(probes, ['/fixtures/bad']);
  }
  assert.throws(() => selectElectronRuntime({ ...runtimeOptions, overrideDir: '/fixtures/bad' }, () => {
    throw new Error('probe failed');
  }), /probe failed/);
});

test('matching installed runtime is selected from actual probe information', () => {
  const probes = [];
  const result = selectElectronRuntime(runtimeOptions, (dir) => { probes.push(dir); return observedRuntime; });
  assert.deepEqual(probes, ['/fixtures/installed']);
  assert.equal(result.electronDist, '/fixtures/installed');
  assert.equal(result.electron.source, 'installed');
  assert.equal(result.electron.verification, 'runtime-probe');
  assert.equal(result.electron.actualArch, 'arm64');
  assert.equal(result.electron.executableSha256, observedRuntime.executableSha256);
});

test('metadata 42.11.15 cannot certify installed 42.11.14 or wrong architecture', () => {
  for (const actual of [{ ...observedRuntime, version: '42.11.14' }, { ...observedRuntime, arch: 'x64' }]) {
    const result = selectElectronRuntime(runtimeOptions, () => actual);
    assert.equal(result.electronDist, null);
    assert.equal(result.electron.source, 'builder-download');
    assert.equal(result.electron.verification, 'not-probed');
    assert.equal(result.electron.expectedVersion, '42.11.15');
    assert.equal(result.electron.actualVersion, null);
    assert.equal(result.electron.actualPlatform, null);
    assert.equal(result.electron.actualArch, null);
    assert.equal(result.electron.executableSha256, null);
    assert.match(result.warning, /unverified/);
    assert.match(result.electron.fallbackReason, /runtime mismatch/);
  }
});

test('unreadable or malformed installed probe remains unknown', () => {
  for (const probe of [() => { throw new Error('unreadable'); }, () => null, () => [],
    () => ({ ...observedRuntime, executableSha256: null })]) {
    const result = selectElectronRuntime(runtimeOptions, probe);
    assert.equal(result.electronDist, null);
    assert.equal(result.electron.actualVersion, null);
    assert.equal(result.electron.verification, 'not-probed');
  }
});

test('manifest records runtime build input without converting it to final app proof', (t) => {
  const dir = artifactsFixture(t);
  const old = JSON.parse(readFileSync(join(dir, manifestName(targets[0])), 'utf8'));
  assert.equal(old.electron, null);
  const { electron } = selectElectronRuntime({ ...runtimeOptions, overrideDir: '/fixtures/runtime' }, () => observedRuntime);
  writeManifest(dir, targets[0], '0.0.1', {
    commit: 'commit-a', dirty: false, signed: false, verification: 'dmg-mounted', electron,
  });
  const persisted = JSON.parse(readFileSync(join(dir, manifestName(targets[0])), 'utf8'));
  assert.deepEqual(persisted.electron, electron);
  assert.equal(persisted.electron.scope, 'build-input');
  assert.equal(persisted.schemaVersion, 2);
  assert.equal(persisted.signed, false);
  assert.equal(persisted.verification, 'dmg-mounted');
  // Synthetic installer/hash fixtures are not evidence of an actual runtime.
});
