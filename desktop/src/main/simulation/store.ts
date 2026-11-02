import { createHash, randomBytes, randomUUID, scryptSync, timingSafeEqual } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { z } from 'zod';
import { syncPublishedFile } from '../../../native/durable-file';
import type { AuthSnapshot, DictionaryPage, Insertion, Page, Session, SessionResult, Settings, Stats, Term, TokenPair, User } from '@shared/types';
import type { ImportResult } from '../core/api-client';

export const SIMULATION_TEXT = '这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。';
export const SIMULATION_MAX_SAMPLES = 2_880_000;

export class SimulationError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly details: Record<string, unknown> = {}) {
    super(message);
  }
}

interface Account {
  user: User;
  settings: Settings;
  salt: string;
  passwordHash: string;
  dictionaryVersion: number;
  terms: Term[];
}
interface Login {
  userId: string;
  deviceId: string;
  refreshDigest: string;
  refreshExpiresAt: number;
  access: Array<{ digest: string; expiresAt: number }>;
}
interface StoredSession {
  userId: string;
  deviceId: string;
  requestId: string;
  requestHash: string;
  dictionaryVersion: number;
  session: Session;
}
interface StoredInsertion {
  userId: string;
  deviceId: string;
  requestHash: string;
  insertion: Insertion;
}
interface Replay {
  userId: string;
  kind: string;
  key: string;
  hash: string;
  result: unknown;
}
interface Snapshot {
  schema: 1;
  accounts: Account[];
  logins: Login[];
  sessions: StoredSession[];
  insertions: StoredInsertion[];
  replays: Replay[];
}
type Receipt = { status: 'inserted' | 'failed' | 'unverified'; observable: boolean; error_code: string | null };
export type Claim = { result_id: string; insertion_id: string; text_hash: string; review_confirmed: boolean };
export type CsvRow = { row: number; term: string; aliases: string[] };
export type PageInput = { cursor?: string; limit: number };
export type DictionaryInput = PageInput & { q?: string; source?: 'all' | 'manual' | 'auto' };

const nowIso = () => new Date().toISOString();
const digest = (value: string) => createHash('sha256').update(value).digest('hex');
const requestHash = (value: unknown) => digest(JSON.stringify(value));
const freshSnapshot = (): Snapshot => ({ schema: 1, accounts: [], logins: [], sessions: [], insertions: [], replays: [] });
const terminal = new Set<Session['state']>(['ready', 'empty', 'failed', 'cancelled']);
const notFound = () => new SimulationError(404, 'NOT_FOUND', '模拟记录不存在');
const invalid = (message = '模拟请求无效') => new SimulationError(422, 'VALIDATION_ERROR', message);

const identifier = z.string().uuid();
const timestamp = z.string().datetime();
const nonnegative = z.number().int().nonnegative();
const userSchema = z.object({ id: identifier, email: z.string().email(), display_name: z.string().nullable(), role: z.literal('user'),
  status: z.literal('active'), locale: z.enum(['zh-CN', 'en']), created_at: timestamp, last_login_at: timestamp.nullable() }).strict();
const settingsSchema = z.object({ history_retention_days: z.union([z.literal(7), z.literal(30), z.literal(90)]).nullable(),
  learning_enabled: z.boolean(), locale: z.enum(['zh-CN', 'en']), version: z.number().int().positive() }).strict();
const resultSchema = z.object({ result_id: identifier, attempt_no: z.number().int().positive(), attempt_kind: z.literal('initial'),
  raw_text: z.string(), refined_text: z.string(), review_required: z.boolean(), review_reason: z.string().nullable(),
  text_hash: z.string().regex(/^[0-9a-f]{64}$/), dictionary_version: nonnegative, stop_to_result_ms: nonnegative, created_at: timestamp }).strict();
const sessionSchema = z.object({ session_id: identifier, mode: z.literal('dictation'),
  state: z.enum(['created', 'streaming', 'finalizing', 'refining', 'ready', 'empty', 'failed', 'cancelled']),
  current_attempt_no: z.number().int().positive(), learning_enabled: z.boolean(), current_result_id: identifier.nullable(),
  audio_complete: z.boolean(), received_samples: nonnegative, audio_ms: nonnegative, max_record_seconds: z.literal(180),
  created_at: timestamp, first_terminal_at: timestamp.nullable(), content_expires_at: timestamp.nullable(), content_expired: z.boolean(),
  error_code: z.string().nullable(), result: resultSchema.nullable(), retry_kind: z.literal('none'), retry_expires_at: z.null(),
  retry_unavailable_reason: z.string().nullable(), insertion_status: z.enum(['reserved', 'inserted', 'failed', 'unverified']).nullable() }).strict();
