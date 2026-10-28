import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { run } from './common.mjs';

export function verifyMacAppIdentity(appDir) {
  const plist = join(appDir, 'Contents', 'Info.plist');
  const bundleId = run('plutil', ['-extract', 'CFBundleIdentifier', 'raw', '-o', '-', plist], { stdio: 'pipe' });
  if (bundleId !== 'com.smartvoiceinput.desktop') throw new Error('Unexpected macOS bundle identifier');
  const iconName = run('plutil', ['-extract', 'CFBundleIconFile', 'raw', '-o', '-', plist], { stdio: 'pipe' });
  if (iconName !== 'icon.icns') throw new Error('Unexpected macOS bundle icon');
  const icon = join(appDir, 'Contents', 'Resources', iconName);
  if (!existsSync(icon) || readFileSync(icon).subarray(0, 4).toString() !== 'icns') {
    throw new Error('macOS application icon is absent or invalid');
  }
  run('codesign', ['--verify', '--deep', '--strict', appDir], { stdio: 'pipe' });
  const signature = spawnSync('codesign', ['-dv', appDir], { encoding: 'utf8' });
  if (signature.error) throw signature.error;
  if (signature.status !== 0 || !/^Identifier=com\.smartvoiceinput\.desktop$/m.test(signature.stderr) ||
      /Info\.plist=not bound/.test(signature.stderr)) {
    throw new Error('macOS signature does not bind the application identity and Info.plist');
  }
  return { signature: /^Signature=adhoc$/m.test(signature.stderr) ? 'ad-hoc' : 'certificate' };
}
