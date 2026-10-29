import { makeAutoObservable, reaction, runInAction } from 'mobx';
import { dictationLevelEvent, hudCancelEvent, hudConfirmEvent, sessionProgressEvent, sessionResultEvent, sessionErrorEvent } from '@shared/events';
import type { InsertionOutcome, SessionProgress, SessionState } from '@shared/types';
import { errorMessage } from '../lib/error-message';
import { events, rpc } from '../lib/ipc';
import { tr, type Locale } from '../lib/i18n';
import { PcmRecorder, RecorderError } from '../audio/recorder';
import type { AuthStore } from './auth-store';
import type { UiStore } from './ui-store';

export type LocalPhase = 'idle' | 'starting' | 'recording' | 'finalizing' | 'refining' | 'ready' | 'empty' | 'failed' | 'cancelled';
export interface HistorySelectionState {
  sessionId: string;
  kind: 'asr' | 'refine';
  loading: boolean;
  error: string | null;
}
const EMPTY: SessionProgress = { acknowledged_samples: 0, session_id: null, attempt_no: null, revision: 0, auto_insert_allowed: false, capture_ready: false, state: 'created', audio_ms: 0, stable_text: '', provisional_text: '', error_code: null, result: null };
const terminal = (state: SessionState) => ['ready', 'empty', 'failed', 'cancelled'].includes(state);
function phaseOf(progress: SessionProgress): LocalPhase { return progress.state === 'created' ? 'idle' : progress.state === 'streaming' ? progress.capture_ready === true ? 'recording' : 'starting' : progress.state; }

export class SessionStore {
  phase: LocalPhase = 'idle';
  progress: SessionProgress = { ...EMPTY };
  level = 0;
  targetApp: string | null = null;
  targetError: string | null = null;
  insertionAvailable = false;
  busy = false;
  error: string | null = null;
  outcome: InsertionOutcome | null = null;
  historySelection: HistorySelectionState | null = null;
  private readonly recorder = new PcmRecorder();
  private initialized = false;
  private generation = 0;
  // Events belong to the local operation that adopted this session. A cancel
  // invalidates that operation before its RPC settles, even at a newer revision.
  private eventGeneration = -1;
  private sessionId: string | null = null;
  private resultRevision = -1;
  private pendingStartId: string | null = null;
  private activeAction: 'insert' | 'retarget' | 'history' | null = null;
  private stopping: Promise<void> | null = null;
  private historySelectionRequest = 0;

