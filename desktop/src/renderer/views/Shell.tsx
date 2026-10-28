import { useEffect, useState } from 'react';
import { observer } from 'mobx-react-lite';
import { BookOpen, History, Home, LogOut, Settings } from 'lucide-react';
import logo from '../assets/logo.png';
import { stores } from '../stores';
import { tr } from '../lib/i18n';
import { HomeView } from './HomeView';
import { HistoryView } from './HistoryView';
import { DictionaryView } from './DictionaryView';
import { SettingsView } from './SettingsView';

export type Route = 'home' | 'history' | 'dictionary' | 'settings';

const NAV: Array<{ key: Route; labelKey: string; icon: typeof Home }> = [
  { key: 'home', labelKey: 'nav.home', icon: Home },
  { key: 'history', labelKey: 'nav.history', icon: History },
  { key: 'dictionary', labelKey: 'nav.dictionary', icon: BookOpen },
];

export const Shell = observer(function Shell() {
  const { auth, history, dictionary, stats, session, ui } = stores;
  const locale = auth.settings?.locale ?? 'zh-CN';
  const [route, setRoute] = useState<Route>(() => {
    const saved = localStorage.getItem('svi.route');
    return saved === 'history' || saved === 'dictionary' ? saved : 'home';
  });
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    localStorage.setItem('svi.route', route);
    if (route === 'home') {
      void stats.load(196);
      void history.load(true);
    } else if (route === 'history') {
      void history.load(true);
    } else if (route === 'dictionary') {
      void dictionary.load(true);
    }
  }, [route]);

  const navigate = (next: Route) => {
    if (next === 'settings') {
      setSettingsOpen(true);
      return;
    }
    setRoute(next);
  };

  return (
    <div className="flex h-full w-full overflow-hidden bg-[var(--bg)]">
      <aside className="flex w-[210px] shrink-0 flex-col py-2 pl-2">
        <div className="flex min-h-0 flex-1 flex-col rounded-[20px] bg-[var(--bg-sidebar)] px-3 pb-2 pt-[44px]">
          <div className="flex h-12 items-center gap-2 px-2 pb-4">
            <img src={logo} alt="SmartVoiceInput" className="h-[22px] w-[22px] object-contain" />
            <span className="text-[15px] font-semibold tracking-[-0.2px]">SmartVoiceInput</span>
          </div>

          <nav className="flex flex-col gap-0.5">
            {NAV.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.key}
                  className={`nav-item no-drag ${route === item.key ? 'active' : ''}`}
                  onClick={() => navigate(item.key)}
                >
                  <Icon size={16} strokeWidth={1.8} />
                  {tr(locale, item.labelKey)}
                  {item.key === 'home' && session.recording && (
                    <span className="ml-auto h-[6px] w-[6px] rounded-full bg-[var(--danger)]" />
                  )}
                </button>
              );
            })}
          </nav>

          <div className="mt-auto border-t border-[var(--border-thin)] px-1 pt-2">
            <div className="flex items-center gap-2.5 rounded-lg px-1 py-1.5">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[var(--surface-strong)] text-[12px] font-semibold">
                {(auth.user?.display_name || auth.user?.email || 'U').slice(0, 1).toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px]">
                  {auth.user?.display_name || auth.user?.email.split('@')[0]}
                </div>
                <div className="truncate text-[11px] text-[var(--text-3)]">{auth.user?.email}</div>
              </div>
              <button
                className="btn-ghost h-7 w-7 px-0"
                title={tr(locale, 'shell.settings')}
                onClick={() => setSettingsOpen(true)}
              >
                <Settings size={14} />
              </button>
              <button
                className="btn-ghost h-7 w-7 px-0"
                title={tr(locale, 'shell.logout')}
                onClick={async () => {
                  await session.reset().catch(() => undefined);
                  try { await auth.logout(); ui.toast(tr(locale, 'shell.loggedOut')); }
                  catch { /* AuthView displays the stored logout/persistence error. */ }
                }}
              >
                <LogOut size={14} />
              </button>
            </div>
            {auth.snapshot?.memory_only && (
              <div className="mt-1.5 px-1 text-[10px] leading-3.5 text-[var(--warning)]">
                {tr(locale, 'shell.memoryOnly')}
              </div>
            )}
          </div>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <div className="drag-region h-[36px] shrink-0" />
        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-4">
          {route === 'home' && <HomeView onNavigate={navigate} />}
          {route === 'history' && <HistoryView onNavigate={navigate} />}
          {route === 'dictionary' && <DictionaryView />}
        </div>
      </main>

      {settingsOpen && <SettingsView onClose={() => setSettingsOpen(false)} />}
    </div>
  );
});
