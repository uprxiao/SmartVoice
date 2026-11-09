import { spawn, type ChildProcessByStdio } from 'node:child_process';
import type { Readable } from 'node:stream';
import { nativeHelperPath } from '../core/native-input';

interface HudClickCallbacks {
  onCancel(): void;
  onConfirm(): void;
}

const MAX_LINE_BYTES = 4096;
type HudHookProcess = ChildProcessByStdio<null, Readable, Readable>;
let child: HudHookProcess | null = null;
let stopping = false;
let restartTimer: NodeJS.Timeout | null = null;

/**
 * The HUD is deliberately non-activating. On Windows the first click on such
 * a window can be consumed before the renderer receives it, leaving the
 * cancel/confirm buttons dead. A tiny native low-level mouse hook handles only
 * presses inside the capsule buttons and reports them here, so the HUD can stay
 * unfocused and the insertion target is never stolen.
 */
export function startHudClickHook(callbacks: HudClickCallbacks): void {
  if (process.platform !== 'win32' || child) return;
  stopping = false;
  let proc: HudHookProcess;
  try {
    proc = spawn(nativeHelperPath(), ['--hud-hook', '--parent-pid', String(process.pid)], {
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } catch {
    return;
  }
  child = proc;
  proc.stderr.resume();
  let buffered = Buffer.alloc(0);
  proc.stdout.on('data', (chunk: Buffer) => {
    if (child !== proc) return;
    buffered = Buffer.concat([buffered, chunk]);
    while (child === proc) {
      const newline = buffered.indexOf(10);
      if (newline < 0) {
        if (buffered.length > MAX_LINE_BYTES) buffered = Buffer.alloc(0);
        return;
      }
      const line = buffered.subarray(0, newline).toString('utf8').trim();
      buffered = buffered.subarray(newline + 1);
      if (!line) continue;
      try {
        const parsed = JSON.parse(line) as { action?: unknown };
        if (parsed.action === 'cancel') callbacks.onCancel();
        else if (parsed.action === 'confirm') callbacks.onConfirm();
      } catch {
        // The hook only emits fixed actions; ignore anything malformed.
      }
    }
  });
  const handleExit = () => {
    if (child !== proc) return;
    child = null;
    scheduleRestart(callbacks);
  };
  proc.on('error', handleExit);
  proc.on('exit', handleExit);
}

function scheduleRestart(callbacks: HudClickCallbacks): void {
  if (stopping || restartTimer) return;
  restartTimer = setTimeout(() => {
    restartTimer = null;
    startHudClickHook(callbacks);
  }, 2000);
  restartTimer.unref?.();
}

export function stopHudClickHook(): void {
  stopping = true;
  if (restartTimer) {
    clearTimeout(restartTimer);
    restartTimer = null;
  }
  if (child) {
    child.kill();
    child = null;
  }
}
