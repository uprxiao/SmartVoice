import { observer } from 'mobx-react-lite';
import { AlertTriangle, CheckCircle2, X } from 'lucide-react';
import { stores } from '../stores';

export const ToastStack = observer(function ToastStack() {
  const { ui } = stores;
  if (ui.toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-5 left-1/2 z-50 flex w-[420px] -translate-x-1/2 flex-col gap-2">
      {ui.toasts.map((toast) => (
        <div
          key={toast.id}
          className="pointer-events-auto flex items-center gap-2.5 rounded-lg bg-[var(--bg)] px-3.5 py-2.5 shadow-[0_8px_30px_rgba(0,0,0,0.18)]"
        >
          {toast.tone === 'error' ? (
            <AlertTriangle size={15} className="shrink-0 text-[var(--danger)]" />
          ) : toast.tone === 'warning' ? (
            <AlertTriangle size={15} className="shrink-0 text-[var(--warning)]" />
          ) : (
            <CheckCircle2 size={15} className="shrink-0 text-[var(--success)]" />
          )}
          <span className="flex-1 text-[12.5px] leading-5 text-[var(--text-2)]">{toast.message}</span>
          <button className="btn-ghost h-6 w-6 px-0" onClick={() => ui.dismiss(toast.id)} aria-label="关闭提示">
            <X size={13} />
          </button>
        </div>
      ))}
    </div>
  );
});
