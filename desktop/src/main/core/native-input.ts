import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { join } from 'node:path';
import { app } from 'electron';

export type NativeEffect = 'none' | 'written' | 'unknown';
export class NativeCallError extends Error {
  constructor(message: string, readonly code: string, readonly effect: NativeEffect, readonly action?: string) {
    super(message);
    this.name = 'NativeCallError';
  }
}
interface PendingCall {
  resolve: (data: Record<string, unknown>) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
  action: string;
  dispatched: boolean;
}
const MAX_LINE_BYTES = 1024 * 1024;
const isWrite = (action: string): boolean => action === 'insert' || action === 'insertCurrent';
const effectOf = (value: unknown): NativeEffect | undefined =>
  value === 'none' || value === 'written' || value === 'unknown' ? value : undefined;

export class NativeVoiceInput {
  private child: ChildProcessWithoutNullStreams | null = null;
  private seq = 0;
  private disposed = false;
  private readonly pending = new Map<number, PendingCall>();
  private readonly permissionProbes = new Set<NativeVoiceInput>();
  private permissionRevision = 0;
  private lastPermission: boolean | null = null;
  private permissionResetPending = false;

  async call(action: string, data: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    if (action !== 'permission' || this.disposed || process.platform !== 'darwin') {
      if (this.permissionResetPending && !this.disposed && process.platform === 'darwin') {
        // Never tear down a helper while a paste acknowledgement is in flight.
        // New work is proven undispatched and is not automatically retried.
        if (this.pending.size) throw new NativeCallError('权限正在刷新，请稍后重试', 'NATIVE_REFRESH_PENDING', 'none', action);
        this.invalidate('辅助功能授权状态已更新');
        this.permissionResetPending = false;
      }
      return this.callOnChild(action, data);
    }
    // AX trust can remain stale in an already-running helper after a system
    // settings change. Query a fresh, isolated helper without touching targets
    // or in-flight writes in the long-lived insertion helper.
    const revision = ++this.permissionRevision;
    const probe = new NativeVoiceInput();
    this.permissionProbes.add(probe);
    try {
      const result = await probe.callOnChild(action, data);
      if (this.disposed) throw new NativeCallError('原生输入桥已关闭', 'NATIVE_CLOSED', 'none', action);
      if (revision === this.permissionRevision && typeof result.trusted === 'boolean') {
        if (result.trusted !== this.lastPermission) this.permissionResetPending = true;
        this.lastPermission = result.trusted;
      }
      return result;
    } finally {
      this.permissionProbes.delete(probe);
      probe.dispose();
    }
  }

