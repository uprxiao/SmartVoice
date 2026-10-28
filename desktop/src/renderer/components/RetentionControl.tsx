import { useEffect, useRef, useState } from 'react';
import { Dialog } from '@base-ui/react/dialog';
import { observer } from 'mobx-react-lite';
import { stores } from '../stores';
import { errorMessage } from '../lib/error-message';
import { tr, type Locale } from '../lib/i18n';
import { RetentionChange } from '../lib/retention-change';

export const RetentionControl = observer(function RetentionControl({
  locale,
  className,
  disabled = false,
}: { locale: Locale; className: string; disabled?: boolean }) {
  const { auth, history, ui } = stores;
  const active = useRef(true);
  const select = useRef<HTMLSelectElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [change] = useState(() => new RetentionChange({
    read: () => auth.user && auth.settings
      ? { userId: auth.user.id, version: auth.settings.version, days: auth.settings.history_retention_days }
      : null,
    save: (patch) => auth.updateSettings(patch),
    clearHistory: () => history.clear(),
    reloadHistory: () => history.load(true),
  }));
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; change.cancel(); };
  }, [change]);
  const act = async (action: () => Promise<boolean>) => {
    if (await action() && active.current) ui.toast(tr(locale, 'settings.saved'));
  };
  const failure = change.stale
    ? tr(locale, 'settings.retention.changed')
    : change.error ? errorMessage(change.error) || tr(locale, 'settings.saveFailed') : null;

  return (
    <div className="shrink-0">
      <select
        ref={select}
        aria-label={tr(locale, 'settings.retention.label')}
        className={className}
        value={auth.settings?.history_retention_days ?? ''}
        disabled={!auth.settings || disabled || change.busy}
        onChange={(event) => {
          const next = event.target.value ? Number(event.target.value) : null;
          void act(() => change.request(next));
        }}
      >
        <option value="">{tr(locale, 'settings.retention.forever')}</option>
        {[7, 30, 90].map((days) => <option key={days} value={days}>{tr(locale, 'settings.retention.days', { n: days })}</option>)}
      </select>
      {failure && !change.needsConfirmation && <p role="alert" className="mt-2 max-w-[240px] text-[13px] text-[var(--danger)]">{failure}</p>}
      <Dialog.Root
        open={change.needsConfirmation}
        modal
        disablePointerDismissal
        onOpenChange={(open, details) => {
          if (open) return;
          if (change.busy) details.cancel();
          else change.cancel();
        }}
      >
        <Dialog.Portal>
          <Dialog.Backdrop className="fixed inset-0 z-[60] bg-black/45 backdrop-blur-[2px]" onMouseDown={(event) => event.stopPropagation()} />
          <Dialog.Popup
            role="dialog"
            aria-modal="true"
            initialFocus={cancel}
            finalFocus={select}
            onMouseDown={(event) => event.stopPropagation()}
            className="fixed left-1/2 top-1/2 z-[61] w-[min(460px,90vw)] -translate-x-1/2 -translate-y-1/2 rounded-[20px] bg-[var(--dialog-bg)] p-6 text-[var(--text)] shadow-[0_30px_90px_rgba(0,0,0,0.45)]"
          >
            <Dialog.Title className="text-[18px] font-semibold">{tr(locale, 'settings.retention.confirmTitle')}</Dialog.Title>
            <Dialog.Description className="mt-3 text-[14px] leading-6 text-[var(--text-2)]">
              {tr(locale, 'settings.retention.confirmDescription', { n: change.pending?.next ?? '' })}
            </Dialog.Description>
            {failure && <p role="alert" className="mt-3 text-[13px] text-[var(--danger)]">{failure}</p>}
            <div className="mt-6 flex justify-end gap-3">
              <Dialog.Close ref={cancel} disabled={change.busy} className="btn-secondary">{tr(locale, 'settings.retention.cancel')}</Dialog.Close>
              <button type="button" disabled={change.busy} className="btn-primary" onClick={() => void act(() => change.confirm())}>
                {tr(locale, change.busy ? 'settings.retention.saving' : 'settings.retention.confirm')}
              </button>
            </div>
          </Dialog.Popup>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
});
