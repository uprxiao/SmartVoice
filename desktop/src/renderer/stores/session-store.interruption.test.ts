import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { AuthSnapshot, SessionProgress } from '@shared/types';
const doubles = vi.hoisted(() => ({ listeners: new Map<string, (value: unknown) => void>(), stop: vi.fn().mockResolvedValue(undefined) }));
vi.mock('../audio/recorder', async importOriginal => ({ ...await importOriginal<typeof import('../audio/recorder')>(), PcmRecorder: class { start = vi.fn().mockResolvedValue(undefined); stop = doubles.stop; acknowledge = vi.fn(); } }));
vi.mock('../lib/ipc', () => ({
  events: { on: (event: { name: string }, listener: (value: unknown) => void) => { doubles.listeners.set(event.name, listener); return () => doubles.listeners.delete(event.name); }, emit: vi.fn() },
  rpc: { session: { status: vi.fn(), start: vi.fn(), captureReady: vi.fn(), retry: vi.fn() } },
}));
import { rpc } from '../lib/ipc';
import { AuthStore } from './auth-store';
import { SessionStore } from './session-store';
import type { UiStore } from './ui-store';
const SID = '22222222-2222-4222-8222-222222222222';
const progress = (code: string): SessionProgress => ({ acknowledged_samples: 8000, session_id: SID, attempt_no: 1, revision: 2, auto_insert_allowed: false, state: 'failed', audio_ms: 500, stable_text: '', provisional_text: '', error_code: code, result: null });
const active: SessionStore[] = [];
beforeEach(() => {
  vi.clearAllMocks(); doubles.listeners.clear();
  vi.mocked(rpc.session.status).mockResolvedValue({ ...progress(''), session_id: null, state: 'created' });
  vi.mocked(rpc.session.start).mockResolvedValue({ session_id: SID, attempt_no: 1, insertion_available: false, target_app: null, target_error: null });
  vi.mocked(rpc.session.captureReady).mockResolvedValue({ ...progress(''), state: 'streaming', capture_ready: true, acknowledged_samples: 0, audio_ms: 0, error_code: null, revision: 1 });
  vi.mocked(rpc.session.retry).mockResolvedValue({ ...progress(''), revision: 3, error_code: null });
});
afterEach(() => { for (const store of active.splice(0)) store.invalidateLocal(); });
async function fixture(locale: 'zh-CN' | 'en') {
  const auth = new AuthStore(); auth.snapshot = { user: { id: '11111111-1111-4111-8111-111111111111' }, settings: { learning_enabled: true, locale } } as AuthSnapshot;
  const ui = { toast: vi.fn() }; const store = new SessionStore(auth, ui as unknown as UiStore); active.push(store); store.init(); await store.start();
  return { store, ui };
}
it.each([
  ['zh-CN', '录音上传已中断，请检查网络并重新录音。'],
  ['en', 'Audio upload was interrupted. Check your connection and record again.'],
] as const)('requires a new recording after an incomplete upload (%s)', async (locale, message) => {
  const { store, ui } = await fixture(locale);
  doubles.listeners.get('session.result')?.(progress('AUDIO_INTERRUPTED'));
  expect.soft(store.phase).toBe('failed');
  expect.soft(store.progress.error_code).toBe('AUDIO_INTERRUPTED');
  expect.soft(store.error).toBe(message);
  expect.soft(store.retryAvailable).toBe(false);
  expect.soft(doubles.stop).toHaveBeenLastCalledWith({ discard: true });
  expect.soft(ui.toast).toHaveBeenLastCalledWith(message, 'error');
});
it('does not dispatch recognition retry for an interrupted upload', async () => {
  const { store } = await fixture('zh-CN'); doubles.listeners.get('session.progress')?.(progress('AUDIO_INTERRUPTED'));
  await store.retry('asr');
  expect(rpc.session.retry).not.toHaveBeenCalled();
  expect(store.progress.session_id).toBe(SID); expect(store.phase).toBe('failed');
});
it('keeps the existing retry path for a complete ASR failure', async () => {
  const { store } = await fixture('zh-CN'); doubles.listeners.get('session.progress')?.(progress('ASR_TIMEOUT'));
  expect(store.retryAvailable).toBe(true); await store.retry('asr'); expect(rpc.session.retry).toHaveBeenCalledExactlyOnceWith(SID, 'asr');
});
