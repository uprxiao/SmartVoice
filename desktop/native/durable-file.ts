import { closeSync, fsyncSync, openSync } from 'node:fs';

/** POSIX directory fsync is not supported by Windows. Flush the published file
 * through a writable handle (FlushFileBuffers), including after atomic rename.
 * Errors propagate: an unconfirmed journal is never permission to paste. */
export function syncPublishedFile(directory: string, file: string): void {
  const fd = process.platform === 'win32' ? openSync(file, 'r+') : openSync(directory, 'r');
  try { fsyncSync(fd); } finally { closeSync(fd); }
}
