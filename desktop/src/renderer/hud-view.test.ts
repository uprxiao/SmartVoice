import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { advanceHud, EMPTY_HUD, hudFrame } from '@shared/hud-presentation';
import type { SessionProgress } from '@shared/types';
import { HudView } from './hud-view';

function progress(patch: Partial<SessionProgress> = {}): SessionProgress {
  return { acknowledged_samples: 0, session_id: 'owned-session', attempt_no: 1, revision: 1,
    auto_insert_allowed: false, state: 'streaming', capture_ready: true, audio_ms: 2_000,
    stable_text: '', provisional_text: '', error_code: null, result: null, ...patch };
}

describe('real HUD markup', () => {
  const markup = (snapshot: SessionProgress, level = 0, now = 0) => renderToStaticMarkup(createElement(HudView, {
    frame: hudFrame(advanceHud(EMPTY_HUD, snapshot, 0), now), level, onCancel() {}, onConfirm() {},
  }));

  it('only presents recording controls and RMS bars after capture handshake', () => {
    const preparing = markup(progress({ capture_ready: false }));
    expect(preparing).toContain('data-phase="preparing"');
    expect(preparing).not.toMatch(/完成录音|取消录音|hud-wave/);
    const silent = markup(progress());
    const voiced = markup(progress(), .8);
    expect(silent).toContain('完成录音'); expect(silent).toContain('取消录音');
    expect(silent).toContain('height:2px'); expect(voiced).not.toBe(silent);
  });

  it('renders persistent processing, no fake RMS/finish button, and an elapsed hint', () => {
    const output = markup(progress({ state: 'refining' }), 1, 8_000);
    expect(output).toContain('data-phase="processing"');
    expect(output).toContain('hud-loading'); expect(output).toContain('hud-progress');
    expect(output).toContain('处理耗时比平时更长');
    expect(output).not.toMatch(/hud-wave|完成录音|取消录音|已输入|网络故障/);
  });
});

// TC-INSERT-22: Accessibility hint during recording and the failure card after the session.
describe('HUD accessibility notices', () => {
  const render = (snapshot: SessionProgress | null, notice: Parameters<typeof HudView>[0]['notice'], now = 0) =>
    renderToStaticMarkup(createElement(HudView, {
      frame: snapshot ? hudFrame(advanceHud(EMPTY_HUD, snapshot, 0), now) : hudFrame(EMPTY_HUD, now),
      level: 0, notice, onCancel() {}, onConfirm() {},
    }));
  const permission = { kind: 'permission' as const, message: '未开启辅助功能，结果不会自动输入', permission: true };

  it('shows the hint with a settings action above the recording capsule, replacing the slow hint', () => {
    const recording = render(progress(), permission);
    expect(recording).toContain('未开启辅助功能'); expect(recording).toContain('去授权'); expect(recording).toContain('完成录音');
    const slow = render(progress({ state: 'refining' }), permission, 8_000);
    expect(slow).toContain('去授权'); expect(slow).not.toContain('处理耗时比平时更长');
  });

  it('renders the failure card even after the session window phase is hidden', () => {
    const card = render(null, { kind: 'insert-failed', message: '未开启辅助功能，结果未输入', permission: true });
    expect(card).toContain('role="alert"'); expect(card).toContain('复制结果'); expect(card).toContain('去授权');
    expect(card).toContain('关闭提示'); expect(card).not.toMatch(/完成录音|hud-pill/);
    const other = render(null, { kind: 'insert-failed', message: '输入位置已改变', permission: false });
    expect(other).toContain('输入位置已改变'); expect(other).not.toContain('去授权');
  });

  it('a hidden session without a failure notice renders nothing', () => {
    expect(render(null, permission)).toBe('');
  });
});
