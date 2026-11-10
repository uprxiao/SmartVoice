import { afterEach, describe, expect, it } from 'vitest';
import {
  calibrateClock, ClockCalibrationError, clockCalibrationCached, ensureClockOffset, resetClockCalibration,
} from './clock-calibration';

interface Sample {
  rttMs: number;
  offset: number;
}

/** A renderer clock and a main clock whose midpoint differs by `offset`. */
function fakeClock(samples: Sample[], onRead?: () => void) {
  let renderer = 1_000;
  const now = () => renderer;
  let index = 0;
  const read = async () => {
    const sample = samples[index++] ?? samples.at(-1)!;
    const before = renderer;
    renderer += sample.rttMs;
    onRead?.();
    return { now: before + sample.rttMs / 2 + sample.offset };
  };
  return { now, read };
}

afterEach(() => resetClockCalibration());

describe('renderer/main clock calibration', () => {
  it('uses the lowest-round-trip sample instead of a slow startup sample', async () => {
    const clock = fakeClock([
      { rttMs: 320, offset: 8_000 },
      { rttMs: 24, offset: 8_000 },
      { rttMs: 410, offset: 8_000 },
    ]);
    const result = await calibrateClock(clock.read, { samples: 3, now: clock.now });
    expect(result.offset).toBeCloseTo(8_000, 6);
    expect(result.rttMs).toBe(24);
    expect(result.accepted).toBe(3);
  });

  it('discards slow round trips and offsets outside the safe range', async () => {
    const clock = fakeClock([
      { rttMs: 900, offset: 5_000 },
      { rttMs: 20, offset: 90_000 },
      { rttMs: 22, offset: 5_000 },
    ]);
    const result = await calibrateClock(clock.read, { samples: 3, now: clock.now, maxRttMs: 500, maxOffsetMs: 60_000 });
    expect(result.offset).toBeCloseTo(5_000, 6);
    expect(result.rttMs).toBe(22);
  });

  it('fails closed when no usable sample exists', async () => {
    const clock = fakeClock([{ rttMs: 900, offset: 5_000 }]);
    await expect(calibrateClock(clock.read, { samples: 3, now: clock.now, maxRttMs: 500 }))
      .rejects.toBeInstanceOf(ClockCalibrationError);
  });

  it('caches the calibrated offset for the renderer process lifetime', async () => {
    let reads = 0;
    const clock = fakeClock([{ rttMs: 12, offset: 3_000 }], () => { reads++; });
    const first = await ensureClockOffset(clock.read, { samples: 2, now: clock.now });
    expect(first).toBeCloseTo(3_000, 6);
    expect(reads).toBe(2);
    const second = await ensureClockOffset(clock.read, { samples: 2, now: clock.now });
    expect(second).toBe(first);
    expect(reads).toBe(2);
    expect(clockCalibrationCached()).toBe(true);
  });
});