  constructor(private readonly auth: AuthStore, private readonly ui: UiStore) {
    makeAutoObservable<SessionStore, 'recorder' | 'initialized'>(this, { recorder: false, initialized: false });
  }
  get result() { return this.progress.result; }
  get partialText(): string { return `${this.progress.stable_text}${this.progress.provisional_text}`; }
  get recording(): boolean { return this.phase === 'recording' || this.phase === 'starting'; }
  get retryAvailable(): boolean { return this.progress.state === 'failed' && this.progress.error_code !== 'AUDIO_INTERRUPTED'; }
  get failureMessage(): string | null {
    return this.progress.error_code === 'AUDIO_INTERRUPTED'
      ? tr((this.auth.settings?.locale ?? 'zh-CN') as Locale, 'home.uploadInterrupted')
      : this.error;
  }
  private current(generation: number, userId: string | undefined, sessionId = this.sessionId): boolean {
    return generation === this.generation && userId === this.auth.user?.id && sessionId === this.sessionId;
  }
  private accept(progress: SessionProgress): boolean {
    return Boolean(this.eventGeneration === this.generation && this.auth.user && this.sessionId && progress.session_id === this.sessionId && progress.revision >= this.progress.revision && (progress.attempt_no ?? 0) >= (this.progress.attempt_no ?? 0));
  }
  private apply(progress: SessionProgress): void {
    this.progress = progress;
    if (progress.target) {
      this.insertionAvailable = progress.target.insertion_available;
      this.targetApp = progress.target.target_app;
      this.targetError = progress.target.target_error;
    }
    this.phase = phaseOf(progress);
    if (progress.state !== 'streaming' || progress.capture_ready !== true) this.level = 0;
    if (progress.error_code) this.error = progress.error_code === 'AUDIO_INTERRUPTED' ? this.failureMessage! : progress.error_code;
    if (terminal(progress.state)) {
      if (!this.activeAction) this.busy = false;
      this.level = 0;
      void this.recorder.stop({ discard: true }).catch(() => undefined);
    }
  }
  private clearHistorySelection(): void {
    this.historySelectionRequest++;
    this.historySelection = null;
  }
  /** Invalidate local callbacks without sending a command that could cancel a new account's session. */
  invalidateLocal(): void {
    this.clearHistorySelection();
    this.generation++; this.eventGeneration = -1; this.sessionId = null; this.pendingStartId = null; this.activeAction = null; this.resultRevision = -1; this.stopping = null;
    void this.recorder.stop({ discard: true }).catch(() => undefined);
    this.phase = 'idle'; this.progress = { ...EMPTY }; this.outcome = null; this.error = null;
    this.targetApp = null; this.targetError = null; this.insertionAvailable = false; this.busy = false; this.level = 0;
  }
  init(): void {
    if (this.initialized) return;
    this.initialized = true;
    reaction(() => this.auth.user?.id ?? null, userId => { this.invalidateLocal(); if (userId) void this.restoreCurrent(); }, { fireImmediately: true });
    events.on(sessionProgressEvent, progress => {
      if (!this.accept(progress)) return;
      this.recorder.acknowledge(progress.acknowledged_samples);
      runInAction(() => this.apply(progress));
      // V2: stop at this session's own limit (server-granted, 180 s fallback).
      if (progress.state === 'streaming' && progress.audio_ms >= (progress.max_audio_ms ?? 180_000)) void this.stop();
    });
    events.on(hudConfirmEvent, () => { void this.stop(); });
    events.on(hudCancelEvent, () => { void this.cancel(); });
    events.on(sessionErrorEvent, message => { if (this.auth.user) this.ui.toast(message, 'warning'); });
    events.on(sessionResultEvent, progress => {
      if (!this.accept(progress) || progress.revision === this.resultRevision) return;
      runInAction(() => { this.resultRevision = progress.revision; this.apply(progress); });
      if (progress.state === 'ready') {
        if (progress.auto_insert_allowed && !progress.result?.review_required) void this.insert(false, false);
        else this.ui.toast('识别完成，可以插入或复制');
      }
      else if (progress.state === 'empty') this.ui.toast('这次没有识别到语音');
      else if (progress.state === 'failed') this.ui.toast(
        progress.error_code === 'AUDIO_INTERRUPTED' ? this.failureMessage! : '识别失败，可尝试重试', 'error');
    });
  }
  private async restoreCurrent(): Promise<void> {
    const generation = this.generation, userId = this.auth.user?.id;
    try {
      const progress = await rpc.session.status();
      if (generation !== this.generation || userId !== this.auth.user?.id || !progress.session_id) return;
      runInAction(() => { this.sessionId = progress.session_id; this.eventGeneration = generation; this.apply({ ...progress, auto_insert_allowed: false }); });
    } catch { /* The normal status/error surface remains available for a new recording. */ }
  }

