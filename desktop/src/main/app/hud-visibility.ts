import { HUD_CLOSE_MS, hudOperation } from '@shared/hud-presentation';
import type { SessionProgress } from '@shared/types';

/** Window lifetime mirrors the renderer's completed transition, without claiming insertion success. */
export class HudVisibility {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private processingOperation: string | null = null;
  private closingOperation: string | null = null;
  private revision = -1;
  private generation = 0;
  // A post-result notice (e.g. insertion failed) keeps the window up until it
  // expires, is dismissed, or a new recording takes the HUD over.
  private noticeActive = false;

  constructor(
    private readonly show: () => void,
    private readonly hide: () => void,
    private readonly onNoticeEnd: () => void = () => undefined,
  ) {}

  update(progress: SessionProgress, busy: boolean): void {
    if (progress.revision < this.revision) return;
    this.revision = progress.revision;
    const operation = hudOperation(progress);
    if (busy && ['created', 'streaming', 'finalizing', 'refining'].includes(progress.state)) {
      this.endNotice(false);
      this.clearTimer();
      this.processingOperation = ['finalizing', 'refining'].includes(progress.state) ? operation : null;
      this.show();
      return;
    }
    if (this.noticeActive) return;
    if (progress.state === 'ready' && this.timer && this.closingOperation === operation) return;
    const completed = progress.state === 'ready' && this.processingOperation === operation;
    this.processingOperation = null;
    this.clearTimer();
    if (completed) {
      this.closingOperation = operation;
      const generation = this.generation;
      this.timer = setTimeout(() => {
        if (generation !== this.generation) return;
        this.timer = null;
        this.closingOperation = null;
        this.hide();
      }, HUD_CLOSE_MS);
    } else this.hide();
  }

  /** Show a notice after the session settled; the window stays until `ms` elapse or dismiss(). */
  notice(ms: number): void {
    this.clearTimer();
    this.processingOperation = null;
    this.noticeActive = true;
    this.show();
    const generation = this.generation;
    this.timer = setTimeout(() => {
      if (generation !== this.generation) return;
      this.timer = null;
      this.endNotice(true);
    }, ms);
  }

  dismiss(): void {
    if (!this.noticeActive) return;
    this.clearTimer();
    this.endNotice(true);
  }

  private endNotice(hide: boolean): void {
    if (!this.noticeActive) return;
    this.noticeActive = false;
    this.onNoticeEnd();
    if (hide) this.hide();
  }

  private clearTimer(): void {
    this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.closingOperation = null;
  }

  reset(): void {
    this.clearTimer();
    this.processingOperation = null;
    this.endNotice(false);
    this.hide();
  }
}
