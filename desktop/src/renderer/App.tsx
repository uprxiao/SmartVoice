import { useEffect } from 'react';
import { reaction } from 'mobx';
import { observer } from 'mobx-react-lite';
import { authChangedEvent, hotkeyToggleEvent, sessionResultEvent } from '@shared/events';
import { events } from './lib/ipc';
import { initTheme } from './lib/theme';
import { stores } from './stores';
import { AuthView } from './views/AuthView';
import { Shell } from './views/Shell';
import { ToastStack } from './components/ToastStack';
import { LearningSuggestions } from './components/LearningSuggestions';

const { auth, session, stats, history, dictionary, ui } = stores;

export const App = observer(function App() {
  useEffect(() => {
    initTheme();
    session.init();
    // An unavailable local info read remains unknown; never imply the default key.
    void ui.loadAppInfo().catch(() => undefined);
    const offAccount = reaction(() => auth.user?.id ?? null, () => {
      history.clear(); stats.clear(); dictionary.clear(); ui.clear();
    });
    void auth.boot();
    const offAuth = events.on(authChangedEvent, () => {
      void auth.boot();
    });
    const offHotkey = events.on(hotkeyToggleEvent, () => {
      if (!auth.user) return;
      void session.toggle();
    });
    const offResult = events.on(sessionResultEvent, (progress) => {
      if (!auth.user) return;
      if (['ready', 'empty', 'failed', 'cancelled'].includes(progress.state)) {
        void stats.load(196);
        void history.load(true);
      }
    });
    // Only this application's document owns Escape; dialogs and shortcut capture
    // get the first opportunity to consume it. Never register a global Escape.
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented || event.repeat || event.isComposing || !auth.user) return;
      if (!['starting', 'recording', 'finalizing', 'refining'].includes(session.phase)) return;
      event.preventDefault();
      void session.cancel();
    };
    window.addEventListener('keydown', onEscape);
    return () => {
      window.removeEventListener('keydown', onEscape);
      offAccount();
      offAuth();
      offHotkey();
      offResult();
    };
  }, []);

  if (auth.booting) {
    return (
      <div className="flex h-full items-center justify-center bg-[var(--bg)]">
        <div className="flex flex-col items-center gap-3">
          <div className="h-7 w-7 animate-spin rounded-full border-2 border-[var(--border)] border-t-[var(--accent)]" />
          <div className="text-[12px] text-[var(--text-3)]">正在启动 SmartVoiceInput…</div>
        </div>
      </div>
    );
  }

  return (
    <>
      {auth.user ? <Shell /> : <AuthView />}
      <ToastStack />
      {auth.user && <LearningSuggestions />}
    </>
  );
});
