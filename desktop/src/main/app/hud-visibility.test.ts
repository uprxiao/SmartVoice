import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HUD_CLOSE_MS } from '@shared/hud-presentation';
import type { SessionProgress } from '@shared/types';
import { HudVisibility } from './hud-visibility';

function progress(state: SessionProgress['state'], revision: number, session_id: string | null = 'own'): SessionProgress {
  return { acknowledged_samples: 0, session_id, attempt_no: 1, revision, auto_insert_allowed: false,
    state, capture_ready: state === 'streaming', audio_ms: 2_000, stable_text: '', provisional_text: '', error_code: null, result: null };
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('non-activating HUD window lifetime', () => {
  it('hides idle null-SID but permits the existing busy preparation window', () => {
    const show = vi.fn(), hide = vi.fn(), window = new HudVisibility(show, hide);
    window.update(progress('created', 1, null), false);
    expect(show).not.toHaveBeenCalled(); expect(hide).toHaveBeenCalledOnce();
    window.update(progress('created', 2, null), true);
    expect(show).toHaveBeenCalledOnce();
  });
  it('retains the processing window through completion and does not renew on duplicate ready', () => {
    const show = vi.fn(), hide = vi.fn(), window = new HudVisibility(show, hide);
    window.update(progress('refining', 1), true);
    window.update(progress('ready', 2), false);
    vi.advanceTimersByTime(300);
    window.update(progress('ready', 3), false);
    expect(hide).not.toHaveBeenCalled();
    vi.advanceTimersByTime(HUD_CLOSE_MS - 300);
    expect(hide).toHaveBeenCalledOnce();
  });
  it('an old completion timer/revision cannot hide a new session', () => {
    const show = vi.fn(), hide = vi.fn(), window = new HudVisibility(show, hide);
    window.update(progress('refining', 10), true);
    window.update(progress('ready', 11), false);
    vi.advanceTimersByTime(100);
    window.update(progress('streaming', 12, 'new'), true);
    window.update(progress('failed', 11), false);
    vi.advanceTimersByTime(HUD_CLOSE_MS);
    expect(hide).not.toHaveBeenCalled();
    expect(show).toHaveBeenCalledTimes(2);
  });
  it('account change, suspend or shutdown cancels scheduled close work', () => {
    const show = vi.fn(), hide = vi.fn(), window = new HudVisibility(show, hide);
    window.update(progress('refining', 1), true);
    window.update(progress('ready', 2), false);
    window.reset();
    expect(hide).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(HUD_CLOSE_MS);
    expect(hide).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0);
  });
});

// TC-INSERT-22: a failed automatic insertion keeps a visible HUD notice.
describe('post-result HUD notice', () => {
  it('cancels the completion close, survives later ready revisions and expires on its own', () => {
    const show = vi.fn(), hide = vi.fn(), end = vi.fn(), window = new HudVisibility(show, hide, end);
    window.update(progress('refining', 1), true);
    window.update(progress('ready', 2), false);
    window.notice(8_000);
    vi.advanceTimersByTime(HUD_CLOSE_MS);
    window.update(progress('ready', 3), false);
    expect(hide).not.toHaveBeenCalled(); expect(end).not.toHaveBeenCalled();
    vi.advanceTimersByTime(8_000);
    expect(hide).toHaveBeenCalledOnce(); expect(end).toHaveBeenCalledOnce();
  });
  it('dismiss hides immediately and is idempotent', () => {
    const show = vi.fn(), hide = vi.fn(), end = vi.fn(), window = new HudVisibility(show, hide, end);
    window.notice(8_000);
    window.dismiss(); window.dismiss();
    expect(hide).toHaveBeenCalledOnce(); expect(end).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(8_000);
    expect(hide).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0);
  });
  it('a new recording takes the HUD over without the old notice hiding it later', () => {
    const show = vi.fn(), hide = vi.fn(), end = vi.fn(), window = new HudVisibility(show, hide, end);
    window.update(progress('ready', 1), false);
    hide.mockClear();
    window.notice(8_000);
    window.update(progress('streaming', 2, 'new'), true);
    expect(end).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(8_000);
    expect(hide).not.toHaveBeenCalled();
  });
  it('reset ends the notice', () => {
    const show = vi.fn(), hide = vi.fn(), end = vi.fn(), window = new HudVisibility(show, hide, end);
    window.notice(8_000);
    window.reset();
    expect(end).toHaveBeenCalledOnce(); expect(hide).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0);
  });
});
