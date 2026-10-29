import { FormEvent, useState } from 'react';
import { observer } from 'mobx-react-lite';
import { Loader2 } from 'lucide-react';
import logo from '../assets/logo.png';
import { API_BASE_URL } from '@shared/config';
import { stores } from '../stores';

export const AuthView = observer(function AuthView() {
  const { auth, ui } = stores;
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setLocalError(null);
    if (mode === 'register' && password !== confirm) {
      setLocalError('两次输入的密码不一致');
      return;
    }
    try {
      if (mode === 'login') await auth.login(email, password);
      else await auth.register(email, password);
    } catch {
      // auth.error already holds the server message.
    }
  };

  return (
    <div className="flex h-full w-full flex-col bg-[var(--bg)]">
      <div className="drag-region h-[38px] shrink-0 bg-[var(--bg-sidebar)]" />
      <div className="flex min-h-0 flex-1">
        <div className="flex w-[460px] shrink-0 flex-col px-[64px] pb-10 pt-[64px]">
          <div className="mb-12 flex items-center gap-2.5">
            <img src={logo} alt="SmartVoiceInput" className="h-8 w-8 object-contain" />
            <span className="text-[16px] font-semibold tracking-[-0.2px]">SmartVoiceInput</span>
          </div>

          <h1 className="text-[26px] font-semibold tracking-[-0.5px]">
            {mode === 'login' ? '欢迎回来' : '创建账号'}
          </h1>
          <p className="mt-2 text-[13px] leading-6 text-[var(--text-3)]">
            {mode === 'login'
              ? '登录后即可在任何输入框里，把语音变成整理好的文字。'
              : '注册后自动登录。使用云端识别时，语音会发送到第三方云端识别服务；不会长期保存音频。'}
          </p>

          <div className="mt-8 flex rounded-lg bg-[var(--bg-sidebar)] p-[3px]">
            {(['login', 'register'] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => {
                  setMode(value);
                  setLocalError(null);
                  auth.clearError();
                }}
                className={`h-[32px] flex-1 rounded-[6px] text-[12.5px] transition ${
                  mode === value
                    ? 'bg-[var(--bg)] font-medium text-[var(--text)] shadow-[0_1px_2px_rgba(0,0,0,0.08)]'
                    : 'text-[var(--text-3)]'
                }`}
              >
                {value === 'login' ? '登录' : '注册'}
              </button>
            ))}
          </div>

          <form className="mt-6 flex flex-col gap-3.5" onSubmit={submit}>
            <label className="flex flex-col gap-1.5">
              <span className="text-[12px] font-medium text-[var(--text-2)]">邮箱</span>
              <input
                className="input"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@company.com"
                required
              />
            </label>
            <label className="flex flex-col gap-1.5">
              <span className="text-[12px] font-medium text-[var(--text-2)]">密码</span>
              <input
                className="input"
                type="password"
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="至少 8 个字符"
                minLength={8}
                required
              />
            </label>
            {mode === 'register' && (
              <label className="flex flex-col gap-1.5">
                <span className="text-[12px] font-medium text-[var(--text-2)]">确认密码</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  placeholder="再次输入密码"
                  minLength={8}
                  required
                />
              </label>
            )}
            {(localError || auth.error) && (
              <div className="rounded-lg bg-[rgba(217,45,32,0.06)] px-3 py-2 text-[12px] leading-5 text-[var(--danger)]">
                {localError || auth.error}
              </div>
            )}
            <button className="btn-primary mt-2 h-[40px]" type="submit" disabled={auth.busy}>
              {auth.busy && <Loader2 size={14} className="animate-spin" />}
              {mode === 'login' ? '登录' : '注册并登录'}
            </button>
          </form>

          <div className="mt-auto text-[11.5px] leading-5 text-[var(--text-disabled)]">
            账号仅用于同步词库与记录；密码使用 Argon2id 存储，客户端只保存加密后的登录令牌。
          </div>
        </div>

        <div className="relative flex flex-1 flex-col justify-center overflow-hidden bg-[var(--bg-sidebar)] px-[64px]">
          <div className="pointer-events-none absolute -right-24 -top-24 h-[420px] w-[420px] rounded-full bg-[radial-gradient(circle,rgba(31,93,242,0.12),transparent_65%)]" />
          <div className="pointer-events-none absolute -bottom-32 left-10 h-[360px] w-[360px] rounded-full bg-[radial-gradient(circle,rgba(17,17,17,0.06),transparent_65%)]" />
          <div className="relative max-w-[430px]">
            <div className="pill accent mb-6">本地内网 ASR · 可配置 LLM</div>
            <h2 className="text-[30px] font-semibold leading-[1.28] tracking-[-0.6px]">
              把说出的话，
              <br />
              变成可直接发送的文字。
            </h2>
            <p className="mt-4 text-[13.5px] leading-7 text-[var(--text-3)]">
              按一下快捷键开始录音，再按一下停止。SmartVoiceInput 会识别并整理文字，再填入结果返回时的当前输入框；需要确认时会保留结果供你检查或复制。
            </p>
            <div className="mt-10 flex flex-col gap-5">
              {[
                ['01', '流式识别', '250ms 音频分块持续上传，说完即可看到结果。'],
                ['02', '忠实整理', '保留约束、数字与专名；不确定时回退原文并提示确认。'],
                ['03', '越用越准', '局部词条修正会进入个人词库，下一次自动纠正。'],
              ].map(([index, title, body]) => (
                <div key={index} className="flex gap-4">
                  <span className="mt-0.5 text-[11px] font-medium tabular-nums text-[var(--text-disabled)]">
                    {index}
                  </span>
                  <div>
                    <div className="text-[13.5px] font-medium">{title}</div>
                    <div className="mt-1 text-[12.5px] leading-6 text-[var(--text-3)]">{body}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="absolute bottom-8 left-[64px] right-[64px] flex items-center justify-between text-[11px] text-[var(--text-disabled)]">
            <span>{ui.appInfo?.version ? `v${ui.appInfo.version}` : '—'} · 自托管服务</span>
            <span>{API_BASE_URL.replace(/^https?:\/\//, '')}</span>
          </div>
        </div>
      </div>
    </div>
  );
});
