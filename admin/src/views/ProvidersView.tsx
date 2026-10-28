import { FormEvent, useEffect, useRef, useState } from 'react';
import { Loader2, Plus, RefreshCw, X } from 'lucide-react';
import { api } from '../api';
import { formatDateTime } from '../lib';
import {
  ACTION_LABELS, DEV_TYPES, KEYED, LOCAL_TYPES, NEEDS_URL, TYPES, createPayload, emptyForm, formError,
  needsCloudConfirmation, patchPayload, providerErrorMessage, recordLimit, rowStatus, sourceReason, testReason,
  typeShort,
} from '../providers';
import type { ActiveView, AuditItem, ProviderForm, ProviderItem, ProviderKind } from '../providers';

type Dialog =
  | { type: 'form'; kind: ProviderKind; item?: ProviderItem }
  | { type: 'activate'; item: ProviderItem }
  | { type: 'delete'; item: ProviderItem }
  | null;

const KIND_TITLE: Record<ProviderKind, string> = { asr: '语音识别', llm: '文字整理' };
const KIND_DESC: Record<ProviderKind, string> = {
  asr: '录音转文字。切换后只影响新开始的口述；进行中的口述按原配置完成。',
  llm: '把识别结果整理成可发送的文字。词库在这一步参与纠错。',
};

