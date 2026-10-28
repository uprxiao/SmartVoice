import { useState } from 'react';
import { observer } from 'mobx-react-lite';
import { HardDrive, Copy, Loader2, LockKeyhole, List } from 'lucide-react';
import type { Session } from '@shared/types';
import { stores } from '../stores';
import { RetentionControl } from '../components/RetentionControl';
import { RowMenu } from '../components/RowMenu';
import { IconAction } from '../components/IconAction';
import { formatDay, formatDuration, formatShortDuration, formatTime } from '../lib/format';
import { tr } from '../lib/i18n';
import { errorMessage } from '../lib/error-message';
import { historyContentExpired } from '../lib/history-retention';

type Filter = 'all' | 'dictation';

export const HistoryView = observer(function HistoryView({ onNavigate }: { onNavigate?: (route: 'home') => void }) {
  const { history, ui, session, auth } = stores;
  const locale = auth.settings?.locale ?? 'zh-CN';
  const [filter, setFilter] = useState<Filter>('all');
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const userId = auth.user?.id;
  const copy = async (item: Session, variant: 'raw' | 'refined') => {
    if (!userId || auth.user?.id !== userId || !item.result) return;
    try {
      const outcome = await history.copy(item.session_id, item.result.result_id, variant);
      if (auth.user?.id !== userId) return;
      if (outcome === 'copied') {
        ui.toast(tr(locale, variant === 'raw' ? 'home.copiedOriginal' : 'history.copiedRefined'));
      } else if (outcome === 'unavailable') {
        ui.toast(tr(locale, 'history.detailUnavailable'), 'warning');
      }
    } catch (error) {
      if (auth.user?.id === userId) ui.toast(errorMessage(error), 'error');
    }
  };

  // Both tabs cover dictation in the current product scope, including attempts
  // that were cancelled or produced no speech.
  const visible = history.items;
  const groups = new Map<string, Session[]>();
  for (const item of visible) {
    const key = formatDay(item.created_at);
    const list = groups.get(key) ?? [];
    list.push(item);
    groups.set(key, list);
  }

  return (
    <div className="mx-auto w-full max-w-[822px] pb-8">
      <div className="flex h-10 items-center">
        <h1 className="page-title">历史记录</h1>
      </div>

      <div className="mt-3 rounded-lg bg-[var(--surface)] p-3">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-[14px] font-medium">
              <HardDrive size={15} strokeWidth={1.8} />
              保存历史
            </div>
            <div className="mt-1 text-[14px] leading-5 text-[var(--text-2)]">
              选择账号历史正文与纠错片段的保留时间。
            </div>
          </div>
          <RetentionControl
            locale="zh-CN"
            className="history-retention-select"
          />
        </div>

        <div className="mt-3 border-t border-[var(--border-thin)] pt-3">
          <div className="flex items-center gap-2 text-[14px] font-medium">
            <LockKeyhole size={15} strokeWidth={1.8} />
            您的数据保持私密
          </div>
          <div className="mt-1 text-[14px] leading-5 text-[var(--text-2)]">
            语音由配置的识别与整理服务处理，使用云端识别时，语音会发送到第三方云端识别服务。历史文字保存到你的账号。音频在识别完成后释放。
          </div>
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between">
        <div className="segmented">
          <button
            className={`segmented-item ${filter === 'all' ? 'active' : ''}`}
            onClick={() => setFilter('all')}
          >
            全部
          </button>
          <button
            className={`segmented-item ${filter === 'dictation' ? 'active' : ''}`}
            onClick={() => setFilter('dictation')}
          >
            听写
          </button>
        </div>
        {history.loading && (
          <div role="status" className="flex items-center gap-1.5 text-[13px] text-[var(--text-3)]">
            <Loader2 size={14} className="animate-spin" /> {tr(locale, 'list.loadingHistory')}
          </div>
        )}
      </div>

      {history.error && (
        <div role="alert" className="mt-3 flex items-center justify-between gap-3 rounded-lg bg-[rgba(217,45,32,0.08)] px-3 py-2 text-[13px] text-[var(--danger)]">
          <span>{history.error}</span>
          <button
            className="btn-ghost shrink-0 text-[var(--danger)]"
            onClick={() => void history.load(true)}
            disabled={history.loading}
          >
            {tr(locale, 'list.retry')}
          </button>
        </div>
      )}

      <div className="mt-3 flex flex-col gap-3">
        {[...groups.entries()].map(([day, items]) => (
          <div key={day}>
            <div className="px-3 text-[12px] leading-[18px] text-[var(--text-2)]">{day}</div>
            <div className="mt-1 divide-y divide-[var(--border-thin)] overflow-hidden rounded-[10px]">
              {items.map((item) => {
                const expired = historyContentExpired(item);
                const detailKey = `${userId}:${item.session_id}:${item.result?.result_id ?? 'metadata'}`;
                const selection = session.historySelection?.sessionId === item.session_id
                  ? session.historySelection : null;
                const text = expired
                  ? tr(locale, 'home.contentExpired')
                  : item.result?.refined_text ??
                  (item.state === 'failed'
                      ? `本次识别失败${item.error_code ? `（${item.error_code}）` : ''}`
                      : item.state === 'empty'
                        ? '没有识别到语音'
                        : item.state === 'cancelled'
                          ? '本次录音已取消'
                          : '没有可用正文');
                return (
                  <div
                    key={item.session_id}
                    className="history-entry group relative rounded-[10px] px-3 py-2 transition hover:bg-[var(--fill)] focus-within:bg-[var(--fill)]"
                  >
                    <div className="flex min-h-8 items-start gap-4">
                    <div className="w-16 shrink-0 pt-0.5 text-[12px] leading-5 tabular-nums text-[var(--text-3)]">
                      {formatTime(item.created_at)}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="whitespace-pre-wrap text-[14px] leading-[22px] text-[var(--text)]">{text}</div>
                      {selection?.loading && (
                        <div role="status" className="mt-2 flex items-center gap-1.5 text-[13px] text-[var(--text-3)]">
                          <Loader2 size={14} className="animate-spin" /> {tr(locale, 'history.loadingSelection')}
                        </div>
                      )}
                      {selection?.error && (
                        <div role="alert" className="mt-2 text-[13px] text-[var(--danger)]">{selection.error}</div>
                      )}
                    </div>
                    {!expired && (item.retry_kind === 'asr' || item.retry_kind === 'refine') && (
                      <button
                        className="btn-secondary h-8 shrink-0"
                        aria-busy={selection?.loading || undefined}
                        disabled={session.busy || ['starting', 'recording', 'finalizing', 'refining'].includes(session.phase)}
                        onClick={async () => {
                          const kind = item.retry_kind;
                          if (!userId || auth.user?.id !== userId || (kind !== 'asr' && kind !== 'refine')) return;
                          if (!await session.selectHistoryForRetry(item.session_id, kind)) return;
                          if (auth.user?.id !== userId || session.progress.session_id !== item.session_id || historyContentExpired(item)) return;
                          onNavigate?.('home');
                          await session.retry(kind);
                        }}
                      >
                        {tr(locale, selection?.error ? 'history.retrySelection' : item.retry_kind === 'asr' ? 'home.attempt.asr' : 'home.attempt.refine')}
                      </button>
                    )}
                    <div className="row-hover-actions">
                      {item.result && !expired && (
                        <IconAction label={tr(locale, 'history.copyRefined')}
                          onClick={() => void copy(item, 'refined')}><Copy size={14} /></IconAction>
                      )}
                      <RowMenu label={locale === 'en' ? 'More history actions' : '更多历史操作'} items={[
                        { label: tr(locale, 'history.details'), icon: <List size={15} />, run: () => setExpandedKey(expandedKey === detailKey ? null : detailKey) },
                        ...(item.result && !expired ? [{ label: tr(locale, 'home.copyOriginal'), icon: <Copy size={14} />, run: () => copy(item, 'raw') }] : []),
                      ]} />
                    </div>
                    </div>
                    <div hidden={expandedKey !== detailKey} role="region" aria-label={tr(locale, 'history.details')}
                      className="history-detail-panel mt-2 rounded-lg bg-[var(--surface)] px-3 py-2 text-[13px] text-[var(--text-3)]">
                      <div className="mt-1 text-[11px] text-[var(--text-disabled)]">
                        {tr(locale, `history.mode.${item.mode}`)}
                        {` · ${tr(locale, `history.state.${item.state}`)}`}
                        {` · ${tr(locale, `history.insertion.${item.insertion_status ?? 'none'}`)}`}
                        {` · ${formatDuration(item.audio_ms)}`}
                        {item.result?.attempt_kind === 'asr' ? ' · 重新识别' : item.result?.attempt_kind === 'refine' ? ' · 重新整理' : ''}
                        {item.result && ` · ${tr(locale, item.result.attempt_kind === 'initial' ? 'home.stopToResult' : 'home.retryDuration', { value: formatShortDuration(item.result.stop_to_result_ms, locale) })}`}
                      </div>
                      {item.result && !expired && <>
                        <div className="mt-3 font-medium">{tr(locale, 'history.raw')}</div>
                        <div className="mt-1 whitespace-pre-wrap leading-6 text-[var(--text)]">{item.result.raw_text}</div>
                        <button className="btn-ghost mt-1.5" onClick={() => void copy(item, 'raw')}>
                          <Copy size={12} /> {tr(locale, 'home.copyOriginal')}
                        </button>
                        <div className="mt-3 font-medium">{tr(locale, 'home.result')}</div>
                        <div className="mt-1 whitespace-pre-wrap leading-6 text-[var(--text)]">{item.result.refined_text}</div>
                        <button className="btn-ghost mt-1.5" onClick={() => void copy(item, 'refined')}>
                          <Copy size={12} /> {tr(locale, 'history.copyRefined')}
                        </button>
                      </>}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}

        {history.loaded && !history.loading && !history.error && visible.length === 0 && (
          <div className="py-24 text-center">
            <div className="text-[14px] font-medium text-[var(--text-2)]">还没有语音记录</div>
            <div className="mt-1 text-[13px] text-[var(--text-3)]">回到首页开始第一段口述。</div>
          </div>
        )}
      </div>
    </div>
  );
});
