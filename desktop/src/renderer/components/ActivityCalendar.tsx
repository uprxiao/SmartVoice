import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { DailyStat } from '@shared/types';
import { calendarMonth, calendarWeeks } from '../lib/calendar';
import { heatClass } from '../lib/format';
import { tr, type Locale } from '../lib/i18n';

export function ActivityCalendar({ daily, locale }: { daily: readonly DailyStat[]; locale: Locale }) {
  const weeks = useMemo(() => calendarWeeks(daily), [daily]);
  const viewport = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });
  const updateEdges = () => {
    const node = viewport.current;
    if (node) setEdges({ left: node.scrollLeft > 1, right: node.scrollLeft + node.clientWidth < node.scrollWidth - 1 });
  };
  useEffect(() => {
    const node = viewport.current;
    if (!node) return;
    node.scrollLeft = node.scrollWidth;
    const resize = new ResizeObserver(updateEdges);
    resize.observe(node);
    updateEdges();
    return () => resize.disconnect();
  }, [weeks]);

  return <div className="activity-calendar">
    <div className="flex gap-2">
      <div className="calendar-weekdays" aria-hidden="true">
        {['S', 'M', 'T', 'W', 'T', 'F', 'S'].map((label, index) => <span key={index}>{label}</span>)}
      </div>
      <div ref={viewport} onScroll={updateEdges} className="calendar-viewport">
        <div className="calendar-weeks" role="list" aria-label={locale === 'en' ? 'Activity by date' : '按日期统计活动'}>
          {weeks.map((week, index) => <div key={index} className="calendar-week">
            {week.map((day, weekday) => day ? <span key={day.date} role="listitem"
              aria-label={tr(locale, 'home.dayTitle', { date: day.date, sessions: day.sessions, chars: day.chars })}
              title={tr(locale, 'home.dayTitle', { date: day.date, sessions: day.sessions, chars: day.chars })}
              className={`calendar-cell ${heatClass(day.heat_level)}`} />
              : <span key={`blank-${weekday}`} className="calendar-cell invisible" aria-hidden="true" />)}
          </div>)}
        </div>
        <div className="calendar-months" aria-hidden="true">
          {weeks.map((week, index) => {
            const month = calendarMonth(week, index === 0);
            return <span key={index}>{month === null ? '' : locale === 'en'
              ? new Intl.DateTimeFormat('en', { month: 'short', timeZone: 'UTC' }).format(new Date(Date.UTC(2026, month - 1)))
              : `${month}月`}</span>;
          })}
        </div>
      </div>
    </div>
    <div className="calendar-navigation-row text-[var(--text-3)]">
      <button className="calendar-navigation" aria-label={locale === 'en' ? 'Earlier dates' : '较早日期'} disabled={!edges.left}
        onClick={() => viewport.current?.scrollBy({ left: -220, behavior: 'smooth' })}><ChevronLeft size={14} /></button>
      <button className="calendar-navigation" aria-label={locale === 'en' ? 'Later dates' : '较近日期'} disabled={!edges.right}
        onClick={() => viewport.current?.scrollBy({ left: 220, behavior: 'smooth' })}><ChevronRight size={14} /></button>
    </div>
    <div className="mt-4 flex h-3 items-center gap-1.5 text-[12px] leading-3 text-[var(--text-3)]">
      {tr(locale, 'home.heat.less')}
      {[1, 2, 3, 4].map(level => <span key={level} className={`h-[12px] w-[12px] rounded-[2px] ${heatClass(level)}`} />)}
      {tr(locale, 'home.heat.more')}
    </div>
  </div>;
}
