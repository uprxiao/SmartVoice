import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { verifyBuildOutput, verifyNormalArchive, verifySimulationManifest, readAmd64Image } from './verify-build.mjs';

test('the installed builder accepts the isolated simulation product configuration', async () => {
  const desktopRequire = createRequire(new URL('../../desktop/package.json', import.meta.url));
  const builderRequire = createRequire(desktopRequire.resolve('electron-builder'));
  const { validateConfiguration } = builderRequire('app-builder-lib/out/util/config/config.js');
  await validateConfiguration(desktopRequire('./electron-builder.simulation.config.cjs'), { isEnabled: false });
});

const text = '这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。';
function buildFixture(flavor) {
  const directory = mkdtempSync(join(tmpdir(), 'svi-simulation-build-test-'));
  for (const layer of ['main', 'preload', 'renderer']) {
    mkdirSync(join(directory, layer));
    const modules = layer === 'renderer'
      ? [flavor === 'normal' ? 'src/renderer/audio/recorder.ts' : 'src/renderer/simulation/synthetic-capture.ts']
      : [layer === 'main' && flavor !== 'normal' ? 'src/main/simulation/host.ts' : `src/${layer}/index.ts`];
    writeFileSync(join(directory, layer, 'build-modules.json'), JSON.stringify({ modules }));
  }
  writeFileSync(join(directory, 'main/index.js'), flavor === 'normal' ? 'normal product'
    : `${text} com.smartvoiceinput.desktop.simulation smartvoiceinput:windows-simulation:v1`);
  return directory;
}

test('actual ASAR archives use the locked package resolver and reject simulation content in normal products', async () => {
  const desktopRequire = createRequire(new URL('../../desktop/package.json', import.meta.url));
  const builderRequire = createRequire(desktopRequire.resolve('electron-builder'));
  const asar = createRequire(builderRequire.resolve('app-builder-lib'))('@electron/asar');
  const directory = buildFixture('normal');
  const source = mkdtempSync(join(tmpdir(), 'svi-normal-asar-source-'));
  cpSync(directory, join(source, 'out'), { recursive: true });
  writeFileSync(join(source, 'package.json'), JSON.stringify({ main: 'out/main/index.js', version: '0.0.2' }));
  const output = mkdtempSync(join(tmpdir(), 'svi-normal-asar-output-'));
  const archive = join(output, 'normal.asar');
  await asar.createPackage(source, archive);
  assert.ok(verifyNormalArchive(archive, '0.0.2').includes('src/renderer/audio/recorder.ts'));
  const listPackage = asar.listPackage;
  try {
    asar.listPackage = (...args) => listPackage(...args).map(value => value.replaceAll('/', '\\'));
    assert.ok(verifyNormalArchive(archive, '0.0.2').includes('src/renderer/audio/recorder.ts'));
  } finally { asar.listPackage = listPackage; }
  writeFileSync(join(source, 'out/main/index.js'), text);
  const contaminated = join(output, 'contaminated.asar');
  await asar.createPackage(source, contaminated);
  assert.throws(() => verifyNormalArchive(contaminated, '0.0.2'), /simulation capabilities/);
});

test('normal builds retain hardware capture and reject simulation modules or data', () => {
  const directory = buildFixture('normal');
  assert.equal(verifyBuildOutput('normal', directory).flavor, 'normal');
  writeFileSync(join(directory, 'main/index.js'), text);
  assert.throws(() => verifyBuildOutput('normal', directory), /simulation capabilities/);
  writeFileSync(join(directory, 'main/index.js'), 'normal');
  writeFileSync(join(directory, 'main/build-modules.json'), JSON.stringify({ modules: ['src/main/simulation/host.ts'] }));
  assert.throws(() => verifyBuildOutput('normal', directory), /simulation modules/);
});

test('simulation builds reject hardware APIs, source modules, and leaked absolute build paths', () => {
  const directory = buildFixture('windows-simulation');
  assert.equal(verifyBuildOutput('windows-simulation', directory).flavor, 'windows-simulation');
  const file = join(directory, 'main/index.js');
  const body = readFileSync(file, 'utf8');
  for (const api of ['getUserMedia', 'AudioContext', 'AudioWorkletNode', 'getMediaAccessStatus']) {
    writeFileSync(file, body + '\n' + api);
    assert.throws(() => verifyBuildOutput('windows-simulation', directory), /audio-device API/);
  }
  writeFileSync(file, body);
  writeFileSync(join(directory, 'renderer/build-modules.json'), JSON.stringify({ modules: ['src/renderer/audio/recorder.ts'] }));
  assert.throws(() => verifyBuildOutput('windows-simulation', directory), /input source|hardware/);
  writeFileSync(join(directory, 'renderer/build-modules.json'), JSON.stringify({ modules: ['/private/build/recorder.ts'] }));
  assert.throws(() => verifyBuildOutput('windows-simulation', directory), /relative module graph/);
});

test('simulation provenance and installer bytes are independently checked', () => {
  const directory = mkdtempSync(join(tmpdir(), 'svi-simulation-artifact-test-'));
  const file = 'SmartVoiceInput-Simulation-0.0.2-win-x64.exe';
  const bytes = Buffer.from('fixture-only installer');
  writeFileSync(join(directory, file), bytes);
  const manifest = { schemaVersion: 1, flavor: 'windows-simulation', version: '0.0.2', commit: 'source-a', dirty: false,
    platform: 'win', arch: 'x64', appId: 'com.smartvoiceinput.desktop.simulation', productName: 'SmartVoiceInput Simulation',
    verification: 'nsis-installed', files: [{ file, size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') }] };
  const path = join(directory, 'manifest-windows-simulation.json');
  writeFileSync(path, JSON.stringify(manifest));
  assert.equal(verifySimulationManifest(directory, 'source-a').version, '0.0.2');
  assert.throws(() => verifySimulationManifest(directory, 'source-b'), /provenance/);
  writeFileSync(path, JSON.stringify({ ...manifest, dirty: true }));
  assert.throws(() => verifySimulationManifest(directory, 'source-a'), /provenance/);
  writeFileSync(path, JSON.stringify(manifest));
  writeFileSync(join(directory, file), 'changed');
  assert.throws(() => verifySimulationManifest(directory, 'source-a'), /checksum/);
});

test('payload architecture checks require both the machine and optional-header format', () => {
  const directory = mkdtempSync(join(tmpdir(), 'svi-simulation-pe-test-'));
  const file = join(directory, 'fixture.exe');
  const bytes = Buffer.alloc(256);
  bytes.write('MZ'); bytes.writeUInt32LE(128, 0x3c); bytes.write('PE\u0000\u0000', 128);
  bytes.writeUInt16LE(0x8664, 132); bytes.writeUInt16LE(0x20b, 152);
  writeFileSync(file, bytes);
  assert.equal(readAmd64Image(file).machine, 'AMD64');
  bytes.writeUInt16LE(0x14c, 132); writeFileSync(file, bytes);
  assert.throws(() => readAmd64Image(file), /AMD64/);
});
