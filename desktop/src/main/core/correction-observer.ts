import { randomUUID } from 'node:crypto';
import { singleReplacement, type Replacement } from './replacement';

export interface ObservationIdentity {
  readonly origin: string;
  readonly userId: string;
  readonly authEpoch: number;
  readonly sessionId: string;
  readonly resultId: string;
  readonly insertionId: string;
  readonly targetId: string;
}
export type ObservationEndReason = 'submitted' | 'unchanged' | 'invalid_replacement' |
  'receipt_unconfirmed' | 'target_changed' | 'outside_changed' | 'not_observable' |
  'ime_unknown' | 'ime_uncommitted' | 'deadline' | 'submit_failed' | 'cancelled' | 'identity_changed';
export interface ObservationSample {
  valid: boolean;
  text?: string;
  composition: 'committed' | 'composing' | 'unknown';
  reason?: string;
}
export interface CorrectionSubmission extends Replacement {
  session_id: string;
  insertion_id: string;
  correction_id: string;
}
interface Options {
  identity: ObservationIdentity;
  insertedText: string;
  poll: () => Promise<ObservationSample>;
  submit: (body: CorrectionSubmission, identity: ObservationIdentity, signal: AbortSignal) => Promise<{ event_id: string }>;
  isCurrent: (identity: ObservationIdentity) => boolean;
  release: (identity: ObservationIdentity) => void | Promise<void>;
  onSubmitted: (eventId: string, replacement: Replacement, identity: ObservationIdentity) => void;
  onEnd: (reason: ObservationEndReason, identity: ObservationIdentity) => void;
  shouldRetry?: (error: unknown) => boolean;
}

/** A missing IME signal is not evidence that the composition has committed. */
export function parseNativeObservation(value: Record<string, unknown>): ObservationSample {
  const composition = value.composition === 'committed' || value.composition === 'composing'
    ? value.composition : 'unknown';
  return { valid: value.valid === true && typeof value.text === 'string',
    text: typeof value.text === 'string' ? value.text : undefined,
    reason: typeof value.reason === 'string' ? value.reason : undefined, composition };
}

const identityKeys: (keyof ObservationIdentity)[] = [
  'origin', 'userId', 'authEpoch', 'sessionId', 'resultId', 'insertionId', 'targetId',
];

/** Owns exactly one captured target. No timer ever reads mutable SessionService identity. */
export class CorrectionObserver {
  readonly identity: Readonly<ObservationIdentity>;
  private readonly abort = new AbortController();
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private deadlineTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  private ended = false;
  private receiptConfirmed = false;
  private submitting = false;
  private seenSample = false;
  private deadline = 0;
  private stableSince = 0;
  private lastText: string;
  private originalText: string;
  private readonly options: Options;
  private lastComposition: ObservationSample['composition'] = 'unknown';

  constructor(options: Options) {
    this.identity = Object.freeze({ ...options.identity });
    this.options = { ...options, identity: this.identity, insertedText: '' };
    this.originalText = options.insertedText;
    this.lastText = options.insertedText;
  }

  start(): void {
    if (this.started || this.ended) return;
    this.started = true;
    this.stableSince = Date.now();
    this.deadline = this.stableSince + 60_000;
    if (!this.current()) return;
    // Separate hard deadline also ends a hung native call / HTTP submission.
    this.deadlineTimer = setTimeout(() => {
      this.finish(this.receiptConfirmed ? 'deadline' : 'receipt_unconfirmed');
    }, 60_000);
    this.schedule();
  }

  confirmReceipt(identity: ObservationIdentity): boolean {
    if (this.ended || !identityKeys.every(key => this.identity[key] === identity[key]) || !this.current()) return false;
    this.receiptConfirmed = true;
    return true;
  }

  stop(reason: ObservationEndReason = 'cancelled'): void { this.finish(reason); }

  private current(): boolean {
    if (this.ended) return false;
    if (!this.options.isCurrent(this.identity)) { this.finish('identity_changed'); return false; }
    return true;
  }
  private schedule(): void {
    if (!this.ended && !this.submitting) this.pollTimer = setTimeout(() => void this.tick(), 300);
  }
  private async tick(): Promise<void> {
    this.pollTimer = null;
    if (!this.current()) return;
    try {
      const sample = await this.options.poll();
      if (!this.current()) return;
      if (Date.now() >= this.deadline) { this.finish(this.receiptConfirmed ? 'deadline' : 'receipt_unconfirmed'); return; }
      if (!sample.valid || typeof sample.text !== 'string') {
        const reason = sample.reason === 'outside_changed' ? 'outside_changed' :
          sample.reason === 'target_changed' ? 'target_changed' : 'not_observable';
        this.finish(reason); return;
      }
      if (sample.text !== this.lastText || (this.seenSample && sample.composition !== this.lastComposition)) {
        this.lastText = sample.text;
        this.stableSince = Date.now();
      }
      this.lastComposition = sample.composition;
      this.seenSample = true;
      // A candidate can sit unchanged while the user chooses a word. Do not
      // abandon it at the idle threshold; a proven commit starts a fresh
      // stable period above. The independent 60-second deadline still owns it.
      if (sample.composition === 'composing') { this.schedule(); return; }
      if (Date.now() - this.stableSince < 15_000) { this.schedule(); return; }
      if (sample.composition !== 'committed') {
        this.finish('ime_unknown'); return;
      }
      if (sample.text === this.originalText) { this.finish('unchanged'); return; }
      const replacement = singleReplacement(this.originalText, sample.text);
      if (!replacement) { this.finish('invalid_replacement'); return; }
      // Observation may start immediately after read-back. HTTP receipt recovery
      // may run concurrently, but no fragment leaves this process until confirmed.
      if (!this.receiptConfirmed) { this.finish('receipt_unconfirmed'); return; }
      await this.submit(replacement);
    } catch {
      if (this.current()) this.finish('not_observable');
    }
  }
  private async submit(replacement: Replacement): Promise<void> {
    this.submitting = true;
    // Freeze the payload and UUID once. A transport retry cannot create a second event.
    const body = Object.freeze({ ...replacement, session_id: this.identity.sessionId,
      insertion_id: this.identity.insertionId, correction_id: randomUUID() });
    for (let attempt = 0; attempt < 2; attempt++) {
      if (!this.current()) return;
      if (Date.now() >= this.deadline) { this.finish('deadline'); return; }
      try {
        const event = await this.options.submit(body, this.identity, this.abort.signal);
        if (!this.current()) return;
        if (Date.now() >= this.deadline) { this.finish('deadline'); return; }
        if (!event.event_id) { this.finish('submit_failed'); return; }
        try { this.options.onSubmitted(event.event_id, replacement, this.identity); }
        finally { this.finish('submitted'); }
        return;
      } catch (error) {
        if (!this.current()) return;
        if (attempt === 0 && this.options.shouldRetry?.(error) === true) continue;
        this.finish('submit_failed'); return;
      }
    }
  }
  private stopTimers(): void {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    if (this.deadlineTimer) clearTimeout(this.deadlineTimer);
    this.pollTimer = null;
    this.deadlineTimer = null;
  }
  private finish(reason: ObservationEndReason): void {
    if (this.ended) return;
    this.ended = true;
    this.stopTimers();
    this.abort.abort();
    // Exactly once, always the frozen target. Release errors cannot re-open a window.
    try { void Promise.resolve(this.options.release(this.identity)).catch(() => undefined); }
    catch { /* The helper may already have been invalidated. */ }
    this.lastText = '';
    this.originalText = '';
    this.options.onEnd(reason, this.identity);
  }
}
