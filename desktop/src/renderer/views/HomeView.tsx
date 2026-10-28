import { observer } from 'mobx-react-lite';
import {
  Check,
  Clipboard,
  Loader2,
  Mic,
  RotateCcw,
  Square,
  Sparkles,
  Target,
  X,
} from 'lucide-react';
import type { Route } from './Shell';
import { stores } from '../stores';
import {
  formatDay,
  formatDuration,
  formatNumber,
  formatShortDuration,
  formatTime,
} from '../lib/format';
import { reviewReason, tr, type Locale } from '../lib/i18n';
import { friendlyShortcut } from '../lib/shortcut';
import { ActivityCalendar } from '../components/ActivityCalendar';

function unit(locale: Locale, count: number, base: string): string {
  return tr(locale, `home.unit.${base}${locale === 'en' && count === 1 ? 'One' : ''}`);
}

function Metric({
  value,
  unit,
  label,
}: {
  value: string;
  unit: string;
  label: string;
}) {
  return (
    <div className="insight-metric rounded-[20px] bg-[var(--insight-card)] p-4">
      <div className="flex items-baseline gap-1">
        <span className="text-[24px] font-[590] leading-7 tabular-nums">{value}</span>
        <span className="text-[12px] font-[510] text-[var(--text-2)]">{unit}</span>
      </div>
      <div className="mt-2 text-[14px] leading-4 text-[var(--text-3)]">{label}</div>
    </div>
  );
}

function MiniMetric({ value, unit, label }: { value: string; unit: string; label: string }) {
  return (
    <div>
      <div className="flex items-baseline gap-1">
        <span className="text-[24px] font-[590] leading-7 tabular-nums">{value}</span>
        <span className="text-[12px] font-[510] text-[var(--text-2)]">{unit}</span>
      </div>
      <div className="mt-2 text-[12px] leading-[14px] text-[var(--text-3)]">{label}</div>
    </div>
  );
}