  async start(): Promise<void> {
    if (!this.auth.user || this.busy || ['starting', 'recording', 'finalizing', 'refining'].includes(this.phase)) return;
    this.clearHistorySelection();
    const generation = ++this.generation;
    const userId = this.auth.user.id;
    this.sessionId = null; this.progress = { ...EMPTY }; this.resultRevision = -1;
    this.phase = 'starting'; this.error = null; this.outcome = null; this.level = 0;
    this.targetApp = null; this.targetError = null; this.insertionAvailable = false;
    let sessionId: string | null = null;
    const startOperationId = crypto.randomUUID();
    this.pendingStartId = startOperationId;
    try {
      const started = await rpc.session.start(this.auth.settings?.learning_enabled ?? true, startOperationId);
      if (generation !== this.generation || userId !== this.auth.user?.id) return;
      sessionId = started.session_id;
      runInAction(() => {
        this.sessionId = sessionId; this.eventGeneration = generation; this.targetApp = started.target_app; this.targetError = started.target_error;
        this.insertionAvailable = started.insertion_available;
        this.progress = { ...EMPTY, session_id: sessionId, attempt_no: started.attempt_no, state: 'streaming' };
      });
      await this.recorder.start({
        onChunk: (buffer, capturedAtEpochMs) => {
          if (!this.current(generation, userId, sessionId)) return;
          window.electronAPI.eventSend('session.audio', { session_id: sessionId, buffer, captured_at_ms: capturedAtEpochMs });
        },
        onLevel: level => {
          if (!this.current(generation, userId, sessionId)) return;
          events.emit(dictationLevelEvent, level);
          window.electronAPI.eventSend('dictation.level', { session_id: sessionId, level });
          runInAction(() => { this.level = level; });
        },
        onLimit: () => { if (this.current(generation, userId, sessionId)) void this.stop(); },
        onError: error => {
          if (!this.current(generation, userId, sessionId)) return;
          this.ui.toast(error.message, 'error'); void this.cancel();
        },
      }, { maxSamples: started.max_samples });
      // Terminal events do not advance the local generation. A completed startup
      // must also still belong to this streaming attempt before it can light UI.
      const captureCurrent = () => this.current(generation, userId, sessionId)
        && this.progress.state === 'streaming' && this.progress.attempt_no === started.attempt_no;
      if (!captureCurrent()) return;
      const ready = await rpc.session.captureReady(sessionId!, started.attempt_no);
      if (!captureCurrent() || ready.session_id !== sessionId || ready.attempt_no !== started.attempt_no
        || ready.state !== 'streaming' || ready.capture_ready !== true || !this.accept(ready)) return;
      runInAction(() => this.apply(ready));
    } catch (error) {
      if (generation !== this.generation || userId !== this.auth.user?.id) return;
      await this.recorder.stop({ discard: true }).catch(() => undefined);
      if (sessionId && terminal(this.progress.state)) return;
      if (sessionId) await rpc.session.cancel(sessionId).catch(() => undefined);
      if (generation !== this.generation || userId !== this.auth.user?.id) return;
      const message = this.progress.error_code === 'AUDIO_INTERRUPTED' ? this.failureMessage!
        : (error instanceof RecorderError && error.code === 'MICROPHONE_PERMISSION_DENIED') || errorMessage(error) === 'MICROPHONE_PERMISSION_DENIED'
          ? tr((this.auth.settings?.locale ?? 'zh-CN') as Locale, this.ui.appInfo?.platform === 'win32' ? 'windows.microphone.hint' : 'home.microphonePermissionDenied')
          : errorMessage(error) === 'MICROPHONE_PERMISSION_UNAVAILABLE'
            ? tr((this.auth.settings?.locale ?? 'zh-CN') as Locale, this.ui.appInfo?.platform === 'win32' ? 'windows.microphone.hint' : 'home.microphonePermissionUnavailable')
          : errorMessage(error);
      runInAction(() => { this.phase = 'failed'; this.error = message; });
      this.ui.toast(message, 'error');
    } finally { if (this.pendingStartId === startOperationId) this.pendingStartId = null; }
  }