const insertionSchema = z.object({ replayed: z.boolean(), insertion_id: identifier, session_id: identifier, result_id: identifier,
  status: z.enum(['reserved', 'inserted', 'failed', 'unverified']), observable: z.boolean(), error_code: z.string().nullable(),
  claimed_at: timestamp, completed_at: timestamp.nullable() }).strict();
const termSchema = z.object({ id: identifier, term: z.string(), norm: z.string(), aliases: z.array(z.string()),
  contexts: z.array(z.object({ alias_norm: z.string(), before: z.string(), after: z.string(), left: z.string(), right: z.string() }).strict()),
  scope: z.literal('default'), source: z.literal('manual'), version: z.number().int().positive(), dictionary_version: nonnegative,
  created_at: timestamp, updated_at: timestamp, ambiguous_aliases: z.array(z.string()) }).strict();
const snapshotSchema = z.object({ schema: z.literal(1),
  accounts: z.array(z.object({ user: userSchema, settings: settingsSchema, salt: z.string().regex(/^[0-9a-f]{32}$/),
    passwordHash: z.string().regex(/^[0-9a-f]{64}$/), dictionaryVersion: nonnegative, terms: z.array(termSchema) }).strict()),
  logins: z.array(z.object({ userId: identifier, deviceId: identifier, refreshDigest: z.string().regex(/^[0-9a-f]{64}$/),
    refreshExpiresAt: nonnegative, access: z.array(z.object({ digest: z.string().regex(/^[0-9a-f]{64}$/), expiresAt: nonnegative }).strict()) }).strict()),
  sessions: z.array(z.object({ userId: identifier, deviceId: identifier, requestId: identifier, requestHash: z.string(),
    dictionaryVersion: nonnegative, session: sessionSchema }).strict()),
  insertions: z.array(z.object({ userId: identifier, deviceId: identifier, requestHash: z.string(), insertion: insertionSchema }).strict()),
  replays: z.array(z.object({ userId: identifier, kind: z.string(), key: identifier, hash: z.string(), result: z.json() }).strict()),
}).strict();

function loadSnapshot(file: string): Snapshot {
  if (!existsSync(file)) return freshSnapshot();
  if (!statSync(file).isFile() || statSync(file).size > 32 * 1024 * 1024) throw new Error('SIMULATION_STATE_INVALID');
  try {
    const parsed = snapshotSchema.parse(JSON.parse(readFileSync(file, 'utf8')));
    const accounts = new Set(parsed.accounts.map(account => account.user.id));
    if (accounts.size !== parsed.accounts.length || new Set(parsed.accounts.map(account => account.user.email)).size !== accounts.size
      || parsed.accounts.some(account => !fictionalEmail(account.user.email))
      || parsed.logins.some(login => !accounts.has(login.userId))
      || parsed.sessions.some(row => !accounts.has(row.userId))
      || parsed.insertions.some(row => !accounts.has(row.userId))) throw new Error();
    const unique = (values: string[]) => new Set(values).size === values.length;
    if (!unique(parsed.sessions.map(row => row.session.session_id))
      || !unique(parsed.sessions.map(row => `${row.userId}:${row.requestId}`))
      || !unique(parsed.insertions.map(row => row.insertion.insertion_id))
      || !unique(parsed.logins.map(row => row.refreshDigest))
      || !unique(parsed.replays.map(row => `${row.userId}:${row.kind}:${row.key}`))
      || parsed.accounts.some(account => !unique(account.terms.map(term => term.id)) || !unique(account.terms.map(term => term.norm)))
      || parsed.sessions.some(row => row.session.result && (row.session.result.result_id !== row.session.current_result_id
        || row.session.result.raw_text !== SIMULATION_TEXT || row.session.result.refined_text !== SIMULATION_TEXT
        || row.session.result.text_hash !== digest(SIMULATION_TEXT)))
      || parsed.insertions.some(row => !parsed.sessions.some(session => session.userId === row.userId
        && session.session.session_id === row.insertion.session_id && session.session.current_result_id === row.insertion.result_id))
      || !unique(parsed.insertions.filter(row => row.insertion.status !== 'failed').map(row => `${row.userId}:${row.insertion.result_id}`))) throw new Error();
    return parsed;
  } catch { throw new Error('SIMULATION_STATE_INVALID'); }
}

function fictionalEmail(value: string): boolean {
  return /^[^\s@]+@(?:example\.test|(?:[a-z0-9-]+\.)*invalid)$/i.test(value);
}