  private async callOnChild(action: string, data: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    if (this.disposed) throw new NativeCallError('原生输入桥已关闭', 'NATIVE_CLOSED', 'none', action);
    if (process.platform !== 'darwin') {
      if (action === 'permission') return { supported: false, trusted: false };
      throw new NativeCallError('外部输入目前仅在 macOS 提供', 'NATIVE_UNSUPPORTED', 'none', action);
    }
    const id = ++this.seq;
    // Serialize before spawning or enqueuing; errors here are proven zero-write.
    let line: string;
    try { line = `${JSON.stringify({ ...data, id, action })}\n`; }
    catch { throw new NativeCallError('输入桥请求无效', 'NATIVE_REQUEST_INVALID', 'none', action); }
    if (Buffer.byteLength(line) > MAX_LINE_BYTES) {
      throw new NativeCallError('输入桥请求过长', 'NATIVE_REQUEST_TOO_LARGE', 'none', action);
    }
    if (!this.child) {
      try { this.spawnChild(); }
      catch { throw new NativeCallError('原生输入桥未安装，请重新构建客户端', 'NATIVE_UNAVAILABLE', 'none', action); }
    }
    const child = this.child;
    if (!child) throw new NativeCallError('原生输入桥未安装，请重新构建客户端', 'NATIVE_UNAVAILABLE', 'none', action);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail('原生输入桥响应超时；请检查目标，未自动重试', 'NATIVE_TIMEOUT', child), 5000);
      const entry: PendingCall = { resolve, reject, timer, action, dispatched: false };
      this.pending.set(id, entry);
      try {
        // A thrown write or asynchronous pipe error can occur after partial delivery.
        // Treat every attempted insert dispatch as unknown unless helper proves none.
        entry.dispatched = true;
        child.stdin.write(line);
      } catch { this.fail('原生输入桥连接已关闭', 'NATIVE_DISCONNECTED', child); }
    });
  }

  private spawnChild(): void {
    const binary = app.isPackaged
      ? join(process.resourcesPath, 'voice', 'smartvoiceinput-input')
      : join(app.getAppPath(), 'resources', 'voice', 'smartvoiceinput-input');
    const child = spawn(binary, [], { stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;
    child.stderr.resume();
    let buffered = Buffer.alloc(0);
    child.stdout.on('data', (chunk: Buffer) => {
      if (this.child !== child) return;
      buffered = Buffer.concat([buffered, chunk]);
      while (this.child === child) {
        const newline = buffered.indexOf(10);
        if (newline < 0) {
          if (buffered.length > MAX_LINE_BYTES) this.fail('输入桥响应过长', 'NATIVE_PROTOCOL_ERROR', child);
          return;
        }
        if (newline > MAX_LINE_BYTES) { this.fail('输入桥响应过长', 'NATIVE_PROTOCOL_ERROR', child); return; }
        const bytes = buffered.subarray(0, newline);
        buffered = buffered.subarray(newline + 1);
        try {
          const result: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
          if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('object required');
          const record = result as Record<string, unknown>;
          if (typeof record.id !== 'number' || !Number.isSafeInteger(record.id)) throw new Error('id required');
          const entry = this.pending.get(record.id);
          if (!entry) continue;
          this.pending.delete(record.id);
          clearTimeout(entry.timer);
          if (record.error) {
            const error = typeof record.error === 'object' ? record.error as Record<string, unknown> : {};
            entry.reject(new NativeCallError(typeof error.message === 'string' ? error.message : String(record.error),
              typeof error.code === 'string' ? error.code : 'NATIVE_ERROR',
              effectOf(error.effect) ?? (isWrite(entry.action) ? 'unknown' : 'none'), entry.action));
          } else {
            // Preserve flat legacy responses, but never promote missing effect to none.
            if (isWrite(entry.action)) {
              record.effect = effectOf(record.effect) ?? 'unknown';
              if (record.status === 'inserted' && (record.effect !== 'written' || record.observable !== true)) {
                record.status = 'unverified'; record.effect = 'unknown'; record.observable = false;
                record.code = 'WRITE_UNVERIFIED';
              }
            }
            entry.resolve(record);
          }
        } catch { this.fail('原生输入桥返回无效数据', 'NATIVE_PROTOCOL_ERROR', child); return; }
      }
    });
    child.stdout.on('error', () => this.fail('原生输入桥读取失败', 'NATIVE_DISCONNECTED', child));
    child.once('error', () => this.fail('原生输入桥未安装，请重新构建客户端', 'NATIVE_UNAVAILABLE', child, !child.pid));
    child.once('exit', () => this.fail('原生输入桥已退出', 'NATIVE_DISCONNECTED', child));
    child.stdin.on('error', () => this.fail('原生输入桥连接已关闭', 'NATIVE_DISCONNECTED', child));
  }

  private fail(message: string, code: string, owner = this.child, provenNoProcess = false): void {
    if (owner !== this.child) return;
    const child = this.child;
    this.child = null;
    child?.kill();
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      const effect = isWrite(entry.action) && entry.dispatched && !provenNoProcess ? 'unknown' : 'none';
      entry.reject(new NativeCallError(message, code, effect, entry.action));
    }
    this.pending.clear();
  }

  /** Drop all captured targets on account/permission change. The next call starts fresh. */
  invalidate(reason = '原生输入目标已重置'): void { this.fail(reason, 'NATIVE_INVALIDATED'); }
  dispose(): void {
    this.disposed = true;
    this.permissionRevision++;
    for (const probe of this.permissionProbes) probe.dispose();
    this.permissionProbes.clear();
    this.fail('应用已退出', 'NATIVE_CLOSED');
  }
}