  stop(): Promise<void> {
    if (this.stopping) return this.stopping;
    if (!this.recording) return Promise.resolve();
    if (!this.sessionId || this.phase === 'starting') return this.cancel();
    const generation = this.generation, userId = this.auth.user?.id, sessionId = this.sessionId;
    const operation = (async () => {
      try {
        await this.recorder.stop();
        if (!this.current(generation, userId, sessionId)) return;
        runInAction(() => { this.level = 0; this.phase = 'finalizing'; this.busy = true; });
        const progress = await rpc.session.finish(sessionId);
        if (this.current(generation, userId, sessionId) && this.accept(progress)) runInAction(() => this.apply(progress));
      } catch (error) {
        if (!this.current(generation, userId, sessionId)) return;
        await rpc.session.cancel(sessionId).catch(() => undefined);
        if (!this.current(generation, userId, sessionId)) return;
        const message = this.progress.error_code === 'AUDIO_INTERRUPTED' ? this.failureMessage! : errorMessage(error);
        runInAction(() => { this.phase = 'failed'; this.error = message; this.busy = false; });
        this.ui.toast(message, 'error');
      }
    })();
    this.stopping = operation;
    void operation.then(() => { if (this.stopping === operation) this.stopping = null; }, () => { if (this.stopping === operation) this.stopping = null; });
    return operation;
  }
  async toggle(): Promise<void> { if (this.recording) await this.stop(); else await this.start(); }
  async cancel(): Promise<void> {
    this.clearHistorySelection();
    if (this.activeAction === 'history') this.activeAction = null;
    const sessionId = this.sessionId, userId = this.auth.user?.id, generation = ++this.generation;
    const pendingStart = this.pendingStartId; this.pendingStartId = null;
    this.phase = 'cancelled'; this.level = 0; this.busy = false; this.outcome = null;
    if (pendingStart) void rpc.session.cancelStart(pendingStart).catch(() => undefined);
    await this.recorder.stop({ discard: true }).catch(() => undefined);
    if (generation !== this.generation || userId !== this.auth.user?.id || !sessionId) return;
    const progress = await rpc.session.cancel(sessionId).catch(() => null);
    if (!this.current(generation, userId, sessionId)) return;
    runInAction(() => { if (progress) this.apply(progress); else this.phase = 'cancelled'; });
  }
  async selectHistoryForRetry(sessionId: string, kind: 'asr' | 'refine'): Promise<boolean> {
    if (!this.auth.user || this.busy || ['starting', 'recording', 'finalizing', 'refining'].includes(this.phase)) return false;
    const userId = this.auth.user.id;
    let generation = this.generation;
    const request = ++this.historySelectionRequest;
    const current = () => request === this.historySelectionRequest && this.current(generation, userId);
    this.historySelection = { sessionId, kind, loading: true, error: null };
    this.busy = true; this.activeAction = 'history';
    try {
      const progress = await rpc.history.selectForRetry(sessionId, kind);
      if (!current()) return false;
      if (progress.session_id !== sessionId || !Number.isInteger(progress.attempt_no) || (progress.attempt_no ?? 0) < 1
        || progress.auto_insert_allowed !== false || (kind === 'asr' ? progress.state !== 'failed' : progress.state !== 'ready')) {
        throw new Error('历史会话快照无效');
      }
      runInAction(() => {
        generation = ++this.generation;
        this.sessionId = sessionId; this.eventGeneration = generation; this.resultRevision = progress.revision;
        this.outcome = null; this.error = null; this.targetApp = null;
        this.targetError = '历史结果请重新指定输入目标，或复制结果';
        this.insertionAvailable = false;
        this.historySelection = null;
        this.apply({ ...progress, auto_insert_allowed: false });
      });
      return true;
    } catch (error) {
      if (current()) {
        runInAction(() => { this.historySelection = { sessionId, kind, loading: false, error: errorMessage(error) }; });
        this.ui.toast(errorMessage(error), 'error');
      }
      return false;
    } finally {
      if (current()) runInAction(() => { this.activeAction = null; this.busy = false; });
    }
  }

