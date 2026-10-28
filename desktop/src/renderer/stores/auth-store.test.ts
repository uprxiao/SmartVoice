import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AuthSnapshot, Settings } from '@shared/types';
vi.mock('../lib/ipc', () => ({ rpc: { auth: { me: vi.fn(), login: vi.fn(), register: vi.fn(), logout: vi.fn() }, settings: { update: vi.fn() } } }));
import { rpc } from '../lib/ipc';
import { AuthStore } from './auth-store';
const deferred = <T>() => { let resolve!: (x: T) => void; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; };
const snapshot = (id: string) => ({ user: { id }, settings: { version: 1, learning_enabled: true, locale: 'zh-CN' } } as AuthSnapshot);
afterEach(() => { vi.clearAllMocks(); });
describe('renderer ignores stale account results (AUTH-16)', () => {
  it('an old boot response cannot replace a completed login', async () => {
    const boot = deferred<AuthSnapshot | null>(); vi.mocked(rpc.auth.me).mockReturnValueOnce(boot.promise);
    vi.mocked(rpc.auth.login).mockResolvedValueOnce(snapshot('new'));
    const store = new AuthStore(); const pending = store.boot(); await store.login('new@example.test', 'DemoPass123');
    boot.resolve(snapshot('old')); await pending; expect(store.user?.id).toBe('new'); expect(store.booting).toBe(false);
  });
  it('a slow login cannot overwrite a newer account or busy state', async () => {
    const a = deferred<AuthSnapshot>(); const b = deferred<AuthSnapshot>();
    vi.mocked(rpc.auth.login).mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const store = new AuthStore(); const first = store.login('a@example.test', 'DemoPass123'); const second = store.login('b@example.test', 'DemoPass123');
    b.resolve(snapshot('b')); await second; a.resolve(snapshot('a')); await first;
    expect(store.user?.id).toBe('b'); expect(store.busy).toBe(false);
  });
  it('logout hides account state immediately and rejects old login display results', async () => {
    const a = deferred<AuthSnapshot>(); const out = deferred<void>();
    vi.mocked(rpc.auth.login).mockReturnValueOnce(a.promise); vi.mocked(rpc.auth.logout).mockReturnValueOnce(out.promise);
    const store = new AuthStore(); const login = store.login('a@example.test', 'DemoPass123'); const logout = store.logout();
    expect(store.snapshot).toBeNull(); a.resolve(snapshot('a')); await login; expect(store.snapshot).toBeNull();
    out.resolve(); await logout; expect(store.snapshot).toBeNull();
  });
  it('late settings responses do not modify a subsequent account', async () => {
    const store = new AuthStore(); vi.mocked(rpc.auth.login).mockResolvedValueOnce(snapshot('a')).mockResolvedValueOnce(snapshot('b'));
    await store.login('a@example.test', 'DemoPass123');
    const settings = deferred<Settings>(); vi.mocked(rpc.settings.update).mockReturnValueOnce(settings.promise);
    const update = store.updateSettings({ version: 1, learning_enabled: false }); await store.login('b@example.test', 'DemoPass123');
    settings.resolve({ ...snapshot('a').settings, learning_enabled: false, version: 2 }); await update;
    expect(store.user?.id).toBe('b'); expect(store.settings?.learning_enabled).toBe(true);
  });
});
