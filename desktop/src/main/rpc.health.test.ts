import { describe, expect, it, vi } from 'vitest';

vi.mock('electron', () => ({ app: {}, dialog: {}, shell: {}, systemPreferences: { getMediaAccessStatus:()=> 'granted' } }));
import type { ApiClient } from './core/api-client';
import { createRpcRouter, type RpcDeps } from './rpc';

type Capabilities = Awaited<ReturnType<ApiClient['capabilities']>>;
function capabilities(patch: Partial<Capabilities> = {}): Capabilities {
  return { api_version: 'v1', ready: false, degraded: true, unavailable_reason: 'STARTING',
    asr: { provider: 'fixture-asr', model_id: 'frozen-speech-model', stable_partial: true },
    llm: { provider: 'fixture-llm', model_id: 'frozen-text-model', available: false },
    limits: { global_sessions: 20, active_sessions_per_user: 1 }, ...patch };
}
function fixture(caps = capabilities()) {
  const api = { capabilities: vi.fn().mockResolvedValue(caps) };
  const native = { call: vi.fn() };
  const sessions = { isBusy: () => false, start: vi.fn() };
  const auth = { current: { user: { id: 'fixture-user' } }, ensure: () => ({ user: { id: 'fixture-user' } }), contextVersion: 1 };
  return { api, native, sessions,
    router: createRpcRouter({ api, native, sessions, auth } as unknown as RpcDeps) };
}

// Invokes the actual Main router; declared API outcomes are not a live HTTP/GUI claim.
describe('NFR02 Main health keeps model loading distinct from connection checking', () => {
  it.each([true, false])('STARTING is loading without granting ready (degraded=%s)', async degraded => {
    const f = fixture(capabilities({ degraded }));
    const health = await f.router.app.health();
    expect(health).toEqual({ status: 'loading', ready: false, degraded, reason: 'STARTING',
      asrModel: 'frozen-speech-model', llmModel: 'frozen-text-model', llmAvailable: false,
      checkedAt: expect.any(String) });
    expect(f.api.capabilities).toHaveBeenCalledOnce();
    expect(f.native.call).not.toHaveBeenCalled(); expect(f.sessions.start).not.toHaveBeenCalled();
  });

  it.each([
    { ready: true, degraded: false, unavailable_reason: null, expected: 'ready' },
    { ready: true, degraded: true, unavailable_reason: 'LLM_UNAVAILABLE', expected: 'ready' },
    { ready: false, degraded: true, unavailable_reason: 'ASR_UNAVAILABLE', expected: 'degraded' },
    { ready: false, degraded: false, unavailable_reason: 'ASR_UNAVAILABLE', expected: 'error' },
  ])('preserves $expected for existing ready=$ready / $unavailable_reason', async ({ expected, ...patch }) => {
    const health = await fixture(capabilities(patch)).router.app.health();
    expect(health.status).toBe(expected);
    expect(health.ready).toBe(patch.ready); expect(health.degraded).toBe(patch.degraded);
    expect(health.reason).toBe(patch.unavailable_reason);
  });

  it.each([
    { error: Object.assign(new Error('服务尚未就绪'), { status: 503 }), expected: 'error' },
    { error: new Error('fetch failed'), expected: 'unreachable' },
  ])('preserves $expected without inventing a STARTING response', async ({ error, expected }) => {
    const f = fixture(); f.api.capabilities.mockRejectedValueOnce(error);
    const health = await f.router.app.health();
    expect(health).toMatchObject({ status: expected, ready: false, degraded: false,
      reason: error.message, asrModel: null, llmModel: null, llmAvailable: null });
  });

  it('does not convert the existing start command 503 into accepted work', async () => {
    const f = fixture(); const rejection = Object.assign(new Error('语音服务尚未就绪'), { status: 503 });
    f.sessions.start.mockRejectedValueOnce(rejection);
    await expect(f.router.session.start(true, '11111111-1111-4111-8111-111111111111')).rejects.toBe(rejection);
    expect(f.sessions.start).toHaveBeenCalledOnce();
  });
});
