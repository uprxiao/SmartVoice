/** Real notification/undo handlers with explicit hook/store/IPC doubles.
 * Does not prove React DOM scheduling, Electron, transport, DB or native.
 * 2026-10-20: user chose automatic additions only; uncertain suggestions have
 * no confirmation card. Legacy server confirmation has separate API tests.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import type { LearningSuggestion } from '@shared/types';
import type { Stats } from '@shared/types';
import { StatsStore } from '../stores/stats-store';

interface HookSlot {
  kind: 'state' | 'ref' | 'effect';
  value?: unknown;
  deps?: readonly unknown[];
  cleanup?: () => void;
}
interface HookOwner {
  active: boolean;
  cursor: number;
  dirty: boolean;
  slots: HookSlot[];
  effects: Array<{ index: number; deps?: readonly unknown[]; callback: () => void | (() => void) }>;
}
const controlled = vi.hoisted(() => ({
  current: null as HookOwner | null,
  listeners: new Map<string, Set<(value: unknown) => void>>(),
  auth: { user: { id: '11111111-1111-4111-8111-111111111111' } as { id: string } | null },
  toast: vi.fn(),
  load: vi.fn(),
  confirm: vi.fn(),
  undo: vi.fn(),
  statsMe: vi.fn(),
  stats: undefined as unknown as StatsStore,
}));

vi.mock('react', async () => {
  const actual = await vi.importActual<typeof import('react')>('react');
  const owner = (): HookOwner => {
    if (!controlled.current?.active) throw new Error('HOOK_WITHOUT_ACTIVE_TEST_OWNER');
    return controlled.current;
  };
  return {
    ...actual,
    useState(initial: unknown) {
      const scope = owner(), index = scope.cursor++;
      let slot = scope.slots[index];
      if (!slot) {
        slot = { kind: 'state', value: typeof initial === 'function' ? (initial as () => unknown)() : initial };
        scope.slots[index] = slot;
      }
      if (slot.kind !== 'state') throw new Error('HOOK_ORDER_CHANGED');
      const stateSlot = slot;
      const setState = (next: unknown) => {
        // React does not run a state update from an unmounted old owner. The
        // callback may still attempt global toast/reload, which must be fenced.
        if (!scope.active) return;
        const value = typeof next === 'function'
          ? (next as (previous: unknown) => unknown)(stateSlot.value) : next;
        if (!Object.is(value, stateSlot.value)) { stateSlot.value = value; scope.dirty = true; }
      };
      return [slot.value, setState];
    },
    useRef(initial: unknown) {
      const scope = owner(), index = scope.cursor++;
      let slot = scope.slots[index];
      if (!slot) { slot = { kind: 'ref', value: { current: initial } }; scope.slots[index] = slot; }
      if (slot.kind !== 'ref') throw new Error('HOOK_ORDER_CHANGED');
      return slot.value;
    },
    useEffect(callback: () => void | (() => void), deps?: readonly unknown[]) {
      const scope = owner(), index = scope.cursor++;
      const previous = scope.slots[index];
      if (previous && previous.kind !== 'effect') throw new Error('HOOK_ORDER_CHANGED');
      const changed = !previous || deps === undefined || previous.deps === undefined
        || deps.length !== previous.deps.length || deps.some((value, n) => !Object.is(value, previous.deps![n]));
      if (changed) scope.effects.push({ index, callback, deps: deps === undefined ? undefined : [...deps] });
    },
  };
});
vi.mock('mobx-react-lite', () => ({ observer: <T>(render: T): T => render }));
vi.mock('../stores', () => ({ stores: {
  auth: controlled.auth,
  dictionary: { load: controlled.load },
  get stats() { return controlled.stats; },
  ui: { toast: controlled.toast },
} }));
vi.mock('../lib/ipc', () => ({
  events: {
    on(event: { name: string }, callback: (value: unknown) => void) {
      const callbacks = controlled.listeners.get(event.name) ?? new Set<(value: unknown) => void>();
      controlled.listeners.set(event.name, callbacks); callbacks.add(callback);
      return () => { callbacks.delete(callback); if (callbacks.size === 0) controlled.listeners.delete(event.name); };
    },
  },
  rpc: { learning: { confirm: controlled.confirm, undo: controlled.undo }, stats: { me: controlled.statsMe } },
}));
import { LearningSuggestions } from './LearningSuggestions';

const U1 = '11111111-1111-4111-8111-111111111111';
const U2 = '22222222-2222-4222-8222-222222222222';
const SUGGESTION: LearningSuggestion = {
  event_id: '33333333-3333-4333-8333-333333333333',
  correction_id: '44444444-4444-4444-8444-444444444444',
  status: 'suggested', classification: 'suggest', reason: 'ambiguous', ambiguous: true,
  term: null, term_id: null, after_version: null, undone: false, undo_available: false,
  created_at: '2026-10-18T00:00:00Z', before: '云', after: '芸',
};
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const flush = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };

/** Minimal explicit hook lifecycle; effects clean on dependency change/unmount. */
class ComponentOwner {
  private readonly scope: HookOwner = { active: true, cursor: 0, dirty: false, slots: [], effects: [] };
  private tree: React.ReactNode = null;
  render(): React.ReactNode {
    if (!this.scope.active) throw new Error('RENDER_AFTER_UNMOUNT');
    for (let pass = 0; pass < 8; pass++) {
      this.scope.cursor = 0; this.scope.dirty = false; this.scope.effects = [];
      controlled.current = this.scope;
      try { this.tree = (LearningSuggestions as unknown as () => React.ReactNode)(); }
      finally { controlled.current = null; }
      const pending = this.scope.effects; this.scope.effects = [];
      for (const effect of pending) {
        this.scope.slots[effect.index]?.cleanup?.();
        const cleanup = effect.callback();
        this.scope.slots[effect.index] = { kind: 'effect', deps: effect.deps,
          cleanup: typeof cleanup === 'function' ? cleanup : undefined };
      }
      if (!this.scope.dirty) return this.tree;
    }
    throw new Error('UNBOUNDED_CONTROLLED_RENDER');
  }
  unmount(): void {
    if (!this.scope.active) return;
    this.scope.active = false;
    for (const slot of this.scope.slots) slot.cleanup?.();
  }
}
interface Element {
  type: unknown;
  props: { children?: unknown; onClick?: () => unknown; disabled?: boolean };
}
const element = (node: unknown): node is Element => Boolean(node && typeof node === 'object' && 'type' in node && 'props' in node);
function text(node: unknown): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(text).join('');
  return element(node) ? text(node.props.children) : '';
}
function buttons(node: unknown): Element[] {
  if (Array.isArray(node)) return node.flatMap(buttons);
  if (!element(node)) return [];
  return [...(node.type === 'button' ? [node] : []), ...buttons(node.props.children)];
}
const owners: ComponentOwner[] = [];
vi.stubGlobal('React', React); // Existing node config transforms imported JSX in classic mode.
afterAll(() => { vi.unstubAllGlobals(); });
beforeEach(() => {
  controlled.current = null; controlled.listeners.clear(); controlled.auth.user = { id: U1 };
  controlled.toast.mockReset(); controlled.load.mockReset().mockResolvedValue(undefined); controlled.confirm.mockReset();
  controlled.undo.mockReset();
  controlled.statsMe.mockReset().mockResolvedValue({ cumulative_learning_events: 2, dictionary_terms: 1 } as Stats);
  controlled.stats = new StatsStore();
});

