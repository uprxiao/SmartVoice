import { describe, expect, it } from 'vitest';
import { acceleratorFromKeyEvent, friendlyShortcut } from './shortcut';
import { shortcutPolicyError } from '@root/native/shortcut-policy';

const keyEvent = (key: string, code: string, patch: Partial<KeyboardEvent> = {}) => ({
  key, code, metaKey: true, ctrlKey: false, altKey: false, shiftKey: false, ...patch,
});
describe('shortcut recorder preserves the underlying chord', () => {
  it.each([
    ['#', 'Digit3', { shiftKey: true }, 'CommandOrControl+Shift+3', true],
    ['(', 'Digit9', { shiftKey: true }, 'CommandOrControl+Shift+9', false],
    ['ç', 'KeyC', { altKey: true }, 'CommandOrControl+Alt+C', false],
    ['<', 'Comma', { shiftKey: true }, 'CommandOrControl+Shift+,', false],
    ['c', 'KeyC', {}, 'CommandOrControl+C', true],
  ] as const)('%s/%s records %s', (key, code, patch, expected, reserved) => {
    const chord = acceleratorFromKeyEvent(keyEvent(key, code, patch));
    expect(chord).toBe(expected);
    expect(Boolean(shortcutPolicyError(chord!, 'darwin'))).toBe(reserved);
  });

  it.each(['Escape', 'Meta', 'Control', 'Shift', 'Alt'])('does not save %s alone', key => {
    expect(acceleratorFromKeyEvent(keyEvent(key, key))).toBeNull();
  });
  it('does not turn plain typing into a global shortcut', () => {
    expect(acceleratorFromKeyEvent(keyEvent('c', 'KeyC', { metaKey: false }))).toBeNull();
  });
});

it('Windows renders actual key names and records Win separately from Ctrl', () => {
  expect(friendlyShortcut('CommandOrControl+Shift+Space', 'win32')).toBe('Ctrl + Shift + Space');
  expect(friendlyShortcut('Super+Alt+X', 'win32')).toBe('Win + Alt + X');
  expect(acceleratorFromKeyEvent({key:'x',code:'KeyX',metaKey:true,ctrlKey:false,altKey:false,shiftKey:false},'win32')).toBe('Super+X');
});
