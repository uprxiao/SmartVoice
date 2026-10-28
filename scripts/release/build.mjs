import {
  existsSync,
  copyFileSync,
  constants,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { parseArgs } from 'node:util';
import {
  appVersion,
  desktopDir,
  readJson,
  root,
  run,
  runBin,
} from './common.mjs';
import { installerName, targets, writeManifest } from './artifacts.mjs';
import { releasePolicy } from './policy.mjs';
import { selectElectronRuntime } from './electron-runtime.mjs';
import { parseVersion } from './version.mjs';
import { verifyMacAppIdentity } from './mac-identity.mjs';
import { serviceBuildConfig, serviceBuildEnvironment, verifyServiceBuildOutput } from './service-config.mjs';

const { values } = parseArgs({
  options: {
    platform: {
      type: 'string',
      default:
        process.platform === 'darwin'
          ? 'mac'
          : process.platform === 'win32'
            ? 'win'
            : process.platform,
    },
    arch: { type: 'string', default: process.arch },
    dir: { type: 'boolean', default: false },
    'keep-temp': { type: 'boolean', default: false },
    'output-dir': { type: 'string' },
  },
});

const target = targets.find(
  (entry) => entry.platform === values.platform && entry.arch === values.arch
);
if (!target) {
  throw new Error('Supported release targets: mac arm64, mac x64, win x64');
}
const nativePlatform = { mac: 'darwin', win: 'win32' }[target.platform];
if (nativePlatform !== process.platform || target.arch !== process.arch) {
  throw new Error(
    `Build ${target.platform}/${target.arch} on a matching native runner ` +
      `(current ${process.platform}/${process.arch})`
  );
}

const version = appVersion();
parseVersion(version);
const policy = releasePolicy();
const stable = policy.channel === 'stable';
const signMac =
  target.platform === 'mac' &&
  (stable || process.env.SVI_REQUIRE_MAC_SIGNING === '1');
const commit = run('git', ['rev-parse', 'HEAD'], { stdio: 'pipe' });
const dirty = Boolean(
  run('git', ['status', '--porcelain', '--untracked-files=no'], {
    stdio: 'pipe',
  })
);

const service = serviceBuildConfig();
const env = serviceBuildEnvironment(service);
if (signMac) {
  const required = [
    'APPLE_ID',
    'APPLE_APP_SPECIFIC_PASSWORD',
    'APPLE_TEAM_ID',
  ];
  const missing = required.filter((key) => !process.env[key]);
  if (!process.env.CSC_LINK && !process.env.MAC_CERTIFICATE) {
    missing.push('CSC_LINK/MAC_CERTIFICATE');
  }
  if (!process.env.CSC_KEY_PASSWORD && !process.env.MAC_CERTIFICATE_PASSWORD) {
    missing.push('CSC_KEY_PASSWORD/MAC_CERTIFICATE_PASSWORD');
  }
  if (missing.length) {
    throw new Error(`Stable macOS release is missing: ${missing.join(', ')}`);
  }
  env.CSC_LINK = process.env.CSC_LINK || process.env.MAC_CERTIFICATE;
  env.CSC_KEY_PASSWORD =
    process.env.CSC_KEY_PASSWORD || process.env.MAC_CERTIFICATE_PASSWORD;
} else {
  for (const key of Object.keys(env)) {
    if (/^(CSC_|WIN_CSC_|APPLE_)/.test(key)) delete env[key];
  }
  env.CSC_IDENTITY_AUTO_DISCOVERY = 'false';
}

const outDir = values['output-dir'] ? resolve(values['output-dir']) : values.dir
  ? join(root, 'release', `${target.platform}-${target.arch}`)
  : join(root, 'release', 'packages', `${target.platform}-${target.arch}`);
// Sync folders may reattach FinderInfo immediately after signing. Keep the
// bundle on local temporary storage; only a verified DMG leaves that folder.
const packageDir = target.platform === 'mac'
  ? mkdtempSync(join(tmpdir(), 'svi-mac-package-')) : outDir;

// Package metadata states the expected version; only executing the selected
// local runtime can establish an observed version/platform/architecture.
const electronPackage = readJson(join(desktopDir, 'node_modules', 'electron', 'package.json'));
const sha256 = (file) => createHash('sha256').update(readFileSync(file)).digest('hex');
const { electronDist, electron, warning } = selectElectronRuntime({
  overrideDir: process.env.SVI_ELECTRON_DIST ? resolve(process.env.SVI_ELECTRON_DIST) : null,
  installedDir: join(desktopDir, 'node_modules', 'electron', 'dist'),
  expectedVersion: electronPackage.version,
  platform: nativePlatform,
  arch: target.arch,
  lockfileSha256: sha256(join(desktopDir, 'pnpm-lock.yaml')),
}, (directory) => {
  const binary = join(directory, process.platform === 'darwin'
    ? join('Electron.app', 'Contents', 'MacOS', 'Electron')
    : process.platform === 'win32' ? 'electron.exe' : 'electron');
  const before = sha256(binary);
  const info = JSON.parse(run(binary, [
    '-e',
    'console.log(JSON.stringify({version:process.versions.electron,arch:process.arch,platform:process.platform}))',
  ], {
    env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' },
    stdio: 'pipe',
    timeout: 15_000,
    maxBuffer: 1024 * 1024,
  }));
  if (sha256(binary) !== before) throw new Error('Electron executable changed during probe');
  if (!info || typeof info !== 'object' || Array.isArray(info)) {
    throw new Error('Electron runtime probe did not return an object');
  }
  return { ...info, executableSha256: before };
});
// The CJS config also reads SVI_ELECTRON_DIST in the builder subprocess.
// Keep that route and the explicit CLI value on the exact same selected path.
if (electronDist) env.SVI_ELECTRON_DIST = electronDist;
else delete env.SVI_ELECTRON_DIST;
if (warning) console.warn(warning);
console.log(`Electron build input: ${electron.source}; expected=${electron.expectedVersion}; actual=${electron.actualVersion ?? 'unknown'}/${electron.actualPlatform ?? 'unknown'}/${electron.actualArch ?? 'unknown'}`);

console.log(
  `Building SmartVoiceInput ${version}: ${target.platform}/${target.arch} ` +
    `(${stable ? 'stable' : 'preview'}, ${dirty ? 'dirty worktree' : 'clean commit'})`
);

run(process.execPath, [join(desktopDir, 'scripts', 'build-voice-helper.mjs')], {
  cwd: desktopDir,
  stdio: 'inherit',
});
runBin('electron-vite', 'electron-vite', ['build'], {
  cwd: desktopDir,
  env,
  stdio: 'inherit',
});
verifyServiceBuildOutput(desktopDir, service);
runBin(
  'electron-builder',
  'electron-builder',
  [
    target.platform === 'mac' ? '--mac' : '--win',
    `--${target.arch}`,
    '--publish',
    'never',
    '--config',
    join(desktopDir, 'electron-builder.config.cjs'),
    `--config.directories.output=${packageDir}`,
    ...(electronDist ? [`--config.electronDist=${electronDist}`] : []),
    ...(values.dir ? ['--dir'] : []),
  ],
  { cwd: desktopDir, env, stdio: 'inherit' }
);

if (values.dir) {
  if (target.platform === 'mac') {
    const candidates = readdirSync(packageDir).map(name => join(packageDir, name, 'SmartVoiceInput.app'))
      .filter(file => existsSync(file));
    if (candidates.length !== 1) throw new Error('Expected exactly one unpacked macOS app');
    verifyMacAppIdentity(candidates[0]);
  }
  console.log(`Unpacked application written to ${packageDir}`);
  process.exit(0);
}

let installer = join(packageDir, installerName(version, target));
if (!existsSync(installer)) {
  throw new Error(`Expected installer was not produced: ${installer}`);
}

let verification;
let signed = false;
if (target.platform === 'mac') {
  ({ verification, signed } = verifyMacDmg(installer, version, signMac));
} else {
  verification = verifyWindowsInstaller(installer, version);
}

if (target.platform === 'mac') {
  mkdirSync(outDir, { recursive: true });
  const destination = join(outDir, installerName(version, target));
  copyFileSync(installer, destination, constants.COPYFILE_EXCL);
  installer = destination;
}

const manifest = writeManifest(outDir, target, version, {
  commit,
  dirty,
  signed,
  verification,
  electron,
  service: { apiBaseUrl: service.apiBaseUrl, caSha256: service.caSha256 },
});
console.log(
  `Installer verified: ${installer} ` +
    `(${manifest.files[0].size} bytes, ${manifest.files[0].sha256})`
);
if (target.platform === 'mac' && !values['keep-temp']) rmSync(packageDir, { recursive: true });

function verifyMacDmg(file, expectedVersion, requireSigning) {
  const mount = mkdtempSync(join(tmpdir(), 'svi-dmg-'));
  console.log(`DMG verification directory: ${mount}`);
  let attached = false;
  let verificationError;
  try {
    run(
      'hdiutil',
      ['attach', '-nobrowse', '-readonly', '-mountpoint', mount, file],
      { stdio: 'pipe' }
    );
    attached = true;
    const apps = readdirSync(mount).filter((name) => name.endsWith('.app'));
    if (apps.length !== 1 || apps[0] !== 'SmartVoiceInput.app') {
      throw new Error(`Unexpected app bundle in DMG: ${apps.join(', ')}`);
    }
    const appDir = join(mount, apps[0]);
    const plist = join(appDir, 'Contents', 'Info.plist');
    const installedVersion = run(
      'plutil',
      ['-extract', 'CFBundleShortVersionString', 'raw', '-o', '-', plist],
      { stdio: 'pipe' }
    );
    if (installedVersion !== expectedVersion) {
      throw new Error(
        `DMG version mismatch: expected ${expectedVersion}, got ${installedVersion}`
      );
    }
    const bundleId = run(
      'plutil',
      ['-extract', 'CFBundleIdentifier', 'raw', '-o', '-', plist],
      { stdio: 'pipe' }
    );
    if (bundleId !== 'com.smartvoiceinput.desktop') {
      throw new Error(`Unexpected bundle identifier: ${bundleId}`);
    }
    if (!existsSync(join(appDir, 'Contents', 'Resources', 'icon.icns'))) {
      throw new Error('Packaged macOS app has no application icon');
    }
    if (!existsSync(join(appDir, 'Contents', 'Resources', 'icon.png'))) {
      throw new Error('Packaged macOS app has no runtime icon resource');
    }
    if (!existsSync(join(appDir, 'Contents', 'Resources', 'voice', 'smartvoiceinput-input'))) {
      throw new Error('Packaged macOS app has no voice helper');
    }
    const identity = verifyMacAppIdentity(appDir);
    if (requireSigning) {
      run('xcrun', ['stapler', 'validate', appDir], { stdio: 'pipe' });
      run(
        'spctl',
        ['--assess', '--type', 'execute', '--verbose=2', appDir],
        { stdio: 'pipe' }
      );
    }
    return {
      verification: requireSigning ? 'dmg-mounted+signed' : `dmg-mounted+${identity.signature}`,
      signed: requireSigning,
    };
  } catch (error) {
    verificationError = error;
    throw error;
  } finally {
    if (attached) {
      // A failed detach must leave the mount intact for inspection. Never
      // recursively remove a path that may still be an attached volume.
      try {
        run('hdiutil', ['detach', mount, '-quiet'], { stdio: 'pipe' });
      } catch (error) {
        const message = `DMG detach failed; volume retained at ${mount}`;
        if (verificationError) throw new AggregateError([verificationError, error], message);
        throw new Error(message, { cause: error });
      }
    }
    if (values['keep-temp']) console.log(`Verification directory retained: ${mount}`);
    else rmSync(mount, { recursive: true, force: true });
  }
}

function verifyWindowsInstaller(file, expectedVersion) {
  const installDir = mkdtempSync(join(tmpdir(), 'svi-nsis-'));
  const ps = (value) => `'${String(value).replace(/'/g, "''")}'`;
  const script = `
$ErrorActionPreference = 'Stop'
$installer = ${ps(file)}
$installDir = ${ps(installDir)}
if (Test-Path $installDir) { Remove-Item -Recurse -Force $installDir }
$process = Start-Process -FilePath $installer -ArgumentList @('/S', "/D=$installDir") -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "installer exited with $($process.ExitCode)" }
$exe = Join-Path $installDir 'SmartVoiceInput.exe'
if (-not (Test-Path $exe)) { throw "installed executable is missing: $exe" }
$installedVersion = (Get-Item $exe).VersionInfo.ProductVersion
if ($installedVersion -notlike ${ps(expectedVersion)} + '*') {
  throw "installed version mismatch: expected ${expectedVersion}, got $installedVersion"
}
if (-not (Test-Path (Join-Path $installDir 'resources\\icon.png'))) {
  throw 'runtime icon resource is missing'
}
$uninstaller = Join-Path $installDir 'Uninstall SmartVoiceInput.exe'
if (Test-Path $uninstaller) {
  Start-Process -FilePath $uninstaller -ArgumentList '/S' -Wait | Out-Null
}
Write-Output "nsis-installed $installedVersion"
`;
  try {
    const output = run(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command', script],
      { stdio: 'pipe' }
    );
    if (!output.includes('nsis-installed')) {
      throw new Error(`Unexpected installer verification output: ${output}`);
    }
    return 'nsis-installed';
  } finally {
    if (values['keep-temp']) console.log(`Verification directory retained: ${installDir}`);
    else rmSync(installDir, { recursive: true, force: true });
  }
}
