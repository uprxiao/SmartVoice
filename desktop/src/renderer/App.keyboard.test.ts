import { afterEach, beforeEach, expect, it, vi } from 'vitest';

// Execute the production App effect against a local EventTarget. This covers
// handler ownership/lifetime, not real Electron focus or external OS key events.
const doubles = vi.hoisted(() => ({
  cleanup: undefined as (() => void) | undefined,
  stores: {
    auth: { user: { id: 'owned-user' } as { id: string } | null, booting: true, boot: vi.fn() },
    session: { init: vi.fn(), phase: 'idle', cancel: vi.fn().mockResolvedValue(undefined) },
    stats: { clear: vi.fn() }, history: { clear: vi.fn() }, dictionary: { clear: vi.fn() },
    ui: { clear: vi.fn(), loadAppInfo: vi.fn().mockResolvedValue(undefined) },
  },
}));
vi.mock('react', async importOriginal => ({ ...await importOriginal<typeof import('react')>(),
  useEffect: (effect: () => (() => void)) => { doubles.cleanup = effect(); },
}));
vi.mock('mobx', () => ({ reaction: () => () => undefined }));
vi.mock('mobx-react-lite', () => ({ observer: (component: unknown) => component }));
vi.mock('./stores', () => ({ stores: doubles.stores }));
vi.mock('./lib/theme', () => ({ initTheme: vi.fn() }));
vi.mock('./lib/ipc', () => ({ events: { on: () => () => undefined } }));
vi.mock('./views/AuthView', () => ({ AuthView: () => null }));
vi.mock('./views/Shell', () => ({ Shell: () => null }));
vi.mock('./components/ToastStack', () => ({ ToastStack: () => null }));
vi.mock('./components/LearningSuggestions', () => ({ LearningSuggestions: () => null }));
import { App } from './App';

let target: EventTarget;
function escape(patch: Record<string, unknown> = {}) {
  const event = new Event('keydown', { cancelable: true });
  Object.assign(event, { key: 'Escape', repeat: false, isComposing: false }, patch);
  return event;
}
beforeEach(() => {
  vi.clearAllMocks();
  doubles.stores.auth.user = { id: 'owned-user' };
  doubles.stores.session.phase = 'idle';
  target = new EventTarget(); vi.stubGlobal('window', target);
  (App as unknown as () => unknown)();
});
afterEach(() => { doubles.cleanup?.(); doubles.cleanup = undefined; vi.unstubAllGlobals(); });

it.each(['starting', 'recording', 'finalizing', 'refining'])('cancels the current %s operation on local Escape', phase => {
  doubles.stores.session.phase = phase;
  const event = escape(); target.dispatchEvent(event);
  expect(doubles.stores.session.cancel).toHaveBeenCalledOnce();
  expect(event.defaultPrevented).toBe(true);
});
it.each(['idle', 'ready', 'empty', 'failed', 'cancelled'])('preserves a %s result on Escape', phase => {
  doubles.stores.session.phase = phase; target.dispatchEvent(escape());
  expect(doubles.stores.session.cancel).not.toHaveBeenCalled();
});
it('leaves consumed, composing, repeated and logged-out keys to their owner and removes the listener on unmount', () => {
  doubles.stores.session.phase = 'recording';
  const consumed = escape(); consumed.preventDefault(); target.dispatchEvent(consumed);
  target.dispatchEvent(escape({ isComposing: true }));
  target.dispatchEvent(escape({ repeat: true }));
  target.dispatchEvent(escape({ key: 'Enter' }));
  doubles.stores.auth.user = null; target.dispatchEvent(escape());
  doubles.stores.auth.user = { id: 'owned-user' };
  doubles.cleanup?.(); doubles.cleanup = undefined; target.dispatchEvent(escape());
  expect(doubles.stores.session.cancel).not.toHaveBeenCalled();
});
