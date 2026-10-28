import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// This is our own app's registered-key port state, not a model of macOS or
// a third-party conflict detector. No Electron process or OS key event exists.
const fixture = vi.hoisted(() => ({
  directory: '',
  registered: new Map<string, () => void>(),
  registrationAttempts: [] as string[],
  denied: new Set<string>(),
  failNextWriteAfterPartial: false,
  failNextRename: false,
  fixedUuid: null as string | null,
  partialPath: null as string | null,
}));
vi.mock('electron', () => ({
  app: {
    getPath(name: string) {
      if (name !== 'userData' || !fixture.directory) throw new Error('owned userData is required');
      return fixture.directory;
    },
  },
  globalShortcut: {
    register(accelerator: string, callback: () => void) {
      fixture.registrationAttempts.push(accelerator);
      if (fixture.denied.has(accelerator) || fixture.registered.has(accelerator)) return false;
      fixture.registered.set(accelerator, callback);
      return true;
    },
    unregister(accelerator: string) { fixture.registered.delete(accelerator); },
    unregisterAll() { fixture.registered.clear(); },
    isRegistered(accelerator: string) { return fixture.registered.has(accelerator); },
  },
}));
vi.mock('node:crypto', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:crypto')>();
  return { ...actual, randomUUID: () => fixture.fixedUuid ?? actual.randomUUID() };
});
vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('node:fs')>();
  return {
    ...actual,
    writeFileSync(...args: Parameters<typeof actual.writeFileSync>) {
      const [path, , options] = args;
      if (fixture.failNextWriteAfterPartial && typeof path === 'string'
          && path.startsWith(`${fixture.directory}/`)) {
        fixture.failNextWriteAfterPartial = false;
        // A real, short write to this test-owned path occurs before failure.
        // With the old implementation this truncates settings.json. With the
        // candidate it affects only the newly owned temporary settings file.
        actual.writeFileSync(path, '{partial', options);
        fixture.partialPath = path;
        throw Object.assign(new Error('controlled settings partial write'), { code: 'EACCES' });
      }
      return actual.writeFileSync(...args);
    },
    renameSync(...args: Parameters<typeof actual.renameSync>) {
      if (fixture.failNextRename) {
        fixture.failNextRename = false;
        throw Object.assign(new Error('controlled settings rename'), { code: 'EACCES' });
      }
      return actual.renameSync(...args);
    },
  };
});

// Root copies this same test into each fresh Desktop snapshot, next to the
// actual baseline/candidate module. The class and its results are not mocked.
import { ShortcutService } from './shortcut-service';

const K1 = 'F19';
const K2 = 'F18';
let file: string;
let original: Buffer;
let service: ShortcutService;
let callback: ReturnType<typeof vi.fn<() => void>>;

beforeEach(() => {
  fixture.directory = mkdtempSync(join(tmpdir(), 'svi-shortcut-owned-unit-'));
  fixture.registered.clear();
  fixture.registrationAttempts.length = 0;
  fixture.denied.clear();
  fixture.failNextWriteAfterPartial = false;
  fixture.failNextRename = false;
  fixture.fixedUuid = null;
  fixture.partialPath = null;
  file = join(fixture.directory, 'settings.json');
  original = Buffer.from(JSON.stringify({ shortcut: K1 }, null, 2));
  writeFileSync(file, original, { mode: 0o600 });
  service = new ShortcutService();
  callback = vi.fn<() => void>();
  service.setCallback(callback);
  expect([...fixture.registered.keys()]).toEqual([K1]);
});
afterEach(() => {
  service?.dispose();
  // Only this test's fresh directory is removed; no real application profile.
  if (fixture.directory) rmSync(fixture.directory, { recursive: true, force: true });
  fixture.directory = '';
});

const expectOriginalStillUsable = () => {
  expect.soft(service.current).toBe(K1);
  expect.soft(readFileSync(file)).toEqual(original);
  expect.soft([...fixture.registered.keys()]).toEqual([K1]);
  const installed = fixture.registered.get(K1);
  expect.soft(installed).toBe(callback);
  if (installed) installed(); // Direct JS callback only, never an OS key event.
  expect.soft(callback).toHaveBeenCalledTimes(1);
};

