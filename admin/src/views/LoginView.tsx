import { FormEvent, useRef, useState } from 'react';
import logo from '../assets/logo.png';
import { api } from '../api';
import type { User } from '../types';

export function LoginView({ onLogin, message }: { onLogin: (user: User) => void; message?: string | null }) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submitting = useRef(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const pair = await api.login(email.trim().toLowerCase(), password);
      if (pair.user.role !== 'admin') {
        setError('该账号没有管理员权限');
        return;
      }
      onLogin(pair.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败');
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <img className="brand-mark" src={logo} alt="SmartVoiceInput" />
          <div>
            <div className="brand-name">SmartVoiceInput</div>
            <div className="brand-sub">ADMIN CONSOLE</div>
          </div>
        </div>
        <h1 style={{ margin: '24px 0 0', fontSize: 20, letterSpacing: '-0.3px' }}>管理后台登录</h1>
        <p style={{ margin: '6px 0 0', fontSize: 11.5, color: 'var(--text-3)', lineHeight: 1.7 }}>
          仅限管理员账号。普通用户请使用桌面客户端登录。
        </p>
        <div className="field">
          <label htmlFor="email">邮箱</label>
          <input
            id="email"
            className="input"
            type="email"
            disabled={busy}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="admin@company.com"
            required
          />
        </div>
        <div className="field">
          <label htmlFor="password">密码</label>
          <input
            id="password"
            className="input"
            type="password"
            disabled={busy}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="输入密码"
            required
          />
        </div>
        {(error || message) && <div className="error-box" role="alert">{error || message}</div>}
        <button
          className="btn primary"
          type="submit"
          disabled={busy}
          style={{ width: '100%', marginTop: 18, height: 38 }}
        >
          {busy ? '登录中…' : '登录'}
        </button>
        <div style={{ marginTop: 14, fontSize: 10.5, color: 'var(--text-disabled)', lineHeight: 1.7 }}>
          登录令牌只保存在当前页面内存，刷新页面需要重新登录。
        </div>
      </form>
    </div>
  );
}
