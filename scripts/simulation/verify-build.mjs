import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, resolve, isAbsolute, basename } from 'node:path';
import { createRequire } from 'node:module';
import { isMain, desktopDir } from '../release/common.mjs';

const require = createRequire(import.meta.url);
const { simulation } = require('../../desktop/build/products.cjs');
const fixedText = '这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。';
const layers = ['main', 'preload', 'renderer'];
const audioApi = /getUserMedia|\bAudioContext\b|\bAudioWorklet(?:Node)?\b|audioWorklet\.addModule|getMediaAccessStatus|askForMediaAccess/;
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');

function textFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = join(directory, entry.name);
    if (entry.isDirectory()) return textFiles(file);
    return /\.(?:js|html|json)$/.test(entry.name) ? [file] : [];
  });
}

function checkModuleGraphs(flavor, readGraph) {
  const modules = layers.flatMap(layer => {
    const graph = JSON.parse(readGraph(layer));
    if (!Array.isArray(graph.modules) || !graph.modules.length
      || graph.modules.some(value => typeof value !== 'string' || isAbsolute(value)
        || /^[A-Za-z]:/.test(value) || value.startsWith('../') || value.includes('\\'))) {
      throw new Error(`Invalid relative module graph for ${layer}`);
    }
    return graph.modules;
  });
  if (modules.some(value => /\.(?:test|spec)\.[jt]sx?$/.test(value) || value.includes('/fixtures/'))) {
    throw new Error('A test fixture entered the application module graph');
  }
  if (flavor === 'normal') {
    if (modules.some(value => value.includes('/simulation/'))) throw new Error('Normal build includes simulation modules');
    if (!modules.includes('src/renderer/audio/recorder.ts')) throw new Error('Normal build lost its hardware recorder');
  } else {
    if (!modules.some(value => value.startsWith('src/main/simulation/'))
      || !modules.includes('src/renderer/simulation/synthetic-capture.ts')) {
      throw new Error('Simulation build is missing its isolated service or input source');
    }
    if (modules.some(value => value === 'src/renderer/audio/recorder.ts'
      || value.includes('/product/normal-') || value.includes('pcm-worklet'))) {
      throw new Error('Simulation build includes a normal hardware/service module');
    }
  }
  return [...new Set(modules)].sort();
}

function checkContents(flavor, entries) {
  const code = entries.map(([, content]) => content).join('\n');
  if (flavor === 'normal') {
    if (code.includes(fixedText) || code.includes(simulation.appId)
      || code.includes(simulation.storageIdentity)) throw new Error('Normal build contains simulation capabilities');
  } else {
    if (audioApi.test(code)) throw new Error('Simulation build contains a real audio-device API');
    if (!code.includes(fixedText) || !code.includes(simulation.appId)
      || !code.includes(simulation.storageIdentity)) throw new Error('Simulation build identity or fixed result is missing');
    if (entries.some(([name]) => name.includes('pcm-worklet'))) throw new Error('Simulation build contains the hardware worklet asset');
  }
}

export function verifyBuildOutput(flavor, outputDirectory) {
  if (!['normal', 'windows-simulation'].includes(flavor)) throw new Error('Unknown build flavor');
  const modules = checkModuleGraphs(flavor, layer => readFileSync(join(outputDirectory, layer, 'build-modules.json'), 'utf8'));
  const entries = textFiles(outputDirectory).filter(file => basename(file) !== 'build-modules.json')
    .map(file => [file, readFileSync(file, 'utf8')]);
  checkContents(flavor, entries);
  return { flavor, moduleCount: modules.length, modules };
}

function asarReader() {
  const desktopRequire = createRequire(join(desktopDir, 'package.json'));
  const builderRequire = createRequire(desktopRequire.resolve('electron-builder'));
  return createRequire(builderRequire.resolve('app-builder-lib'))('@electron/asar');
}

function verifyArchiveModules(archive, flavor, output) {
  const asar = asarReader();
  const modules = checkModuleGraphs(flavor, layer =>
    asar.extractFile(archive, `${output}/${layer}/build-modules.json`).toString('utf8'));
  const paths = asar.listPackage(archive).map(value => value.replaceAll('\\', '/').replace(/^\/+/, ''));
  const entries = paths.filter(value => /\.(?:js|html|json)$/.test(value)
    && !value.endsWith('/build-modules.json') && !value.startsWith('node_modules/'))
    .map(value => [value, asar.extractFile(archive, value).toString('utf8')]);
  checkContents(flavor, entries);
  return modules;
}