function termData(value: string, aliases: string[]): Pick<Term, 'term' | 'norm' | 'aliases'> {
  const clean = (text: string) => {
    const result = text.trim();
    if ([...result].length < 1 || [...result].length > 64 || /[\p{Cc}\p{Zl}\p{Zp}]/u.test(text)) throw invalid('词条或别名必须为 1–64 个字符且不含控制字符');
    return result;
  };
  const term = clean(value), norm = normalize(term);
  if (aliases.length > 10) throw invalid('别名最多 10 个');
  const normalized = new Set<string>();
  const cleaned: string[] = [];
  for (const alias of aliases) {
    const text = clean(alias), key = normalize(text);
    if (key === norm) throw invalid('别名不能与词条相同');
    if (!normalized.has(key)) { normalized.add(key); cleaned.push(text); }
  }
  return { term, norm, aliases: cleaned };
}

const normalize = (text: string) => text.normalize('NFKC').toLowerCase();
function termOut(account: Account, term: Term): Term {
  return { ...term, dictionary_version: account.dictionaryVersion,
    ambiguous_aliases: term.aliases.filter(alias => account.terms.some(other => other.id !== term.id
      && (other.norm === normalize(alias) || other.aliases.some(value => normalize(value) === normalize(alias))))) };
}

function expireContent(state: Snapshot): boolean {
  let changed = false;
  const now = Date.now();
  for (const row of state.sessions) {
    const account = state.accounts.find(value => value.user.id === row.userId)!;
    const time = row.session.first_terminal_at;
    const expiry = time && account.settings.history_retention_days !== null
      ? new Date(Date.parse(time) + account.settings.history_retention_days * 86_400_000).toISOString() : null;
    if (row.session.content_expires_at !== expiry) { row.session.content_expires_at = expiry; changed = true; }
    if (expiry && Date.parse(expiry) <= now && !row.session.content_expired) {
      row.session.content_expired = true;
      row.session.result = null;
      changed = true;
    }
  }
  return changed;
}

function page<T>(items: T[], input: PageInput, scope: unknown, version?: number): Page<T> {
  let offset = 0;
  if (input.cursor) {
    try {
      const parsed = z.object({ offset: nonnegative, scope: z.string(), version: nonnegative.optional() }).strict()
        .parse(JSON.parse(Buffer.from(input.cursor, 'base64url').toString('utf8')));
      if (parsed.scope !== requestHash(scope)) throw invalid('列表游标无效');
      if (parsed.version !== version) throw new SimulationError(409, 'DICTIONARY_CHANGED', '词库已变化，请刷新');
      offset = parsed.offset;
    } catch (error) { if (error instanceof SimulationError) throw error; throw invalid('列表游标无效'); }
  }
  const next = offset + input.limit;
  return { items: items.slice(offset, next), next_cursor: next < items.length
    ? Buffer.from(JSON.stringify({ offset: next, scope: requestHash(scope), ...(version === undefined ? {} : { version }) })).toString('base64url') : null };
}

export class SimulationStore {
  private state: Snapshot;
  private queue: Promise<void> = Promise.resolve();
  private unavailable = false;
  private readonly file: string;

  constructor(private readonly directory: string) {
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.file = join(directory, 'state.json');
    const fresh = !existsSync(this.file);
    this.state = loadSnapshot(this.file);
    const next = structuredClone(this.state);
    let changed = false;
    if (fresh) {
      const salt = randomBytes(16).toString('hex');
      next.accounts.push({ user: { id: randomUUID(), email: 'tester@example.test', display_name: 'Simulation Tester',
        role: 'user', status: 'active', locale: 'zh-CN', created_at: nowIso(), last_login_at: null }, salt,
      passwordHash: scryptSync('Simulation123!', salt, 32).toString('hex'),
      settings: { version: 1, history_retention_days: null, learning_enabled: false, locale: 'zh-CN' }, dictionaryVersion: 0, terms: [] });
    }
    for (const row of next.sessions) {
      if (!terminal.has(row.session.state)) {
        row.session.state = 'cancelled'; row.session.first_terminal_at ??= nowIso(); row.session.error_code = 'CLIENT_INTERRUPTED';
        changed = true;
      }
    }
    if (expireContent(next)) changed = true;
    if (changed || !existsSync(this.file)) { this.persist(next); this.state = next; }
  }

  private persist(next: Snapshot): void {
    const temporary = join(this.directory, `.state-${randomUUID()}.tmp`);
    let published = false;
    try {
      const descriptor = openSync(temporary, 'wx', 0o600);
      try { writeFileSync(descriptor, JSON.stringify(next)); fsyncSync(descriptor); } finally { closeSync(descriptor); }
      renameSync(temporary, this.file); published = true;
      syncPublishedFile(this.directory, this.file);
    } catch {
      if (published) this.unavailable = true;
      throw new SimulationError(503, 'SIMULATION_STORAGE_UNAVAILABLE', '模拟状态未能持久保存，本次操作未获确认');
    } finally {
      if (!published) { try { unlinkSync(temporary); } catch {} }
    }
  }