  async retry(kind: 'asr' | 'refine'): Promise<void> {
    const sessionId = this.sessionId, generation = this.generation, userId = this.auth.user?.id;
    if (!sessionId || this.busy) return;
    if (kind === 'asr' && this.progress.error_code === 'AUDIO_INTERRUPTED') {
      this.ui.toast(this.failureMessage!, 'warning');
      return;
    }
    this.busy = true; this.error = null;
    try {
      const progress = await rpc.session.retry(sessionId, kind);
      if (this.current(generation, userId, sessionId) && this.accept(progress)) runInAction(() => this.apply(progress));
    } catch (error) {
      if (!this.current(generation, userId, sessionId)) return;
      runInAction(() => { this.error = errorMessage(error); }); this.ui.toast(errorMessage(error), 'error');
    } finally { if (this.current(generation, userId, sessionId)) runInAction(() => { this.busy = false; }); }
  }
  async insert(reviewConfirmed: boolean, explicit = true): Promise<void> {
    const sessionId = this.sessionId, generation = this.generation, userId = this.auth.user?.id;
    if (!sessionId || !this.result || this.busy) return;
    this.busy = true; this.activeAction = 'insert'; this.outcome = null;
    try {
      const outcome = await rpc.session.insert(sessionId, reviewConfirmed, explicit);
      if (!this.current(generation, userId, sessionId)) return;
      runInAction(() => { this.outcome = outcome; });
      if (outcome.warning) this.ui.toast(outcome.warning, 'warning');
      else if (outcome.status === 'inserted') this.ui.toast('已插入到目标输入框');
      else if (outcome.status === 'unverified') this.ui.toast(outcome.message || '输入状态未确认，请检查目标；不会重复输入', 'warning');
      else this.ui.toast(outcome.message || '无法插入，请复制结果', 'error');
    } catch (error) { if (this.current(generation, userId, sessionId)) this.ui.toast(errorMessage(error), 'error'); }
    finally { if (this.current(generation, userId, sessionId)) runInAction(() => { this.activeAction = null; this.busy = false; }); }
  }
  async copy(): Promise<void> {
    if (!this.result) return;
    await rpc.session.copy(this.result.refined_text); this.ui.toast('已复制整理稿');
  }

  async copyRaw(): Promise<void> {
    if (!this.result) return;
    const locale = (this.auth.settings?.locale ?? 'zh-CN') as Locale;
    await rpc.session.copy(this.result.raw_text);
    this.ui.toast(tr(locale, 'home.copiedOriginal'));
  }

  async retarget(): Promise<void> {
    const sessionId = this.sessionId, generation = this.generation, userId = this.auth.user?.id;
    if (!sessionId || !this.result || this.busy) return;
    const locale = (this.auth.settings?.locale ?? 'zh-CN') as Locale;
    this.busy = true; this.activeAction = 'retarget'; this.ui.toast(tr(locale, 'home.retargetPrompt'));
    try {
      const next = await rpc.session.retarget(sessionId, 3000);
      if (!this.current(generation, userId, sessionId)) return;
      runInAction(() => { this.targetApp = next.target_app; this.targetError = next.target_error; this.insertionAvailable = next.insertion_available; this.outcome = null; });
      if (next.insertion_available) this.ui.toast(tr(locale, 'home.retargetDone', { app: next.target_app ?? tr(locale, 'home.side.currentTarget') }));
      else this.ui.toast(next.target_error ?? tr(locale, 'home.retargetFailed'), 'warning');
    } catch (error) { if (this.current(generation, userId, sessionId)) this.ui.toast(errorMessage(error), 'error'); }
    finally { if (this.current(generation, userId, sessionId)) runInAction(() => { this.activeAction = null; this.busy = false; }); }
  }
  async reset(): Promise<void> {
    const sessionId = this.sessionId, pendingStart = this.pendingStartId;
    if (pendingStart) void rpc.session.cancelStart(pendingStart).catch(() => undefined);
    this.invalidateLocal();
    if (sessionId) await rpc.session.reset(sessionId).catch(() => undefined);
  }
}
