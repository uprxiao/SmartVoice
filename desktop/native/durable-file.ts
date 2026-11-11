import { closeSync, fsyncSync, openSync, renameSync } from 'node:fs';

const transient = new Set(['EPERM', 'EACCES', 'EBUSY']);
const sleeper = new Int32Array(new SharedArrayBuffer(4));
const pause = (ms: number) => { Atomics.wait(sleeper, 0, 0, ms); };
function retry<T>(operation: () => T): T {
  let last: unknown;
  for (let attempt = 0; attempt < 10; attempt++) {
    try { return operation(); } catch (error) {
      last = error;
      if (!transient.has((error as NodeJS.ErrnoException).code ?? '')) throw error;
      pause(10 + attempt * 10);
    }
  }
  throw last;
}

/** Windows can transiently deny a rename while an indexer or antivirus holds
 * the destination. Retry only sharing/access failures; a persistent failure
 * still propagates so an unconfirmed journal is never permission to paste. */
export function renamePublishedFile(from: string, to: string): void {
  retry(() => renameSync(from, to));
}

/** POSIX directory fsync is not supported by Windows. Flush the published file
 * through a writable handle (FlushFileBuffers), including after atomic rename.
 * Errors propagate: an unconfirmed journal is never permission to paste. */
export function syncPublishedFile(directory: string, file: string): void {
  retry(() => {
    const fd = process.platform === 'win32' ? openSync(file, 'r+') : openSync(directory, 'r');
    try { fsyncSync(fd); } finally { closeSync(fd); }
  });
}
