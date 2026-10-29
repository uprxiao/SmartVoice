// Product-owned reserved-key policy, not an OS/third-party registration query.
// Platform meaning and supported aliases follow Electron's accelerator format.
// Common macOS operations: https://support.apple.com/en-us/102650
// Accelerator aliases: https://www.electronjs.org/docs/latest/tutorial/keyboard-shortcuts
export function shortcutSignature(accelerator: string, platform: NodeJS.Platform): string {
  const aliases: Record<string, string> = {
    cmd: 'command', ctrl: 'control', option: 'alt', esc: 'escape', enter: 'return',
    commandorcontrol: platform === 'darwin' ? 'command' : 'control',
    cmdorctrl: platform === 'darwin' ? 'command' : 'control',
    super: platform === 'darwin' ? 'command' : 'super',
    meta: platform === 'darwin' ? 'command' : 'super',
  };
  return [...new Set(accelerator.split('+').map(part => {
    const token = part.trim().toLowerCase();
    return aliases[token] ?? token;
  }))].sort().join('+');
}

const editingKeys = ['A', 'C', 'V', 'X', 'S', 'Z', 'Y', 'W', 'Q', 'R', 'N', 'O', 'F', 'G'];
const bareKeys = ['Return', 'Tab', 'Space', 'Backspace', 'Delete', 'CapsLock', 'Pause',
  ...Array.from({ length: 12 }, (_, index) => `F${index + 1}`)];
const common = [...bareKeys, ...editingKeys.map(key => `Control+${key}`),
  'Control+Tab', 'Control+Shift+Tab', 'Control+Shift+Z', 'Alt+Tab', 'Alt+F4'];
const mac = [...common, ...[...editingKeys, 'H', 'M', 'P', 'T'].map(key => `Command+${key}`),
  'Command+Tab', 'Command+Shift+Tab', 'Command+Space', 'Command+,', 'Command+`',
  'Command+Shift+Z', 'Command+Shift+3', 'Command+Shift+4', 'Command+Shift+5',
  'Control+Space', 'Control+Shift+Space'];
const windows = [...common, 'Control+P', 'Control+T', 'Super+L', 'Super+D', 'Super+E',
  'Super+R', 'Super+Space', 'Control+Alt+Delete', 'Shift+Space', 'Control+Space'];
const reserved = {
  darwin: new Set(mac.map(key => shortcutSignature(key, 'darwin'))),
  win32: new Set(windows.map(key => shortcutSignature(key, 'win32'))),
  other: new Set(common.map(key => shortcutSignature(key, 'linux'))),
};

export function shortcutPolicyError(accelerator: string, platform: NodeJS.Platform): string | null {
  const signature = shortcutSignature(accelerator, platform);
  // Escape belongs to the focused app even when combined with modifiers.
  if (signature.split('+').includes('escape')) {
    return 'Escape 用于取消操作，不能设为全局快捷键';
  }
  const table = platform === 'darwin' ? reserved.darwin
    : platform === 'win32' ? reserved.win32 : reserved.other;
  return table.has(signature) ? '此快捷键为系统或常用操作保留，请选择其他组合' : null;
}
