import { FormEvent, useEffect, useRef, useState } from 'react';
import { Gauge, Loader2, Pencil, RefreshCw, X } from 'lucide-react';
import { api } from '../api';
import type { CapacityView as CapacityData } from '../types';
import { formatNumber } from '../lib';
import { capacityInput, percentileLabel, saveCapacity, validationSummary } from '../capacity';

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <div className="card card-pad"><div className="stat-label">{label}</div>
    <div className="stat-value" style={{ marginTop: 5 }}>{value}</div>
    {hint && <div className="stat-hint">{hint}</div>}</div>;
}

export function CapacityView({ onError }: { onError: (error: unknown) => void }) {
  const [data, setData] = useState<CapacityData | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editBase, setEditBase] = useState<CapacityData['config'] | null>(null);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [needsRecheck, setNeedsRecheck] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const saving = useRef(false);
  const requestId = useRef(0);
  const readInFlight = useRef(false);

  const load = async (forEdit = false) => {
    const id = ++requestId.current;
    readInFlight.current = true;
    setLoading(true);
    setLoadError(null);
    try {
      const next = await api.capacity();
      if (id !== requestId.current) return;
      setData(next);
      setStale(false);
      if (forEdit) {
        setEditBase(next.config);
        setNeedsRecheck(false);
        setModalError(`当前生效 ${next.config.max_active_sessions} 路（版本 ${next.config.version}）。输入已保留，请重新比较后保存。`);
      }
    } catch (error) {
      if (id !== requestId.current) return;
      setStale(true);
      const message = error instanceof Error ? error.message : '容量读取失败';
      setLoadError(message);
      if (forEdit) {
        setNeedsRecheck(true);
        setModalError(`${message}；尚未确认当前配置，请稍后重新读取。`);
      }
      onError(error);
    } finally {
      if (id === requestId.current) {
        readInFlight.current = false;
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (!readInFlight.current && !saving.current) void load();
    }, 5000);
    return () => {
      window.clearInterval(timer);
      requestId.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openEditor = () => {
    if (!data || stale || loading) return;
    setEditBase(data.config);
    setValue(String(data.config.max_active_sessions));
    setNeedsRecheck(false);
    setModalError(null);
    setNotice(null);
    setEditing(true);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!data || !editBase || saving.current || readInFlight.current || needsRecheck || loading) return;
    let desired: number;
    try { desired = capacityInput(value, data.deployment.effective_ceiling); }
    catch (error) { setModalError((error as Error).message); return; }
    if (desired < editBase.max_active_sessions && !window.confirm(
      `将上限从 ${editBase.max_active_sessions} 路降至 ${desired} 路？正在进行的会话不会中断；在活动数降到新上限以下之前，新会话可能被拒绝。`
    )) return;
    saving.current = true;
    setBusy(true);
    setModalError(null);
    try {
      const outcome = await saveCapacity(api, editBase.version, desired);
      if (outcome.view) setData(outcome.view);
      setStale(outcome.view === null);
      if (outcome.kind === 'review') {
        setNeedsRecheck(true);
        setModalError(outcome.message);
      } else {
        setNotice(outcome.message);
        setEditing(false);
      }
    } catch (error) {
      setModalError(error instanceof Error ? error.message : '保存失败');
      onError(error);
    } finally {
      saving.current = false;
      setBusy(false);
    }
  };

  const runtime = data?.runtime ?? null;
  const validation = validationSummary(data);
  const count = (number: number | undefined) => number === undefined ? '未知' : formatNumber(number);
  const report = data?.validation.report;
  const draftExceeds = report && Number(value) > report.target_sessions;

  return <>
    <div className="page-head">
      <div><div className="page-title">容量与运行</div>
        <div className="page-sub">全局同时进行的语音会话上限；创建会话时重新校验，不用缓存预留槽位。</div></div>
      <div className="toolbar">
        <button className="btn" onClick={() => void load()} disabled={loading || busy || editing}>
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> 刷新</button>
        <button className="btn primary" onClick={openEditor} disabled={!data || loading || busy || stale || editing}>
          <Pencil size={13} /> 调整上限</button>
      </div>
    </div>
    {loading && <div className="muted" role="status">正在读取容量与运行状态…</div>}
    {loadError && <div className="error-box" role="alert">{loadError}。{data ? '以下为上次读取的配置，运行指标已过期。' : '配置与运行指标未知，请重试。'}</div>}
    {notice && <div className="muted" role="status">{notice}</div>}
    <div className="grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
      <Metric label="当前配置" value={data ? `${data.config.max_active_sessions} 路` : '未知'}
        hint={data ? `版本 ${data.config.version} · ${new Date(data.config.updated_at).toLocaleString('zh-CN')}${stale ? ' · 上次读取' : ''}` : undefined} />
      <Metric label="运行中" value={runtime ? `${runtime.active_sessions} 路` : '未知'}
        hint={runtime ? `可用 ${runtime.available_sessions} 路${runtime.draining ? ' · 正在收敛' : ''}${stale ? ' · 指标已过期' : ''}` : '运行指标不可用'} />
      <Metric label="部署上限" value={data ? `${data.deployment.effective_ceiling} 路` : '未知'}
        hint={data ? `配置上限 ${data.deployment.admission_ceiling} · PCM 预算 ${formatNumber(data.deployment.active_pcm_budget_bytes)} 字节` : undefined} />
      <Metric label="ASR / LLM 并发" value={data ? `${data.deployment.asr_execution_slots} / ${data.deployment.llm_max_inflight}` : '未知'} hint="执行槽与前台模型并发" />
    </div>
    <div className="grid" style={{ gridTemplateColumns: '1.1fr 1fr', marginTop: 12 }}>
      <div className="card">
        <div className="card-pad" style={{ borderBottom: '1px solid var(--border-thin)' }}>
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>实时资源</div>
          <div className="muted" style={{ fontSize: 10.5, marginTop: 2 }}>
            采样时间 {runtime ? new Date(runtime.sampled_at).toLocaleString('zh-CN') : '未知'} · 窗口 {runtime?.window_seconds ?? '未知'} 秒{stale && ' · 指标已过期'}
          </div>
        </div>
        <table className="table"><tbody>
          <tr><td>活动 PCM 字节</td><td>{count(runtime?.active_pcm_bytes)}</td></tr>
          <tr><td>预留 PCM 字节</td><td>{count(runtime?.reserved_pcm_bytes)}</td></tr>
          <tr><td>重试缓存字节</td><td>{count(runtime?.retry_pcm_bytes)}</td></tr>
          <tr><td>ASR 运行 / 增量等待 / 收尾等待</td><td>{count(runtime?.asr_running)} / {count(runtime?.asr_partial_waiting)} / {count(runtime?.asr_finish_waiting)}</td></tr>
          <tr><td>LLM 运行 / 前台等待 / 分类等待</td><td>{count(runtime?.llm_running)} / {count(runtime?.llm_foreground_waiting)} / {count(runtime?.llm_classify_waiting)}</td></tr>
          <tr><td>窗口样本</td><td>{count(runtime?.sample_count)}</td></tr>
          <tr><td>失败 / 回退</td><td>{count(runtime?.failed_count)} / {count(runtime?.fallback_count)}</td></tr>
          <tr><td>停止到结果 P50 / P95</td><td>{percentileLabel(runtime, 'p50')} / {percentileLabel(runtime, 'p95')}</td></tr>
          <tr><td>拒绝创建（窗口）</td><td>{count(runtime?.rejected_count)}</td></tr>
        </tbody></table>
      </div>
      <div className="card card-pad">
        <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}><Gauge size={15} />
          <div style={{ fontSize: 12.5, fontWeight: 600 }}>容量验证</div><span className="pill">{validation.label}</span></div>
        <div className="muted" style={{ marginTop: 8, fontSize: 11, lineHeight: 1.8 }}>{validation.description}</div>
        {report && <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>报告 {report.report_id}</div>}
        {validation.exceeds && <div className="error-box" role="status">当前上限超出验证范围；报告仅覆盖 {report?.target_sessions} 路。</div>}
        <div style={{ marginTop: 14, borderTop: '1px solid var(--border-thin)', paddingTop: 12, fontSize: 11, color: 'var(--text-3)', lineHeight: 1.8 }}>
          调整容量只会改变接纳上限，不会自动扩容 ASR/LLM 资源。报告是验证范围，不是部署硬上限。
        </div>
      </div>
    </div>
    {editing && data && editBase && <div className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-labelledby="capacity-title">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div id="capacity-title" style={{ fontSize: 14, fontWeight: 600 }}>调整全局会话上限</div>
        <button className="btn" aria-label="关闭" disabled={busy || loading} style={{ border: 0, height: 26, padding: '0 7px' }} onClick={() => setEditing(false)}><X size={14} /></button>
      </div>
      <div className="muted" style={{ marginTop: 4, fontSize: 11, lineHeight: 1.7 }}>
        编辑基于版本 {editBase.version}、上限 {editBase.max_active_sessions} 路。实际修改递增版本；同值保存不增版本。
      </div>
      <form onSubmit={submit}>
        <div className="field"><label htmlFor="capacity-value">最大同时会话数（1–{data.deployment.effective_ceiling}）</label>
          <input id="capacity-value" className="input" type="number" min={1} max={data.deployment.effective_ceiling} step={1}
            value={value} onChange={(event) => setValue(event.target.value)} disabled={busy} required /></div>
        {draftExceeds && <div className="muted">输入值超出报告验证的 {report?.target_sessions} 路；仍须满足部署上限。</div>}
        {modalError && <div className="error-box" role="alert">{modalError}</div>}
        {needsRecheck && <button className="btn" type="button" disabled={loading || busy} onClick={() => void load(true)}>读取当前配置并重新比较</button>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
          <button type="button" className="btn" disabled={busy || loading} onClick={() => setEditing(false)}>取消</button>
          <button type="submit" className="btn primary" disabled={busy || loading || needsRecheck}>{busy && <Loader2 size={13} className="animate-spin" />} 保存</button>
        </div>
      </form>
    </div></div>}
  </>;
}
