import type { SessionProgress } from './types';

export const HUD_COMPLETE_HOLD_MS = 350;
export const HUD_FADE_MS = 250;
export const HUD_CLOSE_MS = HUD_COMPLETE_HOLD_MS + HUD_FADE_MS;
export type HudPhase = 'hidden' | 'preparing' | 'recording' | 'processing' | 'completed';

export interface HudPresentation {
  revision: number;
  operation: string | null;
  phase: HudPhase;
  processingStartedAt: number | null;
  completedAt: number | null;
  audioMs: number;
}

export const EMPTY_HUD: HudPresentation = {
  revision: -1, operation: null, phase: 'hidden', processingStartedAt: null, completedAt: null, audioMs: 0,
};

export function hudOperation(progress: SessionProgress): string {
  return `${progress.session_id ?? ''}:${progress.attempt_no ?? ''}`;
}

/** Display state only. It never changes audio.end, the server state, or result budgets. */
export function advanceHud(previous: HudPresentation, progress: SessionProgress, now: number): HudPresentation {
  if (progress.revision < previous.revision) return previous;
  const operation = hudOperation(progress);
  const sameOperation = operation === previous.operation;
  const common = { revision: progress.revision, operation, audioMs: Math.max(0, progress.audio_ms) };
  if (progress.state === 'created' || (progress.state === 'streaming' && progress.capture_ready !== true)) {
    return { ...common, phase: 'preparing', processingStartedAt: null, completedAt: null };
  }
  if (progress.state === 'streaming') {
    return { ...common, phase: 'recording', processingStartedAt: null, completedAt: null };
  }
  if (progress.state === 'finalizing' || progress.state === 'refining') {
    return { ...common, phase: 'processing',
      processingStartedAt: sameOperation && previous.phase === 'processing' ? previous.processingStartedAt : now,
      completedAt: null };
  }
  if (progress.state === 'ready' && sameOperation && ['processing', 'completed'].includes(previous.phase)) {
    return { ...common, phase: 'completed', processingStartedAt: previous.processingStartedAt,
      completedAt: previous.completedAt ?? now };
  }
  return { ...common, phase: 'hidden', processingStartedAt: null, completedAt: null };
}

export function hudSlowDelayMs(audioMs: number): number {
  return 6_000 + Math.ceil(Math.max(0, audioMs) / 30_000) * 500;
}

const PROGRESS_STOPS = [[0, 0], [1_000, 80], [2_000, 90], [3_000, 95], [4_000, 97], [5_000, 98], [10_000, 99]] as const;

/** Decorative waiting animation, deliberately capped below completion. */
export function hudWaitingProgress(elapsedMs: number): number {
  const elapsed = Math.max(0, elapsedMs);
  for (let index = 1; index < PROGRESS_STOPS.length; index++) {
    const [beforeMs, beforeValue] = PROGRESS_STOPS[index - 1];
    const [afterMs, afterValue] = PROGRESS_STOPS[index];
    if (elapsed < afterMs) return beforeValue + (afterValue - beforeValue) * (elapsed - beforeMs) / (afterMs - beforeMs);
  }
  return 99;
}

export function hudFrame(presentation: HudPresentation, now: number) {
  const completedElapsed = presentation.completedAt === null ? 0 : Math.max(0, now - presentation.completedAt);
  const elapsed = presentation.processingStartedAt === null ? 0 : Math.max(0, now - presentation.processingStartedAt);
  const phase = presentation.phase === 'completed' && completedElapsed >= HUD_CLOSE_MS ? 'hidden' : presentation.phase;
  return {
    phase,
    fading: phase === 'completed' && completedElapsed >= HUD_COMPLETE_HOLD_MS,
    slow: phase === 'processing' && elapsed >= hudSlowDelayMs(presentation.audioMs),
    fill: phase === 'completed' ? 100 : hudWaitingProgress(elapsed),
  };
}
