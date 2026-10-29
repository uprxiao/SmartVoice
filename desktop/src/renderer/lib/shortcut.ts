export function friendlyShortcut(accelerator: string | null | undefined, platform: NodeJS.Platform = 'darwin'): string {
  if (!accelerator) return '—';
  if (platform === 'win32') {
    return accelerator.split('+').map(part => {
      const key = part.trim();
      if (/^(CommandOrControl|CmdOrCtrl|Control|Ctrl)$/i.test(key)) return 'Ctrl';
      if (/^(Command|Cmd|Super|Meta)$/i.test(key)) return 'Win';
      if (/^(Alt|Option)$/i.test(key)) return 'Alt';
      return key;
    }).join(' + ');
  }
  return accelerator
    .replace(/CommandOrControl|Command|Cmd/g, '⌘')
    .replace(/Control|Ctrl/g, '⌃')
    .replace(/Alt|Option/g, '⌥')
    .replace(/Shift/g, '⇧')
    .replace(/\+/g, '');
}

// Modifiers can change event.key (Shift+3 -> #, Option+letter -> a symbol).
// Record the base key so native registration and reserved-table comparison
// describe the same chord that the user pressed.
export function acceleratorFromKeyEvent(event: Pick<KeyboardEvent,
  'key' | 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>, platform: NodeJS.Platform = 'darwin'): string | null {
  if (['Escape', 'Meta', 'Control', 'Alt', 'Shift'].includes(event.key)) return null;
  const modifiers = [event.metaKey && (platform === 'win32' ? 'Super' : 'CommandOrControl'), event.ctrlKey && 'Control',
    event.altKey && 'Alt', event.shiftKey && 'Shift'].filter(Boolean);
  if (modifiers.length === 0) return null;
  const punctuation: Record<string, string> = {
    Comma: ',', Period: '.', Slash: '/', Backquote: '`', Minus: '-', Equal: '=',
    BracketLeft: '[', BracketRight: ']', Backslash: '\\', Semicolon: ';', Quote: "'",
  };
  const baseKey = /^(?:Key([A-Z])|Digit([0-9]))$/.exec(event.code);
  const key = baseKey ? baseKey[1] ?? baseKey[2] : punctuation[event.code]
    ?? (event.key === ' ' ? 'Space' : event.key.length === 1 ? event.key.toUpperCase() : event.key);
  return [...modifiers, key].join('+');
}
