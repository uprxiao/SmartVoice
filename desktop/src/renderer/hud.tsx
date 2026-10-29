import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  dictationLevelEvent, hudCancelEvent, hudConfirmEvent, hudCopyEvent, hudDismissEvent, hudNoticeEvent,
  hudOpenAccessibilityEvent, sessionProgressEvent, type HudNotice,
} from '@shared/events';
import { advanceHud, EMPTY_HUD, HUD_CLOSE_MS, hudFrame } from '@shared/hud-presentation';
import type { SessionProgress } from '@shared/types';
import { events, rpc } from './lib/ipc';
import { HudView } from './hud-view';
import './hud.css';

function Hud() {
  const [presentation, setPresentation] = useState(EMPTY_HUD);
  const [now, setNow] = useState(() => performance.now());
  const [level, setLevel] = useState(0);
  const [notice, setNotice] = useState<HudNotice | null>(null);
  const [locale, setLocale] = useState<'zh-CN' | 'en'>('zh-CN');
  const presentationRef = useRef(EMPTY_HUD);
  const progressRef = useRef<SessionProgress | null>(null);

  useEffect(() => {
    const apply = (next: SessionProgress) => {
      if (progressRef.current && next.revision < progressRef.current.revision) return;
      const time = performance.now();
      progressRef.current = next;
      if (next.locale) setLocale(next.locale);
      const view = advanceHud(presentationRef.current, next, time);
      presentationRef.current = view;
      setPresentation(view);
      setNow(time);
      if (view.phase !== 'recording') setLevel(0);
    };
    const offProgress = events.on(sessionProgressEvent, apply);
    const offLevel = events.on(dictationLevelEvent, value => {
      if (presentationRef.current.phase === 'recording') setLevel(value);
    });
    const offNotice = events.on(hudNoticeEvent, value => setNotice(value));
    void rpc.session.status().then(apply).catch(() => undefined);
    return () => { offProgress(); offLevel(); offNotice(); };
  }, []);

  useEffect(() => {
    if (presentation.phase !== 'processing' && presentation.phase !== 'completed') return;
    const timer = setInterval(() => {
      const time = performance.now();
      setNow(time);
      if (presentation.phase === 'completed' && presentation.completedAt !== null
        && time - presentation.completedAt >= HUD_CLOSE_MS) clearInterval(timer);
    }, 50);
    return () => clearInterval(timer);
  }, [presentation.phase, presentation.completedAt]);

  return <HudView frame={hudFrame(presentation, now)} level={level} notice={notice} locale={notice?.locale ?? locale}
    onCancel={() => events.emit(hudCancelEvent, null)}
    onConfirm={() => events.emit(hudConfirmEvent, null)}
    onCopy={() => events.emit(hudCopyEvent, null)}
    onOpenSettings={() => events.emit(hudOpenAccessibilityEvent, null)}
    onDismiss={() => events.emit(hudDismissEvent, null)} />;
}

createRoot(document.getElementById('root')!).render(<Hud />);
