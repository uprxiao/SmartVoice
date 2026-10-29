import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync } from 'node:fs';

if (process.platform === 'darwin') {
  mkdirSync('resources/voice', { recursive: true });
  const result = spawnSync(
    'xcrun',
    [
      'swiftc',
      '-O',
      'native/voice-input.swift',
      '-o',
      'resources/voice/smartvoiceinput-input',
      '-framework',
      'AppKit',
      '-framework',
      'ApplicationServices',
    ],
    { stdio: 'inherit' }
  );
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
if (process.platform === 'win32') {
  const result = spawnSync('cargo', ['build', '--locked', '--release', '--target', 'x86_64-pc-windows-msvc',
    '--manifest-path', 'native/windows/Cargo.toml', '--bin', 'smartvoiceinput-input'], { stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
  mkdirSync('resources/voice', { recursive: true });
  const { join } = await import('node:path');
  copyFileSync(join(process.env.CARGO_TARGET_DIR || 'native/windows/target', 'x86_64-pc-windows-msvc/release/smartvoiceinput-input.exe'),
    'resources/voice/smartvoiceinput-input.exe');
}
