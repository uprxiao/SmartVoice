import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { createRequire } from 'node:module';
import { appVersion, desktopDir, root, run, runBin } from '../release/common.mjs';
import { selectElectronRuntime } from '../release/electron-runtime.mjs';
import { parseVersion } from '../release/version.mjs';
import { verifyBuildOutput, verifyWindowsSimulationPayload, verifySimulationManifest } from './verify-build.mjs';

if (process.platform !== 'win32' || process.arch !== 'x64') {
  throw new Error('Build the Windows simulation installer on a native Windows x64 runner');
}
const { values } = parseArgs({ options: { 'output-dir': { type: 'string' } } });
const require = createRequire(import.meta.url);
const { simulation } = require('../../desktop/build/products.cjs');
const version = appVersion();
parseVersion(version);
const outDir = values['output-dir'] ? resolve(values['output-dir']) : join(root, 'release/simulation/win-x64');
const installerName = `SmartVoiceInput-Simulation-${version}-win-x64.exe`;
if (existsSync(join(outDir, installerName)) || existsSync(join(outDir, 'manifest-windows-simulation.json'))) {
  throw new Error('Choose a new simulation output directory; existing artifacts are retained');
}
mkdirSync(outDir, { recursive: true });
const commit = run('git', ['rev-parse', 'HEAD'], { stdio: 'pipe' });
const dirty = Boolean(run('git', ['status', '--porcelain', '--untracked-files=no'], { stdio: 'pipe' }));
const env = { ...process.env, CSC_IDENTITY_AUTO_DISCOVERY: 'false' };
for (const key of Object.keys(env)) {
  if (/^(CSC_|WIN_CSC_|APPLE_)/.test(key) || ['VITE_API_BASE_URL', 'VITE_SERVICE_CA_SHA256', 'SVI_ELECTRON_DIST'].includes(key)) delete env[key];
}
env.CSC_IDENTITY_AUTO_DISCOVERY = 'false';
const hash = file => createHash('sha256').update(readFileSync(file)).digest('hex');
const installed = join(desktopDir, 'node_modules/electron/dist');
const electronPackage = JSON.parse(readFileSync(join(desktopDir, 'node_modules/electron/package.json'), 'utf8'));
const { electronDist, electron } = selectElectronRuntime({ overrideDir: null, installedDir: installed,
  expectedVersion: electronPackage.version, platform: 'win32', arch: 'x64',
  lockfileSha256: hash(join(desktopDir, 'pnpm-lock.yaml')) }, directory => {
  const binary = join(directory, 'electron.exe');
  const before = hash(binary);
  const result = JSON.parse(run(binary, ['-e', 'console.log(JSON.stringify({version:process.versions.electron,arch:process.arch,platform:process.platform}))'],
    { env: { ...env, ELECTRON_RUN_AS_NODE: '1' }, stdio: 'pipe', timeout: 15000 }));
  if (hash(binary) !== before) throw new Error('Electron input changed during verification');
  return { ...result, executableSha256: before };
});
if (!electronDist) throw new Error('A verified local Windows Electron runtime is required');
env.SVI_ELECTRON_DIST = electronDist;
run(process.execPath, [join(desktopDir, 'scripts/build-voice-helper.mjs')], { cwd: desktopDir, env, stdio: 'inherit' });
runBin('electron-vite', 'electron-vite', ['build', '--config', 'electron.vite.simulation.config.ts'],
  { cwd: desktopDir, env, stdio: 'inherit' });
const graph = verifyBuildOutput(simulation.flavor, join(desktopDir, 'out-simulation'));
runBin('electron-builder', 'electron-builder', ['--win', '--x64', '--publish', 'never', '--config',
  join(desktopDir, 'electron-builder.simulation.config.cjs'), `--config.directories.output=${outDir}`,
  `--config.electronDist=${electronDist}`], { cwd: desktopDir, env, stdio: 'inherit' });
const payload = verifyWindowsSimulationPayload(join(outDir, 'win-unpacked'), version);
const installer = join(outDir, installerName);
if (!existsSync(installer)) throw new Error('Simulation installer was not produced');
const installDir = join(mkdtempSync(join(tmpdir(), 'svi-simulation-install-')), 'Application');
const quote = value => `'${String(value).replaceAll("'", "''")}'`;
const script = `
$ErrorActionPreference = 'Stop'
$installDir = ${quote(installDir)}
if (Test-Path $installDir) { throw 'The owned install target already exists' }
$installed = $false
try {
  $process = Start-Process -FilePath ${quote(installer)} -ArgumentList @('/S', "/D=$installDir") -Wait -PassThru
  if ($process.ExitCode -ne 0) { throw "Simulation installer exited with $($process.ExitCode)" }
  $installed = $true
  $exe = Join-Path $installDir ${quote(simulation.executableName + '.exe')}
  if (-not (Test-Path $exe)) { throw 'Installed simulation executable is missing' }
  if ((Get-Item $exe).VersionInfo.ProductVersion -notlike ${quote(version)} + '*') { throw 'Installed simulation version differs' }
  if ((Get-FileHash $exe).Hash.ToLowerInvariant() -ne ${quote(payload.executable.sha256)}) { throw 'Installed simulation executable digest differs' }
  $helper = Join-Path $installDir 'resources/voice/smartvoiceinput-input.exe'
  if ((Get-FileHash $helper).Hash.ToLowerInvariant() -ne ${quote(payload.helper.sha256)}) { throw 'Installed simulation helper digest differs' }
  if (Test-Path (Join-Path $installDir 'resources/service-ca.crt')) { throw 'Unexpected production CA in simulation installation' }
  Write-Output 'nsis-installed'
} finally {
  if ($installed) {
    $uninstaller = Join-Path $installDir ${quote('Uninstall ' + simulation.productName + '.exe')}
    if (-not (Test-Path $uninstaller)) { throw 'Simulation uninstaller is missing' }
    $removed = Start-Process -FilePath $uninstaller -ArgumentList '/S' -Wait -PassThru
    if ($removed.ExitCode -ne 0) { throw 'Simulation uninstall failed' }
  }
}
`;
const verification = run('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { stdio: 'pipe', env });
if (!verification.split(/\r?\n/).includes('nsis-installed')) throw new Error('Simulation installation was not confirmed');
const manifest = { schemaVersion: 1, flavor: simulation.flavor, version, commit, dirty, platform: 'win', arch: 'x64',
  appId: simulation.appId, productName: simulation.productName, signed: false, verification: 'nsis-installed',
  electron, service: { kind: 'embedded-loopback', storageIdentity: simulation.storageIdentity },
  moduleCount: graph.moduleCount, payload,
  files: [{ file: installerName, size: readFileSync(installer).length, sha256: hash(installer) }] };
writeFileSync(join(outDir, 'manifest-windows-simulation.json'), JSON.stringify(manifest, null, 2)+'\n', { flag: 'wx' });
writeFileSync(join(outDir, 'SHA256SUMS.txt'), `${manifest.files[0].sha256}  ${installerName}\n`, { flag: 'wx' });
verifySimulationManifest(outDir, commit);
console.log(`Verified Windows simulation installer: ${installer}`);
