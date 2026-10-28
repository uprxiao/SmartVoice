import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = resolve(fileURLToPath(new URL('../../', import.meta.url)));
export const desktopDir = join(root, 'desktop');
export const desktopPackageFile = join(desktopDir, 'package.json');

export function readJson(file) {
  return JSON.parse(readFileSync(file, 'utf8'));
}

export function appVersion() {
  return readJson(desktopPackageFile).version;
}

export function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: root,
    encoding: 'utf8',
    ...options,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const detail = [result.stdout, result.stderr].filter(Boolean).join('\n').trim();
    throw new Error(
      `${command} exited with ${result.status ?? result.signal}${detail ? `\n${detail}` : ''}`
    );
  }
  return (result.stdout ?? '').trim();
}

export function runBin(packageName, binName, args, options = {}) {
  const dir = join(desktopDir, 'node_modules', packageName);
  const { bin } = readJson(join(dir, 'package.json'));
  const relative = typeof bin === 'string' ? bin : bin[binName];
  if (!relative) throw new Error(`${packageName} does not declare bin ${binName}`);
  return run(process.execPath, [join(dir, relative), ...args], options);
}

export function isMain(moduleUrl) {
  return Boolean(process.argv[1]) && fileURLToPath(moduleUrl) === resolve(process.argv[1]);
}