describe('ShortcutService update keeps its registered key and durable settings consistent', () => {
  it('keeps the original key when the candidate and any attempted old-key re-registration return false', () => {
    // These are controlled register return values, not detected OS occupancy.
    fixture.denied.add(K2);
    fixture.denied.add(K1);
    expect(() => service.update(K2)).toThrow();
    expectOriginalStillUsable();
    expect(readdirSync(fixture.directory)).toEqual(['settings.json']);
  });

  it.each(['partial write', 'rename'] as const)('preserves the original key and complete settings after %s fails', (failure) => {
    if (failure === 'partial write') fixture.failNextWriteAfterPartial = true;
    else fixture.failNextRename = true;
    expect(() => service.update(K2)).toThrow(`controlled settings ${failure}`);
    if (failure === 'partial write') expect(fixture.partialPath).not.toBeNull();
    expectOriginalStillUsable();
    expect(readdirSync(fixture.directory)).toEqual(['settings.json']);
    // A consumed/removed own temporary is absent; do not demand a particular
    // random filename or number of filesystem calls from the implementation.
    if (fixture.partialPath !== null && fixture.partialPath !== file) {
      expect(existsSync(fixture.partialPath)).toBe(false);
    }
  });

  it('does not remove an existing path when the exclusive temporary write rejects a UUID collision', () => {
    fixture.fixedUuid = '11111111-1111-4111-8111-111111111111';
    const existing = `${file}.${fixture.fixedUuid}.tmp`;
    const unrelated = Buffer.from('existing test-owned file must remain unchanged');
    writeFileSync(existing, unrelated, { mode: 0o600, flag: 'wx' });
    expect(() => service.update(K2)).toThrow();
    expectOriginalStillUsable();
    expect(existsSync(existing)).toBe(true);
    expect(readFileSync(existing)).toEqual(unrelated);
    expect(readdirSync(fixture.directory).sort()).toEqual(['settings.json', `settings.json.${fixture.fixedUuid}.tmp`].sort());
  });

  it('publishes one new registration and restores that saved key after reconstruction', () => {
    expect(service.update(K2)).toBe(K2);
    expect(service.current).toBe(K2);
    expect([...fixture.registered.keys()]).toEqual([K2]);
    expect(fixture.registered.get(K2)).toBe(callback);
    expect(JSON.parse(readFileSync(file, 'utf8'))).toEqual({ shortcut: K2 });
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(readdirSync(fixture.directory)).toEqual(['settings.json']);
    service.dispose();
    expect(fixture.registered.size).toBe(0);
    const restored = new ShortcutService();
    const restoredCallback = vi.fn();
    restored.setCallback(restoredCallback);
    service = restored;
    expect(restored.current).toBe(K2);
    expect([...fixture.registered.keys()]).toEqual([K2]);
    fixture.registered.get(K2)!();
    expect(restoredCallback).toHaveBeenCalledTimes(1);
  });
});

describe('Escape stays local to the focused application', () => {
  const escapeAccelerators = ['Escape', 'Esc', 'escape', 'ESC', ' Shift+Escape ',
    'CommandOrControl+Esc', 'Option+ Escape', 'Escape+Control'];

  it.each(escapeAccelerators)('rejects %s before attempting global registration or changing settings', (accelerator) => {
    const attempts = [...fixture.registrationAttempts];
    let failure: unknown;
    try { service.update(accelerator); } catch (error) { failure = error; }
    expect.soft(failure).toBeInstanceOf(Error);
    expect.soft(failure instanceof Error ? failure.message : '').toMatch(/Escape/);
    expect.soft(fixture.registrationAttempts).toEqual(attempts);
    expectOriginalStillUsable();
    expect(readdirSync(fixture.directory)).toEqual(['settings.json']);
  });

  it.each(escapeAccelerators)('uses the default for persisted %s without rewriting the saved file', (accelerator) => {
    service.dispose();
    const saved = Buffer.from(JSON.stringify({ shortcut: accelerator }, null, 2));
    writeFileSync(file, saved, { mode: 0o600 });
    const attempts = fixture.registrationAttempts.length;
    service = new ShortcutService();
    service.setCallback(callback);
    expect.soft(service.current).toBe('CommandOrControl+Shift+Space');
    expect.soft(fixture.registrationAttempts.slice(attempts)).toEqual(['CommandOrControl+Shift+Space']);
    expect.soft([...fixture.registered.keys()]).toEqual(['CommandOrControl+Shift+Space']);
    expect.soft(readFileSync(file)).toEqual(saved);
    expect(readdirSync(fixture.directory)).toEqual(['settings.json']);
    const installed = fixture.registered.get('CommandOrControl+Shift+Space');
    expect.soft(installed).toBe(callback);
    installed?.();
    expect.soft(callback).toHaveBeenCalledTimes(1);
  });
});

describe('known reserved shortcuts are rejected without probing other applications', () => {
  it.each(['Command+C', 'Cmd+v', 'CommandOrControl+X', 'c+COMMAND',
    ' Meta + A ', 'Super+S', 'shift+cmd+z', 'Command+Tab', 'Command+Space',
    'F5', 'Return', 'Space'])('keeps the old registration and file for %s', (accelerator) => {
    const attempts = [...fixture.registrationAttempts];
    expect(() => service.update(accelerator)).toThrow(/保留/);
    expect(fixture.registrationAttempts).toEqual(attempts);
    expectOriginalStillUsable();
    expect(readdirSync(fixture.directory)).toEqual(['settings.json']);
  });

  it('does not mistake an equivalent spelling of the current key for a new registration', () => {
    const attempts = [...fixture.registrationAttempts];
    expect(service.update('f19')).toBe(K1);
    expect(fixture.registrationAttempts).toEqual(attempts);
    expectOriginalStillUsable();
  });

  it('reports an explicit registration failure without asserting third-party ownership', () => {
    fixture.denied.add(K2);
    expect(() => service.update(K2)).toThrow(/不可用/);
    expectOriginalStillUsable();
  });

  it('falls back from a legacy reserved binding without rewriting the saved file', () => {
    service.dispose();
    const saved = Buffer.from(JSON.stringify({ shortcut: 'c+cmd' }));
    writeFileSync(file, saved);
    const attempts = fixture.registrationAttempts.length;
    service = new ShortcutService();
    service.setCallback(callback);
    expect(service.current).toBe('CommandOrControl+Shift+Space');
    expect(fixture.registrationAttempts.slice(attempts)).toEqual(['CommandOrControl+Shift+Space']);
    expect(readFileSync(file)).toEqual(saved);
  });
});