  private run<T>(write: boolean, operation: (state: Snapshot) => T): Promise<T> {
    const task = this.queue.then(() => {
      if (this.unavailable) throw new SimulationError(503, 'SIMULATION_STORAGE_UNAVAILABLE', '模拟状态保存状态不确定，请退出后重启');
      const next = structuredClone(this.state);
      const expired = expireContent(next);
      const result = operation(next);
      if (write || expired) { this.persist(next); this.state = next; }
      return structuredClone(result);
    });
    this.queue = task.then(() => undefined, () => undefined);
    return task;
  }

  private loginFor(state: Snapshot, access: string): Login {
    const hashed = digest(access), now = Date.now();
    const login = state.logins.find(value => value.refreshExpiresAt > now && value.access.some(token => token.digest === hashed && token.expiresAt > now));
    if (!login) throw new SimulationError(401, 'AUTH_INVALID', '模拟登录已过期，请重新登录');
    return login;
  }

  private withAccount<T>(access: string, write: boolean, operation: (state: Snapshot, account: Account, login: Login) => T): Promise<T> {
    return this.run(write, state => {
      const login = this.loginFor(state, access);
      return operation(state, state.accounts.find(account => account.user.id === login.userId)!, login);
    });
  }

  private sessionFor(state: Snapshot, userId: string, sessionId: string): StoredSession {
    const found = state.sessions.find(row => row.userId === userId && row.session.session_id === sessionId);
    if (!found) throw notFound();
    return found;
  }

  private replay<T extends object>(state: Snapshot, account: Account, kind: string, key: string, payload: unknown, operation: () => T): T & { replayed: boolean } {
    const hash = requestHash(payload);
    const existing = state.replays.find(row => row.userId === account.user.id && row.kind === kind && row.key === key);
    if (existing) {
      if (existing.hash !== hash) throw new SimulationError(409, 'IDEMPOTENCY_CONFLICT', '相同请求标识对应了不同内容');
      return { ...structuredClone(existing.result as T), dictionary_version: account.dictionaryVersion, replayed: true };
    }
    const result = operation();
    state.replays.push({ userId: account.user.id, kind, key, hash, result: structuredClone(result) });
    return { ...result, replayed: false };
  }

  register(email: string, password: string): Promise<User> {
    if (!fictionalEmail(email)) return Promise.reject(invalid('模拟版仅接受 example.test 或 .invalid 虚构邮箱'));
    return this.run(true, state => {
      if (state.accounts.some(account => account.user.email === email)) throw new SimulationError(409, 'EMAIL_EXISTS', '该模拟邮箱已注册');
      const salt = randomBytes(16).toString('hex');
      const user: User = { id: randomUUID(), email, display_name: null, role: 'user', status: 'active', locale: 'zh-CN',
        created_at: nowIso(), last_login_at: null };
      state.accounts.push({ user, salt, passwordHash: scryptSync(password, salt, 32).toString('hex'),
        settings: { version: 1, history_retention_days: null, learning_enabled: false, locale: 'zh-CN' }, dictionaryVersion: 0, terms: [] });
      return user;
    });
  }

  login(email: string, password: string, deviceId: string): Promise<TokenPair> {
    return this.run(true, state => {
      const account = state.accounts.find(value => value.user.email === email);
      if (!account || !timingSafeEqual(scryptSync(password, account.salt, 32), Buffer.from(account.passwordHash, 'hex'))) {
        throw new SimulationError(401, 'AUTH_INVALID', '模拟邮箱或密码错误');
      }
      const refresh = randomBytes(32).toString('base64url'), access = randomBytes(32).toString('base64url');
      const refreshExpiresAt = Date.now() + 30 * 86_400_000;
      state.logins.push({ userId: account.user.id, deviceId, refreshDigest: digest(refresh), refreshExpiresAt,
        access: [{ digest: digest(access), expiresAt: Date.now() + 900_000 }] });
      account.user.last_login_at = nowIso();
      return { access_token: access, refresh_token: refresh, token_type: 'Bearer', expires_in: 900,
        refresh_expires_at: new Date(refreshExpiresAt).toISOString(), user: account.user };
    });
  }

  refresh(refresh: string): Promise<TokenPair> {
    return this.run(true, state => {
      const login = state.logins.find(value => value.refreshDigest === digest(refresh) && value.refreshExpiresAt > Date.now());
      if (!login) throw new SimulationError(401, 'AUTH_INVALID', '模拟登录已过期');
      const access = randomBytes(32).toString('base64url');
      login.access = login.access.filter(value => value.expiresAt > Date.now());
      login.access.push({ digest: digest(access), expiresAt: Date.now() + 900_000 });
      return { access_token: access, refresh_token: refresh, token_type: 'Bearer', expires_in: 900,
        refresh_expires_at: new Date(login.refreshExpiresAt).toISOString(), user: state.accounts.find(account => account.user.id === login.userId)!.user };
    });
  }

