import { describe, expect, it } from 'vitest';
import type { DailyStat } from '@shared/types';
import { calendarMonth, calendarWeeks } from './calendar';

const day = (date: string): DailyStat => ({ date, sessions: 2, chars: 14, successful_audio_ms: 2000, heat_level: 2 });

describe('business-date calendar layout', () => {
  it('puts Monday at row one and leaves out-of-range cells blank, without inventing statistics', () => {
    const first = day('2026-10-19'), last = day('2026-10-21');
    const weeks = calendarWeeks([first, day('2026-10-20'), last]);
    expect(weeks).toHaveLength(1);
    expect(weeks[0]).toEqual([null, first, day('2026-10-20'), last, null, null, null]);
    expect(weeks[0][1]).toBe(first);
  });
  it('crosses a year boundary and derives month labels from real dates', () => {
    const weeks = calendarWeeks([day('2025-12-31'), day('2026-01-01'), day('2026-01-04')]);
    expect(weeks).toHaveLength(2);
    expect(weeks[0][3]?.date).toBe('2025-12-31');
    expect(weeks[0][4]?.date).toBe('2026-01-01');
    expect(weeks[1][0]?.date).toBe('2026-01-04');
    expect(calendarMonth(weeks[0], true)).toBe(1);
    expect(calendarMonth(weeks[1], false)).toBeNull();
  });
  it('keeps a Sunday start, labels the initial partial month, and handles empty data', () => {
    const weeks = calendarWeeks([day('2026-10-18')]);
    expect(weeks[0][0]?.date).toBe('2026-10-18');
    expect(calendarMonth(weeks[0], true)).toBe(10);
    expect(calendarWeeks([])).toEqual([]);
  });
});
