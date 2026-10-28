import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dependencyRoot = path.resolve(process.env.SVI_ADMIN_DEPENDENCY_ROOT ?? root);
const sourceRoot = path.resolve(process.env.SVI_ADMIN_SOURCE_ROOT ?? path.join(root, 'src'));
const ts = createRequire(path.join(dependencyRoot, 'package.json'))('typescript');
function load(location) {
  const source = fs.readFileSync(path.join(sourceRoot, 'config.ts'), 'utf8');
  const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const context = vm.createContext({ location, exports: {} });
  new vm.Script(output).runInContext(context);
  return context.exports;
}

for (const origin of ['https://192.168.14.151', 'https://voice.example.invalid:8443', 'http://localhost:17864']) {
  test(`Admin login and management endpoints follow the hosting origin: ${origin}`, () => {
    const config = load({ origin, protocol: new URL(origin).protocol });
    assert.equal(config.apiUrl('/api/auth/login'), `${origin}/api/auth/login`);
    assert.equal(config.apiUrl('/api/admin/providers'), `${origin}/api/admin/providers`);
  });
}

test('Node-only API harness retains the local development fallback', () => {
  assert.equal(load(undefined).apiUrl('/api/auth/login'), 'http://127.0.0.1:17863/api/auth/login');
});