  logout(access: string): Promise<void> {
    return this.withAccount(access, true, (state, _account, login) => {
      state.logins = state.logins.filter(value => value !== login);
      for (const row of state.sessions) {
        if (row.userId === login.userId && row.deviceId === login.deviceId && !terminal.has(row.session.state)) {
          row.session.state = 'cancelled'; row.session.error_code = 'CLIENT_INTERRUPTED'; row.session.first_terminal_at ??= nowIso();
        }
      }
    });
  }

  authenticate(access: string): Promise<{ userId: string; deviceId: string }> {
    return this.withAccount(access, false, (_state, _account, login) => ({ userId: login.userId, deviceId: login.deviceId }));
  }

  me(access: string): Promise<AuthSnapshot> {
    return this.withAccount(access, false, (_state, account) => ({ user: account.user, settings: account.settings }));
  }

  settings(access: string): Promise<Settings> { return this.withAccount(access, false, (_state, account) => account.settings); }

  patchSettings(access: string, patch: Partial<Settings> & { version: number }): Promise<Settings> {
    return this.withAccount(access, true, (state, account) => {
      if (account.settings.version !== patch.version) throw new SimulationError(409, 'VERSION_CONFLICT', '设置已变化，请刷新');
      account.settings = { ...account.settings, ...patch, version: patch.version + 1 };
      account.user.locale = account.settings.locale;
      expireContent(state);
      return account.settings;
    });
  }

  createSession(access: string, requestId: string, learningEnabled: boolean): Promise<Session> {
    return this.withAccount(access, true, (state, account, login) => {
      const hash = requestHash({ learningEnabled });
      const existing = state.sessions.find(row => row.userId === login.userId && row.requestId === requestId);
      if (existing) {
        if (existing.deviceId !== login.deviceId || existing.requestHash !== hash) throw new SimulationError(409, 'IDEMPOTENCY_CONFLICT', '会话请求标识已使用');
        return existing.session;
      }
      if (state.sessions.some(row => row.userId === login.userId && !terminal.has(row.session.state))) {
        throw new SimulationError(409, 'SESSION_BUSY', '已有进行中的模拟会话');
      }
      if (state.sessions.filter(row => !terminal.has(row.session.state)).length >= 16) {
        throw new SimulationError(429, 'CAPACITY_EXCEEDED', '本机模拟会话容量已满');
      }
      const session: Session = { session_id: randomUUID(), mode: 'dictation', state: 'created', current_attempt_no: 1,
        learning_enabled: false, current_result_id: null, audio_complete: false, received_samples: 0, audio_ms: 0,
        max_record_seconds: 180, created_at: nowIso(), first_terminal_at: null, content_expires_at: null,
        content_expired: false, error_code: null, result: null, retry_kind: 'none', retry_expires_at: null,
        retry_unavailable_reason: 'simulation_no_retry', insertion_status: null };
      state.sessions.push({ userId: login.userId, deviceId: login.deviceId, requestId, requestHash: hash, dictionaryVersion: account.dictionaryVersion, session });
      return session;
    });
  }

  getSession(access: string, id: string): Promise<Session> {
    return this.withAccount(access, false, (state, account) => this.sessionFor(state, account.user.id, id).session);
  }

  beginAudio(access: string, id: string): Promise<Session> {
    return this.withAccount(access, true, (state, _account, login) => {
      const row = this.sessionFor(state, login.userId, id);
      if (row.deviceId !== login.deviceId) throw new SimulationError(403, 'FORBIDDEN', '模拟会话属于其他设备');
      if (row.session.state !== 'created') throw new SimulationError(409, 'SESSION_STATE_CONFLICT', '模拟音频连接已建立或会话已结束');
      row.session.state = 'streaming';
      return row.session;
    });
  }

  completeAudio(access: string, id: string, samples: number, processingMs: number): Promise<Session> {
    return this.withAccount(access, true, (state, account, login) => {
      const row = this.sessionFor(state, login.userId, id);
      if (row.deviceId !== login.deviceId || row.session.state !== 'streaming') throw new SimulationError(409, 'SESSION_STATE_CONFLICT', '模拟会话不再接收音频');
      const created = nowIso();
      const result: SessionResult = { result_id: randomUUID(), attempt_no: 1, attempt_kind: 'initial', raw_text: SIMULATION_TEXT,
        refined_text: SIMULATION_TEXT, review_required: false, review_reason: null, text_hash: digest(SIMULATION_TEXT),
        dictionary_version: row.dictionaryVersion, stop_to_result_ms: processingMs, created_at: created };
      Object.assign(row.session, { state: 'ready', result, current_result_id: result.result_id, audio_complete: true,
        received_samples: samples, audio_ms: Math.floor(samples * 1000 / 16000), first_terminal_at: created });
      if (account.settings.history_retention_days !== null) row.session.content_expires_at = new Date(Date.parse(created) + account.settings.history_retention_days * 86_400_000).toISOString();
      return row.session;
    });
  }

