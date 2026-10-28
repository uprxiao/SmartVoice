import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { Session } from '@shared/types';

const view = vi.hoisted(() => ({
  history: { items: [] as Session[], loaded: true, loading: false, error: null },
  session: { busy: false, phase: 'idle' },
  auth: { settings: { locale: 'zh-CN' } }, ui: {},
}));
vi.mock('../stores', () => ({ stores: view }));
vi.mock('../components/RetentionControl', () => ({ RetentionControl: () => null }));
import { HistoryView } from './HistoryView';
vi.stubGlobal('React', React);
afterAll(() => vi.unstubAllGlobals());
beforeEach(() => { view.history.items = []; view.auth.settings.locale = 'zh-CN'; });

function record(state: Session['state'], insertion: Session['insertion_status']): Session {
  return { session_id: '11111111-1111-4111-8111-111111111111', mode: 'dictation', state,
    current_attempt_no: 1, learning_enabled: false, current_result_id: null,
    audio_complete: true, received_samples: 32000, audio_ms: 2000, created_at: '2026-10-18T00:00:00Z',
    first_terminal_at: '2026-10-18T00:00:01Z', content_expires_at: null, content_expired: false,
    error_code: null, retry_kind: 'none', retry_expires_at: null, retry_unavailable_reason: null,
    insertion_status: insertion, result: null };
}
function metadata(): string {
  const output = renderToStaticMarkup(React.createElement(HistoryView));
  // Limit assertions to this record's metadata, not the always-visible Dictation tab.
  const line = output.match(/<div class="mt-1 text-\[11px\] text-\[var\(--text-disabled\)\]">([\s\S]*?)<\/div>/);
  expect(line).not.toBeNull();
  return line![1];
}

describe('history exposes separate processing and insertion states', () => {
  it.each([
    ['ready', 'inserted', '处理完成', '已插入'],
    ['ready', 'failed', '处理完成', '插入失败'],
    ['ready', 'unverified', '处理完成', '插入无法核验'],
    ['ready', 'reserved', '处理完成', '插入待确认'],
    ['cancelled', null, '已取消', '未插入'],
    ['empty', null, '未识别到语音', '未插入'],
    ['failed', null, '处理失败', '未插入'],
    ['refining', null, '正在整理', '未插入'],
  ] as const)('%s / %s is not collapsed into audio length or success', (state, insertion, processingLabel, insertionLabel) => {
    view.history.items = [record(state, insertion)];
    const output = metadata();
    expect(output).toContain('听写');
    expect(output).toContain(processingLabel);
    expect(output).toContain(insertionLabel);
    expect(output).toContain('0:02');
    if (insertion !== 'inserted') expect(output).not.toContain('已插入');
  });
  it('uses the corresponding English metadata labels without claiming an insertion', () => {
    view.auth.settings.locale = 'en'; view.history.items = [record('ready', 'unverified')];
    const output = metadata();
    expect(output).toContain('Dictation');
    expect(output).toContain('Processing complete');
    expect(output).toContain('Insertion unverified');
    expect(output).not.toContain('已插入');
  });
});
