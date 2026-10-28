import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AuthSnapshot, Settings } from '@shared/types';
vi.mock('electron', () => ({ app: { getPath: () => '' }, safeStorage: {} }));
import { AuthService } from './auth-service';
import { ApiClient, AuthContextChangedError } from './api-client';
import { TokenStore } from './token-store';
const deferred = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; };
const snapshot = (id: string) => ({ user: { id }, settings: { version: 1, learning_enabled: true } } as AuthSnapshot);
const setup = () => {
  const api = { login: vi.fn().mockResolvedValue({}), logout: vi.fn().mockResolvedValue(undefined), register: vi.fn().mockResolvedValue({}), me: vi.fn(), patchSettings: vi.fn(), clearTokens: vi.fn(), setTokens: vi.fn(), hasSession: vi.fn().mockReturnValue(true) };
  const tokens = { load: vi.fn(), canPersist: true };
  const changed = vi.fn();
  return { api, tokens, changed, service: new AuthService(api as unknown as ApiClient, tokens as unknown as TokenStore, changed) };
};
afterEach(() => vi.restoreAllMocks());
describe('main account publication boundaries (AUTH-16)', () => {
  it('a delayed previous login cannot publish over a new login', async () => {
    const {api, service} = setup(); const old = deferred<AuthSnapshot>();
    api.me.mockReturnValueOnce(old.promise).mockResolvedValueOnce(snapshot('new'));
    const first = service.login('old@example.test', 'DemoPass123'); await Promise.resolve();
    await service.login('new@example.test', 'DemoPass123');
    old.resolve(snapshot('old')); await expect(first).rejects.toBeInstanceOf(AuthContextChangedError);
    expect(service.current?.user.id).toBe('new');
  });
  it('a delayed logout finally cannot clear a newer account', async () => {
    const {api, service} = setup(); api.me.mockResolvedValueOnce(snapshot('old')).mockResolvedValueOnce(snapshot('new'));
    await service.login('old@example.test', 'DemoPass123');
    const logout = deferred<void>(); api.logout.mockReturnValueOnce(logout.promise);
    const pending = service.logout(); expect(service.current).toBeNull();
    await service.login('new@example.test', 'DemoPass123'); logout.resolve(); await pending;
    expect(service.current?.user.id).toBe('new');
  });
  it('late old settings cannot modify the newly authenticated account', async () => {
    const {api, service} = setup(); api.me.mockResolvedValueOnce(snapshot('old')).mockResolvedValueOnce(snapshot('new'));
    await service.login('old@example.test', 'DemoPass123'); const settings = deferred<Settings>(); api.patchSettings.mockReturnValueOnce(settings.promise);
    const pending = service.updateSettings({version: 1, learning_enabled: false});
    await service.login('new@example.test', 'DemoPass123'); settings.resolve({...snapshot('old').settings, learning_enabled: false});
    await expect(pending).rejects.toBeInstanceOf(AuthContextChangedError); expect(service.current?.settings.learning_enabled).toBe(true);
  });
  it('invalidating credentials prevents a pending restored snapshot from resurrecting them', async () => {
    const {api, tokens, service} = setup(); tokens.load.mockReturnValue({access: 'a', refresh: 'r'});
    const old = deferred<AuthSnapshot>(); api.me.mockReturnValueOnce(old.promise); const restore = service.restore();
    service.invalidate(); old.resolve(snapshot('old')); await restore;
    expect(service.current).toBeNull(); expect(api.clearTokens).not.toHaveBeenCalled();
  });
  it('a late registration cannot automatically log in after logout', async () => {
    const {api, service} = setup(); const registered = deferred<unknown>(); api.register.mockReturnValueOnce(registered.promise);
    const pending = service.register('old@example.test', 'DemoPass123'); await service.logout(); registered.resolve({});
    await expect(pending).rejects.toBeInstanceOf(AuthContextChangedError); expect(api.login).not.toHaveBeenCalled();
  });
  it('known missing credentials clears the cached snapshot immediately', async () => {
    const {api, service} = setup(); api.me.mockResolvedValueOnce(snapshot('old')); await service.login('old@example.test', 'DemoPass123');
    api.hasSession.mockReturnValue(false); expect(await service.refreshSnapshot()).toBeNull(); expect(service.current).toBeNull();
  });
});