  endSession(access: string, id: string, state: 'cancelled' | 'failed', code: string | null, samples?: number): Promise<Session> {
    return this.withAccount(access, true, (snapshot, account) => {
      const row = this.sessionFor(snapshot, account.user.id, id);
      if (terminal.has(row.session.state)) return row.session;
      row.session.state = state; row.session.error_code = code; row.session.first_terminal_at ??= nowIso();
      if (samples !== undefined) { row.session.received_samples = samples; row.session.audio_ms = Math.floor(samples * 1000 / 16000); }
      return row.session;
    });
  }

  interruptSession(userId: string, id: string, code: string, samples: number, status: 'cancelled' | 'failed' = 'cancelled'): Promise<void> {
    return this.run(true, state => {
      const row = this.sessionFor(state, userId, id);
      if (terminal.has(row.session.state)) return;
      Object.assign(row.session, { state: status, error_code: code, first_terminal_at: row.session.first_terminal_at ?? nowIso(),
        received_samples: samples, audio_ms: Math.floor(samples * 1000 / 16000) });
    });
  }

  claim(access: string, id: string, payload: Claim): Promise<Insertion> {
    return this.withAccount(access, true, (state, _account, login) => {
      const row = this.sessionFor(state, login.userId, id), hash = requestHash({ id, ...payload });
      const existing = state.insertions.find(value => value.userId === login.userId && value.insertion.insertion_id === payload.insertion_id);
      if (existing) {
        if (existing.deviceId !== login.deviceId) throw new SimulationError(403, 'FORBIDDEN', '插入资格属于其他设备');
        if (existing.requestHash !== hash) throw new SimulationError(409, 'IDEMPOTENCY_CONFLICT', '插入标识对应了不同请求');
        return { ...existing.insertion, replayed: true };
      }
      if (state.insertions.some(value => value.insertion.insertion_id === payload.insertion_id)) throw notFound();
      if (row.session.content_expired || !row.session.result) throw new SimulationError(row.session.content_expired ? 410 : 409,
        row.session.content_expired ? 'CONTENT_EXPIRED' : 'STALE_RESULT', '结果正文已过期或尚未就绪');
      if (row.session.state !== 'ready' || row.session.current_result_id !== payload.result_id) throw new SimulationError(409, 'STALE_RESULT', '结果已变化');
      if (row.session.result.text_hash !== payload.text_hash) throw new SimulationError(409, 'RESULT_HASH_MISMATCH', '结果正文摘要不匹配');
      if (row.session.result.review_required && !payload.review_confirmed) throw new SimulationError(409, 'REVIEW_REQUIRED', '结果需要确认');
      if (state.insertions.some(value => value.userId === login.userId && value.insertion.result_id === payload.result_id && value.insertion.status !== 'failed')) {
        throw new SimulationError(409, 'INSERTION_ALREADY_CLAIMED', '该结果已有插入资格，不能重复输入');
      }
      const insertion: Insertion = { replayed: false, insertion_id: payload.insertion_id, session_id: id,
        result_id: payload.result_id, status: 'reserved', observable: false, error_code: null, claimed_at: nowIso(), completed_at: null };
      state.insertions.push({ userId: login.userId, deviceId: login.deviceId, requestHash: hash, insertion });
      row.session.insertion_status = 'reserved';
      return insertion;
    });
  }

  receipt(access: string, sessionId: string, insertionId: string, payload: Receipt): Promise<Insertion> {
    return this.withAccount(access, true, (state, _account, login) => {
      const session = this.sessionFor(state, login.userId, sessionId).session;
      const row = state.insertions.find(value => value.userId === login.userId && value.insertion.session_id === sessionId && value.insertion.insertion_id === insertionId);
      if (!row) throw notFound();
      if (row.deviceId !== login.deviceId) throw new SimulationError(403, 'FORBIDDEN', '插入资格属于其他设备');
      if (row.insertion.status !== 'reserved') {
        if (row.insertion.status !== payload.status || row.insertion.observable !== payload.observable || row.insertion.error_code !== payload.error_code) {
          throw new SimulationError(409, 'INSERTION_STATE_CONFLICT', '已确认的插入回执不能变更');
        }
        return { ...row.insertion, replayed: true };
      }
      Object.assign(row.insertion, payload, { completed_at: nowIso() });
      session.insertion_status = payload.status;
      return row.insertion;
    });
  }

