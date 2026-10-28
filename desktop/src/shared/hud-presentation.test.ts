import { describe, expect, it } from 'vitest';
import { advanceHud, EMPTY_HUD, HUD_CLOSE_MS, HUD_COMPLETE_HOLD_MS, hudFrame, hudSlowDelayMs, hudWaitingProgress } from './hud-presentation';
import type { SessionProgress } from './types';

function progress(patch: Partial<SessionProgress> = {}): SessionProgress {
  return { acknowledged_samples: 0, session_id: 'owned-session', attempt_no: 1, revision: 1,
    auto_insert_allowed: false, state: 'streaming', capture_ready: true, audio_ms: 2_000,
    stable_text: '', provisional_text: '', error_code: null, result: null, ...patch };
}

describe('HUD waiting lifetime', () => {
  it('starts its deadline once across finalizing/refining and still animates without WS updates', () => {
    const first = advanceHud(EMPTY_HUD, progress({ state: 'finalizing' }), 100);
    const refining = advanceHud(first, progress({ state: 'refining', revision: 2 }), 4_000);
    expect(refining.processingStartedAt).toBe(100);
    expect(hudFrame(refining, 500).fill).toBeGreaterThan(hudFrame(first, 100).fill);
    expect(hudFrame(refining, 6_599).slow).toBe(false);
    expect(hudFrame(refining, 6_600).slow).toBe(true);
    expect(hudFrame(refining, 1_000_000)).toMatchObject({ phase: 'processing', fill: 99, slow: true });
  });

  it('only completes on a real ready event, holds and fades without extending on duplicate ready', () => {
    const waiting = advanceHud(EMPTY_HUD, progress({ state: 'refining' }), 0);
    const complete = advanceHud(waiting, progress({ state: 'ready', revision: 2 }), 7_000);
    const duplicate = advanceHud(complete, progress({ state: 'ready', revision: 3 }), 7_300);
    expect(duplicate.completedAt).toBe(7_000);
    expect(hudFrame(duplicate, 7_000)).toMatchObject({ phase: 'completed', fill: 100, slow: false, fading: false });
    expect(hudFrame(duplicate, 7_000 + HUD_COMPLETE_HOLD_MS).fading).toBe(true);
    expect(hudFrame(duplicate, 7_000 + HUD_CLOSE_MS).phase).toBe('hidden');
    expect(advanceHud(EMPTY_HUD, progress({ state: 'ready' }), 0).phase).toBe('hidden');
  });

  it('new session or retry clears the old waiting deadline, and old revisions cannot revive it', () => {
    const waiting = advanceHud(EMPTY_HUD, progress({ state: 'refining', revision: 10 }), 0);
    const next = advanceHud(waiting, progress({ session_id: 'next', revision: 11 }), 8_000);
    expect(hudFrame(next, 20_000)).toMatchObject({ phase: 'recording', slow: false });
    expect(advanceHud(next, progress({ state: 'refining', revision: 10 }), 9_000)).toBe(next);
    const retry = advanceHud(waiting, progress({ state: 'refining', attempt_no: 2, revision: 11 }), 8_000);
    expect(retry.processingStartedAt).toBe(8_000);
    expect(hudFrame(retry, 8_100).slow).toBe(false);
  });

  it.each(['failed', 'cancelled', 'empty'] as const)('%s clears the whole processing display immediately', state => {
    const waiting = advanceHud(EMPTY_HUD, progress({ state: 'refining' }), 0);
    const terminal = advanceHud(waiting, progress({ state, revision: 2 }), 9_000);
    expect(hudFrame(terminal, 99_000)).toMatchObject({ phase: 'hidden', slow: false });
    expect(terminal.processingStartedAt).toBeNull();
  });

  it('uses duration as a hint budget, never as evidence of completion', () => {
    expect(hudSlowDelayMs(0)).toBe(6_000);
    expect(hudSlowDelayMs(30_000)).toBe(6_500);
    expect(hudSlowDelayMs(30_001)).toBe(7_000);
    expect(hudWaitingProgress(-10)).toBe(0);
    expect(hudWaitingProgress(35_000)).toBe(99);
  });
});