const LEARNED: LearningSuggestion = {
  ...SUGGESTION, status: 'learned', classification: 'term', ambiguous: false,
  before: 'SQL lite', after: 'SQLite', undo_available: true,
};
function learnedOwner(event = LEARNED): ComponentOwner {
  const owner = new ComponentOwner(); owners.push(owner); owner.render();
  for (const callback of controlled.listeners.get('learning.learned') ?? []) callback(event);
  return owner;
}
function undoButton(owner: ComponentOwner): Element {
  const found = buttons(owner.render()).filter(item => text(item).trim() === '撤销学习');
  expect(found).toHaveLength(1);
  return found[0];
}

it('a server-learned term is shown automatically with undo and never asks for confirmation', () => {
  const owner = learnedOwner();
  expect(text(owner.render())).toContain('已自动学习');
  expect(text(owner.render())).toContain('SQLite');
  expect(buttons(owner.render()).some(button => text(button).trim() === '加入词典')).toBe(false);
  expect(undoButton(owner)).toBeDefined();
  expect(controlled.confirm).not.toHaveBeenCalled();
  expect(controlled.toast).toHaveBeenCalledExactlyOnceWith('已自动学习「SQLite」');
});
it('automatic learning refreshes the real cached homepage counts without another recording or route change', async () => {
  await controlled.stats.load(196);
  expect(controlled.stats.stats?.cumulative_learning_events).toBe(2);
  controlled.statsMe.mockResolvedValue({ cumulative_learning_events: 3, dictionary_terms: 2 } as Stats);
  learnedOwner(); await flush();
  expect(controlled.stats.stats?.cumulative_learning_events).toBe(3);
  expect(controlled.stats.stats?.dictionary_terms).toBe(2);
});
it('confirmed undo refreshes the term count while preserving cumulative learning, and never publishes before confirmation', async () => {
  controlled.statsMe.mockResolvedValue({ cumulative_learning_events: 3, dictionary_terms: 2 } as Stats);
  await controlled.stats.load(196);
  const owner = learnedOwner(); await flush();
  const pending = deferred<unknown>(); controlled.undo.mockReturnValueOnce(pending.promise);
  controlled.statsMe.mockResolvedValue({ cumulative_learning_events: 3, dictionary_terms: 1 } as Stats);
  undoButton(owner).props.onClick!(); await flush();
  expect(controlled.stats.stats?.dictionary_terms).toBe(2);
  pending.resolve({ event_id: LEARNED.event_id, undone: true }); await flush();
  expect(controlled.stats.stats?.cumulative_learning_events).toBe(3);
  expect(controlled.stats.stats?.dictionary_terms).toBe(1);
});
describe('learned word undo uses the real component action and server confirmation', () => {
  it('offers the learned word and invokes its undo only once while pending', async () => {
    const pending = deferred<unknown>(); controlled.undo.mockReturnValueOnce(pending.promise);
    const owner = learnedOwner(); const button = undoButton(owner);
    expect(text(owner.render())).toContain('SQLite');
    controlled.toast.mockClear(); controlled.load.mockClear();
    button.props.onClick!(); button.props.onClick!();
    expect(controlled.undo).toHaveBeenCalledExactlyOnceWith(LEARNED.event_id);
    expect(undoButton(owner).props.disabled).toBe(true);
    expect(controlled.toast).not.toHaveBeenCalled();
    pending.resolve({ event_id: LEARNED.event_id, undone: true }); await flush();
    expect(buttons(owner.render()).some(item => text(item).trim() === '撤销学习')).toBe(false);
    expect(controlled.toast).toHaveBeenCalledExactlyOnceWith('已撤销学习「SQLite」');
    expect(controlled.load).toHaveBeenCalledExactlyOnceWith(true);
  });
  it('keeps the word on a version conflict and directs manual handling', async () => {
    const pending = deferred<unknown>(); controlled.undo.mockReturnValueOnce(pending.promise);
    const owner = learnedOwner(); controlled.toast.mockClear(); controlled.load.mockClear();
    undoButton(owner).props.onClick!();
    pending.reject(new Error('词条已变化，无法撤销')); await flush();
    expect(undoButton(owner).props.disabled).toBe(false);
    expect(controlled.toast).toHaveBeenCalledExactlyOnceWith('词条已变化，无法撤销；请在词库中手动处理', 'error');
    expect(controlled.load).not.toHaveBeenCalled();
  });
  it('does not claim success before the returned event confirms undo', async () => {
    controlled.undo.mockResolvedValueOnce({ event_id: LEARNED.event_id, undone: false });
    const owner = learnedOwner(); controlled.toast.mockClear(); controlled.load.mockClear();
    undoButton(owner).props.onClick!(); await flush();
    expect(undoButton(owner).props.disabled).toBe(false);
    expect(controlled.toast).toHaveBeenCalledExactlyOnceWith('服务端尚未确认撤销；请在词库中手动处理', 'error');
    expect(controlled.load).not.toHaveBeenCalled();
  });
  it('does not offer an unavailable or already undone event', () => {
    for (const event of [{ ...LEARNED, undo_available: false }, { ...LEARNED, undone: true }]) {
      const owner = learnedOwner(event);
      expect(buttons(owner.render()).some(item => text(item).trim() === '撤销学习')).toBe(false);
      owner.unmount();
    }
  });
  it.each(['success', 'error'] as const)('fences late undo %s after account change', async outcome => {
    const pending = deferred<unknown>(); controlled.undo.mockReturnValueOnce(pending.promise);
    const owner = learnedOwner(); controlled.toast.mockClear(); controlled.load.mockClear();
    undoButton(owner).props.onClick!(); controlled.auth.user = { id: U2 }; owner.render();
    if (outcome === 'success') pending.resolve({ event_id: LEARNED.event_id, undone: true });
    else pending.reject(new Error('LATE_UNDO_ERROR'));
    await flush();
    expect(controlled.toast).not.toHaveBeenCalled(); expect(controlled.load).not.toHaveBeenCalled();
  });
  it.each(['success', 'error'] as const)('fences late undo %s after unmount', async outcome => {
    const pending = deferred<unknown>(); controlled.undo.mockReturnValueOnce(pending.promise);
    const owner = learnedOwner(); controlled.toast.mockClear(); controlled.load.mockClear();
    undoButton(owner).props.onClick!(); owner.unmount();
    if (outcome === 'success') pending.resolve({ event_id: LEARNED.event_id, undone: true });
    else pending.reject(new Error('LATE_UNDO_ERROR'));
    await flush();
    expect(controlled.toast).not.toHaveBeenCalled(); expect(controlled.load).not.toHaveBeenCalled();
  });
});
afterEach(() => { for (const owner of owners.splice(0)) owner.unmount(); controlled.listeners.clear(); });

