import { EventEmitter } from 'node:events';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ spawn: vi.fn() }));
vi.mock('node:child_process', () => ({ spawn: mocks.spawn }));
vi.mock('electron', () => ({ app: { isPackaged: false, getAppPath: () => '/tmp/svi-native-test' } }));
import { NativeCallError, NativeVoiceInput } from './native-input';

function child() {
  return Object.assign(new EventEmitter(), { pid: 1234,
    stdin: Object.assign(new EventEmitter(), { write: vi.fn() }),
    stdout: new EventEmitter(), stderr: { resume: vi.fn() }, kill: vi.fn() });
}
let bridge: NativeVoiceInput;
let children: ReturnType<typeof child>[];
const platformDescriptor = Object.getOwnPropertyDescriptor(process, 'platform')!;
beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(process, 'platform', { ...platformDescriptor, value: 'darwin' });
  children = [];
  mocks.spawn.mockReset().mockImplementation(() => { const c = child(); children.push(c); return c; });
  bridge = new NativeVoiceInput();
});
afterEach(() => {
  bridge.dispose();
  vi.useRealTimers();
  Object.defineProperty(process, 'platform', platformDescriptor);
});
function reply(c: ReturnType<typeof child>, body: object) {
  const request = JSON.parse(c.stdin.write.mock.calls.at(-1)![0] as string);
  c.stdout.emit('data', Buffer.from(`${JSON.stringify({ id: request.id, ...body })}\n`));
}

describe('NativeVoiceInput failure effects and generation', () => {
  it('accepts helper-proven zero-write failure as none', async () => {
    const result = bridge.call('insert', { target: 'a', text: 'text' }).catch(e => e);
    reply(children[0], { error: { code: 'TARGET_CHANGED', message: 'changed', effect: 'none' } });
    expect(await result).toMatchObject({ code: 'TARGET_CHANGED', effect: 'none', action: 'insert' });
  });

  it.each([{ error: 'legacy failure' }, { error: { code: 'WRITE_ERROR', message: 'error' } }])(
    'treats an unstructured insert failure as unknown', async body => {
      const result = bridge.call('insert').catch(e => e);
      reply(children[0], body);
      expect(await result).toMatchObject({ effect: 'unknown', action: 'insert' });
    });

  it.each(['insert', 'insertCurrent'])('%s returns unverified without promoting missing effect to zero-write', async action => {
    const result = bridge.call(action);
    reply(children[0], { status: 'unverified', observable: false });
    expect(await result).toMatchObject({ status: 'unverified', effect: 'unknown' });
  });

  it.each(['insert', 'insertCurrent'])('%s requires written effect and readback before accepting inserted', async action => {
    const result = bridge.call(action);
    reply(children[0], { status: 'inserted', observable: true });
    expect(await result).toMatchObject({ status: 'unverified', effect: 'unknown', observable: false });
  });

  it.each(['insert', 'insertCurrent'])('%s is not replayed after timeout and resets the helper', async action => {
    const result = bridge.call(action).catch(e => e);
    await vi.advanceTimersByTimeAsync(5000);
    expect(await result).toBeInstanceOf(NativeCallError);
    expect(await result).toMatchObject({ code: 'NATIVE_TIMEOUT', effect: 'unknown' });
    expect(children[0].kill).toHaveBeenCalledTimes(1);
    expect(children[0].stdin.write).toHaveBeenCalledTimes(1);
  });

  it('invalidates pending writes conservatively and ignores stale child data', async () => {
    const pending = bridge.call('insert').catch(e => e);
    bridge.invalidate('account changed');
    expect(await pending).toMatchObject({ code: 'NATIVE_INVALIDATED', effect: 'unknown' });
    const permission = bridge.call('permission');
    expect(children).toHaveLength(2);
    reply(children[0], { trusted: false });
    reply(children[1], { trusted: true, supported: true });
    expect(await permission).toMatchObject({ trusted: true });
    expect(children[0].stdin.write).toHaveBeenCalledTimes(1);
  });

  it('returns proven zero-write if no child process could start', async () => {
    const result = bridge.call('insert').catch(e => e);
    children[0].pid = undefined as unknown as number;
    children[0].emit('error', new Error('ENOENT'));
    expect(await result).toMatchObject({ code: 'NATIVE_UNAVAILABLE', effect: 'none' });
  });

  it('treats pipe failure after dispatch as unknown', async () => {
    const result = bridge.call('insert').catch(e => e);
    children[0].stdin.emit('error', new Error('EPIPE'));
    expect(await result).toMatchObject({ code: 'NATIVE_DISCONNECTED', effect: 'unknown' });
  });

  it('rejects oversized or unserializable requests before starting a process', async () => {
    await expect(bridge.call('insert', { text: 'x'.repeat(1024 * 1024) })).rejects.toMatchObject({ effect: 'none' });
    const cyclic: Record<string, unknown> = {}; cyclic.self = cyclic;
    await expect(bridge.call('insert', cyclic)).rejects.toMatchObject({ effect: 'none' });
    expect(mocks.spawn).not.toHaveBeenCalled();
  });

  it('does not allow caller data to override action or correlation ID', async () => {
    const result = bridge.call('permission', { id: 99, action: 'insert' });
    expect(JSON.parse(children[0].stdin.write.mock.calls[0][0] as string)).toMatchObject({ id: 1, action: 'permission' });
    reply(children[0], { trusted: true }); await result;
  });

  it('handles chunked UTF-8 JSON lines and bounds malformed responses', async () => {
    const result = bridge.call('insert');
    const payload = Buffer.from(JSON.stringify({ id: 1, status: 'unverified', message: '未知', effect: 'unknown' }) + '\n');
    const at = payload.indexOf(Buffer.from('未知')) + 1;
    children[0].stdout.emit('data', payload.subarray(0, at));
    children[0].stdout.emit('data', payload.subarray(at));
    expect(await result).toMatchObject({ message: '未知' });
    const malformed = bridge.call('insert').catch(e => e);
    children[0].stdout.emit('data', Buffer.alloc(1024 * 1024 + 1, 120));
    expect(await malformed).toMatchObject({ code: 'NATIVE_PROTOCOL_ERROR', effect: 'unknown' });
  });

  it('permanently closes only on dispose', async () => {
    bridge.dispose();
    await expect(bridge.call('insert')).rejects.toMatchObject({ code: 'NATIVE_CLOSED', effect: 'none' });
    expect(mocks.spawn).not.toHaveBeenCalled();
  });
});