export const HomeView = observer(function HomeView({ onNavigate }: { onNavigate: (route: Route) => void }) {
  const { session, stats, history, auth, ui } = stores;
  const shortcut = friendlyShortcut(ui.localShortcut);
  const locale = (auth.settings?.locale ?? 'zh-CN') as Locale;
  const statsReady = !stats.loading && !stats.error && stats.stats !== null;
  const daily = statsReady ? stats.stats?.daily ?? [] : [];
  const totals = statsReady ? stats.stats?.totals : undefined;
  const today = daily[daily.length - 1];

  const preparing = session.phase === 'starting';
  const capturing = session.phase === 'recording';
  const recording = capturing || preparing;
  const processing = session.phase === 'finalizing' || session.phase === 'refining';
  const result = session.result;

  const totalMinutes = Math.round((totals?.successful_audio_ms ?? 0) / 60_000);
  const totalTime = totalMinutes >= 60 ? `${(totalMinutes / 60).toFixed(1)}` : `${totalMinutes}`;
  const totalTimeUnit =
    totalMinutes >= 60
      ? unit(locale, Math.round(totalMinutes / 60), 'hours')
      : unit(locale, totalMinutes, 'minutes');

  return (
    <div className="home-page mx-auto w-full max-w-[1080px] pb-8">
        <h1 className="text-[48px] font-medium leading-[54px] tracking-[-0.6px]">
          {tr(locale, 'home.title')}
        </h1>
      <div className="home-layout mt-6">
      <div className="min-w-0 flex-1">

        {(stats.error || history.error) && (
          <div role="alert" className="mt-4 flex items-center justify-between gap-3 rounded-lg bg-[rgba(217,45,32,0.08)] px-3 py-2 text-[13px] text-[var(--danger)]">
            <span>{stats.error ?? history.error}</span>
            <button
              className="btn-ghost shrink-0 text-[var(--danger)]"
              onClick={() => {
                void stats.load(196);
                void history.load(true);
              }}
              disabled={stats.loading || history.loading}
            >
              {tr(locale, 'list.retry')}
            </button>
          </div>
        )}

        <section className="insight-surface flex flex-col gap-4 rounded-[20px] p-3">
        <div className="flex min-h-8 items-center text-[24px] font-medium leading-7 text-[var(--text-3)]">
          {tr(locale, 'home.insight')}
        </div>
        {stats.loading && (
          <div role="status" className="mt-2 flex items-center gap-1.5 text-[13px] text-[var(--text-3)]">
            <Loader2 size={13} className="animate-spin" /> {tr(locale, 'list.loadingStats')}
          </div>
        )}
        <div className="grid grid-cols-2 gap-4">
          <Metric
            value={statsReady ? formatNumber(totals?.chars ?? 0) : '—'}
            unit={unit(locale, totals?.chars ?? 0, 'chars')}
            label={tr(locale, 'home.metric.dictated')}
          />
          <Metric
            value={statsReady ? totalTime : '—'}
            unit={totalTimeUnit}
            label={tr(locale, 'home.metric.totalTime')}
          />
          <Metric
            value={statsReady ? formatNumber(totals?.average_chars_per_minute ?? 0) : '—'}
            unit={tr(locale, 'home.unit.charsPerMin')}
            label={tr(locale, 'home.metric.averageSpeed')}
          />
          <Metric
            value={statsReady ? formatNumber(today?.sessions ?? 0) : '—'}
            unit={unit(locale, today?.sessions ?? 0, 'times')}
            label={tr(locale, 'home.metric.today')}
          />
        </div>

        <div className="rounded-[16px] bg-[var(--insight-card)] p-4">
          <div className="grid grid-cols-3 gap-3">
            <MiniMetric
              value={statsReady ? formatNumber(stats.stats?.active_days_in_range ?? 0) : '—'}
              unit={unit(locale, stats.stats?.active_days_in_range ?? 0, 'days')}
              label={tr(locale, 'home.metric.activeDays')}
            />
            <MiniMetric
              value={statsReady ? formatNumber(stats.stats?.current_streak_days ?? 0) : '—'}
              unit={unit(locale, stats.stats?.current_streak_days ?? 0, 'days')}
              label={tr(locale, 'home.metric.currentStreak')}
            />
            <MiniMetric
              value={statsReady ? formatNumber(stats.stats?.longest_streak_days ?? 0) : '—'}
              unit={unit(locale, stats.stats?.longest_streak_days ?? 0, 'days')}
              label={tr(locale, 'home.metric.longestStreak')}
            />
          </div>
          <div className="mt-6"><ActivityCalendar daily={daily} locale={locale} /></div>
        </div>
        <section aria-label={tr(locale, 'home.learning.total')} className="flex h-[242px] items-center justify-between gap-4 rounded-[20px] bg-[var(--insight-card)] p-4">
          <div className="min-w-0 max-w-[60%]">
            <div className="flex items-baseline gap-1.5">
              <Sparkles size={18} className="self-center text-[var(--term-auto)]" />
              <span className="text-[24px] font-[590] leading-7 tabular-nums">{statsReady ? formatNumber(stats.stats?.cumulative_learning_events ?? 0) : '—'}</span>
              <span className="text-[12px]">{tr(locale, 'home.learning.countUnit')}</span>
            </div>
            <p className="mt-2 text-[14px] text-[var(--text-3)]">{tr(locale, 'home.learning.continuing')}</p>
            <button className="btn-secondary mt-4 h-[26px] text-[12px]" onClick={() => onNavigate('dictionary')}>{tr(locale, 'home.learning.openDictionary')}</button>
          </div>
          <div className="flex aspect-square w-[42%] max-w-[210px] shrink-0 items-center justify-center rounded-full border-[12px] border-[var(--surface)] bg-[var(--fill)] text-center">
            <div className="text-[14px] text-[var(--text-3)]">{tr(locale, 'home.learning.dictionaryCount', { n: statsReady ? formatNumber(stats.stats?.dictionary_terms ?? 0) : '—' })}</div>
          </div>
        </section>
        </section>

        <div className="mt-2.5 rounded-[20px] bg-[var(--field)] p-4">
          {!result && !processing && (
            <div className="flex flex-col items-center py-4">
              <button
                onClick={() => void session.toggle()}
                disabled={session.phase === 'starting'}
                className={`flex h-[76px] w-[76px] items-center justify-center rounded-full bg-[var(--primary-btn-bg)] text-[var(--primary-btn-text)] transition hover:scale-[1.03] ${
                  capturing ? 'recording-pulse' : ''
                }`}
                aria-label={recording ? tr(locale, 'home.aria.stop') : tr(locale, 'home.aria.start')}
              >
                {session.phase === 'starting' ? (
                  <Loader2 size={28} className="animate-spin" />
                ) : recording ? (
                  <Square size={24} fill="currentColor" />
                ) : (
                  <Mic size={28} strokeWidth={1.8} />
                )}
              </button>
              <div className="mt-3 text-[14px] font-medium">
                {preparing ? tr(locale, 'home.preparing') : capturing ? tr(locale, 'home.listening') : tr(locale, 'home.tapToStart')}
              </div>
              <div className="mt-1 text-[13px] text-[var(--text-3)]">
                {preparing ? tr(locale, 'home.preparingHint') : recording
                  ? tr(locale, 'home.tapToStop')
                  : tr(locale, 'home.shortcutOr', { value: shortcut })}
              </div>
              {!recording && !session.insertionAvailable && session.targetError && (
                <div className="mt-3 rounded-lg bg-[rgba(178,94,9,0.12)] px-2.5 py-1.5 text-[13px] text-[var(--warning)]">
                  {session.targetError}
                </div>
              )}
            </div>
          )}

          {(recording || processing) && (
            <div className="py-2">
              <div className="flex items-center justify-center gap-3">
                <div className="flex h-[46px] items-center gap-[3px]">
                  {preparing ? (
                    <span className="text-[13px] text-[var(--text-3)]">{tr(locale, 'home.preparing')}</span>
                  ) : Array.from({ length: 36 }).map((_, index) => {
                    const base = 6 + Math.abs(Math.sin(index * 1.7)) * 10;
                    const height = base + session.level * 34 * (0.5 + Math.abs(Math.cos(index * 0.9)));
                    return (
                      <span
                        key={index}
                        className="w-[3px] rounded-full bg-[var(--accent)] transition-[height] duration-100"
                        style={{ height: `${Math.max(6, height)}px`, opacity: recording ? 0.85 : 0.35 }}
                      />
                    );
                  })}
                </div>
                <div className="w-[64px] text-[14px] font-medium tabular-nums text-[var(--text-2)]">
                  {formatDuration(session.progress.audio_ms)}
                </div>
                {capturing && (
                  <button className="btn-secondary" onClick={() => void session.stop()}>
                    <Square size={12} /> {tr(locale, 'home.stopAndRefine')}
                  </button>
                )}
                {processing && (
                  <span className="flex items-center gap-1.5 text-[13px] text-[var(--text-3)]">
                    <Loader2 size={13} className="animate-spin" /> {tr(locale, 'home.refining')}
                  </span>
                )}
              </div>
              <div className="mt-4 min-h-[54px] rounded-[14px] bg-[var(--surface)] px-4 py-3 text-[14px] leading-[22px] text-[var(--text-2)]">
                {session.partialText || (
                  <span className="text-[var(--text-disabled)]">
                    {tr(locale, 'home.partialPlaceholder')}
                  </span>
                )}
              </div>
              {(recording || processing) && (
                <div className="mt-2 flex justify-center">
                  <button className="btn-ghost" onClick={() => void session.cancel()}>
                    <X size={12} /> {tr(locale, 'home.cancelRecording')}
                  </button>
                </div>
              )}
            </div>
          )}

          {!recording && !processing && result && (
            <div>
              <div className="mb-2 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-[14px] font-medium">{tr(locale, 'home.result')}</span>
                  <span className="pill">
                    {result.attempt_kind === 'initial'
                      ? tr(locale, 'home.attempt.initial')
                      : result.attempt_kind === 'asr'
                        ? tr(locale, 'home.attempt.asr')
                        : tr(locale, 'home.attempt.refine')}
                  </span>
                  <span className="text-[12px] text-[var(--text-3)]">
                    {tr(locale, result.attempt_kind === 'initial' ? 'home.stopToResult' : 'home.retryDuration', {
                      value: formatShortDuration(result.stop_to_result_ms, locale),
                    })}
                  </span>
                </div>
                <button className="btn-ghost" onClick={() => void session.start()} disabled={session.busy}>
                  <Mic size={12} /> {tr(locale, 'home.new')}
                </button>
              </div>
              {result.review_required && (
                <div className="mb-2 rounded-lg bg-[rgba(178,94,9,0.12)] px-3 py-2 text-[13px] leading-5 text-[var(--warning)]">
                  {reviewReason(locale, result.review_reason) ??
                    tr(locale, 'home.reviewNeedsConfirm')}
                  {locale === 'zh-CN' ? '。确认无误后点击“确认并插入”。' : '.'}
                </div>
              )}
              <div className="max-h-[260px] overflow-y-auto whitespace-pre-wrap rounded-[14px] bg-[var(--surface)] px-4 py-3 text-[14px] leading-[22px]">
                {result.refined_text}
              </div>
              {result.raw_text !== result.refined_text && (
                <details className="mt-2 text-[13px] text-[var(--text-3)]">
                  <summary className="cursor-pointer select-none">{tr(locale, 'home.original')}</summary>
                  <div className="mt-2 whitespace-pre-wrap rounded-lg bg-[var(--surface)] px-3 py-2 leading-6">
                    {result.raw_text}
                  </div>
                  <button
                    className="btn-ghost mt-1.5"
                    onClick={() => void session.copyRaw()}
                  >
                    <Clipboard size={12} /> {tr(locale, 'home.copyOriginal')}
                  </button>
                </details>
              )}
              {session.outcome && session.outcome.status !== 'inserted' && (
                <div className="mt-2 text-[13px] text-[var(--text-3)]">{session.outcome.message}</div>
              )}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  className="btn-primary"
                  onClick={() => void session.insert(Boolean(result.review_required))}
                  disabled={session.busy}
                >
                  {session.busy ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                  {result.review_required
                    ? tr(locale, 'home.reviewConfirmAction')
                    : tr(locale, 'home.insert')}
                </button>
                <button className="btn-secondary" onClick={() => void session.copy()} disabled={session.busy}>
                  <Clipboard size={14} /> {tr(locale, 'home.copy')}
                </button>
                {(session.outcome?.status === 'failed' ||
                  session.outcome?.status === 'unavailable') && (
                  <button
                    className="btn-secondary"
                    onClick={() => void session.retarget()}
                    disabled={session.busy}
                  >
                    <Target size={14} /> {tr(locale, 'home.retarget')}
                  </button>
                )}
                {result.review_required && (
                  <button
                    className="btn-secondary"
                    onClick={() => void session.retry('refine')}
                    disabled={session.busy}
                  >
                    <RotateCcw size={14} /> {tr(locale, 'home.attempt.refine')}
                  </button>
                )}
                <button
                  className="btn-secondary"
                  onClick={() => void session.retry('asr')}
                  disabled={session.busy || !session.retryAvailable}
                  title={
                    session.retryAvailable
                      ? tr(locale, 'home.retryAsrTitle')
                      : tr(locale, 'home.retryAsrUnavailable')
                  }
                >
                  <RotateCcw size={14} /> {tr(locale, 'home.retry')}
                </button>
              </div>
            </div>
          )}

          {!recording && !processing && !result && session.phase !== 'idle' && (
            <div className="py-6 text-center">
              <div className="text-[14px] font-medium">
                {session.phase === 'empty'
                  ? tr(locale, 'home.empty')
                  : session.phase === 'cancelled'
                    ? tr(locale, 'home.cancelled')
                    : tr(locale, 'home.failed')}
              </div>
              <div className="mt-1 text-[13px] text-[var(--text-3)]">
                {session.failureMessage ||
                  (session.phase === 'empty'
                    ? tr(locale, 'home.emptyHint')
                    : tr(locale, 'home.failedHint'))}
              </div>
              <div className="mt-3 flex justify-center gap-2">
                {session.retryAvailable && (
                  <button
                    className="btn-secondary"
                    onClick={() => void session.retry('asr')}
                    disabled={session.busy}
                    title={tr(locale, 'home.retryAsrTitle')}
                  >
                    <RotateCcw size={14} /> {tr(locale, 'home.retry')}
                  </button>
                )}
                <button className="btn-primary" onClick={() => void session.reset()}>
                  <Mic size={14} /> {tr(locale, 'home.restart')}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      <aside className="home-sidebar w-[186px] shrink-0 space-y-6 pt-3">
        <div className="rounded-[16px] bg-[var(--home-side-card)] p-4">
          <div className="text-[12px] text-[var(--text-2)]">{tr(locale, 'home.side.dictation')}</div>
          <kbd className="mt-1 inline-flex rounded-md border border-[var(--border-thin)] bg-[var(--insight-card)] px-1.5 py-0.5 font-sans text-[14px] font-[590]">{shortcut}</kbd>
          <div className="mt-1 text-[11px] leading-4 text-[var(--text-3)]">
            {tr(locale, 'home.side.startStop')}
          </div>
        </div>

        <div className="rounded-[16px] bg-[var(--home-side-card)] p-4">
          <div className="text-[12px] text-[var(--text-2)]">{tr(locale, 'home.target')}</div>
          {session.insertionAvailable ? (
            <>
              <div className="mt-1.5 flex items-center gap-1.5 text-[13px] text-[var(--text-2)]">
                <Target size={13} className="text-[var(--accent)]" />{' '}
                {session.targetApp ?? tr(locale, 'home.side.currentTarget')}
              </div>
              <div className="mt-1 text-[11px] leading-4 text-[var(--text-3)]">
                {tr(locale, 'home.side.targetHint')}
              </div>
            </>
          ) : (
            <>
              <div className="mt-1.5 text-[11px] leading-4 text-[var(--text-3)]">
                {session.targetError ?? tr(locale, 'home.side.targetMissing')}
              </div>
              <button className="btn-secondary mt-2 h-7 w-full" onClick={() => onNavigate('settings')}>
                {tr(locale, 'home.checkPermission')}
              </button>
            </>
          )}
        </div>

        <div className="rounded-[16px] bg-[var(--home-side-card)] p-4">
          <div className="text-[12px] text-[var(--text-2)]">{tr(locale, 'home.learning')}</div>
          <div className="mt-1 text-[13px] text-[var(--text)]">
            {auth.settings?.learning_enabled
              ? tr(locale, 'home.learningOn')
              : tr(locale, 'home.learningOff')}
          </div>
          <div className="mt-1 text-[11px] leading-4 text-[var(--text-3)]">
            {tr(locale, 'home.side.learningHint')}
          </div>
        </div>

        <div className="rounded-[16px] bg-[var(--home-side-card)] p-4">
          <div className="flex items-center justify-between px-1">
            <span className="text-[13px] text-[var(--text-2)]">{tr(locale, 'home.side.recent')}</span>
            <button className="text-[12px] text-[var(--accent)]" onClick={() => onNavigate('history')}>
              {tr(locale, 'home.side.all')}
            </button>
          </div>
          <div className="mt-1 flex flex-col">
            {history.loading && (
              <div role="status" className="flex items-center gap-1.5 px-1.5 py-3 text-[12px] text-[var(--text-3)]">
                <Loader2 size={13} className="animate-spin" /> {tr(locale, 'list.loadingHistory')}
              </div>
            )}
            {history.error && !history.loading && (
              <div className="px-1.5 py-3 text-[12px] text-[var(--danger)]">{tr(locale, 'list.historyUnavailable')}</div>
            )}
            {!history.loading && !history.error && history.items.slice(0, 4).map((item) => (
              <button
                key={item.session_id}
                className="flex flex-col gap-0.5 rounded-lg px-1.5 py-1.5 text-left hover:bg-[var(--fill)]"
                onClick={() => onNavigate('history')}
              >
                <div className="flex items-center justify-between text-[10px] text-[var(--text-3)]">
                  <span>{formatDay(item.created_at)} {formatTime(item.created_at)}</span>
                  <span>{formatDuration(item.audio_ms)}</span>
                </div>
                <div className="line-clamp-2 text-[12px] leading-4 text-[var(--text-2)]">
                  {item.result?.refined_text ??
                    (item.content_expired
                      ? tr(locale, 'home.contentExpired')
                      : tr(locale, 'home.noText'))}
                </div>
              </button>
            ))}
            {history.loaded && !history.loading && !history.error && history.items.length === 0 && (
              <div className="py-6 text-center text-[12px] text-[var(--text-3)]">
                {tr(locale, 'home.side.empty')}
              </div>
            )}
          </div>
        </div>
      </aside>
      </div>
    </div>
  );
});
