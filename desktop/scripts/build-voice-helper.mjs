import { spawnSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';

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