describe('live Accessibility permission refresh', () => {
  it('checks each permission read in a fresh probe and disposes both probes', async () => {
    const denied = bridge.call('permission', { prompt: false });
    reply(children[0], { trusted: false, supported: true });
    expect(await denied).toMatchObject({ trusted: false });
    const granted = bridge.call('permission', { prompt: false });
    expect(children).toHaveLength(2);
    reply(children[1], { trusted: true, supported: true });
    expect(await granted).toMatchObject({ trusted: true });
    expect(children.every(c => c.kill.mock.calls.length === 1)).toBe(true);
  });

  it('refreshes a denied insertion helper before new capture without restarting the app', async () => {
    const capture = bridge.call('capture').catch(e => e);
    reply(children[0], { error: { code: 'PERMISSION_REQUIRED', message: 'denied', effect: 'none' } });
    await capture;
    const permission = bridge.call('permission');
    reply(children[1], { trusted: true, supported: true }); await permission;
    const current = bridge.call('capture');
    expect(children).toHaveLength(3);
    expect(children[0].kill).toHaveBeenCalledOnce();
    reply(children[2], { target: 'fresh-authorized-target', observable: true });
    expect(await current).toMatchObject({ target: 'fresh-authorized-target' });
    expect(JSON.parse(children[2].stdin.write.mock.calls[0][0]).action).toBe('capture');
  });

  it('does not interrupt or replay an in-flight paste when trust changes', async () => {
    const write = bridge.call('insertCurrent', { text: 'fictional text' });
    const permission = bridge.call('permission');
    reply(children[1], { trusted: true, supported: true }); await permission;
    expect(children[0].kill).not.toHaveBeenCalled();
    await expect(bridge.call('insertCurrent')).rejects.toMatchObject({ code: 'NATIVE_REFRESH_PENDING', effect: 'none' });
    expect(children[0].stdin.write).toHaveBeenCalledOnce();
    reply(children[0], { status: 'inserted', effect: 'written', observable: true });
    expect(await write).toMatchObject({ status: 'inserted' });
    const next = bridge.call('capture');
    expect(children).toHaveLength(3);
    reply(children[2], { target: 'next-target' }); await next;
    expect(children[0].stdin.write).toHaveBeenCalledOnce();
  });

  it('parent disposal closes an outstanding isolated permission probe', async () => {
    const permission = bridge.call('permission').catch(e => e);
    bridge.dispose();
    expect(await permission).toMatchObject({ code: 'NATIVE_CLOSED', effect: 'none' });
    expect(children[0].kill).toHaveBeenCalledOnce();
  });
});