describe('automatic-only learning notifications', () => {
  it('never subscribes to uncertain suggestions or calls confirm automatically', () => {
    const owner = new ComponentOwner(); owners.push(owner); owner.render();
    expect(controlled.listeners.has('learning.suggested')).toBe(false);
    expect(buttons(owner.render())).toHaveLength(0);
    expect(controlled.confirm).not.toHaveBeenCalled();
  });
  it.each(['suggested', 'pending', 'ignored', 'failed'] as const)('does not announce a %s payload as learned', status => {
    const owner = learnedOwner({ ...LEARNED, status });
    expect(buttons(owner.render())).toHaveLength(0);
    expect(controlled.toast).not.toHaveBeenCalled();
    expect(controlled.load).not.toHaveBeenCalled();
    expect(controlled.confirm).not.toHaveBeenCalled();
  });
  it('ignores a late learned callback from the previous account lifetime', () => {
    const owner = new ComponentOwner(); owners.push(owner); owner.render();
    const oldCallback = [...controlled.listeners.get('learning.learned')!][0];
    controlled.auth.user = { id: U2 }; owner.render(); oldCallback(LEARNED);
    expect(buttons(owner.render())).toHaveLength(0);
    expect(controlled.toast).not.toHaveBeenCalled(); expect(controlled.load).not.toHaveBeenCalled();
  });
  it('removes the notice on account change and does not resurrect a late undone event', async () => {
    const owner = learnedOwner();
    controlled.auth.user = { id: U2 }; owner.render();
    expect(buttons(owner.render())).toHaveLength(0);
    controlled.toast.mockClear(); controlled.load.mockClear();
    for (const callback of controlled.listeners.get('learning.learned') ?? []) callback({ ...LEARNED, undone: true });
    expect(buttons(owner.render())).toHaveLength(0);
    expect(controlled.toast).not.toHaveBeenCalled(); expect(controlled.load).not.toHaveBeenCalled();
  });
});
