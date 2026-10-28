import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Session, SessionResult, Stats, Term } from '@shared/types';

const view = vi.hoisted(() => ({
  history: { items: [] as Session[], loaded: false, loading: false, error: null as string | null },
  dictionary: { items: [] as Term[], loaded: false, loading: false, error: null as string | null,
    query: '', source: 'all', version: 0, hasMore: false, busy: false,
    selected: new Map<string, { id: string; version: number; term: string }>(), allLoadedSelected: false },
  stats: { stats: null as Stats | null, loading: false, error: null as string | null },
  session: { phase: 'idle', result: null as SessionResult | null, busy: false, retryAvailable: false,
    insertionAvailable: false, targetError: null, targetApp: null, outcome: null, error: null },
  auth: { settings: { locale: 'zh-CN' as 'zh-CN' | 'en', learning_enabled: false } },
  ui: { localShortcut: 'CommandOrControl+Alt+Shift+F19' },
}));
vi.mock('../stores', () => ({ stores: view }));
vi.mock('../components/RetentionControl', () => ({ RetentionControl: () => null }));
import { HomeView } from './HomeView';
import { DictionaryView } from './DictionaryView';
import { HistoryView } from './HistoryView';

vi.stubGlobal('React', React);
afterAll(() => vi.unstubAllGlobals());
beforeEach(() => {
  Object.assign(view.history, { items: [], loaded: false, loading: false, error: null });
  Object.assign(view.dictionary, { items: [], loaded: false, loading: false, error: null,
    query: '', source: 'all', version: 0, hasMore: false, busy: false, selected: new Map(), allLoadedSelected: false });
  Object.assign(view.stats, { stats: null, loading: false, error: null });
  Object.assign(view.session, { phase: 'idle', result: null, busy: false });
  view.auth.settings.locale = 'zh-CN';
});
const home = () => renderToStaticMarkup(React.createElement(HomeView, { onNavigate: () => undefined }));
const dictionary = () => renderToStaticMarkup(React.createElement(DictionaryView));
const history = () => renderToStaticMarkup(React.createElement(HistoryView));
const stripTags = (html: string) => html.replace(/<[^>]*>/g, '');

it('renders selected count and keyboard checkboxes while keeping default filters out of selection mode', () => {
  view.dictionary.loaded = true;
  view.dictionary.items = ['React', 'Docker'].map(id => ({
    id, term: id, norm: id.toLowerCase(), aliases: [], contexts: [], scope: 'default', source: 'manual',
    version: 1, dictionary_version: 1, created_at: '', updated_at: '', ambiguous_aliases: [],
  }));
  view.dictionary.selected.set('React', { id: 'React', version: 1, term: 'React' });
  const output = dictionary();
  expect(stripTags(output)).toContain('已选择 1 个词');
  expect(output).toContain('aria-label="全选或取消已加载词条"');
  expect(output).toContain('aria-checked="mixed"');
  expect(output).toContain('aria-label="选择词条 React"');
  expect(output).toContain('aria-label="选择词条 Docker"');
  expect(output).not.toContain('dictionary-source-filter');
  expect(output).not.toContain('更多词条操作：');
});
const statuses = (html: string) => html.match(/<[^>]+role="status"[^>]*>[\s\S]*?<\/[^>]+>/g) ?? [];
function zeroStats(): Stats {
  return { timezone: 'Asia/Shanghai', date_from: '2026-10-18', date_to: '2026-10-18',
    totals: { sessions: 0, result_sessions: 0, inserted_sessions: 0, chars: 0,
      failed_sessions: 0, empty_sessions: 0, cancelled_sessions: 0, fallback_sessions: 0,
      fallback_rate: 0, learning_events: 0,
      successful_audio_ms: 0, received_audio_ms: 0, average_chars_per_minute: 0,
      processing_success_rate: 0, insertion_success_rate: 0 },
    active_days_in_range: 0, current_streak_days: 0, longest_streak_days: 0,
    dictionary_terms: 0, cumulative_learning_events: 0, daily: [] };
}

