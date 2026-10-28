import { useEffect, useRef, useState } from 'react';
import { dateRangeError } from '../date-range';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import type { ModelUsage } from '../types';
import { formatAudio, formatNumber, useRange } from '../lib';

const PURPOSE_LABEL: Record<string, string> = {
  asr_decode: 'ASR 增量',
  asr_finish: 'ASR 收尾',
  refine: '整理',
  audit: '忠实性审核',
  classify: '学习分类',
};

export function ModelsView({ onError }: { onError: (error: unknown) => void }) {
  const range = useRange(7);
  const [items, setItems] = useState<ModelUsage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const requestId = useRef(0);

  const load = async () => {
    const id = ++requestId.current;
    const invalid = dateRangeError(range.from, range.to);
    setLoadError(invalid);
    setLoaded(false);
    setItems([]);
    if (invalid) { setLoading(false); return; }
    setLoading(true);
    try {
      const result = await api.models(range.from, range.to);
      if (id !== requestId.current) return;
      setItems(result.items);
      setLoaded(true);
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

  const totals = items.reduce(
    (acc, item) => ({
      calls: acc.calls + item.calls,
      failures: acc.failures + item.failures,
      unknown: acc.unknown + item.unknown_calls,
      elapsed: acc.elapsed + item.elapsed_ms,
    }),
    { calls: 0, failures: 0, unknown: 0, elapsed: 0 }
  );

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">模型用量</div>
          <div className="page-sub">实际调用含重试；未知 Token 单独计数。ASR 处理音频含重复窗口，用户接收时长见概览。</div>
        </div>
        <div className="toolbar">
          <input className="input" type="date" style={{ width: 140 }} value={range.from} onChange={(event) => range.setFrom(event.target.value)} />
          <span className="muted" style={{ fontSize: 11 }}>至</span>
          <input className="input" type="date" style={{ width: 140 }} value={range.to} onChange={(event) => range.setTo(event.target.value)} />
          <button className="btn" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> 刷新
          </button>
        </div>
      </div>

      {loading && <div className="muted" role="status">正在读取数据…</div>}
      {loadError && <div className="error-box" role="alert">{loadError}。请检查后重试。</div>}

      <div className="grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)', marginBottom: 12 }}>
        <div className="card card-pad">
          <div className="stat-label">调用次数</div>
          <div className="stat-value">{loaded ? formatNumber(totals.calls) : '—'}</div>
        </div>
        <div className="card card-pad">
          <div className="stat-label">失败</div>
          <div className="stat-value" style={{ color: totals.failures ? 'var(--danger)' : undefined }}>{loaded ? formatNumber(totals.failures) : '—'}</div>
        </div>
        <div className="card card-pad">
          <div className="stat-label">结果未知</div>
          <div className="stat-value" style={{ color: totals.unknown ? 'var(--warning)' : undefined }}>{loaded ? formatNumber(totals.unknown) : '—'}</div>
        </div>
        <div className="card card-pad">
          <div className="stat-label">累计耗时</div>
          <div className="stat-value">{loaded ? formatAudio(totals.elapsed) : '—'}</div>
        </div>
      </div>

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>日期</th>
              <th>Provider / 模型</th>
              <th>用途</th>
              <th>调用</th>
              <th>成功</th>
              <th>失败</th>
              <th>未知</th>
              <th>耗时</th>
              <th>ASR 处理音频时长</th>
              <th>Token 已知（入/出）</th>
              <th>Token 未知调用（入/出）</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => (
              <tr key={`${item.date}-${item.provider}-${item.model_id}-${item.purpose}-${index}`}>
                <td className="mono">{item.date}</td>
                <td>
                  <div style={{ color: 'var(--text)' }}>{item.model_id}</div>
                  <div className="muted" style={{ fontSize: 10 }}>{item.provider}</div>
                </td>
                <td>{PURPOSE_LABEL[item.purpose] ?? item.purpose}</td>
                <td>{formatNumber(item.calls)}</td>
                <td>{formatNumber(item.successes)}</td>
                <td style={{ color: item.failures ? 'var(--danger)' : undefined }}>{formatNumber(item.failures)}</td>
                <td>
                  {item.unknown_calls > 0 ? (
                    <span className="pill warn">{item.unknown_calls} 不完整</span>
                  ) : (
                    '0'
                  )}
                </td>
                <td>{formatAudio(item.elapsed_ms)}</td>
                <td>{formatAudio(item.processed_audio_ms)}</td>
                <td>
                  {formatNumber(item.prompt_tokens_known)} / {formatNumber(item.completion_tokens_known)}
                </td>
                <td>
                  {item.prompt_tokens_unknown_calls + item.completion_tokens_unknown_calls > 0
                    ? `${item.prompt_tokens_unknown_calls} / ${item.completion_tokens_unknown_calls}`
                    : '—'}
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={11} className="empty">{loading ? '加载中…' : loadError ? '数据读取失败' : loaded ? '区间内没有模型调用记录' : '尚未读取数据'}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
