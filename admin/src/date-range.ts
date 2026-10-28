/** The API accepts inclusive date ranges of at most 366 days. */
export function dateRangeError(from: string, to: string): string | null {
  const parse = (value: string): number => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
    const time = Date.parse(`${value}T00:00:00Z`);
    return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value ? time : NaN;
  };
  const start = parse(from);
  const end = parse(to);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '请选择完整、有效的开始和结束日期';
  if (start > end) return '开始日期不能晚于结束日期';
  if ((end - start) / 86_400_000 + 1 > 366) return '日期范围不能超过 366 天';
  return null;
}

export function shanghaiDate(date: Date): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(date);
  const part = (type: string) => parts.find((value) => value.type === type)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function lastShanghaiDays(days: number, now = new Date()): { from: string; to: string } {
  const to = shanghaiDate(now);
  const from = new Date(`${to}T00:00:00Z`);
  from.setUTCDate(from.getUTCDate() - days + 1);
  return { from: from.toISOString().slice(0, 10), to };
}