// Pure SSR of real views with declared store fixtures. Run on old code first.
// No actual click, IPC, API, Electron, permissions or native write is claimed.
describe('SET07 list states distinguish pending, failed and confirmed empty', () => {
  it('shows cumulative learning separately from the remaining dictionary size after an undo (STAT04)', () => {
    view.stats.stats = zeroStats();
    view.stats.stats.cumulative_learning_events = 2;
    view.stats.stats.dictionary_terms = 1;
    const text = stripTags(home()).replace(/\s+/g, '');
    expect(text).toContain('2次学习');
    expect(text).toContain('持续学习中');
    expect(text).toContain('1个词条');
    expect(text).not.toContain('%');
  });
  it.each(['zh-CN', 'en'] as const)('home exposes both pending reads in %s without false empty history', locale => {
    view.auth.settings.locale = locale;
    view.stats.loading = view.history.loading = true;
    const output = home();
    expect(statuses(output).length).toBeGreaterThanOrEqual(2);
    expect(output).not.toContain(locale === 'en' ? 'No records yet' : '还没有记录');
    // Initial stats are unknown, rather than seven successful zero metrics.
    expect(output.match(/>—<\/span>/g)?.length ?? 0).toBeGreaterThanOrEqual(7);
  });

  it('home failure has a retry and does not turn an unreceived list into empty or stats into zero', () => {
    view.stats.error = view.history.error = 'SET07_READ_FAILED';
    const output = home();
    expect(output).toContain('SET07_READ_FAILED');
    expect(output).toContain('重试');
    expect(output).not.toContain('还没有记录');
    expect(output.match(/>—<\/span>/g)?.length ?? 0).toBeGreaterThanOrEqual(7);
  });

  it('home shows genuine zero and empty only after successful reads', () => {
    view.stats.stats = zeroStats(); view.history.loaded = true;
    const output = home();
    expect(output).toContain('还没有记录');
    expect(output.match(/>0<\/span>/g)?.length ?? 0).toBeGreaterThanOrEqual(7);
    expect(statuses(output)).toHaveLength(0);
  });

  it('home does not present cached statistics as a fresh result after a failed refresh', () => {
    view.stats.stats = zeroStats(); view.stats.stats.totals.chars = 673;
    view.stats.error = 'SET07_REFRESH_FAILED';
    expect(stripTags(home())).not.toContain('673');
  });

  it('dictionary exposes initial loading even without a next-page cursor', () => {
    view.dictionary.loading = true;
    const output = dictionary();
    expect(statuses(output).length).toBeGreaterThanOrEqual(1);
    expect(output).not.toContain('还没有词条');
    expect(stripTags(output)).not.toContain('0 个词条');
  });

  it.each(['history', 'dictionary'] as const)('%s confirmed-empty cache does not masquerade as the pending refresh', kind => {
    const store = kind === 'history' ? view.history : view.dictionary;
    store.loaded = store.loading = true;
    const output = kind === 'history' ? history() : dictionary();
    expect(statuses(output).length).toBeGreaterThanOrEqual(1);
    expect(output).not.toContain(kind === 'history' ? '还没有语音记录' : '还没有词条');
  });

  it.each(['history', 'dictionary'] as const)('%s failure after a prior empty result stays an error with retry', kind => {
    const store = kind === 'history' ? view.history : view.dictionary;
    store.loaded = true; store.error = 'SET07_REFRESH_FAILED';
    const output = kind === 'history' ? history() : dictionary();
    expect(output).toContain('SET07_REFRESH_FAILED');
    expect(output).toContain('重试');
    expect(output).not.toContain(kind === 'history' ? '还没有语音记录' : '还没有词条');
  });

  it.each(['history', 'dictionary'] as const)('%s successful empty remains visible', kind => {
    (kind === 'history' ? view.history : view.dictionary).loaded = true;
    const output = kind === 'history' ? history() : dictionary();
    expect(output).toContain(kind === 'history' ? '还没有语音记录' : '还没有词条');
    expect(statuses(output)).toHaveLength(0);
  });

  it('preserves the local transcript details while unrelated lists are pending', () => {
    view.stats.loading = view.history.loading = true;
    view.session.phase = 'ready';
    view.session.result = { result_id: '11111111-1111-4111-8111-111111111111', attempt_no: 1,
      attempt_kind: 'initial', raw_text: '原文 fixture', refined_text: '整理 fixture', review_required: false,
      review_reason: null, text_hash: 'a'.repeat(64), dictionary_version: 0, stop_to_result_ms: 35,
      created_at: '2026-10-18T00:00:00Z' };
    const output = home();
    expect(output).toMatch(/<summary[^>]*>查看识别原文<\/summary>/);
    expect(output).toContain('原文 fixture'); expect(output).toContain('整理 fixture');
  });
});
