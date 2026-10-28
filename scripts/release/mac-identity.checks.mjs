import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { root } from './common.mjs';
import { verifyMacAppIdentity } from './mac-identity.mjs';

const { cleanSigningMetadata } = createRequire(import.meta.url)('../../desktop/scripts/prepare-mac-signing.cjs');
const native = process.platform === 'darwin';

test('the installed builder accepts our complete config and rejects the unsupported signing hook', async () => {
  const desktopRequire = createRequire(join(root, 'desktop/package.json'));
  const builderRequire = createRequire(desktopRequire.resolve('electron-builder'));
  const { validateConfiguration } = builderRequire('app-builder-lib/out/util/config/config.js');
  const config = desktopRequire('./electron-builder.config.cjs');
  const logger = { isEnabled: false };
  await validateConfiguration(config, logger);
  await assert.rejects(validateConfiguration({ ...config, beforeSign: () => {} }, logger), /beforeSign/);
});

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'svi-signature-test-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const app = join(directory, 'SmartVoiceInput.app');
  mkdirSync(join(app, 'Contents', 'MacOS'), { recursive: true });
  mkdirSync(join(app, 'Contents', 'Resources'));
  copyFileSync(join(root, 'desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'),
    join(app, 'Contents/MacOS/SmartVoiceInput'));
  chmodSync(join(app, 'Contents/MacOS/SmartVoiceInput'), 0o755);
  copyFileSync(join(root, 'desktop/build/icon.icns'), join(app, 'Contents/Resources/icon.icns'));
  writeFileSync(join(app, 'Contents/Info.plist'), `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.smartvoiceinput.desktop</string>
<key>CFBundleExecutable</key><string>SmartVoiceInput</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleIconFile</key><string>icon.icns</string>
</dict></plist>`);
  return app;
}

test('rejects a real unbound Electron linker signature despite correct bundle/icon declarations', { skip: !native }, t => {
  const app = fixture(t);
  assert.throws(() => verifyMacAppIdentity(app), /codesign|identity|Info\.plist/);
});

test('accepts a real bound ad-hoc identity and rejects a subsequently changed sealed icon', { skip: !native }, t => {
  const app = fixture(t);
  execFileSync('codesign', ['--force', '--sign', '-', '--timestamp=none', '--identifier', 'com.smartvoiceinput.desktop', app], { stdio: 'pipe' });
  assert.equal(verifyMacAppIdentity(app).signature, 'ad-hoc');
  const icon = join(app, 'Contents/Resources/icon.icns');
  const changed = readFileSync(icon); changed[changed.length - 1] ^= 1;
  writeFileSync(icon, changed);
  assert.throws(() => verifyMacAppIdentity(app), /codesign/);
});

test('cleans only signing detritus on a temporary artifact and preserves quarantine', { skip: !native }, t => {
  const app = fixture(t);
  const file = join(app, 'Contents/Resources/icon.icns');
  execFileSync('xattr', ['-wx', 'com.apple.FinderInfo', '0000000000000000040000000000000000000000000000000000000000000000', file]);
  execFileSync('xattr', ['-wx', 'com.apple.ResourceFork', '01020304', file]);
  const quarantine = '0081;00000000;SVI-fixture;test-only';
  execFileSync('xattr', ['-w', 'com.apple.quarantine', quarantine, file]);
  assert.equal(cleanSigningMetadata(app), 2);
  const names = execFileSync('xattr', [file], { encoding: 'utf8' }).trim().split('\n');
  assert.ok(!names.includes('com.apple.FinderInfo'));
  assert.ok(!names.includes('com.apple.ResourceFork'));
  assert.equal(execFileSync('xattr', ['-p', 'com.apple.quarantine', file], { encoding: 'utf8' }).trim(), quarantine);
});
