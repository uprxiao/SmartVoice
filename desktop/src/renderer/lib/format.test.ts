import { describe, expect, it } from 'vitest';
import {
  formatDuration,
  formatNumber,
  formatShortDuration,
  heatClass,
} from './format';

describe('formatDuration', () => {
  it('formats invalid and zero durations as zero', () => {
    expect(formatDuration(0)).toBe('0:00');
    expect(formatDuration(-1)).toBe('0:00');
    expect(formatDuration(Number.NaN)).toBe('0:00');
  });

  it('formats minutes and zero-padded seconds', () => {
    expect(formatDuration(59_000)).toBe('0:59');
    expect(formatDuration(60_000)).toBe('1:00');
    expect(formatDuration(3_661_000)).toBe('61:01');
  });
});

describe('formatShortDuration', () => {
  it('uses readable Chinese units', () => {
    expect(formatShortDuration(999)).toBe('不足 1 秒');
    expect(formatShortDuration(1_500)).toBe('2 秒');
    expect(formatShortDuration(65_000)).toBe('1 分 5 秒');
    expect(formatShortDuration(120_000)).toBe('2 分钟');
  });
});

describe('formatNumber', () => {
  it('switches to ten-thousands above 10000', () => {
    expect(formatNumber(9_999)).toBe('9999');
    expect(formatNumber(10_000)).toBe('1.0 万');
    expect(formatNumber(12_345)).toBe('1.2 万');
  });
});

describe('heatClass', () => {
  it('maps all heat levels to distinct classes', () => {
    // Palette values now follow the runtime visual baseline. This unit protects
    // the five distinct levels; actual colors are checked in the rendered UI.
    expect(new Set([0, 1, 2, 3, 4].map(heatClass)).size).toBe(5);
    expect(heatClass(-1)).toBe(heatClass(0));
    expect(heatClass(99)).toBe(heatClass(4));
  });
});
