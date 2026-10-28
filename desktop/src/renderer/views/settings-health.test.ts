import { afterAll, describe, expect, it, vi } from 'vitest';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { RpcRouter } from '@main/rpc';
import type { Locale } from '../lib/i18n';

vi.mock('../stores', () => ({ stores: {} }));
vi.mock('../components/RetentionControl', () => ({ RetentionControl: () => null }));
import { SettingsServiceHealth } from './SettingsView';

vi.stubGlobal('React', React);
afterAll(() => vi.unstubAllGlobals());
type Health = Awaited<ReturnType<RpcRouter['app']['health']>>;
type ConnectedHealth = Extract<Health, { asrModel: string }>;
function snapshot(patch: Partial<ConnectedHealth> = {}): ConnectedHealth {
  return { status: 'loading', ready: false, degraded: true, reason: 'STARTING',
    asrModel: 'frozen-speech-model', llmModel: 'frozen-text-model', llmAvailable: false,
    checkedAt: '2026-10-18T00:00:00Z', ...patch };
}
function render(health: Health | null, locale: Locale = 'zh-CN', checking = false) {
  return renderToStaticMarkup(React.createElement(SettingsServiceHealth, {
    locale, apiBaseUrl: 'http://127.0.0.1:17863', health, checking, onRecheck: () => undefined,
  }));
}
const status = (html: string) => html.match(/<span[^>]*role="status"[^>]*>([^<]*)<\/span>/)?.[1];

// Actual Settings panel SSR, not a GUI, real transport, model or click claim.
describe('NFR02 Settings renders explicit model loading without fake availability', () => {
  it.each(['zh-CN', 'en'] as const)('renders settled STARTING clearly in %s', locale => {
    const output = render(snapshot(), locale);
    expect(status(output)).toBe(locale === 'en' ? 'Loading speech model…' : '模型加载中…');
    expect(output).toContain(locale === 'en' ? 'The service is loading the speech model.' : '服务正在加载语音模型');
    expect(output).toContain('frozen-speech-model'); expect(output).toContain('frozen-text-model');
    expect(output).not.toContain('STARTING'); expect(output).not.toContain('text-[var(--danger)]');
    for (const text of locale === 'en' ? ['>Connected<', '>Degraded<', '>Service error<', 'unavailable']
      : ['>已连接<', '>服务降级<', '>服务错误<', '不可用']) expect(output).not.toContain(text);
    expect(output).not.toMatch(/<button[^>]*disabled/); // The user can explicitly check again.
  });

  it('an in-flight health request is checking, not evidence that a model is loading', () => {
    const output = render(null, 'zh-CN', true);
    expect(status(output)).toBe('检测中…');
    expect(output).not.toContain('模型加载中'); expect(output).not.toContain('已连接');
    expect(output).toMatch(/<button[^>]*disabled/);
  });

  it.each([
    { health: snapshot({ status: 'ready', ready: true, degraded: false, reason: null, llmAvailable: true }), label: '已连接', detail: 'frozen-text-model (可用)' },
    { health: snapshot({ status: 'ready', ready: true, reason: 'LLM_UNAVAILABLE' }), label: '已连接', detail: '整理模型不可用，结果会回退原文' },
    { health: snapshot({ status: 'degraded', reason: 'ASR_UNAVAILABLE' }), label: '服务降级', detail: '语音模型尚未就绪' },
    { health: { status: 'unreachable', ready: false, degraded: false, reason: 'fetch failed',
      asrModel: null, llmModel: null, llmAvailable: null, checkedAt: '2026-10-18T00:00:00Z' } satisfies Health,
      label: '无法连接', detail: '无法连接服务，请检查网络后重试' },
  ])('preserves $label / $detail', ({ health, label, detail }) => {
    const output = render(health);
    expect(status(output)).toBe(label); expect(output).toContain(detail);
    expect(output).not.toContain('模型加载中');
  });
});
