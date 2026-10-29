import { createHash, randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { clipboard } from 'electron';
import WebSocket from 'ws';
import { API_BASE_URL, AUDIO, sessionMaxSamples, wsUrl } from '@shared/config';
import type {
  Insertion, InsertionOutcome, LearningSuggestion, Session, SessionProgress,
  SessionResult, SessionStartResult,
} from '@shared/types';
import { ApiClient, ApiError, AuthContextChangedError } from './api-client';
import type { AuthService } from './auth-service';
import { NativeCallError, type NativeVoiceInput } from './native-input';
import { AudioFlow, AudioFlowError } from './audio-flow';
import { InsertionJournal, type InsertionIntent, type InsertionMark } from './insertion-journal';
import { CorrectionObserver, parseNativeObservation, type ObservationIdentity } from './correction-observer';
import type { Replacement } from './replacement';
import { windowsInputMessage } from '@shared/windows-input-messages';

const TERMINAL = new Set(['ready', 'empty', 'failed', 'cancelled']);
const monotonicEpochMs = (): number => performance.timeOrigin + performance.now();
const EMPTY_PROGRESS: SessionProgress = { acknowledged_samples: 0,
  session_id: null, attempt_no: null, revision: 0, auto_insert_allowed: false, capture_ready: false,
  state: 'created', audio_ms: 0, stable_text: '', provisional_text: '', error_code: null, result: null,
};
interface AccountContext { userId: string; accountEpoch: number; signal: AbortSignal; }
interface OperationContext extends AccountContext {
  operationEpoch: number; operationSignal: AbortSignal; sessionId: string | null; attemptNo: number | null;
}
interface ReadyWaiter {
  resolve: () => void; reject: (error: Error) => void; timer: NodeJS.Timeout;
}
interface ResultWaiter { resolve: (progress: SessionProgress) => void; timer: NodeJS.Timeout; }

/** Main owns a single live operation. Every async continuation carries its original identity. */
export class SessionService {
  private ws: WebSocket | null = null;
  private sessionId: string | null = null;
  private ownerId: string | null = null;
  private sessionLearningEnabled = false;
  private attemptNo: number | null = null;
  private accountEpoch = 0;
  private operationEpoch = 0;
  private accountAbort = new AbortController();
  private operationAbort = new AbortController();
  private revision = 0;
  private starting = false;
  private selecting: object | null = null;
  private retargeting: object | null = null;
  private progress: SessionProgress = { ...EMPTY_PROGRESS };
  private flow = new AudioFlow();
  private frameTotals = new Map<number, number>();
  private acknowledgedSequence = -1;
  private partialRevision = 0;
  private decodedSamples = 0;
  private lastCapturedAt: number | null = null;
  private endSent = false;
  private endAcknowledged = false;
  private autoInsertAllowed = false;
  private resultHandled = false;
  private readyWaiter: ReadyWaiter | null = null;
  private resultWaiter: ResultWaiter | null = null;
  private ackTimer: NodeJS.Timeout | null = null;
  private endTimer: NodeJS.Timeout | null = null;
  private finishPromise: Promise<SessionProgress> | null = null;
  private readonly audioWrites = new Set<object>();
  private sendEndWhenWritten: (() => void) | null = null;
  private recoveryPromise: Promise<void> | null = null;
  private targetId: string | null = null;
  private targetApp: string | null = null;
  private targetError: string | null = null;
  private resultTargetCapture: Promise<void> | null = null;
  private targetCaptureEpoch = 0;
  private insertionId: string | null = null;
  private lastOutcome: InsertionOutcome | null = null;
  private insertionPromise: Promise<InsertionOutcome> | null = null;
  private observer: CorrectionObserver | null = null;
  private readonly receiptTasks = new Map<string, Promise<boolean>>();

  constructor(
    private readonly api: ApiClient,
    private readonly native: NativeVoiceInput,
    private readonly auth: AuthService,
    private readonly journal: InsertionJournal,
    private readonly emitProgress: (progress: SessionProgress) => void,
    private readonly emitResult: (progress: SessionProgress) => void,
    private readonly emitLearning: (suggestion: LearningSuggestion) => void = () => undefined,
    private readonly emitLearningIssue: (message: string) => void = () => undefined
  ) {}

  get status(): SessionProgress { return this.current(); }

  private account(): AccountContext {
    return { userId: this.auth.ensure().user.id, accountEpoch: this.accountEpoch, signal: this.accountAbort.signal };
  }
  private context(): OperationContext {
    return { ...this.account(), userId: this.ownerId ?? this.auth.ensure().user.id, operationEpoch: this.operationEpoch, operationSignal: this.operationAbort.signal,
      sessionId: this.sessionId, attemptNo: this.attemptNo };
  }
  private accountCurrent(ctx: AccountContext): boolean {
    return !ctx.signal.aborted && ctx.accountEpoch === this.accountEpoch && this.auth.current?.user.id === ctx.userId;
  }
  private currentContext(ctx: OperationContext): boolean {
    return this.accountCurrent(ctx) && !ctx.operationSignal.aborted && ctx.operationEpoch === this.operationEpoch
      && (ctx.sessionId === null || ctx.sessionId === this.sessionId)
      && (ctx.attemptNo === null || ctx.attemptNo === this.attemptNo);
  }
  private assertCurrent(ctx: OperationContext): void {
    if (!this.currentContext(ctx)) throw new AuthContextChangedError();
  }
  private assertAccount(ctx: AccountContext): void {
    if (!this.accountCurrent(ctx)) throw new AuthContextChangedError();
  }
  private wait<T>(ctx: AccountContext | OperationContext, operation: Promise<T>, timeoutMs = 15_000): Promise<T> {
    const signal = 'operationSignal' in ctx ? ctx.operationSignal : ctx.signal;
    const valid = () => 'operationSignal' in ctx ? this.currentContext(ctx) : this.accountCurrent(ctx);
    return new Promise<T>((resolve, reject) => {
      let settled = false;
      const done = (failed: boolean, error: unknown, value?: T) => {
        if (settled) return;
        settled = true; clearTimeout(timer); signal.removeEventListener('abort', aborted);
        if (failed) reject(error); else if (!valid()) reject(new AuthContextChangedError()); else resolve(value as T);
      };
      const aborted = () => done(true, new AuthContextChangedError());
      const timer = setTimeout(() => done(true, new ApiError(0, 'CLIENT_TIMEOUT', '等待服务响应超时')), timeoutMs);
      signal.addEventListener('abort', aborted, { once: true });
      operation.then(value => done(false, null, value), error => done(true, error));
      if (!valid()) aborted();
    });
  }
  private pause(ctx: OperationContext, ms: number): Promise<void> {
    return this.wait(ctx, new Promise<void>(resolve => setTimeout(resolve, ms)), ms + 1000);
  }
  private publish(patch: Partial<SessionProgress> = {}, result = false): SessionProgress {
    const state = patch.state ?? this.progress.state;
    const captureReady = state === 'streaming' && (patch.capture_ready ?? this.progress.capture_ready) === true;
    this.progress = { ...this.progress, ...patch, capture_ready: captureReady, session_id: this.sessionId, attempt_no: this.attemptNo,
      revision: ++this.revision, auto_insert_allowed: this.autoInsertAllowed, acknowledged_samples: this.flow.acknowledgedSamples };
    const snapshot = this.current();
    this.emitProgress(snapshot);
    if (result) this.emitResult(snapshot);
    return snapshot;
  }
  private clearAudioTimers(): void {
    if (this.ackTimer) clearTimeout(this.ackTimer);
    if (this.endTimer) clearTimeout(this.endTimer);
    this.ackTimer = this.endTimer = null;
  }
  private closeSocket(): void {
    // Completion tickets contain no PCM; old callbacks cannot drain a new operation.
    this.audioWrites.clear(); this.sendEndWhenWritten = null;
    const ws = this.ws; this.ws = null;
    if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
  }
  private settleReady(error?: Error): void {
    const waiter = this.readyWaiter; this.readyWaiter = null;
    if (!waiter) return;
    clearTimeout(waiter.timer);
    if (error) waiter.reject(error); else waiter.resolve();
  }
  private settleResult(value = this.progress): void {
    const waiter = this.resultWaiter; this.resultWaiter = null;
    if (!waiter) return;
    clearTimeout(waiter.timer); waiter.resolve(value);
  }
  private finishTerminal(patch: Partial<SessionProgress>): void {
    if (TERMINAL.has(this.progress.state)) return;
    this.publish(patch, true);
    this.settleReady(new Error(this.progress.error_code ?? '语音会话已结束'));
    this.settleResult(); this.clearAudioTimers(); this.closeSocket();
  }
  private fail(ctx: OperationContext, code: string): void {
    if (!this.currentContext(ctx) || TERMINAL.has(this.progress.state)) return;
    this.autoInsertAllowed = false;
    if (ctx.sessionId && ['AUDIO_BACKPRESSURE', 'AUDIO_PROTOCOL_ERROR', 'AUDIO_LIMIT_EXCEEDED', 'RESULT_TIMEOUT'].includes(code)) {
      void this.api.cancelSession(ctx.sessionId).catch(() => undefined);
    }
    this.finishTerminal({ state: 'failed', error_code: code, result: null, provisional_text: '' });
    if (code === 'AUTH_INVALID' || code === 'ACCOUNT_DISABLED') {
      // A WS authentication failure does not necessarily traverse ApiClient's HTTP handler.
      this.api.clearTokens(); this.auth.invalidate();
    }
  }
  private nextOperation(): void {
    this.targetCaptureEpoch++;
    this.progress = { ...this.progress, capture_ready: false };
    this.selecting = null;
    this.operationEpoch++;
    this.operationAbort.abort(); this.operationAbort = new AbortController();
    this.settleReady(new AuthContextChangedError());
    this.settleResult({ ...this.progress, state: 'cancelled', result: null, auto_insert_allowed: false });
    this.clearAudioTimers(); this.closeSocket();
    this.finishPromise = null; this.recoveryPromise = null;
    this.resultTargetCapture = null;
    this.observer?.stop('cancelled'); this.observer = null;
  }

  /** A local Renderer acknowledgement only after its own Recorder has actually begun. */
  captureReady(sessionId: string, attemptNo: number): SessionProgress {
    const ctx = this.context();
    this.assertCurrent(ctx);
    if (!this.sessionId || sessionId !== this.sessionId || this.ownerId !== ctx.userId
      || !Number.isSafeInteger(attemptNo) || attemptNo !== this.attemptNo
      || this.progress.state !== 'streaming') throw new Error('录音准备状态已变化，请重新录音');
    return this.progress.capture_ready === true ? this.current() : this.publish({ capture_ready: true });
  }

  async start(learningEnabled: boolean): Promise<SessionStartResult> {
    if (this.isBusy()) throw new Error('已有进行中的语音会话');
    const snapshot = this.auth.ensure();
    this.reset();
    this.ownerId = snapshot.user.id;
    this.sessionLearningEnabled = this.native.supportsObservation !== false && learningEnabled && snapshot.settings.learning_enabled;
    const ctx = this.context();
    this.starting = true;
    this.autoInsertAllowed = true;
    this.publish();
    try {
      await this.wait(ctx, this.api.ensureFreshAccess());
      this.assertCurrent(ctx);
      const request = this.api.createSession(randomUUID(), this.sessionLearningEnabled);
      // A timed-out/invalidated create may still commit. Cancel only under its original account.
      void request.then(session => {
        if ((!this.currentContext(ctx) || TERMINAL.has(this.progress.state)) && this.accountCurrent(ctx)) void this.api.cancelSession(session.session_id).catch(() => undefined);
      }, () => undefined);
      const session = await this.wait(ctx, request);
      this.assertCurrent(ctx);
      this.sessionId = session.session_id; this.attemptNo = session.current_attempt_no;
      this.sessionLearningEnabled = this.native.supportsObservation !== false && session.learning_enabled === true;
      // V2 §4.1: this session's limit is fixed by the server at creation.
      const maxSamples = sessionMaxSamples(session.max_record_seconds);
      this.flow = new AudioFlow(maxSamples); this.frameTotals.clear(); this.acknowledgedSequence = -1;
      this.publish({ state: session.state, max_audio_ms: maxSamples * 1000 / 16000 });
      const streamCtx = this.context();
      await this.connect(streamCtx);
      this.assertCurrent(streamCtx);
      return { session_id: session.session_id, attempt_no: session.current_attempt_no, max_samples: maxSamples,
        insertion_available: this.targetId !== null, target_app: this.targetApp, target_error: this.targetError };
    } catch (error) {
      if (this.currentContext(ctx)) this.fail(ctx, error instanceof ApiError ? error.code : 'AUDIO_INTERRUPTED');
      throw error;
    } finally { if (this.currentContext(ctx)) this.starting = false; }
  }

  private async captureTarget(ctx: OperationContext): Promise<void> {
    this.assertCurrent(ctx);
    const captureEpoch = this.targetCaptureEpoch;
    let abandoned = false;
    try {
      const permission = await this.wait(ctx, this.native.call('permission', { prompt: false }), 6000);
      this.assertCurrent(ctx);
      if (captureEpoch !== this.targetCaptureEpoch) return;
      if (!permission.trusted) { this.targetError = process.platform === 'darwin' ? '需要辅助功能权限才能插入到其他应用' : '原生输入组件不可用，请复制结果后手动粘贴'; return; }
      const operation = this.native.call('capture');
      void operation.then(target => {
        if ((abandoned || !this.currentContext(ctx) || captureEpoch !== this.targetCaptureEpoch) && typeof target.target === 'string') {
          void this.native.call('release', { target: target.target }).catch(() => undefined);
        }
      }, () => undefined);
      const target = await this.wait(ctx, operation, 6000);
      this.assertCurrent(ctx);
      if (captureEpoch !== this.targetCaptureEpoch) return;
      if (typeof target.target !== 'string' || !target.target) throw new Error('无法捕获可靠的输入目标');
      this.targetId = target.target; this.targetApp = String(target.app ?? '目标应用'); this.targetError = null;
    } catch (error) {
      abandoned = true;
      this.assertCurrent(ctx);
      if (captureEpoch !== this.targetCaptureEpoch) return;
      this.targetId = null; this.targetApp = null;
      this.targetError = error instanceof Error ? error.message : '无法捕获输入目标';
    }
  }

  async retarget(delayMs = 3000): Promise<Pick<SessionStartResult, 'insertion_available' | 'target_app' | 'target_error'>> {
    if (this.selecting) throw new Error('正在读取历史记录，请稍候');
    if (this.retargeting) throw new Error('正在重新指定输入目标，请稍候');
    const retargeting = {};
    this.retargeting = retargeting;
    try {
      return await this.retargetCurrent(delayMs);
    } finally {
      if (this.retargeting === retargeting) this.retargeting = null;
    }
  }
  private async retargetCurrent(delayMs: number): Promise<Pick<SessionStartResult, 'insertion_available' | 'target_app' | 'target_error'>> {
    if (!this.sessionId || !this.progress.result || this.progress.state !== 'ready') throw new Error('结果尚未就绪');
    if (this.insertionPromise) throw new Error('正在确认插入结果，请稍候');
    if (this.lastOutcome && ['inserted', 'unverified'].includes(this.lastOutcome.status)) {
      throw new Error('上次已输入或结果不确定，请检查目标或复制；不会再次输入');
    }
    let ctx = this.context();
    if (this.insertionId) {
      const mark = this.journal.read(ctx.userId, this.insertionId);
      if (mark && (mark.phase !== 'failed' || !(mark.receipt_confirmed || await this.receipt(mark, ctx)))) {
        throw new Error('上次输入回执尚未确认，请稍后重试或复制结果');
      }
      this.assertCurrent(ctx);
    }
    const oldTarget = this.targetId;
    this.nextOperation(); ctx = this.context(); this.autoInsertAllowed = false;
    this.targetId = this.targetApp = this.targetError = null;
    if (oldTarget) await this.wait(ctx, this.native.call('release', { target: oldTarget }).catch(() => undefined), 6000);
    if (delayMs > 0) await this.pause(ctx, Math.min(Math.max(delayMs, 0), 10_000));
    this.assertCurrent(ctx);
    await this.captureTarget(ctx);
    this.assertCurrent(ctx);
    this.insertionId = null; this.lastOutcome = null;
    return { insertion_available: this.targetId !== null, target_app: this.targetApp, target_error: this.targetError };
  }

  private connect(ctx: OperationContext): Promise<void> {
    this.assertCurrent(ctx);
    const token = this.api.accessTokenValue;
    if (!token || !ctx.sessionId) return Promise.reject(new Error('登录已过期，请重新登录'));
    return new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(wsUrl(`/ws/v1/sessions/${ctx.sessionId}/audio`), { headers: { Authorization: `Bearer ${token}` } });
      this.ws = ws;
      const timer = setTimeout(() => this.fail(ctx, 'CONNECT_TIMEOUT'), 10_000);
      this.readyWaiter = { resolve, reject, timer };
      ws.on('message', data => { if (this.ws === ws && this.currentContext(ctx)) this.handleMessage(ctx, data.toString()); });
      const disconnected = (code?: number) => {
        if (this.ws !== ws || !this.currentContext(ctx) || TERMINAL.has(this.progress.state)) return;
        this.ws = null;
        if (ws.readyState !== WebSocket.CLOSED) ws.close();
        this.settleReady(new Error('语音连接中断'));
        if (code === 4401 || code === 4403) this.fail(ctx, code === 4401 ? 'AUTH_INVALID' : 'ACCOUNT_DISABLED');
        else void this.recover(ctx);
      };
      ws.on('error', () => disconnected());
      ws.on('close', code => disconnected(code));
    });
  }

  private handleMessage(ctx: OperationContext, raw: string): void {
    if (!this.currentContext(ctx) || TERMINAL.has(this.progress.state)) return;
    let event: Record<string, unknown>;
    try { event = JSON.parse(raw) as Record<string, unknown>; }
    catch { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
    if (!event || typeof event !== 'object') { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
    if (event.session_id !== undefined && event.session_id !== ctx.sessionId) return;
    if (event.attempt_no !== undefined && event.attempt_no !== ctx.attemptNo) return;
    const type = event.type;
    if (type === 'ready') {
      const audio = event.audio as Record<string, unknown> | undefined;
      if (this.progress.state !== 'created' || event.session_id !== ctx.sessionId || event.attempt_no !== ctx.attemptNo
        || audio?.encoding !== 'pcm_s16le' || audio.sample_rate_hz !== 16000 || audio.channels !== 1
        || audio.chunk_samples !== 4000 || audio.max_samples !== this.flow.maxSamples) { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
      this.publish({ state: 'streaming' }); this.settleReady(); return;
    }
    if (type === 'audio.ack') {
      const seq = event.seq, total = event.total_samples;
      if (!Number.isSafeInteger(seq) || !Number.isSafeInteger(total) || Number(seq) < this.acknowledgedSequence
        || Number(seq) > this.flow.lastSequence || this.frameTotals.get(Number(seq)) !== total) {
        this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return;
      }
      try {
        this.flow.checkDeadline(monotonicEpochMs());
        this.flow.acknowledge(Number(total));
      } catch (error) { this.fail(ctx, error instanceof AudioFlowError ? error.code : 'AUDIO_PROTOCOL_ERROR'); return; }
      this.acknowledgedSequence = Number(seq);
      this.publish({ audio_ms: Math.floor(Number(total) * 1000 / 16000) }); this.armAckDeadline(ctx); return;
    }
    if (type === 'audio.end_ack') {
      if (!this.endSent || event.last_seq !== this.flow.lastSequence || event.total_samples !== this.flow.sentSamples
        || event.attempt_no !== ctx.attemptNo) { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
      this.endAcknowledged = true;
      if (this.endTimer) clearTimeout(this.endTimer); this.endTimer = null;
      this.publish({ state: 'finalizing' }); return;
    }
    if (type === 'asr.partial') {
      const revision = Number(event.revision);
      if (!Number.isSafeInteger(revision) || revision < 1) { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
      if (revision <= this.partialRevision) return;
      const stable = event.stable_text, provisional = event.provisional_text;
      const decoded = Number(event.decoded_samples), received = Number(event.received_samples);
      if (typeof stable !== 'string' || typeof provisional !== 'string' || !stable.startsWith(this.progress.stable_text)
        || !Number.isSafeInteger(decoded) || !Number.isSafeInteger(received) || decoded < this.decodedSamples
        || decoded > received || received > this.flow.sentSamples) { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
      this.partialRevision = revision; this.decodedSamples = decoded;
      this.publish({ stable_text: stable, provisional_text: provisional }); return;
    }
    if (type === 'asr.final') {
      if (!this.endSent || typeof event.text !== 'string' || !event.text.startsWith(this.progress.stable_text)) {
        this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return;
      }
      this.publish({ stable_text: event.text, provisional_text: '', state: 'refining' }); return;
    }
    if (type === 'result.ready') {
      const result = event.result as SessionResult | undefined;
      if (!this.endSent || event.session_id !== ctx.sessionId || event.attempt_no !== ctx.attemptNo
        || !result || result.attempt_no !== ctx.attemptNo || typeof result.refined_text !== 'string') {
        this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return;
      }
      // Candidate metadata is only for presentation and explicit insertion.
      // Automatic insertion obtains its final target inside the native write
      // request after claim; it never gates on this earlier snapshot.
      if (this.autoInsertAllowed) {
        const capture = this.captureTarget(ctx);
        this.resultTargetCapture = capture;
        void capture.then(() => {
          if (this.currentContext(ctx) && this.progress.state === 'ready'
            && this.progress.result?.result_id === result.result_id) this.publish();
        }, () => undefined); // Cancellation still rejects an insertion awaiting this capture.
      }
      this.finishTerminal({ state: 'ready', result, error_code: null, provisional_text: '' }); return;
    }
    if (type === 'session.empty') {
      if (!this.endSent) { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
      this.finishTerminal({ state: 'empty', result: null }); return;
    }
    if (type === 'cancelled') { this.autoInsertAllowed = false; this.finishTerminal({ state: 'cancelled', result: null }); return; }
    if (type === 'error') {
      const error = (event.error ?? {}) as Record<string, unknown>;
      this.fail(ctx, typeof error.code === 'string' ? error.code : 'ASR_FAILED'); return;
    }
    this.fail(ctx, 'AUDIO_PROTOCOL_ERROR');
  }

  private armAckDeadline(ctx: OperationContext): void {
    if (this.ackTimer) clearTimeout(this.ackTimer); this.ackTimer = null;
    const deadline = this.flow.acknowledgementDeadline;
    if (deadline === null) return;
    this.ackTimer = setTimeout(() => {
      if (!this.currentContext(ctx) || TERMINAL.has(this.progress.state)) return;
      try { this.flow.checkDeadline(monotonicEpochMs()); this.armAckDeadline(ctx); }
      catch { this.fail(ctx, 'AUDIO_BACKPRESSURE'); }
    }, Math.max(1, deadline - monotonicEpochMs()));
  }

  /** Identity and monotonic epoch timestamp are captured before Renderer→Main IPC. */
  sendAudio(buffer: ArrayBuffer, sessionId: string, capturedAtMs: number): void {
    if (process.env.SVI_AUDIO_DEBUG) {
      const view = new Int16Array(buffer);
      let peak = 0;
      for (let index = 0; index < view.length; index += 16) peak = Math.max(peak, Math.abs(view[index]));
      console.log('[audio] sendAudio', {
        state: this.progress.state, sessionId: sessionId?.slice(0, 8), current: this.sessionId?.slice(0, 8),
        samples: buffer.byteLength / 2, peak, capturedAtMs, now: monotonicEpochMs(), endSent: this.endSent,
      });
    }
    if (!sessionId || sessionId !== this.sessionId || this.progress.state !== 'streaming' || this.endSent) return;
    const ctx = this.context();
    if (!this.currentContext(ctx)) return;
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) { void this.recover(ctx); return; }
    const bytes = Buffer.from(buffer);
    if (bytes.length < 2 || bytes.length > 8000 || bytes.length % 2 !== 0) { this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return; }
    try {
      const now = monotonicEpochMs();
      // One capture-to-server-ACK budget includes Worklet/Renderer/IPC and WS.
      // A clock-domain defect must be calibrated; receipt cannot restamp old PCM.
      if (!Number.isFinite(capturedAtMs) || capturedAtMs > now
        || (this.lastCapturedAt !== null && capturedAtMs < this.lastCapturedAt)) throw new AudioFlowError('AUDIO_PROTOCOL_ERROR');
      if (now - capturedAtMs >= this.flow.maxUnacknowledgedMs) throw new AudioFlowError('AUDIO_BACKPRESSURE');
      this.flow.checkDeadline(now);
      const frame = this.flow.frame(bytes.length / 2, capturedAtMs);
      this.lastCapturedAt = capturedAtMs;
      this.frameTotals.set(frame.sequence, frame.totalSamples);
      const header = Buffer.alloc(8); header.writeUInt32LE(frame.sequence, 0); header.writeUInt32LE(frame.samples, 4);
      const write = {};
      this.audioWrites.add(write);
      ws.send(Buffer.concat([header, bytes]), error => {
        if (!this.audioWrites.has(write)) return;
        if (!this.currentContext(ctx) || this.ws !== ws) { this.audioWrites.delete(write); return; }
        if (error) { void this.recover(ctx); return; }
        // A delayed success must not rescue an already overdue unacknowledged frame.
        try { this.flow.checkDeadline(monotonicEpochMs()); }
        catch { this.fail(ctx, 'AUDIO_BACKPRESSURE'); return; }
        this.audioWrites.delete(write);
        this.sendEndWhenWritten?.();
      });
      if (this.currentContext(ctx) && this.ws === ws && !TERMINAL.has(this.progress.state)) {
        this.armAckDeadline(ctx);
        if (frame.totalSamples === this.flow.maxSamples) void this.finish().catch(() => undefined);
      }
    } catch (error) {
      if (process.env.SVI_AUDIO_DEBUG) console.warn('[audio] sendAudio failure', error);
      this.fail(ctx, error instanceof AudioFlowError ? error.code : 'AUDIO_INTERRUPTED');
    }
  }

  async finish(): Promise<SessionProgress> {
    if (TERMINAL.has(this.progress.state)) return this.progress;
    if (this.finishPromise) return this.finishPromise;
    if (!this.sessionId) throw new Error('没有进行中的录音');
    const ctx = this.context();
    this.assertCurrent(ctx);
    try { this.flow.checkDeadline(monotonicEpochMs()); }
    catch { this.fail(ctx, 'AUDIO_BACKPRESSURE'); return this.current(); }
    const finishDeadline = monotonicEpochMs() + 35_000;
    const promise = new Promise<SessionProgress>(resolve => {
      const timer = setTimeout(() => this.fail(ctx, 'RESULT_TIMEOUT'), 35_000);
      this.resultWaiter = { resolve, timer };
    });
    this.finishPromise = promise;
    void promise.then(() => { if (this.finishPromise === promise) this.finishPromise = null; });
    const ws = this.ws;
    const end = { type: 'audio.end', last_seq: this.flow.lastSequence, total_samples: this.flow.sentSamples };
    // Freeze new audio immediately, while the original 35-second result timer runs.
    this.publish({ state: 'finalizing' });
    if (!this.currentContext(ctx) || TERMINAL.has(this.progress.state)) return promise;
    if (!ws || ws.readyState !== WebSocket.OPEN) { void this.recover(ctx); return promise; }
    const sendEnd = () => {
      if (this.sendEndWhenWritten !== sendEnd || this.audioWrites.size !== 0) return;
      this.sendEndWhenWritten = null;
      if (!this.currentContext(ctx) || this.ws !== ws || TERMINAL.has(this.progress.state)) return;
      // The callback can run before an overdue result timer gets its turn.
      if (monotonicEpochMs() >= finishDeadline) { this.fail(ctx, 'RESULT_TIMEOUT'); return; }
      try { this.flow.checkDeadline(monotonicEpochMs()); }
      catch { this.fail(ctx, 'AUDIO_BACKPRESSURE'); return; }
      if (ws.readyState !== WebSocket.OPEN) { void this.recover(ctx); return; }
      this.endSent = true;
      try {
        ws.send(JSON.stringify(end), error => { if (error && this.currentContext(ctx)) void this.recover(ctx); });
        if (this.currentContext(ctx) && this.ws === ws && !TERMINAL.has(this.progress.state) && !this.endAcknowledged) {
          this.endTimer = setTimeout(() => { if (!this.endAcknowledged) void this.recover(ctx); }, 2000);
        }
      } catch { void this.recover(ctx); }
    };
    this.sendEndWhenWritten = sendEnd;
    sendEnd(); // Synchronous PCM callbacks preserve the synchronous end fast path.
    return promise;
  }

  /** No audio reconnect/replay. A lost end ACK is resolved only by the authoritative GET. */
  private recover(ctx: OperationContext): Promise<void> {
    if (!this.currentContext(ctx) || TERMINAL.has(this.progress.state)) return Promise.resolve();
    if (this.recoveryPromise) return this.recoveryPromise;
    this.autoInsertAllowed = false;
    const target = this.targetId; this.targetId = this.targetApp = null;
    this.targetError = '结果恢复后请重新指定输入目标，或复制结果';
    if (target) void this.native.call('release', { target }).catch(() => undefined);
    this.clearAudioTimers(); this.closeSocket();
    this.publish({ state: 'finalizing' });
    const operation = (async () => {
      try {
        if (!ctx.sessionId) { this.fail(ctx, 'AUDIO_INTERRUPTED'); return; }
        const deadline = monotonicEpochMs() + 32_000;
        while (this.currentContext(ctx) && monotonicEpochMs() < deadline) {
          const session: Session = await this.wait<Session>(ctx, this.api.getSession(ctx.sessionId), Math.min(5000, deadline - monotonicEpochMs()));
          this.assertCurrent(ctx);
          if (session.session_id !== ctx.sessionId || session.current_attempt_no !== ctx.attemptNo) {
            this.fail(ctx, 'SESSION_STATE_CONFLICT'); return;
          }
          if (!session.audio_complete) { this.fail(ctx, session.error_code ?? 'AUDIO_INTERRUPTED'); return; }
          if (this.applySession(ctx, session)) return;
          await this.pause(ctx, 400);
        }
        this.fail(ctx, 'RESULT_TIMEOUT');
      } catch (error) {
        if (this.currentContext(ctx)) this.fail(ctx, error instanceof ApiError ? error.code : 'AUDIO_INTERRUPTED');
      }
    })();
    this.recoveryPromise = operation;
    void operation.then(() => { if (this.recoveryPromise === operation) this.recoveryPromise = null; });
    return operation;
  }
  private applySession(ctx: OperationContext, session: Session): boolean {
    this.assertCurrent(ctx);
    if (TERMINAL.has(this.progress.state)) return true;
    if (session.state === 'ready' && (!session.result || session.result.attempt_no !== ctx.attemptNo)) {
      this.fail(ctx, 'AUDIO_PROTOCOL_ERROR'); return true;
    }
    const patch = { state: session.state, audio_ms: session.audio_ms, error_code: session.error_code, result: session.state === 'ready' ? session.result : null };
    if (TERMINAL.has(session.state)) { this.finishTerminal(patch); return true; }
    this.publish(patch); return false;
  }

  async cancel(): Promise<SessionProgress> {
    const ctx = this.auth.current ? this.context() : null;
    const sessionId = this.sessionId;
    if (ctx && this.currentContext(ctx) && sessionId && !TERMINAL.has(this.progress.state)) {
      // Invoke immediately under the original token; no delayed call may pick up a new account.
      void this.api.cancelSession(sessionId).catch(() => undefined);
      try {
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: 'cancel' }), () => undefined);
      } catch { /* Local cancellation still settles every waiter if the socket closes concurrently. */ }
    }
    const oldTarget = this.targetId;
    this.nextOperation(); this.starting = false; this.autoInsertAllowed = false;
    this.targetId = this.targetApp = this.targetError = null;
    if (oldTarget) void this.native.call('release', { target: oldTarget }).catch(() => undefined);
    this.publish({ state: 'cancelled', result: null, stable_text: '', provisional_text: '', error_code: null }, true);
    return this.progress;
  }

  /** Adopt an authorized history snapshot; no recording, model call or OS write. */
  async selectForRetry(sessionId: string, kind: 'asr' | 'refine'): Promise<SessionProgress> {
    if (typeof sessionId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sessionId)
      || (kind !== 'asr' && kind !== 'refine')) throw new Error('历史重试参数无效');
    this.auth.ensure();
    if (this.isBusy()) throw new Error('已有进行中的语音操作，请稍候');
    if (sessionId !== this.sessionId && this.progress.result && !this.resultHandled) {
      throw new Error('请先处理或关闭当前结果，再选择历史记录');
    }
    const ctx = this.context();
    const selection = {};
    this.selecting = selection;
    try {
      const session = await this.wait(ctx, this.api.getSession(sessionId));
      this.assertCurrent(ctx);
      if (this.selecting !== selection) throw new AuthContextChangedError();
      if (session.session_id !== sessionId || !Number.isInteger(session.current_attempt_no) || session.current_attempt_no < 1
        || typeof session.learning_enabled !== 'boolean') throw new Error('历史会话快照无效');
      if (session.content_expired || session.retry_kind !== kind
        || (kind === 'asr' ? session.state !== 'failed' : session.state !== 'ready')
        || (session.insertion_status !== null && ['reserved', 'inserted', 'unverified'].includes(session.insertion_status))) {
        throw new Error('该历史记录当前不可重试，请刷新后查看');
      }
      if (kind === 'refine' && (!session.result || !session.result.review_required
        || session.result.attempt_no !== session.current_attempt_no
        || session.result.result_id !== session.current_result_id)) throw new Error('历史结果快照无效');
      // Only commit the local switch after every read/check succeeded. No await
      // may split this adoption; a failed or stale GET leaves the old result intact.
      const oldTarget = this.targetId;
      this.nextOperation();
      this.starting = false;
      this.ownerId = ctx.userId; this.sessionId = session.session_id; this.attemptNo = session.current_attempt_no;
      this.sessionLearningEnabled = this.native.supportsObservation !== false && session.learning_enabled;
      this.targetId = this.targetApp = null;
      this.targetError = '历史结果请重新指定输入目标，或复制结果';
      this.insertionId = null; this.lastOutcome = null;
      this.flow = new AudioFlow(); this.frameTotals.clear(); this.acknowledgedSequence = -1;
      this.lastCapturedAt = null; this.partialRevision = this.decodedSamples = 0;
      this.endSent = this.endAcknowledged = false;
      this.autoInsertAllowed = this.resultHandled = false;
      this.progress = { ...EMPTY_PROGRESS, state: session.state, audio_ms: session.audio_ms,
        error_code: session.error_code, result: session.state === 'ready' ? session.result : null };
      if (oldTarget) void this.native.call('release', { target: oldTarget }).catch(() => undefined);
      return this.publish();
    } finally {
      if (this.selecting === selection) this.selecting = null;
    }
  }

  async retry(kind: 'asr' | 'refine'): Promise<SessionProgress> {
    if (!this.sessionId || this.isBusy()) throw new Error('没有可重试的会话');
    if (this.lastOutcome && ['inserted', 'unverified'].includes(this.lastOutcome.status)) throw new Error('结果已输入或无法确认，不可重试模型');
    const oldTarget = this.targetId;
    this.nextOperation(); this.autoInsertAllowed = false; this.resultHandled = false;
    this.targetId = this.targetApp = null; this.targetError = '重试完成后请重新指定输入目标，或复制结果';
    if (oldTarget) void this.native.call('release', { target: oldTarget }).catch(() => undefined);
    this.insertionId = null; this.lastOutcome = null;
    const ctx = this.context();
    this.starting = true;
    this.publish({ state: kind === 'refine' ? 'refining' : 'finalizing', result: null, error_code: null });
    try {
      const session = await this.wait(ctx, this.api.retrySession(ctx.sessionId!, kind, randomUUID()));
      this.assertCurrent(ctx);
      if (session.session_id !== ctx.sessionId || session.current_attempt_no <= (ctx.attemptNo ?? 0)) throw new Error('重试会话标识无效');
      this.attemptNo = session.current_attempt_no;
      const retryCtx = this.context();
      this.publish({ state: kind === 'refine' ? 'refining' : 'finalizing', result: null, error_code: null, provisional_text: '' });
      if (this.applySession(retryCtx, session)) return this.progress;
      // Observe a bounded full-recording replay as well as the original result
      // budget. The current cloud provider replays at 2x; one audio duration
      // covers it without making Main depend on a provider name. The server
      // still owns ASR/processing deadlines and the original audio lease D.
      const replayMs = kind === 'asr' && Number.isFinite(session.audio_ms)
        ? Math.min(Math.max(0, session.audio_ms), AUDIO.ceilingSamples * 1000 / AUDIO.sampleRate) : 0;
      const deadline = monotonicEpochMs() + replayMs + 35_000;
      // A long retry must not exceed the authenticated HTTP limit (120/min).
      const pollInterval = kind === 'asr' ? 1000 : 400;
      while (this.currentContext(retryCtx) && monotonicEpochMs() < deadline) {
        let next: Session;
        try {
          next = await this.wait(retryCtx, this.api.getSession(retryCtx.sessionId!), Math.min(5000, deadline - monotonicEpochMs()));
        } catch (error) {
          if (!this.currentContext(retryCtx) || monotonicEpochMs() < deadline) throw error;
          break;
        }
        if (monotonicEpochMs() >= deadline) break;
        if (next.session_id !== retryCtx.sessionId || next.current_attempt_no !== retryCtx.attemptNo) throw new Error('重试会话标识已变化');
        if (this.applySession(retryCtx, next)) return this.progress;
        const remaining = deadline - monotonicEpochMs();
        if (remaining > 0) await this.pause(retryCtx, Math.min(pollInterval, remaining));
      }
      this.fail(retryCtx, 'RESULT_TIMEOUT'); return this.progress;
    } catch (error) {
      if (ctx.operationEpoch === this.operationEpoch && this.accountCurrent(ctx)) {
        const active = this.context();
        if (!TERMINAL.has(this.progress.state)) this.fail(active, error instanceof ApiError ? error.code : 'ASR_FAILED');
      }
      throw error;
    } finally {
      if (ctx.operationEpoch === this.operationEpoch && this.accountCurrent(ctx)) this.starting = false;
    }
  }

  /** explicit=false is reserved for the normal result auto-insert path. */
  async insert(reviewConfirmed: boolean, explicit = true): Promise<InsertionOutcome> {
    if (this.selecting) throw new Error('正在读取历史记录，请稍候');
    if (this.insertionPromise) return this.insertionPromise;
    if (!this.sessionId || !this.progress.result || this.progress.state !== 'ready') throw new Error('结果尚未就绪');
    const ctx = this.context();
    const result = { ...this.progress.result };
    if (!explicit && (!this.autoInsertAllowed || result.review_required)) throw new Error('该结果需要明确确认或重新指定目标');
    if (this.lastOutcome) return { ...this.lastOutcome };
    const task = this.performInsertion(ctx, result, reviewConfirmed, explicit);
    this.insertionPromise = task;
    void task.then(() => { if (this.insertionPromise === task) this.insertionPromise = null; },
      () => { if (this.insertionPromise === task) this.insertionPromise = null; });
    return task;
  }
  private outcome(ctx: OperationContext, result: SessionResult, value: InsertionOutcome): InsertionOutcome {
    if (this.currentContext(ctx) && this.progress.result?.result_id === result.result_id) {
      this.lastOutcome = { ...value };
      if (value.status === 'inserted' || value.status === 'unverified') this.resultHandled = true;
    }
    return value;
  }
  private fromMark(mark: InsertionMark, result: SessionResult): InsertionOutcome {
    const status = mark.phase === 'started' ? 'unverified' : mark.phase;
    return { status, observable: status === 'inserted' && mark.observable, error_code: mark.error_code,
      message: status === 'inserted' ? '已输入，不会重复执行' : status === 'failed'
        ? '上次未写入；请重新指定目标后再试' : '无法确认是否已输入，请检查目标；不会重复执行',
      insertion_id: mark.insertion_id, review_required: result.review_required };
  }
  private receiptCode(code: unknown, fallback: 'ZERO_WRITE' | 'VERIFY_FAILED' | 'CLIENT_INTERRUPTED'): string {
    const supported = ['TARGET_CHANGED', 'TARGET_UNSUPPORTED', 'PERMISSION_DENIED', 'ZERO_WRITE', 'VERIFY_FAILED', 'CLIENT_INTERRUPTED'];
    // The native helper reports a missing Accessibility grant as PERMISSION_REQUIRED.
    if (code === 'PERMISSION_REQUIRED') return 'PERMISSION_DENIED';
    return typeof code === 'string' && supported.includes(code) ? code : fallback;
  }
  private async performInsertion(ctx: OperationContext, result: SessionResult, reviewConfirmed: boolean, explicit: boolean): Promise<InsertionOutcome> {
    this.assertCurrent(ctx);
    if (explicit && this.resultTargetCapture) await this.resultTargetCapture;
    this.assertCurrent(ctx);
    const unavailable = (code: string, message: string): InsertionOutcome => this.outcome(ctx, result, {
      status: 'unavailable', observable: false, error_code: code, message, insertion_id: null, review_required: result.review_required,
    });
    let targetId = explicit ? this.targetId : null;
    if (explicit && !targetId) return unavailable('TARGET_UNSUPPORTED', this.targetError ?? '结果已生成，请复制到目标输入框');
    if (createHash('sha256').update(result.refined_text, 'utf8').digest('hex') !== result.text_hash) {
      return unavailable('VERIFY_FAILED', '结果文本校验失败，请重新获取结果');
    }
    if (result.review_required && !reviewConfirmed) throw new Error('请先确认当前原文回退结果');
    const insertionId = this.insertionId ?? randomUUID();
    this.insertionId = insertionId;
    const intent: InsertionIntent = { user_id: ctx.userId, session_id: ctx.sessionId!, result_id: result.result_id,
      insertion_id: insertionId, text_hash: result.text_hash };
    let existing: InsertionMark | null;
    try { existing = this.journal.read(ctx.userId, insertionId); }
    catch {
      return this.outcome(ctx, result, { status: 'unverified', observable: false, error_code: 'CLIENT_INTERRUPTED',
        message: '无法核验上次输入记录，请检查目标；不会重复输入', insertion_id: insertionId, review_required: result.review_required });
    }
    if (existing) {
      if (existing.phase === 'started') existing = this.journal.finish(existing, 'unverified', false, 'CLIENT_INTERRUPTED');
      void this.receipt(existing, ctx);
      return this.outcome(ctx, result, this.fromMark(existing, result));
    }
    // Explicit selection preserves the user's chosen target. Automatic input
    // cannot freeze a target across the network claim round trip.
    if (explicit) try {
      const validation = await this.wait(ctx, this.native.call('validate', { target: targetId, restoreFocus: explicit }), 6000);
      this.assertCurrent(ctx);
      if (validation.valid !== true) return unavailable(this.receiptCode(validation.code, 'ZERO_WRITE'),
        String(validation.message ?? '输入位置或内容已改变，请重新指定目标或复制结果'));
    } catch (error) {
      this.assertCurrent(ctx);
      return unavailable(error instanceof NativeCallError ? this.receiptCode(error.code, 'ZERO_WRITE') : 'TARGET_UNSUPPORTED',
        error instanceof Error ? error.message : '无法核验输入目标，请复制结果');
    }
    if (!explicit) {
      this.targetCaptureEpoch++;
      const candidate = this.targetId;
      this.targetId = this.targetApp = this.targetError = null;
      if (candidate) void this.native.call('release', { target: candidate }).catch(() => undefined);
    }
    let claim: Insertion;
    try {
      this.assertCurrent(ctx);
      claim = await this.wait(ctx, this.api.claimInsertion(ctx.sessionId!, {
        result_id: result.result_id, insertion_id: insertionId, text_hash: result.text_hash, review_confirmed: reviewConfirmed,
      }));
      this.assertCurrent(ctx);
    } catch (error) {
      this.assertCurrent(ctx);
      const uncertain = !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
      if (error instanceof ApiError && !uncertain) return this.outcome(ctx, result, {
        status: 'failed', observable: false, error_code: error.code, message: error.message,
        insertion_id: null, review_required: result.review_required,
      });
      // The claim may have committed. Never assume a lost reply authorizes a second attempt.
      try {
        const started = this.journal.begin(intent);
        const mark = started.mark.phase === 'started'
          ? this.journal.finish(intent, 'unverified', false, 'CLIENT_INTERRUPTED') : started.mark;
        void this.receipt(mark, ctx);
      } catch { /* No OS write occurs when durable intent cannot be confirmed. */ }
      return this.outcome(ctx, result, { status: 'unverified', observable: false, error_code: 'CLIENT_INTERRUPTED',
        message: '无法确认输入资格状态，请保留结果供复制；不会自动重试', insertion_id: insertionId, review_required: result.review_required });
    }
    if (claim.insertion_id !== insertionId || claim.session_id !== ctx.sessionId || claim.result_id !== result.result_id) {
      return this.outcome(ctx, result, { status: 'unverified', observable: false, error_code: 'CLIENT_INTERRUPTED',
        message: '输入资格与当前结果不一致，未执行输入', insertion_id: insertionId, review_required: result.review_required });
    }
    let mark: InsertionMark;
    let created: boolean;
    try {
      const begun = this.journal.begin(intent); mark = begun.mark; created = begun.created;
      if (!created || claim.replayed !== false || claim.status !== 'reserved') {
        if (mark.phase === 'started') {
          const phase = claim.status === 'inserted' || claim.status === 'failed' || claim.status === 'unverified' ? claim.status : 'unverified';
          const code = phase === 'inserted' ? null : claim.status === 'reserved' ? 'CLIENT_INTERRUPTED' : claim.error_code;
          mark = this.journal.finish(intent, phase, phase === 'inserted' && claim.observable, code);
        }
        void this.receipt(mark, ctx);
        return this.outcome(ctx, result, this.fromMark(mark, result));
      }
    } catch {
      return this.outcome(ctx, result, { status: 'unverified', observable: false, error_code: 'CLIENT_INTERRUPTED',
        message: '本机输入执行记录无法确认，未重复执行；请检查目标或复制结果', insertion_id: insertionId, review_required: result.review_required });
    }
    if (!this.currentContext(ctx)) {
      mark = this.journal.finish(intent, 'failed', false, 'CLIENT_INTERRUPTED');
      void this.receipt(mark, ctx); throw new AuthContextChangedError();
    }
    let phase: 'inserted' | 'failed' | 'unverified' = 'unverified';
    let observable = false;
    let errorCode: string | null = 'VERIFY_FAILED';
    let message = '无法确认是否已输入，请检查目标；不会重复执行';
    let warning: string | undefined;
    try {
      // The durable started mark is already fsynced. Exactly one native call follows.
      const response = await this.native.call(explicit ? 'insert' : 'insertCurrent', {
        ...(explicit ? { target: targetId } : {}), text: result.refined_text,
        insertion_id: insertionId, restoreFocus: explicit });
      if (!explicit && typeof response.target === 'string' && response.target) {
        targetId = response.target;
        if (this.currentContext(ctx)) {
          this.targetId = targetId;
          this.targetApp = typeof response.app === 'string' ? response.app : '目标应用';
          this.targetError = null;
        } else void this.native.call('release', { target: targetId }).catch(() => undefined);
      }
      if (response.status === 'inserted' && response.effect === 'written') {
        phase = 'inserted'; observable = response.observable === true; errorCode = null;
      } else if (response.status === 'failed' && response.effect === 'none') {
        phase = 'failed'; errorCode = this.receiptCode(response.code, 'ZERO_WRITE');
      } else { errorCode = this.receiptCode(response.code, 'VERIFY_FAILED'); }
      if (typeof response.warning === 'string') warning = windowsInputMessage(response.warning, this.auth.current?.settings.locale);
      if (typeof response.message === 'string') message = process.platform === 'win32'
        ? windowsInputMessage(response.message, this.auth.current?.settings.locale) : response.message;
    } catch (error) {
      if (error instanceof NativeCallError && error.effect === 'none') {
        phase = 'failed'; errorCode = this.receiptCode(error.code, 'ZERO_WRITE');
      } else { phase = 'unverified'; errorCode = 'CLIENT_INTERRUPTED'; }
      message = error instanceof Error ? error.message : message;
      if (process.platform === 'win32') message = windowsInputMessage(message, this.auth.current?.settings.locale);
    }
    try { mark = this.journal.finish(intent, phase, observable, errorCode); }
    catch {
      // Do not send a terminal receipt that cannot be recovered with the identical payload.
      return this.outcome(ctx, result, { status: 'unverified', observable: false, error_code: 'CLIENT_INTERRUPTED',
        message: '输入执行记录保存失败，请检查目标；不会再次输入', insertion_id: insertionId, review_required: result.review_required });
    }
    const outcome: InsertionOutcome = { status: phase, observable, error_code: errorCode, message, ...(warning ? { warning } : {}),
      insertion_id: insertionId, review_required: result.review_required };
    this.outcome(ctx, result, outcome);
    if (phase === 'inserted' && observable && targetId && this.currentContext(ctx) && this.sessionLearningEnabled && this.auth.learningEnabled) {
      this.startObserver(ctx, mark, targetId, result);
    }
    else if (phase !== 'failed' && targetId) void this.native.call('release', { target: targetId }).catch(() => undefined);
    // Receipt retries are idempotent metadata operations; none call native or recapture a target.
    void this.receipt(mark, ctx).then(confirmed => {
      if (confirmed && targetId && this.currentContext(ctx)) this.observer?.confirmReceipt(this.observationIdentity(ctx, mark, targetId));
    });
    this.assertCurrent(ctx);
    return outcome;
  }

  private receipt(mark: InsertionMark, source: AccountContext): Promise<boolean> {
    // A result reset may stop observation, but this immutable receipt still belongs to the same account.
    const ctx: AccountContext = { userId: source.userId, accountEpoch: source.accountEpoch, signal: source.signal };
    if (!this.accountCurrent(ctx) || mark.user_id !== ctx.userId.toLowerCase() || mark.phase === 'started') return Promise.resolve(false);
    if (mark.receipt_confirmed) return Promise.resolve(true);
    const key = `${ctx.accountEpoch}/${mark.user_id}/${mark.insertion_id}`;
    const existing = this.receiptTasks.get(key);
    if (existing) return existing;
    const payload = Object.freeze({ status: mark.phase, observable: mark.observable, error_code: mark.error_code });
    const operation = (async () => {
      for (let attempt = 0; attempt < 3; attempt++) {
        if (!this.accountCurrent(ctx)) return false;
        try {
          const receipt = await this.wait(ctx, this.api.insertionReceipt(mark.session_id, mark.insertion_id, payload), 5000);
          this.assertAccount(ctx);
          if (receipt.insertion_id !== mark.insertion_id || receipt.session_id !== mark.session_id || receipt.result_id !== mark.result_id
            || receipt.status !== payload.status || receipt.observable !== payload.observable || receipt.error_code !== payload.error_code) return false;
          this.journal.confirmReceipt(mark); return true;
        } catch (error) {
          if (!this.accountCurrent(ctx) || error instanceof AuthContextChangedError) return false;
          if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) return false;
        }
        if (attempt < 2) {
          try { await this.wait(ctx, new Promise<void>(resolve => setTimeout(resolve, 250 * (attempt + 1))), 1500); }
          catch { return false; }
        }
      }
      return false;
    })();
    this.receiptTasks.set(key, operation);
    void operation.then(() => { if (this.receiptTasks.get(key) === operation) this.receiptTasks.delete(key); });
    return operation;
  }

  /** Called once after login/restore. Replays only recorded receipt payloads, never native input. */
  async recoverReceipts(): Promise<void> {
    const ctx = this.account();
    let marks: InsertionMark[];
    try { marks = this.journal.unconfirmed(ctx.userId); }
    catch {
      if (this.accountCurrent(ctx)) this.emitLearningIssue('本机输入记录无法核验，旧结果不会自动再次输入');
      return;
    }
    for (const mark of marks) {
      if (!this.accountCurrent(ctx)) return;
      await this.receipt(mark, ctx);
    }
  }

  private observationIdentity(ctx: OperationContext, mark: InsertionMark, targetId: string): ObservationIdentity {
    return { origin: API_BASE_URL, userId: ctx.userId, authEpoch: ctx.accountEpoch, sessionId: mark.session_id,
      resultId: mark.result_id, insertionId: mark.insertion_id, targetId };
  }
  private startObserver(ctx: OperationContext, mark: InsertionMark, targetId: string, result: SessionResult): void {
    if (!this.currentContext(ctx) || !this.sessionLearningEnabled || !this.auth.learningEnabled) return;
    this.observer?.stop('cancelled');
    const identity = this.observationIdentity(ctx, mark, targetId);
    const isCurrent = () => this.currentContext(ctx) && this.sessionLearningEnabled && this.auth.learningEnabled
      && this.progress.result?.result_id === result.result_id && this.insertionId === mark.insertion_id;
    const observer = new CorrectionObserver({
      identity, insertedText: result.refined_text,
      isCurrent,
      poll: async () => {
        if (!isCurrent()) throw new AuthContextChangedError();
        const sample = await this.wait(ctx, this.native.call('observe', { target: targetId }), 6000);
        if (!isCurrent()) throw new AuthContextChangedError();
        return parseNativeObservation(sample);
      },
      submit: async (body, _identity, signal) => {
        if (!isCurrent() || signal.aborted) throw new AuthContextChangedError();
        const currentMark = this.journal.read(mark.user_id, mark.insertion_id);
        if (!currentMark?.receipt_confirmed || currentMark.phase !== 'inserted' || !currentMark.observable) {
          throw new Error('输入回执未确认');
        }
        const event = await this.wait(ctx, this.api.submitCorrection(body, signal), 10_000);
        if (!isCurrent() || signal.aborted) throw new AuthContextChangedError();
        return event;
      },
      shouldRetry: error => !(error instanceof AuthContextChangedError)
        && (!(error instanceof ApiError) || error.status === 0 || error.status >= 500),
      release: frozen => { void this.native.call('release', { target: frozen.targetId }).catch(() => undefined); },
      onSubmitted: (eventId, replacement) => { void this.pollLearningEvent(ctx, eventId, replacement, result.result_id); },
      onEnd: reason => {
        if (this.observer === observer) this.observer = null;
        if (this.currentContext(ctx) && ['receipt_unconfirmed', 'submit_failed', 'ime_unknown', 'ime_uncommitted'].includes(reason)) {
          this.emitLearningIssue('修改已保留，词库未更新；可在词库中手动添加');
        }
      },
    });
    this.observer = observer;
    observer.start();
    if (mark.receipt_confirmed) observer.confirmReceipt(identity);
  }
  private async pollLearningEvent(ctx: OperationContext, eventId: string, replacement: Replacement, resultId: string): Promise<void> {
    const deadline = monotonicEpochMs() + 11_000;
    try {
      while (this.currentContext(ctx) && this.progress.result?.result_id === resultId && monotonicEpochMs() < deadline) {
        await this.pause(ctx, 300);
        this.assertCurrent(ctx);
        const event = await this.wait(ctx, this.api.getLearningEvent(eventId), Math.max(1, Math.min(5000, deadline - monotonicEpochMs())));
        this.assertCurrent(ctx);
        if (this.progress.result?.result_id !== resultId || !this.auth.learningEnabled) return;
        if (event.status === 'pending') continue;
        // Uncertain classifications remain unapplied; only a real server-side
        // addition is announced. Never turn suggested into a client confirm.
        if (event.status === 'learned') this.emitLearning({ ...event, before: replacement.before, after: replacement.after });
        else if (event.status === 'failed') this.emitLearningIssue('修改已保留，词库未更新；可在词库中手动添加');
        return;
      }
      if (this.currentContext(ctx)) this.emitLearningIssue('本次学习结果尚未确认，可稍后在词库查看');
    } catch {
      if (this.currentContext(ctx)) this.emitLearningIssue('修改已保留，词库更新状态未确认');
    }
  }

  copy(text: string): void {
    clipboard.writeText(text);
    if (text === this.progress.result?.refined_text || text === this.progress.result?.raw_text) this.resultHandled = true;
  }
  reset(): void {
    this.retargeting = null;
    if (this.sessionId && this.ownerId === this.auth.current?.user.id && !TERMINAL.has(this.progress.state)) {
      void this.api.cancelSession(this.sessionId).catch(() => undefined);
    }
    const target = this.targetId;
    this.nextOperation();
    this.starting = false; this.sessionId = null; this.attemptNo = null;
    this.ownerId = null; this.sessionLearningEnabled = false;
    this.targetId = this.targetApp = this.targetError = null;
    this.insertionId = null; this.lastOutcome = null;
    this.flow = new AudioFlow(); this.frameTotals.clear(); this.acknowledgedSequence = -1;
    this.lastCapturedAt = null;
    this.partialRevision = this.decodedSamples = 0; this.endSent = this.endAcknowledged = false;
    this.autoInsertAllowed = this.resultHandled = false;
    if (target) void this.native.call('release', { target }).catch(() => undefined);
    this.progress = { ...EMPTY_PROGRESS }; this.publish();
  }
  /** Invoke at every account boundary, including logout/relogin of the same user. */
  invalidateAccount(): void {
    this.accountEpoch++; this.accountAbort.abort(); this.accountAbort = new AbortController();
    this.reset();
  }
  dispose(): void { this.invalidateAccount(); }
  current(): SessionProgress {
    return Object.freeze({ ...this.progress,
      locale: this.auth.current?.settings.locale ?? 'zh-CN',
      target: Object.freeze({ insertion_available: this.targetId !== null, target_app: this.targetApp, target_error: this.targetError }),
      result: this.progress.result ? Object.freeze({ ...this.progress.result }) : null });
  }
  currentSessionId(): string | null { return this.sessionId; }
  isBusy(): boolean {
    return this.starting || this.selecting !== null || this.retargeting !== null || this.insertionPromise !== null || (this.sessionId !== null && !TERMINAL.has(this.progress.state));
  }
  async getSession(): Promise<Session | null> {
    if (!this.sessionId) return null;
    const ctx = this.context();
    return this.wait(ctx, this.api.getSession(ctx.sessionId!));
  }
}
