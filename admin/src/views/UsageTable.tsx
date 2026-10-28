import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api';
import { formatAudio, formatNumber } from '../lib';
import type { User } from '../types';
import { emptyUsage, UsageReader } from '../usage-reader';

/** Embedded in Overview: the date range is shared; the user filter is local. */
export function UsageTable({ from, to, onError }: {
  from: string; to: string; onError: (error: unknown) => void;
}) {
  const [state, setState] = useState(emptyUsage);
  const [selected, setSelected] = useState<User | null>(null);
  const [search, setSearch] = useState('');
  const [users, setUsers] = useState<User[]>([]);
  const [userCursor, setUserCursor] = useState<string | null>(null);
  const [userLoading, setUserLoading] = useState(false);
  const [userError, setUserError] = useState<string | null>(null);
  const [userLoaded, setUserLoaded] = useState(false);
  const userRevision = useRef(0);
  const userLifetime = useRef(0);
  const userBusy = useRef(false);
  const userQuery = useRef('');
  const labels = useRef(new Map<string, string>());
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const reader = useRef<UsageReader | null>(null);
  if (!reader.current) reader.current = new UsageReader(api, setState, (error) => onErrorRef.current(error));

  const lookupUsers = async (append = false) => {
    if (append && (userBusy.current || !userCursor)) return;
    const revision = ++userRevision.current;
    const lifetime = userLifetime.current;
    const query = append ? userQuery.current : search.trim();
    userQuery.current = query;
    userBusy.current = true;
    setUserLoading(true);
    setUserError(null);
    if (!append) { setUsers([]); setUserCursor(null); setUserLoaded(false); }
    try {
      const page = await api.users({ q: query, cursor: append ? userCursor : null });
      if (revision !== userRevision.current) return;
      const next = page.items.map((item) => item.user);
      for (const user of next) labels.current.set(user.id, user.email);
      setUsers((previous) => append ? [...previous, ...next] : next);
      setUserCursor(page.next_cursor);
      setUserLoaded(true);
    } catch (error) {
      if (lifetime !== userLifetime.current) return;
      const authorizationFailed = error instanceof ApiError && [401, 403].includes(error.status);
      if (authorizationFailed) onErrorRef.current(error);
      if (revision !== userRevision.current) return;
      setUserError(error instanceof Error ? error.message : '用户列表读取失败');
      if (!authorizationFailed) onErrorRef.current(error);
    } finally {
      if (revision === userRevision.current) {
        userBusy.current = false;
        setUserLoading(false);
      }
    }
  };

  useLayoutEffect(() => {
    void reader.current!.replace({ from, to, userId: selected?.id });
  }, [from, to, selected?.id]);

  useEffect(() => {
    void lookupUsers();
    return () => {
      reader.current!.invalidate();
      userLifetime.current += 1;
      userRevision.current += 1;
      userBusy.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const options = selected && !users.some((user) => user.id === selected.id) ? [selected, ...users] : users;
  const error = state.error instanceof Error ? state.error.message : state.error ? '使用统计读取失败' : null;
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <div className="card-pad" style={{ borderBottom: '1px solid var(--border-thin)' }}>
        <div style={{ fontSize: 12.5, fontWeight: 600 }}>按用户使用统计</div>
        <div className="muted" style={{ fontSize: 10.5, marginTop: 2 }}>
          {from} 至 {to} · Asia/Shanghai。用户筛选仅影响本表；上方概览仍为全部用户。
        </div>
        <form className="toolbar usage-toolbar" style={{ marginTop: 12 }} onSubmit={(event) => { event.preventDefault(); void lookupUsers(); }}>
          <input className="input" type="search" aria-label="搜索统计用户" placeholder="搜索用户邮箱或名称"
            value={search} onChange={(event) => setSearch(event.target.value)} />
          <button className="btn" type="submit" disabled={userLoading}>查找用户</button>
          <select className="input" aria-label="使用统计用户" value={selected?.id ?? ''}
            onChange={(event) => setSelected(options.find((user) => user.id === event.target.value) ?? null)}>
            <option value="">全部用户</option>
            {options.map((user) => <option key={user.id} value={user.id}>{user.email}</option>)}
          </select>
          {userCursor && <button className="btn" type="button" disabled={userLoading} onClick={() => void lookupUsers(true)}>更多用户</button>}
          <button className="btn" type="button" disabled={state.loading}
            onClick={() => void reader.current!.replace({ from, to, userId: selected?.id })}>刷新统计</button>
        </form>
        {userLoading && <div className="muted" role="status">正在读取可选用户…</div>}
        {userError && <div className="error-box" role="alert">{userError}。请重新查找用户。</div>}
        {!userLoading && !userError && userLoaded && users.length === 0 && <div className="muted">没有匹配的用户</div>}
        <div className="muted" style={{ fontSize: 10.5, marginTop: 8 }}>接收总时长包含失败和取消；成功口述时长只计有结果的会话。</div>
      </div>
      {state.loading && <div className="card-pad muted" role="status">正在读取使用统计…</div>}
      {error && <div className="error-box" role="alert">{error}。请刷新统计{state.nextCursor ? '或重试加载更多' : ''}。</div>}
      <div style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr><th>日期</th><th>用户</th><th>会话数</th><th>有结果</th><th>接收总时长</th><th>成功口述时长</th><th>口述字数</th></tr></thead>
          <tbody>
            {state.items.map((item) => <tr key={`${item.date}-${item.user_id}`}>
              <td className="mono">{item.date}</td>
              <td>{labels.current.get(item.user_id) ?? `用户 ${item.user_id}`}</td>
              <td>{formatNumber(item.sessions)}</td><td>{formatNumber(item.result_sessions)}</td>
              <td>{formatAudio(item.received_audio_ms)}</td><td>{formatAudio(item.successful_audio_ms)}</td>
              <td>{formatNumber(item.chars)}</td>
            </tr>)}
            {state.items.length === 0 && <tr><td colSpan={7} className="empty">
              {state.loading ? '加载中…' : error ? '使用统计读取失败' : state.loaded ? '所选日期和用户没有使用记录' : '尚未读取使用统计'}
            </td></tr>}
          </tbody>
        </table>
      </div>
      {state.nextCursor && <div className="card-pad"><button className="btn" disabled={state.loading}
        onClick={() => void reader.current!.more()}>{error ? '重试加载更多' : '加载更多'}</button></div>}
    </div>
  );
}
