import type { HudNotice } from '@shared/events';
import { hudFrame } from '@shared/hud-presentation';

interface Props {
  frame: ReturnType<typeof hudFrame>;
  level: number;
  notice?: HudNotice | null;
  onCancel(): void;
  onConfirm(): void;
  onCopy?(): void;
  onOpenSettings?(): void;
  onDismiss?(): void;
}

export function HudView({ frame, level, notice = null, onCancel, onConfirm, onCopy, onOpenSettings, onDismiss }: Props) {
  if (notice?.kind === 'insert-failed') {
    return (
      <div className="hud-wrap">
        <div className="hud-card" role="alert">
          <div className="hud-card-text">{notice.message}</div>
          <div className="hud-card-actions">
            <button className="hud-action is-primary" onClick={onCopy}>复制结果</button>
            {notice.permission && <button className="hud-action" onClick={onOpenSettings}>去授权</button>}
            <button className="hud-action is-icon" aria-label="关闭提示" onClick={onDismiss}>
              <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    );
  }
  if (frame.phase === 'hidden') return null;
  const permissionHint = notice?.kind === 'permission' && frame.phase !== 'completed';
  const processing = frame.phase === 'processing' || frame.phase === 'completed';
  const energy = Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0;
  return (
    <div className={`hud-wrap${frame.fading ? ' is-fading' : ''}`}>
      {permissionHint
        ? <div className="hud-hint" role="status">
            <span>{notice!.message}</span>
            <button className="hud-action" onClick={onOpenSettings}>去授权</button>
          </div>
        : frame.slow && <div className="hud-slow" role="status">处理耗时比平时更长…</div>}
      <div className="hud-pill" data-phase={processing ? 'processing' : frame.phase}
        role="status" aria-label={processing ? '正在处理' : frame.phase === 'preparing' ? '准备录音' : '正在录音'}
        aria-busy={frame.phase === 'preparing' || frame.phase === 'processing'}>
        {frame.phase === 'recording' && <>
          <button className="hud-btn hud-cancel" aria-label="取消录音" onClick={onCancel}>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
          <div className="hud-wave" aria-hidden="true">
            {Array.from({ length: 10 }, (_, index) => <span key={index}
              style={{ height: `${2 + energy * 16 * (0.45 + 0.55 * Math.abs(Math.sin(index * 0.9)))}px` }} />)}
          </div>
          <button className="hud-btn hud-confirm" aria-label="完成录音" onClick={onConfirm}>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12.5l4.2 4.2L19 7" />
            </svg>
          </button>
        </>}
        {processing && <>
          <span className="hud-progress" aria-hidden="true" style={{ width: `${frame.fill}%` }} />
          <span className="hud-loading">正在处理</span>
        </>}
      </div>
    </div>
  );
}
