import type { BrowserWindowConstructorOptions } from 'electron';

/** A non-focusable normal NSWindow can still activate its owning app on click. */
export function hudWindowPlatformOptions(platform: NodeJS.Platform): Pick<BrowserWindowConstructorOptions, 'type'> {
  return platform === 'darwin' ? { type: 'panel' } : {};
}