export function ProvidersView({ onError }: { onError: (error: unknown) => void }) {
  const [items, setItems] = useState<ProviderItem[] | null>(null);
  const [active, setActive] = useState<ActiveView | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [audit, setAudit] = useState<AuditItem[] | null>(null);
  const [auditCursor, setAuditCursor] = useState<string | null>(null);
  const requestId = useRef(0);

  const load = async () => {
    const id = ++requestId.current;
    setLoading(true);
    setLoadError(null);
    try {
      const [list, current] = await Promise.all([api.providers(), api.providersActive()]);
      if (id !== requestId.current) return;
      setItems(list.items);
      setActive(current);
    } catch (error) {
      if (id !== requestId.current) return;
      setLoadError(providerErrorMessage(error));
      onError(error);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  };

  const loadAudit = async (more = false) => {
    try {
      const page = await api.providerAudit(more ? auditCursor : null);
      setAudit((prev) => (more && prev ? [...prev, ...page.items] : page.items));
      setAuditCursor(page.next_cursor);
    } catch (error) {
      onError(error);
    }
  };

  useEffect(() => {
    void load();
    return () => { requestId.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const busy = busyId !== null;
  const localUnsupported = active ? !active.host.local_asr_supported : false;
  const devTypes = active?.host.environment === 'development';

  const runTest = async (item: ProviderItem) => {
    setBusyId(item.id);
    setNotice(null);
    try {
      await api.testProvider(item.id);
      setNotice(`“${item.name}”连接正常。`);
    } catch (error) {
      setNotice(`“${item.name}”${providerErrorMessage(error)}`);
      onError(error);
    } finally {
      setBusyId(null);
      await load();
    }
  };

  const toggle = async (item: ProviderItem) => {
    setBusyId(item.id);
    setNotice(null);
    try {
      await api.updateProvider(item.id, { version: item.version, enabled: !item.enabled });
      setNotice(`“${item.name}”已${item.enabled ? '停用' : '启用'}。`);
    } catch (error) {
      setNotice(providerErrorMessage(error));
      onError(error);
    } finally {
      setBusyId(null);
      await load();
    }
  };

  const summary = active && items && (
    active.asr.source === 'none' || active.llm.source === 'none' ? (
      <div className="error-box" role="alert">当前没有可用的语音识别服务：后台配置不可用且部署 YAML 未配置可用项。用户开始口述会收到“识别服务暂不可用”，不会改用其他服务。</div>
    ) : active.asr.source_reason || active.llm.source_reason ? (
      <div className="error-box" role="alert">
        {sourceReason(active.asr.source_reason || active.llm.source_reason)}，已回落部署 YAML 默认配置。请检查部署密钥/配置后，重新录入并测试后台配置。
      </div>
    ) : items.length === 0 ? (
      <div className="notice-box">当前来源：部署 YAML（后台尚无配置）。添加并测试通过后，可设为当前使用。</div>
    ) : (
      <div className="provider-summary">
        <div><span>当前语音识别</span><strong>{active.asr.provider?.name ?? '部署 YAML 默认'}</strong>
          <small>{active.asr.provider ? typeShort('asr', active.asr.provider.type) : '后台未选择'}
            {active.asr.max_record_seconds ? ` · 单次 ≤${active.asr.max_record_seconds} 秒` : ''}</small></div>
        <div><span>当前文字整理</span><strong>{active.llm.provider?.name ?? '部署 YAML 默认'}</strong>
          <small>{active.llm.provider?.model_id ?? '后台未选择'}</small></div>
        <div><span>配置来源</span><strong>{active.asr.source === 'db' || active.llm.source === 'db' ? '后台配置' : '部署 YAML'}</strong>
          <small>部署 YAML 作为兜底</small></div>
      </div>
    )
  );

  const section = (kind: ProviderKind) => {
    const rows = (items ?? []).filter((item) => item.kind === kind);
    return <section className="provider-section" key={kind}>
      <div className="provider-section-head">
        <div><h2>{KIND_TITLE[kind]}</h2><p>{KIND_DESC[kind]}</p></div>
        <button className="btn" disabled={busy} onClick={() => setDialog({ type: 'form', kind })}><Plus size={13} /> 添加配置</button>
      </div>
      <div className="card"><table className="table provider-table">
        <thead><tr><th>名称 / 类型</th><th>地址 / 模型</th><th>状态</th><th>最近测试</th><th>操作</th></tr></thead>
        <tbody>{rows.length === 0 ? <tr><td colSpan={5} className="muted">后台尚未添加{KIND_TITLE[kind]}配置，正在使用部署 YAML 默认项。</td></tr>
          : rows.map((item) => {
            const status = rowStatus(item);
            const hostBlocked = localUnsupported && item.type === 'qwen3_mlx';
            const testing = busyId === item.id;
            const limit = item.kind === 'asr' ? ` · 单次 ≤${item.params.max_record_seconds ?? recordLimit(item.type)} 秒` : '';
            const key = !KEYED.has(item.type) ? '无需密钥' : item.secret_set ? `密钥 ••••${item.secret_last4 ?? ''}` : '未设置密钥';
            return <tr key={item.id}>
              <td>{item.name}<small>{typeShort(item.kind, item.type)}{hostBlocked ? ' · 当前主机不支持' : ''}</small></td>
              <td>{item.base_url ?? '本机进程（无地址）'}<small>{item.model_id} · {key}{limit}</small></td>
              <td><span className={`pill ${status.tone}`}>{status.label}</span></td>
              <td>{testing ? '测试中…' : item.last_test_ok === null ? '未测试'
                : item.last_test_ok ? '成功' : `失败：${testReason(item.last_test_reason)}`}
                <small>{formatDateTime(item.last_test_at)}</small></td>
              <td className="provider-actions">
                <button className="btn small" disabled={busy} onClick={() => setDialog({ type: 'form', kind: item.kind, item })}>编辑</button>
                <button className="btn small" disabled={busy || hostBlocked} title={hostBlocked ? '当前主机不支持本机识别' : undefined}
                  onClick={() => void runTest(item)}>测试</button>
                {!item.is_active && <button className="btn small" disabled={busy || !item.enabled || hostBlocked}
                  title={!item.enabled ? '停用的配置不能设为当前' : hostBlocked ? '当前主机不支持本机识别' : undefined}
                  onClick={() => setDialog({ type: 'activate', item })}>设为当前</button>}
                <button className="btn small" disabled={busy || item.is_active} title={item.is_active ? '当前使用中的配置不能停用' : undefined}
                  onClick={() => void toggle(item)}>{item.enabled ? '停用' : '启用'}</button>
                <button className="btn small danger" disabled={busy || item.is_active} title={item.is_active ? '当前使用中的配置不能删除' : undefined}
                  onClick={() => setDialog({ type: 'delete', item })}>删除</button>
              </td>
            </tr>;
          })}</tbody>
      </table></div>
    </section>;
  };

  return <>
    <div className="page-head">
      <div><div className="page-title">模型配置</div>
        <div className="page-sub">语音识别与文字整理服务。每类可保存多个配置，选择 1 个作为当前使用。</div></div>
      <div className="toolbar">
        <button className="btn" onClick={() => void load()} disabled={loading || busy}>
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> 刷新</button>
      </div>
    </div>
    {loading && !items && <div className="muted" role="status">正在读取配置…</div>}
    {loadError && <div className="error-box" role="alert">
      暂时无法读取配置：{loadError}。读取失败不代表没有配置；当前服务仍按上次生效的配置运行。请稍后重试。</div>}
    {notice && <div className="muted" role="status" style={{ marginBottom: 10 }}>{notice}</div>}
    {items && active && <>
      {summary}
      {localUnsupported && <div className="notice-box">当前服务器不是 macOS Apple Silicon。本机识别（Qwen3-ASR）只能在该类主机运行，此处不可测试或设为当前。</div>}
      {section('asr')}
      {section('llm')}
      <p className="provider-allow">允许的云端地址（部署配置，后台不可修改）：
        {active.host.egress_policy === 'allowlist' && active.host.egress_allowlist.length
          ? active.host.egress_allowlist.map((entry) => <code key={entry}>{entry}</code>)
          : <span>当前部署不允许公网地址（仅本机/内网）</span>}</p>
      <details className="provider-audit" onToggle={(event) => { if ((event.target as HTMLDetailsElement).open && audit === null) void loadAudit(); }}>
        <summary>最近变更（仅记录字段名，不含密钥与地址内容）</summary>
        {audit === null ? <div className="muted">正在读取…</div> : <div className="card"><table className="table">
          <thead><tr><th>时间</th><th>管理员</th><th>操作</th><th>配置</th><th>变更字段</th><th>结果</th></tr></thead>
          <tbody>{audit.length === 0 ? <tr><td colSpan={6} className="muted">暂无变更</td></tr> : audit.map((row) => <tr key={row.id}>
            <td>{formatDateTime(row.at)}</td><td>{row.actor_user_id.slice(0, 8)}</td><td>{ACTION_LABELS[row.action] ?? row.action}</td>
            <td>{(items.find((item) => item.id === row.provider_id)?.name) ?? (row.provider_id ? '已删除的配置' : '—')}</td>
            <td>{row.changed_fields.length ? row.changed_fields.join('、') : '—'}</td>
            <td>{row.result === 'ok' ? '成功' : `失败${row.reason ? '：' + (testReason(row.reason) || row.reason) : ''}`}</td>
          </tr>)}</tbody></table>
          {auditCursor && <button className="btn" style={{ margin: 10 }} onClick={() => void loadAudit(true)}>加载更多</button>}</div>}
      </details>
    </>}
    {dialog?.type === 'form' && <FormDialog kind={dialog.kind} item={dialog.item} devTypes={devTypes} localUnsupported={localUnsupported}
      onClose={() => setDialog(null)} onSaved={async (message) => { setDialog(null); setNotice(message); setAudit(null); await load(); }}
      onError={onError} />}
    {dialog?.type === 'activate' && <ActivateDialog item={dialog.item} active={active}
      onClose={() => setDialog(null)} onDone={async (message) => { setDialog(null); setNotice(message); setAudit(null); await load(); }}
      onError={onError} />}
    {dialog?.type === 'delete' && <DeleteDialog item={dialog.item}
      onClose={() => setDialog(null)} onDone={async (message) => { setDialog(null); setNotice(message); setAudit(null); await load(); }}
      onError={onError} />}
  </>;
}

function Modal({ title, children, onClose, busy }: { title: string; children: React.ReactNode; onClose: () => void; busy: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = ref.current;
    node?.querySelector<HTMLElement>('input, select, button')?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onClose();
      if (event.key === 'Tab' && node) {
        const focusable = [...node.querySelectorAll<HTMLElement>('input, select, button, textarea')].filter((el) => !el.hasAttribute('disabled'));
        if (!focusable.length) return;
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [busy, onClose]);
  return <div className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
      <div style={{ fontSize: 14, fontWeight: 600 }}>{title}</div>
      <button className="btn" aria-label="关闭" disabled={busy} style={{ border: 0, height: 26, padding: '0 7px' }} onClick={onClose}><X size={14} /></button>
    </div>
    {children}
  </div></div>;
}

function FormDialog({ kind, item, devTypes, localUnsupported, onClose, onSaved, onError }: {
  kind: ProviderKind; item?: ProviderItem; devTypes: boolean; localUnsupported: boolean;
  onClose: () => void; onSaved: (message: string) => Promise<void>; onError: (error: unknown) => void;
}) {
  const [form, setForm] = useState<ProviderForm>(() => emptyForm(kind, item));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const types = devTypes ? [...TYPES[kind], DEV_TYPES[kind]] : TYPES[kind];
  const set = (patch: Partial<ProviderForm>) => setForm((prev) => ({ ...prev, ...patch }));
  const needsUrl = NEEDS_URL.has(form.type);
  const keyed = KEYED.has(form.type);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const local = formError(kind, form, item);
    if (local) { setError(local); return; }
    setBusy(true);
    setError(null);
    try {
      if (item) await api.updateProvider(item.id, patchPayload(item, form));
      else await api.createProvider(createPayload(kind, form));
      setForm((prev) => ({ ...prev, api_key: '' }));
      await onSaved(item ? `“${form.name.trim()}”已保存。` : '连通测试通过，配置已保存（密钥只显示末 4 位）。');
    } catch (caught) {
      setForm((prev) => ({ ...prev, api_key: '' }));  // never keep a typed key around after submit
      setError(providerErrorMessage(caught));
      onError(caught);
    } finally {
      setBusy(false);
    }
  };

  return <Modal title={item ? '编辑配置' : kind === 'asr' ? '添加语音识别配置' : '添加文字整理配置'} onClose={onClose} busy={busy}>
    <form onSubmit={submit}>
      <div className="field"><label htmlFor="p-name">名称</label>
        <input id="p-name" className="input" value={form.name} disabled={busy} onChange={(e) => set({ name: e.target.value })} placeholder="例如 百炼云端识别" />
        <small className="muted">1–64 个字符，同类中不能重名。</small></div>
      <div className="field"><label htmlFor="p-type">类型</label>
        <select id="p-type" className="input" value={form.type} disabled={busy || !!item}
          onChange={(e) => {
            const type = e.target.value;
            set({ type, max_record_seconds: String(Math.min(Number(form.max_record_seconds) || recordLimit(type), recordLimit(type))) });
          }}>
          {types.map((t) => <option key={t.value} value={t.value} disabled={localUnsupported && t.value === 'qwen3_mlx'}>
            {t.label}{localUnsupported && t.value === 'qwen3_mlx' ? '（当前主机不支持）' : ''}</option>)}
        </select></div>
      {needsUrl && <div className="field"><label htmlFor="p-url">地址</label>
        <input id="p-url" className="input" value={form.base_url} disabled={busy} onChange={(e) => set({ base_url: e.target.value })}
          placeholder={kind === 'asr' ? 'wss://<业务空间>.cn-beijing.maas.aliyuncs.com' : 'https://api.deepseek.com'} />
        <small className="muted">必须在部署允许的地址内；本机/内网地址按部署规则另行允许。</small></div>}
      <div className="field"><label htmlFor="p-model">模型名称</label>
        <input id="p-model" className="input" value={form.model_id} disabled={busy} onChange={(e) => set({ model_id: e.target.value })} /></div>
      {keyed && <div className="field"><label htmlFor="p-key">API Key</label>
        <input id="p-key" className="input" type="password" autoComplete="off" value={form.api_key} disabled={busy || form.clear_key}
          onChange={(e) => set({ api_key: e.target.value })}
          placeholder={item?.secret_set ? `已设置 ••••${item.secret_last4 ?? ''}，留空则保持不变` : '粘贴密钥（仅保存，之后不再显示）'} />
        <small className="muted">加密后保存，页面和接口都不会再显示原文。</small>
        {item?.secret_set && <label className="provider-inline"><input type="checkbox" checked={form.clear_key} disabled={busy}
          onChange={(e) => set({ clear_key: e.target.checked, api_key: '' })} /> 清除已保存的密钥</label>}</div>}
      {kind === 'asr' && <div className="field"><label htmlFor="p-limit">单次录音上限（秒）</label>
        <input id="p-limit" className="input" type="number" min={10} max={recordLimit(form.type)} value={form.max_record_seconds} disabled={busy}
          onChange={(e) => set({ max_record_seconds: e.target.value })} />
        <small className="muted">云端最长 360 秒（6 分钟），本机识别最长 180 秒；新上限只用于之后开始的口述。</small></div>}
      {(kind === 'llm' || form.type === 'bailian_realtime') && <div className="field"><label htmlFor="p-conc">最大并发</label>
        <input id="p-conc" className="input" type="number" min={1} value={form.max_concurrent} disabled={busy}
          onChange={(e) => set({ max_concurrent: e.target.value })} placeholder={kind === 'asr' ? '8' : '4'} />
        <small className="muted">不能超过“容量与运行”中的全局上限；留空使用默认值。</small></div>}
      {!LOCAL_TYPES.has(form.type) && <div className="notice-box">保存前会先做一次连通测试{form.type === 'bailian_realtime' ? '（云端识别会发送 1 秒静音，产生少量费用）' : ''}。测试不通过则不保存，原配置不变。</div>}
      {error && <div className="error-box" role="alert">{error}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
        <button type="button" className="btn" disabled={busy} onClick={onClose}>取消</button>
        <button type="submit" className="btn primary" disabled={busy}>{busy && <Loader2 size={13} className="animate-spin" />} {LOCAL_TYPES.has(form.type) ? '保存' : '测试并保存'}</button>
      </div>
    </form>
  </Modal>;
}

function ActivateDialog({ item, active, onClose, onDone, onError }: {
  item: ProviderItem; active: ActiveView | null; onClose: () => void; onDone: (message: string) => Promise<void>; onError: (error: unknown) => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cloud = needsCloudConfirmation(item, active);
  const submit = async () => {
    if (!active || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.activateProvider({ kind: item.kind, provider_id: item.id, version: active[item.kind].version,
        ...(cloud ? { confirm_cloud: true } : {}) });
      await onDone(`已切换为“${item.name}”，之后开始的${item.kind === 'asr' ? '口述' : '整理'}使用新配置。`);
    } catch (caught) {
      setError(providerErrorMessage(caught));
      onError(caught);
    } finally {
      setBusy(false);
    }
  };
  return <Modal title={cloud ? '启用云端语音识别' : '设为当前使用'} onClose={onClose} busy={busy}>
    {cloud ? <>
      <p className="dialog-text">设为当前后，用户之后开始的口述，<b>音频会实时发送到第三方云端识别服务</b>（{item.base_url}）进行识别。服务端不长期保存音频；进行中的口述仍按原配置完成。</p>
      <p className="dialog-text">单次录音上限将变为 {String(item.params.max_record_seconds ?? 360)} 秒。启用记录会写入变更日志。</p>
      <label className="provider-inline"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(e) => setConfirmed(e.target.checked)} /> 我已了解音频会发送到第三方云端识别服务</label>
    </> : <>
      <p className="dialog-text">将“{item.name}”设为当前{item.kind === 'asr' ? '语音识别' : '文字整理'}。切换前会再测一次连接；失败则保持原配置。</p>
      <p className="dialog-text">只影响之后开始的{item.kind === 'asr' ? '口述' : '整理'}，进行中的不受影响。</p>
    </>}
    {error && <div className="error-box" role="alert">{error}</div>}
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
      <button className="btn" disabled={busy} onClick={onClose}>取消</button>
      <button className="btn primary" disabled={busy || (cloud && !confirmed)} onClick={() => void submit()}>
        {busy && <Loader2 size={13} className="animate-spin" />} {cloud ? '确认并设为当前' : '确认切换'}</button>
    </div>
  </Modal>;
}

function DeleteDialog({ item, onClose, onDone, onError }: {
  item: ProviderItem; onClose: () => void; onDone: (message: string) => Promise<void>; onError: (error: unknown) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteProvider(item.id);
      await onDone(`“${item.name}”已删除。`);
    } catch (caught) {
      setError(providerErrorMessage(caught));
      onError(caught);
    } finally {
      setBusy(false);
    }
  };
  return <Modal title="删除配置" onClose={onClose} busy={busy}>
    <p className="dialog-text">删除“{item.name}”？保存的密钥会一起删除，无法恢复；变更日志保留。</p>
    {error && <div className="error-box" role="alert">{error}</div>}
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
      <button className="btn" disabled={busy} onClick={onClose}>取消</button>
      <button className="btn danger" disabled={busy} onClick={() => void submit()}>{busy && <Loader2 size={13} className="animate-spin" />} 确认删除</button>
    </div>
  </Modal>;
}
