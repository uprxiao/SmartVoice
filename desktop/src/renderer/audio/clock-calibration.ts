/**
 * Renderer and Main have separate performance.timeOrigin values. Every captured
 * frame is stamped with the renderer's monotonic epoch, and Main rejects frames
 * whose age reaches its two-second unacknowledged-audio budget. A per-recording
 * clock RPC used to silently fall back to offset 0 when the first call raced
 * startup, which made first recordings fail with AUDIO_BACKPRESSURE.
 *
 * Calibration now takes several samples, keeps the sample with the lowest round
 * trip, caches the offset for the renderer process lifetime, and fails closed
 * (with one short retry) instead of pretending offset 0 is calibrated.
 */
export class ClockCalibrationError extends Error {
  constructor() {
    super('CLOCK_CALIBRATION_FAILED');
    this.name = 'ClockCalibrationError';
  }
}

export interface ClockReading {
  now: number;
}

export interface ClockCalibrationOptions {
  samples?: number;
  maxRttMs?: number;
  maxOffsetMs?: number;
  /** Injectable monotonic epoch clock for tests. */
  now?: () => number;
}

export interface ClockCalibrationResult {
  offset: number;
  rttMs: number;
  accepted: number;
}

const DEFAULT_SAMPLES = 5;
const DEFAULT_MAX_RTT_MS = 500;
const DEFAULT_MAX_OFFSET_MS = 60_000;
const CACHE_TTL_MS = 10 * 60_000;

const epochNow = (): number => performance.timeOrigin + performance.now();
let cached: { offset: number; at: number } | null = null;

export async function calibrateClock(
  read: () => Promise<ClockReading>,
  options: ClockCalibrationOptions = {}
): Promise<ClockCalibrationResult> {
  const samples = Math.max(1, Math.floor(options.samples ?? DEFAULT_SAMPLES));
  const maxRttMs = options.maxRttMs ?? DEFAULT_MAX_RTT_MS;
  const maxOffsetMs = options.maxOffsetMs ?? DEFAULT_MAX_OFFSET_MS;
  const now = options.now ?? epochNow;
  const accepted: { offset: number; rttMs: number }[] = [];

  for (let index = 0; index < samples; index++) {
    const before = now();
    let reading: ClockReading;
    try {
      reading = await read();
    } catch {
      continue;
    }
    const after = now();
    const rttMs = after - before;
    const offset = Number(reading?.now) - (before + after) / 2;
    if (!Number.isFinite(offset) || Math.abs(offset) >= maxOffsetMs || rttMs < 0 || rttMs > maxRttMs) {
      continue;
    }
    accepted.push({ offset, rttMs });
  }

  if (!accepted.length) throw new ClockCalibrationError();
  accepted.sort((left, right) => left.rttMs - right.rttMs);
  const best = accepted[0];
  return { offset: best.offset, rttMs: best.rttMs, accepted: accepted.length };
}

export async function ensureClockOffset(
  read: () => Promise<ClockReading>,
  options: ClockCalibrationOptions = {}
): Promise<number> {
  const now = options.now ?? epochNow;
  if (cached && now() - cached.at < CACHE_TTL_MS) return cached.offset;

  let lastError: unknown = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const result = await calibrateClock(read, options);
      cached = { offset: result.offset, at: now() };
      return result.offset;
    } catch (error) {
      lastError = error;
      if (attempt === 0) await new Promise(resolve => setTimeout(resolve, 120));
    }
  }
  throw lastError instanceof ClockCalibrationError ? lastError : new ClockCalibrationError();
}

export function resetClockCalibration(): void {
  cached = null;
}

export function clockCalibrationCached(): boolean {
  return cached !== null;
}
