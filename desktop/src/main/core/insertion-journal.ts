import { randomUUID, createHash } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { syncPublishedFile } from '../../../native/durable-file';

export type InsertionPhase = 'started' | 'inserted' | 'failed' | 'unverified';
export interface InsertionIntent {
  user_id: string;
  session_id: string;
  result_id: string;
  insertion_id: string;
  text_hash: string;
}
export interface InsertionMark extends InsertionIntent {
  version: 1;
  phase: InsertionPhase;
  observable: boolean;
  error_code: string | null;
  started_at_ms: number;
  receipt_confirmed: boolean;
}
export class JournalUncertainError extends Error {}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const hash = /^[0-9a-f]{64}$/;
function intent(input: InsertionIntent): InsertionIntent {
  if (![input.user_id, input.session_id, input.result_id, input.insertion_id].every(x => typeof x === 'string' && uuid.test(x)) || !hash.test(input.text_hash)) {
    throw new JournalUncertainError('无效的本机输入执行标记');
  }
  // Explicit allowlist: do not accidentally persist result/target plaintext.
  return { user_id: input.user_id.toLowerCase(), session_id: input.session_id.toLowerCase(), result_id: input.result_id.toLowerCase(), insertion_id: input.insertion_id.toLowerCase(), text_hash: input.text_hash };
}
function checked(input: InsertionMark): InsertionMark {
  const key = intent(input);
  if (input.version !== 1 || !['started', 'inserted', 'failed', 'unverified'].includes(input.phase) || typeof input.observable !== 'boolean' || typeof input.receipt_confirmed !== 'boolean' || !Number.isSafeInteger(input.started_at_ms) || input.started_at_ms < 0 || (input.observable && input.phase !== 'inserted') || !(input.error_code === null || (typeof input.error_code === 'string' && /^[A-Z_]{1,64}$/.test(input.error_code)))) {
    throw new JournalUncertainError('本机输入执行标记损坏，请检查目标后复制结果');
  }
  return { ...key, version: 1, phase: input.phase, observable: input.observable, error_code: input.error_code, started_at_ms: input.started_at_ms, receipt_confirmed: input.receipt_confirmed };
}

/** Durable metadata-only intent. An existing mark is never permission to write again. */
export class InsertionJournal {
  constructor(private readonly root: string) {}

  private directory(userId: string): string {
    if (!uuid.test(userId)) throw new JournalUncertainError('无效的账户输入执行标记');
    return join(this.root, createHash('sha256').update(userId.toLowerCase()).digest('hex'));
  }
  private path(userId: string, insertionId: string): string {
    if (!uuid.test(insertionId)) throw new JournalUncertainError('无效的输入执行标记');
    return join(this.directory(userId), `${insertionId.toLowerCase()}.json`);
  }
  read(userId: string, insertionId: string): InsertionMark | null {
    const file = this.path(userId, insertionId);
    if (!existsSync(file)) return null;
    try {
      const raw = readFileSync(file);
      if (raw.byteLength > 4096) throw new Error('oversized');
      const value = checked(JSON.parse(raw.toString('utf8')) as InsertionMark);
      if (value.user_id !== userId.toLowerCase() || value.insertion_id !== insertionId.toLowerCase()) throw new Error('identity mismatch');
      return value;
    } catch {
      // A torn/unknown file is conservatively uncertain, never an absent intent.
      throw new JournalUncertainError('无法核验上次输入状态，请检查目标；不会重复输入');
    }
  }

  begin(input: InsertionIntent): { created: boolean; mark: InsertionMark } {
    const key = intent(input);
    const previous = this.read(key.user_id, key.insertion_id);
    if (previous) {
      if (previous.session_id !== key.session_id || previous.result_id !== key.result_id || previous.text_hash !== key.text_hash) throw new JournalUncertainError('输入执行标记与当前结果不匹配');
      return { created: false, mark: previous };
    }
    const directory = this.directory(key.user_id);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const mark: InsertionMark = { ...key, version: 1, phase: 'started', observable: false, error_code: null, started_at_ms: Date.now(), receipt_confirmed: false };
    const file = this.path(key.user_id, key.insertion_id);
    let fd: number;
    try {
      fd = openSync(file, 'wx', 0o600);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      const existing = this.read(key.user_id, key.insertion_id);
      if (!existing) throw new JournalUncertainError('输入执行标记竞争失败');
      if (existing.result_id !== key.result_id || existing.text_hash !== key.text_hash || existing.session_id !== key.session_id) throw new JournalUncertainError('输入执行标记冲突');
      return { created: false, mark: existing };
    }
    try {
      writeFileSync(fd, JSON.stringify(mark));
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    syncPublishedFile(directory, file);
    this.confirmSaved(mark);
    return { created: true, mark };
  }

  finish(input: InsertionIntent, phase: Exclude<InsertionPhase, 'started'>, observable: boolean, errorCode: string | null): InsertionMark {
    const key = intent(input);
    const previous = this.read(key.user_id, key.insertion_id);
    if (!previous || previous.result_id !== key.result_id || previous.text_hash !== key.text_hash || previous.session_id !== key.session_id) throw new JournalUncertainError('缺少匹配的输入执行标记');
    if (previous.phase !== 'started') {
      if (previous.phase === phase && previous.observable === observable && previous.error_code === errorCode) return previous;
      throw new JournalUncertainError('输入执行状态已终结，禁止更改后重新执行');
    }
    return this.save(checked({ ...previous, phase, observable, error_code: errorCode }));
  }

  confirmReceipt(mark: InsertionMark): InsertionMark {
    const previous = this.read(mark.user_id, mark.insertion_id);
    if (!previous || previous.phase === 'started' || previous.phase !== mark.phase || previous.observable !== mark.observable || previous.error_code !== mark.error_code) throw new JournalUncertainError('回执与本机输入状态不一致');
    return this.save(checked({ ...previous, receipt_confirmed: true }));
  }

  unconfirmed(userId: string): InsertionMark[] {
    const directory = this.directory(userId);
    if (!existsSync(directory)) return [];
    const pending: InsertionMark[] = [];
    for (const name of readdirSync(directory)) {
      if (!name.endsWith('.json') || !uuid.test(name.slice(0, -5))) continue;
      let mark = this.read(userId, name.slice(0, -5));
      if (!mark || mark.receipt_confirmed) continue;
      if (mark.phase === 'started') mark = this.finish(mark, 'unverified', false, 'CLIENT_INTERRUPTED');
      pending.push(mark);
    }
    return pending;
  }

  private save(mark: InsertionMark): InsertionMark {
    const file = this.path(mark.user_id, mark.insertion_id);
    const temporary = `${file}.${randomUUID()}.tmp`;
    let fd: number | null = null;
    try {
      fd = openSync(temporary, 'wx', 0o600);
      writeFileSync(fd, JSON.stringify(mark)); fsyncSync(fd); closeSync(fd); fd = null;
      renameSync(temporary, file); syncPublishedFile(this.directory(mark.user_id), file);
      this.confirmSaved(mark);
    } finally {
      if (fd !== null) closeSync(fd);
      if (existsSync(temporary)) rmSync(temporary, { force: true });
    }
    return mark;
  }
  private confirmSaved(mark: InsertionMark): void {
    const actual = this.read(mark.user_id, mark.insertion_id);
    if (!actual || Object.keys(mark).some(key => actual[key as keyof InsertionMark] !== mark[key as keyof InsertionMark])) {
      throw new JournalUncertainError('本机输入执行标记读回失败，未重复执行');
    }
  }
}
