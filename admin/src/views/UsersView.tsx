import { FormEvent, useEffect, useRef, useState } from 'react';
import { Loader2, Plus, RefreshCw, Search, X } from 'lucide-react';
import { api } from '../api';
import type { AdminUserItem } from '../types';
import { formatAudio, formatDateTime, formatNumber } from '../lib';

export function UsersView({ onError, currentUserId }: { onError: (error: unknown) => void; currentUserId: string }) {
  const [items, setItems] = useState<AdminUserItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestId = useRef(0);
  const mutating = useRef(false);
  const filters = useRef({ q: '', status: '' });
  const creation = useRef<{ key: string; payload: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [modalError, setModalError] = useState<string | null>(null);

  const load = async (reset = true) => {
    const id = ++requestId.current;
    const selected = reset ? { q: query.trim(), status } : filters.current;
    setLoading(true);
    setLoadError(null);
    if (reset) { setItems([]); setCursor(null); }
    try {
      const page = await api.users({
        q: selected.q || undefined,
        status: selected.status || undefined,
        cursor: reset ? null : cursor,
      });
      if (id !== requestId.current) return;
      filters.current = selected;
      setItems((previous) => (reset ? page.items : [...previous, ...page.items]));
      setCursor(page.next_cursor);
    } catch (error) {
      if (id !== requestId.current) return;
      setLoadError(error instanceof Error ? error.message : '读取用户失败');
      onError(error);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  };

  useEffect(() => {
    void load(true);
    return () => { requestId.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const closeCreate = () => {
    if (mutating.current) return;
    setCreating(false);
    setEmail('');
    setPassword('');
    setDisplayName('');
    setModalError(null);
    creation.current = null;
  };

  const submitCreate = async (event: FormEvent) => {
    event.preventDefault();
    if (mutating.current) return;
    if ([...password].length < 8 || [...password].length > 128) {
      setModalError('密码长度须为 8–128 个字符');
      return;
    }
    const payload = {
      email: email.trim().toLowerCase(), password,
      display_name: displayName.trim() || undefined,
    };
    const fingerprint = JSON.stringify(payload);
    if (!creation.current || creation.current.payload !== fingerprint) {
      creation.current = { key: crypto.randomUUID(), payload: fingerprint };
    }
    mutating.current = true;
    setBusy(true);
    setModalError(null);
    try {
      const result = await api.createUser(payload, creation.current.key);
      setCreating(false);
      setEmail('');
      setPassword('');
      setDisplayName('');
      creation.current = null;
      setNotice(`已创建普通用户 ${result.user.email}。`);
      await load(true);
    } catch (error) {
      setModalError(error instanceof Error ? error.message : '创建失败');
      onError(error);
    } finally {
      mutating.current = false;
      setBusy(false);
    }
  };

  const toggle = async (item: AdminUserItem) => {
    if (mutating.current || item.user.id === currentUserId) return;
    const next = item.user.status === 'active' ? 'disabled' : 'active';
    const prompt = next === 'disabled'
      ? `禁用 ${item.user.email}？将撤销其全部登录会话。`
      : `启用 ${item.user.email}？该用户需要重新登录。`;
    if (!window.confirm(prompt)) return;
    mutating.current = true;
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.updateUser(item.user.id, next);
      if (result.user.status !== next) throw new Error('返回状态与操作不一致，请刷新核对');
      setNotice(`${result.user.email} 已${next === 'disabled' ? '禁用' : '启用'}。`);
      await load(true);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : '用户状态修改失败，请刷新核对');
      onError(error);
    } finally {
      mutating.current = false;
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">用户管理</div>
          <div className="page-sub">按注册时间倒序；不展示任何转写正文、词条上下文或令牌。</div>
        </div>
        <div className="toolbar">
          <div style={{ position: 'relative' }}>
            <Search
              size={13}
              style={{ position: 'absolute', left: 10, top: 11, color: 'var(--text-disabled)' }}
            />
            <input
              className="input"
              style={{ width: 230, paddingLeft: 30 }}
              placeholder="搜索邮箱或显示名"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !loading && !busy) void load(true);
              }}
            />
          </div>
          <select className="input" style={{ width: 110 }} disabled={loading || busy} value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">全部状态</option>
            <option value="active">启用</option>
            <option value="disabled">停用</option>
          </select>
          <button className="btn" onClick={() => void load(true)} disabled={loading || busy}>
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} /> 刷新
          </button>
          <button className="btn primary" disabled={busy} onClick={() => { setModalError(null); setCreating(true); }}>
            <Plus size={13} /> 新建用户
          </button>
        </div>
      </div>

      {loading && <div className="muted" role="status">正在读取用户…</div>}
      {loadError && <div className="error-box" role="alert">{loadError}。请刷新核对。</div>}
      {notice && <div className="muted" role="status">{notice}</div>}

      <div className="card">
        <table className="table">
          <thead>
            <tr>
              <th>用户</th>
              <th>角色 / 状态</th>
              <th>注册时间</th>
              <th>最近登录</th>
              <th>会话</th>
              <th>音频</th>
              <th>最近会话</th>
              <th style={{ width: 90 }}>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.user.id}>
                <td>
                  <div style={{ color: 'var(--text)', fontWeight: 500 }}>
                    {item.user.display_name || item.user.email.split('@')[0]}
                  </div>
                  <div className="muted" style={{ fontSize: 10.5 }}>{item.user.email}</div>
                </td>
                <td>
                  <span className={`pill ${item.user.role === 'admin' ? 'accent' : ''}`}>{item.user.role}</span>{' '}
                  <span className={`pill ${item.user.status === 'disabled' ? 'danger' : ''}`}>
                    {item.user.status === 'active' ? '启用' : '停用'}
                  </span>
                </td>
                <td className="mono">{formatDateTime(item.user.created_at)}</td>
                <td className="mono">{formatDateTime(item.user.last_login_at)}</td>
                <td>{formatNumber(item.totals.sessions)}</td>
                <td>{formatAudio(item.totals.received_audio_ms)}</td>
                <td className="mono">{formatDateTime(item.totals.last_session_at)}</td>
                <td>
                  <button
                    className={`btn ${item.user.status === 'active' ? 'danger' : ''}`}
                    style={{ height: 27, padding: '0 9px', fontSize: 11 }}
                    disabled={busy || loading || item.user.id === currentUserId}
                    title={item.user.id === currentUserId ? '不能禁用当前管理员账号' : undefined}
                    onClick={() => void toggle(item)}
                  >
                    {item.user.status === 'active' ? '禁用' : '启用'}
                  </button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={8} className="empty">{loading ? '加载中…' : loadError ? '用户读取失败' : '没有匹配的用户'}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {cursor && (
        <div style={{ display: 'flex', justifyContent: 'center', marginTop: 14 }}>
          <button className="btn" onClick={() => void load(false)} disabled={loading || busy}>
            {loading && <Loader2 size={13} className="animate-spin" />} 加载更多
          </button>
        </div>
      )}

      {creating && (
        <div className="modal-backdrop">
          <div className="modal">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ fontSize: 14, fontWeight: 600 }}>新建用户</div>
              <button className="btn" style={{ border: 0, height: 26, padding: '0 7px' }} disabled={busy} onClick={closeCreate}>
                <X size={14} />
              </button>
            </div>
            <div className="muted" style={{ marginTop: 4, fontSize: 11 }}>
              只创建普通用户账号，不自动登录。请将初始密码安全地交给用户。
            </div>
            <form onSubmit={submitCreate}>
              <div className="field">
                <label>邮箱</label>
                <input className="input" type="email" disabled={busy} value={email} onChange={(event) => setEmail(event.target.value)} required />
              </div>
              <div className="field">
                <label>初始密码（8–128 个字符）</label>
                <input
                  className="input"
                  type="password"
                  disabled={busy}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              </div>
              <div className="field">
                <label>显示名（可选）</label>
                <input className="input" disabled={busy} value={displayName} onChange={(event) => setDisplayName(event.target.value)} maxLength={64} />
              </div>
              {modalError && <div className="error-box" role="alert">{modalError}</div>}
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
                <button type="button" className="btn" disabled={busy} onClick={closeCreate}>取消</button>
                <button type="submit" className="btn primary" disabled={busy}>
                  {busy && <Loader2 size={13} className="animate-spin" />} 创建
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
