import { useCallback, useEffect, useState } from 'react';
import { Activity, Gauge, LogOut, ServerCog, SlidersHorizontal, Users } from 'lucide-react';
import logo from './assets/logo.png';
import { api, ApiError, isCurrentAuthError, onAuthInvalidated, setToken } from './api';
import type { User } from './types';
import { LoginView } from './views/LoginView';
import { OverviewView } from './views/OverviewView';
import { UsersView } from './views/UsersView';
import { ModelsView } from './views/ModelsView';
import { CapacityView } from './views/CapacityView';
import { ProvidersView } from './views/ProvidersView';

export type Route = 'overview' | 'users' | 'models' | 'providers' | 'capacity';

const NAV: Array<{ key: Route; label: string; icon: typeof Activity }> = [
  { key: 'overview', label: '概览', icon: Activity },
  { key: 'users', label: '用户管理', icon: Users },
  { key: 'models', label: '模型用量', icon: ServerCog },
  { key: 'providers', label: '模型配置', icon: SlidersHorizontal },
  { key: 'capacity', label: '容量与运行', icon: Gauge },
];

function routeFromHash(): Route {
  const value = window.location.hash.replace(/^#\/?/, '');
  return value === 'users' || value === 'models' || value === 'providers' || value === 'capacity' ? value : 'overview';
}

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [route, setRoute] = useState<Route>(routeFromHash);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const handleError = useCallback((error: unknown) => {
    if (!isCurrentAuthError(error)) return;
    if (error instanceof ApiError && [401, 403].includes(error.status)) {
      setToken(null);
      setUser(null);
      setNotice(error.status === 403 ? '管理员权限已失效，请使用有权限的账号登录' : '登录已过期，请重新登录');
      return;
    }
    setNotice(error instanceof Error ? error.message : String(error));
    setTimeout(() => setNotice(null), 5000);
  }, []);

  useEffect(() => onAuthInvalidated(handleError), [handleError]);

  if (!user) {
    return (
      <LoginView
        message={notice}
        onLogin={(next) => {
          setNotice(null);
          setUser(next);
        }}
      />
    );
  }

  const navigate = (next: Route) => {
    window.location.hash = `#/${next}`;
    setRoute(next);
  };

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-mark" src={logo} alt="SmartVoiceInput" />
          <div>
            <div className="brand-name">SmartVoiceInput</div>
            <div className="brand-sub">ADMIN CONSOLE</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                className={`nav-item ${route === item.key ? 'active' : ''}`}
                onClick={() => navigate(item.key)}
              >
                <Icon size={14} strokeWidth={1.9} />
                {item.label}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <div style={{ marginBottom: 8 }}>
            <div style={{ color: 'var(--text-2)', fontWeight: 500 }}>{user.display_name || '管理员'}</div>
            <div style={{ fontSize: 10 }}>{user.email}</div>
          </div>
          <button
            className="nav-item"
            onClick={() => {
              // Start first: the request captures this login's token now.
              // Its delayed completion must never clear a subsequent login.
              void api.logout().catch(() => undefined);
              setToken(null);
              setUser(null);
            }}
          >
            <LogOut size={14} /> 退出登录
          </button>
        </div>
      </aside>
      <main className="main">
        <div className="page">
          {notice && <div className="error-box" style={{ marginBottom: 14 }}>{notice}</div>}
          {route === 'overview' && (
            <OverviewView onError={handleError} onSuccess={() => setNotice(null)} />
          )}
          {route === 'users' && <UsersView onError={handleError} currentUserId={user.id} />}
          {route === 'models' && <ModelsView onError={handleError} />}
          {route === 'providers' && <ProvidersView onError={handleError} />}
          {route === 'capacity' && <CapacityView onError={handleError} />}
        </div>
      </main>
    </div>
  );
}