  getInsertion(access: string, sessionId: string, insertionId: string): Promise<Insertion> {
    return this.withAccount(access, false, (state, account) => {
      this.sessionFor(state, account.user.id, sessionId);
      const row = state.insertions.find(value => value.userId === account.user.id && value.insertion.session_id === sessionId && value.insertion.insertion_id === insertionId);
      if (!row) throw notFound();
      return row.insertion;
    });
  }

  history(access: string, input: PageInput): Promise<Page<Session>> {
    return this.withAccount(access, false, (state, account) => page(state.sessions.filter(row => row.userId === account.user.id
      && terminal.has(row.session.state)).map(row => row.session).reverse(), input, { user: account.user.id, kind: 'history' }));
  }

  dictionary(access: string, input: DictionaryInput): Promise<DictionaryPage> {
    return this.withAccount(access, false, (_state, account) => {
      const q = normalize(input.q ?? '');
      const rows = account.terms.filter(term => (input.source !== 'auto') && (!q || normalize(term.term).includes(q) || term.aliases.some(alias => normalize(alias).includes(q))))
        .sort((a, b) => a.norm.localeCompare(b.norm) || a.id.localeCompare(b.id)).map(term => termOut(account, term));
      return { ...page(rows, input, { user: account.user.id, kind: 'dictionary', q, source: input.source ?? 'all' }, account.dictionaryVersion), dictionary_version: account.dictionaryVersion };
    });
  }

  private addTerm(account: Account, data: Pick<Term, 'term' | 'norm' | 'aliases'>): Term {
    if (account.terms.some(term => term.norm === data.norm)) throw new SimulationError(409, 'TERM_EXISTS', '词条已存在');
    if (account.terms.length >= 5000) throw new SimulationError(409, 'DICTIONARY_LIMIT', '词库数量已达上限');
    account.dictionaryVersion++;
    const time = nowIso();
    const term: Term = { ...data, id: randomUUID(), contexts: [], scope: 'default', source: 'manual', version: 1,
      dictionary_version: account.dictionaryVersion, created_at: time, updated_at: time, ambiguous_aliases: [] };
    account.terms.push(term);
    return termOut(account, term);
  }

  createTerm(access: string, key: string, term: string, aliases: string[]): Promise<Term & { replayed: boolean }> {
    const data = termData(term, aliases);
    return this.withAccount(access, true, (state, account) => this.replay(state, account, 'dictionary.create', key, data, () => this.addTerm(account, data)));
  }

  updateTerm(access: string, key: string, id: string, patch: { version: number; term?: string; aliases?: string[] }): Promise<Term & { replayed: boolean }> {
    return this.withAccount(access, true, (state, account) => this.replay(state, account, 'dictionary.update', key, { id, ...patch }, () => {
      const term = account.terms.find(row => row.id === id);
      if (!term) throw notFound();
      if (term.version !== patch.version) throw new SimulationError(409, 'VERSION_CONFLICT', '词条已变化，请刷新');
      const data = termData(patch.term ?? term.term, patch.aliases ?? term.aliases);
      if (account.terms.some(row => row.id !== id && row.norm === data.norm)) throw new SimulationError(409, 'TERM_EXISTS', '词条已存在');
      account.dictionaryVersion++;
      Object.assign(term, data, { version: term.version + 1, updated_at: nowIso() });
      return termOut(account, term);
    }));
  }

  deleteTerm(access: string, key: string, id: string, version: number): Promise<{ id: string; deleted: boolean; version: number; dictionary_version: number; replayed: boolean }> {
    return this.withAccount(access, true, (state, account) => this.replay(state, account, 'dictionary.delete', key, { id, version }, () => {
      const term = account.terms.find(row => row.id === id);
      if (!term) throw notFound();
      if (term.version !== version) throw new SimulationError(409, 'VERSION_CONFLICT', '词条已变化，请刷新');
      account.terms = account.terms.filter(row => row.id !== id); account.dictionaryVersion++;
      return { id, deleted: true, version: version + 1, dictionary_version: account.dictionaryVersion };
    }));
  }