export function verifyNormalArchive(archive, version) {
  const pkg = JSON.parse(asarReader().extractFile(archive, 'package.json').toString('utf8'));
  if (pkg.main !== 'out/main/index.js' || pkg.version !== version || pkg.buildFlavor) {
    throw new Error('Normal packaged application metadata differs from its expected identity');
  }
  return verifyArchiveModules(archive, 'normal', 'out');
}

export function readAmd64Image(file) {
  const bytes = readFileSync(file);
  if (bytes.length < 0x40 || bytes.toString('ascii', 0, 2) !== 'MZ') throw new Error('Missing PE executable header');
  const offset = bytes.readUInt32LE(0x3c);
  if (offset + 26 > bytes.length || bytes.toString('ascii', offset, offset + 4) !== 'PE\u0000\u0000'
    || bytes.readUInt16LE(offset + 4) !== 0x8664 || bytes.readUInt16LE(offset + 24) !== 0x20b) {
    throw new Error('Expected an AMD64 PE32+ executable');
  }
  return { machine: 'AMD64', format: 'PE32+', size: bytes.length, sha256: sha256(bytes) };
}

export function verifyWindowsSimulationPayload(unpackedDirectory, version) {
  const asar = asarReader();
  const resources = join(unpackedDirectory, 'resources');
  const archive = join(resources, 'app.asar');
  const output = 'out-simulation';
  const pkg = JSON.parse(asar.extractFile(archive, 'package.json').toString('utf8'));
  if (pkg.version !== version || pkg.main !== `${output}/main/index.js` || pkg.buildFlavor !== simulation.flavor) {
    throw new Error('Packaged simulation application metadata differs from the build');
  }
  const modules = verifyArchiveModules(archive, simulation.flavor, output);
  const helpers = readdirSync(join(resources, 'voice'));
  if (helpers.length !== 1 || helpers[0] !== 'smartvoiceinput-input.exe') throw new Error('Unexpected simulation native resources');
  if (existsSync(join(resources, 'service-ca.crt'))) throw new Error('Simulation package unexpectedly includes a production service CA');
  if (!existsSync(join(resources, 'icon.png'))) throw new Error('Simulation package is missing its icon');
  const executable = readAmd64Image(join(unpackedDirectory, `${simulation.executableName}.exe`));
  const helper = readAmd64Image(join(resources, 'voice/smartvoiceinput-input.exe'));
  return { flavor: simulation.flavor, appId: simulation.appId, productName: simulation.productName,
    moduleCount: modules.length, executable, helper, asarSha256: sha256(readFileSync(archive)) };
}

export function verifySimulationManifest(directory, expectedCommit) {
  const manifest = JSON.parse(readFileSync(join(directory, 'manifest-windows-simulation.json'), 'utf8'));
  if (manifest.schemaVersion !== 1 || manifest.flavor !== simulation.flavor || manifest.platform !== 'win'
    || manifest.arch !== 'x64' || manifest.appId !== simulation.appId || manifest.productName !== simulation.productName
    || manifest.dirty !== false || (expectedCommit && manifest.commit !== expectedCommit)
    || manifest.verification !== 'nsis-installed' || !Array.isArray(manifest.files) || manifest.files.length !== 1) {
    throw new Error('Simulation installer provenance mismatch');
  }
  const entry = manifest.files[0];
  const expected = `SmartVoiceInput-Simulation-${manifest.version}-win-x64.exe`;
  if (entry.file !== expected) throw new Error('Unexpected simulation installer filename');
  const file = join(directory, expected);
  if (statSync(file).size !== entry.size || entry.size <= 0 || sha256(readFileSync(file)) !== entry.sha256) {
    throw new Error('Simulation installer checksum mismatch');
  }
  return manifest;
}

if (isMain(import.meta.url)) {
  const [flavor, directory] = process.argv.slice(2);
  console.log(JSON.stringify(verifyBuildOutput(flavor, resolve(directory)), null, 2));
}
