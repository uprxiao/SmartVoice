import { describe, expect, it } from 'vitest';
import { shortcutPolicyError, shortcutSignature } from '@root/native/shortcut-policy';

describe('platform reserved shortcut comparison', () => {
  it.each(['Command', 'Cmd', 'CommandOrControl', 'CmdOrCtrl', 'Super', 'Meta'])(
    'maps %s to the macOS Command key, case-insensitively', modifier => {
      expect(shortcutPolicyError(` c + ${modifier.toLowerCase()} `, 'darwin')).toMatch(/保留/);
    });

  it('normalizes Option/Alt and modifier order without treating extra modifiers as equivalent', () => {
    expect(shortcutSignature('Option+Shift+Cmd+K', 'darwin'))
      .toBe(shortcutSignature('k+COMMAND+shift+alt', 'darwin'));
    expect(shortcutSignature('CommandOrControl+K', 'darwin'))
      .not.toBe(shortcutSignature('Control+K', 'darwin'));
    expect(shortcutPolicyError('Command+Shift+Space', 'darwin')).toBeNull();
    expect(shortcutPolicyError('Command+Shift+9', 'darwin')).toBeNull();
  });

  it.each(['Command+C', 'Command+V', 'Command+X', 'Command+A', 'Command+S',
    'Command+Z', 'Command+W', 'Command+Q'])('covers the approved minimum %s', shortcut => {
      expect(shortcutPolicyError(shortcut, 'darwin')).toMatch(/保留/);
    });

  it('uses Windows Control and Super meanings only for its selected policy', () => {
    expect(shortcutSignature('CmdOrCtrl+C', 'win32')).toBe(shortcutSignature('Ctrl+C', 'win32'));
    expect(shortcutPolicyError('CommandOrControl+C', 'win32')).toMatch(/保留/);
    expect(shortcutPolicyError('Meta+L', 'win32')).toMatch(/保留/);
    expect(shortcutPolicyError('Meta+L', 'darwin')).toBeNull();
    // Pure table tests do not claim Windows native/platform acceptance.
  });
});

it.each(['Shift+Space','Space + shift','Ctrl+Space','Control+Space','CmdOrCtrl+Space'])(
  'Windows reserves IME combination %s while retaining the candidate dictation chord', key => {
    expect(shortcutPolicyError(key, 'win32')).not.toBeNull();
    expect(shortcutPolicyError('Control+Shift+Space', 'win32')).toBeNull();
  });