  deleteTerms(access: string, key: string, items: Array<{ id: string; version: number }>): Promise<{ deleted_ids: string[]; dictionary_version: number; replayed: boolean }> {
    return this.withAccount(access, true, (state, account) => this.replay(state, account, 'dictionary.batch-delete', key, items, () => {
      if (new Set(items.map(item => item.id)).size !== items.length) throw invalid('重复词条不能批量删除');
      for (const item of items) {
        const term = account.terms.find(row => row.id === item.id);
        if (!term || term.version !== item.version) throw new SimulationError(409, 'VERSION_CONFLICT', '所选词条已变化，整批未删除');
      }
      const ids = items.map(item => item.id);
      account.terms = account.terms.filter(term => !ids.includes(term.id)); account.dictionaryVersion++;
      return { deleted_ids: ids, dictionary_version: account.dictionaryVersion };
    }));
  }

  importCsv(access: string, key: string, fileDigest: string, records: CsvRow[]): Promise<ImportResult> {
    return this.withAccount(access, true, (state, account) => this.replay(state, account, 'dictionary.import', key, { fileDigest }, () => {
      const rows: ImportResult['rows'] = [];
      let created = 0, skipped = 0, conflicts = 0, invalidCount = 0;
      for (const record of records) {
        try {
          const data = termData(record.term, record.aliases);
          const existing = account.terms.find(term => term.norm === data.norm);
          if (existing) {
            const same = requestHash(existing.aliases.map(normalize).sort()) === requestHash(data.aliases.map(normalize).sort());
            rows.push({ row: record.row, status: same ? 'skipped' : 'conflict', term_id: existing.id });
            if (same) skipped++; else conflicts++;
          } else {
            const added = this.addTerm(account, data); created++;
            rows.push({ row: record.row, status: 'created', term_id: added.id });
          }
        } catch (error) {
          if (!(error instanceof SimulationError)) throw error;
          rows.push({ row: record.row, status: 'invalid', error_code: error.code }); invalidCount++;
        }
      }
      return { import_id: randomUUID(), created, skipped, conflicts, invalid: invalidCount, dictionary_version: account.dictionaryVersion, rows, replayed: false };
    }));
  }

  stats(access: string, dateFrom: string, dateTo: string): Promise<Stats> {
    return this.withAccount(access, false, (state, account) => {
      const rows = state.sessions.filter(row => row.userId === account.user.id && terminal.has(row.session.state)
        && row.session.created_at.slice(0, 10) >= dateFrom && row.session.created_at.slice(0, 10) <= dateTo);
      const successful = rows.filter(row => row.session.state === 'ready');
      const inserted = new Set(state.insertions.filter(row => row.userId === account.user.id && row.insertion.status === 'inserted').map(row => row.insertion.session_id));
      const chars = successful.length * [...SIMULATION_TEXT].length;
      const audio = successful.reduce((sum, row) => sum + row.session.audio_ms, 0);
      const totals: Stats['totals'] = { sessions: rows.length, received_audio_ms: rows.reduce((sum, row) => sum + row.session.audio_ms, 0),
        result_sessions: successful.length, failed_sessions: rows.filter(row => row.session.state === 'failed').length,
        empty_sessions: rows.filter(row => row.session.state === 'empty').length, cancelled_sessions: rows.filter(row => row.session.state === 'cancelled').length,
        chars, successful_audio_ms: audio, average_chars_per_minute: audio ? Math.round(chars * 60_000 / audio) : 0,
        fallback_sessions: 0, inserted_sessions: rows.filter(row => inserted.has(row.session.session_id)).length,
        processing_success_rate: rows.length ? successful.length / rows.length : 0,
        insertion_success_rate: successful.length ? rows.filter(row => inserted.has(row.session.session_id)).length / successful.length : 0,
        fallback_rate: 0, learning_events: 0 };
      const daily: Stats['daily'] = [];
      for (let at = Date.parse(`${dateFrom}T00:00:00Z`); at <= Date.parse(`${dateTo}T00:00:00Z`); at += 86_400_000) {
        const date = new Date(at).toISOString().slice(0, 10), dayRows = rows.filter(row => row.session.created_at.slice(0, 10) === date);
        const good = dayRows.filter(row => row.session.state === 'ready');
        daily.push({ date, sessions: dayRows.length, chars: good.length * [...SIMULATION_TEXT].length,
          successful_audio_ms: good.reduce((sum, row) => sum + row.session.audio_ms, 0), heat_level: dayRows.length ? Math.min(dayRows.length, 4) : 0 });
      }
      let streak = 0, longest = 0;
      for (const day of daily) { streak = day.sessions ? streak + 1 : 0; longest = Math.max(longest, streak); }
      return { timezone: 'UTC', date_from: dateFrom, date_to: dateTo, totals, daily,
        active_days_in_range: daily.filter(day => day.sessions > 0).length, current_streak_days: streak, longest_streak_days: longest,
        dictionary_terms: account.terms.length, cumulative_learning_events: 0 };
    });
  }

  async close(): Promise<void> { await this.queue; this.unavailable = true; }
}
