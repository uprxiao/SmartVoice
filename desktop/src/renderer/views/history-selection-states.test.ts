import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Session } from '@shared/types';

const view = vi.hoisted(() => ({
  history: { items: [] as Session[], loaded: true, loading: false, error: null },
  session: { busy: false, phase: 'idle', historySelection: null as null | {
    sessionId: string; kind: 'asr' | 'refine'; loading: boolean; error: string | null;
  } },
  auth: { user: { id: '11111111-1111-4111-8111-111111111111' }, settings: { locale: 'zh-CN' } }, ui: {},
}));
vi.mock('../stores', () => ({ stores: view }));
vi.mock('../components/RetentionControl', () => ({ RetentionControl: () => null }));
import { HistoryView } from './HistoryView';
vi.stubGlobal('React', React); afterAll(() => vi.unstubAllGlobals());
const FIRST = '22222222-2222-4222-8222-222222222222';
const OTHER = '33333333-3333-4333-8333-333333333333';
function record(id = FIRST, patch: Partial<Session> = {}): Session {
  return { session_id: id, mode: 'dictation', state: 'ready', current_attempt_no: 1, learning_enabled: false,
    current_result_id: '44444444-4444-4444-8444-444444444444', audio_complete: true, received_samples: 32000,
    audio_ms: 2000, created_at: '2026-10-18T00:00:00Z', first_terminal_at: '2026-10-18T00:00:01Z',
    content_expires_at: null, content_expired: false, error_code: null, retry_kind: 'refine', retry_expires_at: null,
    retry_unavailable_reason: null, insertion_status: null,
    result: { result_id: '44444444-4444-4444-8444-444444444444', attempt_no: 1, attempt_kind: 'initial',
      raw_text: '虚构原文', refined_text: '虚构整理稿', review_required: true, review_reason: 'llm_unavailable',
      text_hash: 'a'.repeat(64), dictionary_version: 0, stop_to_result_ms: 9000, created_at: '2026-10-18T00:00:01Z' }, ...patch };
}
const html = () => renderToStaticMarkup(React.createElement(HistoryView));
const retryButtons = (output: string) => (output.match(/<button\b[\s\S]*?<\/button>/g) ?? [])
  .filter(button => /重新整理|重新识别|重试读取|Retry loading|Retry refinement|Retry recognition/.test(button));
beforeEach(() => {
  view.history.items = [record(), record(OTHER)]; view.session.busy = false; view.session.phase = 'idle';
  view.session.historySelection = null; view.auth.settings.locale = 'zh-CN';
});

// SSR observes visible bounded states only. It does not prove a click, IPC GET,
// real server fault, account transition, Electron or native behaviour.
describe('history selection GET loading and persistent errors', () => {
  it.each(['refine', 'asr'] as const)('shows one loading status and disables both retry buttons during %s selection', kind => {
    if (kind === 'asr') view.history.items[0] = record(FIRST, { state: 'failed', retry_kind: 'asr', result: null, current_result_id: null });
    view.session.busy = true;
    view.session.historySelection = { sessionId: FIRST, kind, loading: true, error: null };
    const output = html(); const buttons = retryButtons(output);
    expect(output.match(/role="status"/g)).toHaveLength(1);
    expect(output).toContain('正在读取历史会话…'); expect(output).not.toContain('role="alert"');
    expect(buttons).toHaveLength(2); expect(buttons.every(button => button.includes('disabled=""'))).toBe(true);
    expect(buttons.filter(button => button.includes('aria-busy="true"'))).toHaveLength(1);
    expect(output).not.toContain('还没有语音记录');
  });

  it.each(['zh-CN', 'en'])('retains the row and offers an enabled explicit retry after GET failure (%s)', locale => {
    view.auth.settings.locale = locale;
    view.session.historySelection = { sessionId: FIRST, kind: 'refine', loading: false, error: 'controlled history GET failure' };
    const output = html();
    expect(output.match(/role="alert"/g)).toHaveLength(1);
    expect(output).toContain('controlled history GET failure'); expect(output).toContain('虚构整理稿');
    expect(output).not.toContain('role="status"'); expect(output).not.toContain('还没有语音记录');
    const label = locale === 'en' ? 'Retry loading' : '重试读取';
    const retry = (output.match(/<button\b[\s\S]*?<\/button>/g) ?? []).filter(button => button.includes(label));
    expect(retry).toHaveLength(1); expect(retry[0]).not.toContain('disabled=""');
  });

  it('an expired row keeps the failure explanation but cannot offer model retry', () => {
    view.history.items = [record(FIRST, { content_expired: true, retry_kind: 'none' })];
    view.session.historySelection = { sessionId: FIRST, kind: 'refine', loading: false, error: '正文已过期' };
    const output = html(); expect(output).toContain('role="alert"'); expect(output).toContain('正文已过期');
    expect(retryButtons(output)).toHaveLength(0); expect(output).not.toContain('虚构原文');
    expect(output).not.toContain('虚构整理稿');
  });

  it('a cleared selection has no stale loading or error state', () => {
    const output = html(); expect(output).not.toContain('role="status"'); expect(output).not.toContain('role="alert"');
    expect(retryButtons(output)).toHaveLength(2); expect(output).toContain('查看详情');
  });
});
