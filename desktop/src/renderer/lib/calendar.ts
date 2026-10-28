import type { DailyStat } from '@shared/types';

/** Calendar dates are server business dates, not local instants. UTC arithmetic
 * keeps their weekday stable across host timezones and daylight saving time. */
export function calendarWeeks(daily: readonly DailyStat[]): (DailyStat | null)[][] {
  if (!daily.length) return [];
  const start = new Date(`${daily[0].date}T00:00:00Z`);
  const end = new Date(`${daily[daily.length - 1].date}T00:00:00Z`);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || end < start) return [];
  const byDate = new Map(daily.map(day => [day.date, day]));
  start.setUTCDate(start.getUTCDate() - start.getUTCDay());
  end.setUTCDate(end.getUTCDate() + 6 - end.getUTCDay());
  const weeks: (DailyStat | null)[][] = [];
  for (const cursor = new Date(start); cursor <= end; cursor.setUTCDate(cursor.getUTCDate() + 1)) {
    if (cursor.getUTCDay() === 0) weeks.push([]);
    weeks[weeks.length - 1].push(byDate.get(cursor.toISOString().slice(0, 10)) ?? null);
  }
  return weeks;
}

export function calendarMonth(week: readonly (DailyStat | null)[], first: boolean): number | null {
  const boundary = week.find(day => day?.date.endsWith('-01')) ?? (first ? week.find(Boolean) : null);
  return boundary ? Number(boundary.date.slice(5, 7)) : null;
}
