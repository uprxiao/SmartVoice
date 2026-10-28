import { useEffect, useRef, useState } from 'react';
import { dateRangeError } from '../date-range';
import { RefreshCw } from 'lucide-react';
import { UsageTable } from './UsageTable';
import { api } from '../api';
import type { Overview } from '../types';
import { formatAudio, formatNumber, formatPercent, useRange } from '../lib';

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card card-pad">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={{ marginTop: 6 }}>{value}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

export function OverviewView({
  onError,
  onSuccess,
}: {
  onError: (error: unknown) => void;
  onSuccess?: () => void;
}) {
  const range = useRange(7);
  const [data, setData] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const requestId = useRef(0);

  const load = async () => {
    const id = ++requestId.current;
    const invalid = dateRangeError(range.from, range.to);
    setLoadError(invalid);
    setLoaded(false);
    setData(null);
    if (invalid) { setLoading(false); return; }
    setLoading(true);
    try {
      const result = await api.overview(range.from, range.to);
      if (id !== requestId.current) return;
      setData(result);
      setLoaded(true);
      onSuccess?.();
    } catch (error) {
      if (id !== requestId.current) return;
      setLoadError(error instanceof Error ? error.message : '读取失败');
      onError(error);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    return () => { requestId.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range.from, range.to]);

  const totals = data?.totals;
  const number = (value: number | undefined) => value === undefined ? '—' : formatNumber(value);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">概览</div>
          <div className="page-sub">账号、会话与整理质量；统计按 Asia/Shanghai 自然日。</div>
        </div>
        <div className="toolbar">
          <input
            className="input"
            type="date"
            aria-label="统计开始日期"
            style={{ width: 140 }}
            value={range.from}
            onChange={(event) => range.setFrom(event.target.value)}
          />
          <span className="muted" style={{ fontSize: 11 }}>至</span>
          <input
            className="input"
            type="date"
            aria-label="统计结束日期"
            style={{ width: 140 }}
            value={range.to}
            onChange={(event) => range.setTo(event.target.value)}
          />
          <button className="btn" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> 刷新概览
          </button>
        </div>
      </div>

      {loading && <div className="muted" role="status">正在读取数据…</div>}
      {loadError && <div className="error-box" role="alert">{loadError}。请检查后重试。</div>}

      <div className="grid" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
        <Stat label="注册用户" value={number(data?.registered_users)} />
        <Stat label="启用账号" value={number(data?.active_users)} hint={`停用 ${data?.disabled_users ?? '—'}`} />
        <Stat label="区间活跃用户" value={number(data?.distinct_active_users)} />
        <Stat label="会话数" value={number(totals?.sessions)} hint={`成功 ${totals?.result_sessions ?? '—'}`} />
        <Stat label="整理字数" value={number(totals?.chars)} hint={`${totals?.average_chars_per_minute ?? '—'} 字/分`} />
        <Stat
          label="处理成功率"
          value={totals ? formatPercent(totals.processing_success_rate) : '—'}
          hint={`回退 ${totals ? formatPercent(totals.fallback_rate) : '—'}`}
        />
      </div>

      <div className="grid" style={{ gridTemplateColumns: '1.2fr 1fr', marginTop: 12 }}>
        <div className="card">
          <div className="card-pad" style={{ borderBottom: '1px solid var(--border-thin)' }}>
            <div style={{ fontSize: 12.5, fontWeight: 600 }}>按日会话</div>
            <div className="muted" style={{ fontSize: 10.5, marginTop: 2 }}>区间内每天的活跃用户、会话与失败情况</div>
          </div>
          <div style={{ maxHeight: 420, overflow: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>活跃用户</th>
                  <th>会话</th>
                  <th>结果</th>
                  <th>失败</th>
                  <th>空/取消</th>
                  <th>接收总时长</th>
                </tr>
              </thead>
              <tbody>
                {(data?.daily ?? []).map((item) => (
                  <tr key={item.date}>
                    <td className="mono">{item.date}</td>
                    <td>{item.daily_active_users}</td>
                    <td>{item.sessions}</td>
                    <td>{item.result_sessions}</td>
                    <td style={{ color: item.failed_sessions ? 'var(--danger)' : undefined }}>{item.failed_sessions}</td>
                    <td>{item.empty_sessions + item.cancelled_sessions}</td>
                    <td>{formatAudio(item.received_audio_ms)}</td>
                  </tr>
                ))}
                {(data?.daily.length ?? 0) === 0 && (
                  <tr>
                    <td colSpan={7} className="empty">{loading ? '加载中…' : loadError ? '数据读取失败' : loaded ? '区间内没有数据' : '尚未读取数据'}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card card-pad">
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>区间质量</div>
          <div className="muted" style={{ fontSize: 10.5, marginTop: 2 }}>失败、空结果与回退都会保留原因，不改写已插入文字。</div>
          <div className="grid" style={{ gridTemplateColumns: '1fr 1fr', marginTop: 14 }}>
            <Stat label="结果会话" value={number(totals?.result_sessions)} />
            <Stat label="失败会话" value={number(totals?.failed_sessions)} />
            <Stat label="空结果" value={number(totals?.empty_sessions)} />
            <Stat label="已取消" value={number(totals?.cancelled_sessions)} />
            <Stat label="整理回退" value={number(totals?.fallback_sessions)} />
            <Stat label="已插入" value={number(totals?.inserted_sessions)} />
            <Stat label="学习事件" value={number(totals?.learning_events)} />
            <Stat label="插入成功率" value={totals ? formatPercent(totals.insertion_success_rate) : '—'} />
            <Stat label="接收总时长" value={totals ? formatAudio(totals.received_audio_ms) : '—'} hint="包含失败和取消的音频" />
            <Stat label="成功口述时长" value={totals ? formatAudio(totals.successful_audio_ms) : '—'} hint="只统计有结果的会话" />
          </div>
        </div>
      </div>
      <UsageTable from={range.from} to={range.to} onError={onError} />
    </>
  );
}
