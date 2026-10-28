import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Session } from '@shared/types';

const view = vi.hoisted(() => ({
  history: { items: [] as Session[], loaded: true, loading: false, error: null },
  session: { busy: false, phase: 'idle' },
  auth: { settings: { locale: 'zh-CN' } },
  ui: {},
}));
vi.mock('../stores', () => ({ stores: view }));
vi.mock('../components/RetentionControl', () => ({ RetentionControl: () => null }));
import { HistoryView } from './HistoryView';

vi.stubGlobal('React', React);
afterAll(() => vi.unstubAllGlobals());
beforeEach(() => { view.history.items = []; view.auth.settings.locale = 'zh-CN'; });

function record(patch: Partial<Session> = {}): Session {
  return { session_id: '11111111-1111-4111-8111-111111111111', mode: 'dictation', state: 'ready',
    current_attempt_no: 1, learning_enabled: false, current_result_id: '22222222-2222-4222-8222-222222222222',
    audio_complete: true, received_samples: 32000, audio_ms: 2000, created_at: '2026-10-18T00:00:00Z',
    first_terminal_at: '2026-10-18T00:00:01Z', content_expires_at: null, content_expired: false,
    error_code: null, retry_kind: 'refine', retry_expires_at: null, retry_unavailable_reason: null,
    insertion_status: null, result: { result_id: '22222222-2222-4222-8222-222222222222', attempt_no: 1,
      attempt_kind: 'initial', raw_text: '虚构历史样本', refined_text: '虚构历史样本', review_required: true,
      review_reason: 'llm_unavailable', text_hash: 'a'.repeat(64), dictionary_version: 0,
      stop_to_result_ms: 8000, created_at: '2026-10-18T00:00:01Z' }, ...patch } as Session;
}
function html(): string { return renderToStaticMarkup(React.createElement(HistoryView)); }
function buttons(markup: string): string[] { return markup.match(/<button\b[\s\S]*?<\/button>/g) ?? []; }

// This file can run against the old UI before production changes. SSR checks
// visible actions only; it is not a click, IPC, Electron or native acceptance.
describe('history renders the authoritative retry capability and processing duration', () => {
  it.each([['refine', '重新整理'], ['asr', '重新识别']] as const)('shows the %s retry action', (kind, label) => {
    view.history.items = [record(kind === 'asr'
      ? { state: 'failed', result: null, current_result_id: null, retry_kind: 'asr', error_code: 'ASR_FAILED' }
      : {})];
    expect(buttons(html()).filter(button => button.includes(label))).toHaveLength(1);
  });

  it.each(['none', 'expired'] as const)('does not offer an unavailable retry: %s', reason => {
    view.history.items = [record(reason === 'expired' ? { content_expired: true } : { retry_kind: 'none' })];
    expect(buttons(html()).filter(button => /重新整理|重新识别/.test(button))).toHaveLength(0);
  });

  it('distinguishes the retry processing time from the two-second audio duration', () => {
    const item = record();
    item.result = { ...item.result!, attempt_no: 2, attempt_kind: 'refine', stop_to_result_ms: 3500 };
    item.current_attempt_no = 2; item.retry_kind = 'none';
    view.history.items = [item];
    const output = html();
    expect(output).toContain('0:02');
    expect(output).toContain('本次重试处理耗时 4 秒');
    expect(output).not.toContain('停止到结果');
    expect(output).not.toContain('120');
  });

  it('labels an initial result and an English retry without reusing user wait time', () => {
    view.history.items = [record()];
    expect(html()).toContain('停止到结果 8 秒');
    const item = record();
    item.result = { ...item.result!, attempt_kind: 'refine', stop_to_result_ms: 3500 };
    view.history.items = [item]; view.auth.settings.locale = 'en';
    expect(html()).toContain('This retry took 4 s');
  });
});
