import { randomBytes } from 'node:crypto';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import type { Duplex } from 'node:stream';
import { WebSocket, WebSocketServer } from 'ws';
import { z } from 'zod';
import type { ServiceEndpoint } from '../core/service-endpoint';
import { multipartCsv, parseCsv } from './csv';
import { SIMULATION_MAX_SAMPLES, SimulationError, SimulationStore } from './store';

const MAX_BODY_BYTES = 1024 * 1024 + 8192;
const uuid = z.string().uuid();
const version = z.number().int().positive();
const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(value => {
  const parsed = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value;
});
const term = z.string().min(1).max(256);
const aliases = z.array(term).max(10);
const pageSchema = z.object({ cursor: z.string().min(1).max(1024).optional(), limit: z.coerce.number().int().min(1).max(200).default(50) }).strict();
const credentials = z.object({ email: z.string().email().max(254).transform(value => value.trim().toLowerCase()), password: z.string().min(8).max(128) }).strict();
const loginSchema = credentials.extend({ password: z.string().min(1).max(128), client_type: z.literal('desktop'), device: z.object({ installation_id: uuid,
  name: z.string().min(1).max(64), platform: z.enum(['macos', 'windows']), client_version: z.string().min(1).max(32) }).strict() }).strict();
const receiptSchema = z.object({ status: z.enum(['inserted', 'failed', 'unverified']), observable: z.boolean(),
  error_code: z.enum(['TARGET_CHANGED', 'TARGET_UNSUPPORTED', 'PERMISSION_DENIED', 'ZERO_WRITE', 'VERIFY_FAILED', 'CLIENT_INTERRUPTED']).nullable() }).strict()
  .refine(value => value.status === 'inserted' ? value.error_code === null : value.error_code !== null)
  .refine(value => !value.observable || value.status === 'inserted');

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  const parsed = schema.safeParse(value);
  if (!parsed.success) throw new SimulationError(422, 'VALIDATION_ERROR', '模拟请求参数无效');
  return parsed.data;
}

function query<T>(schema: z.ZodType<T>, url: URL): T {
  const keys = [...url.searchParams.keys()];
  if (new Set(keys).size !== keys.length) throw new SimulationError(422, 'VALIDATION_ERROR', '查询参数不能重复');
  return parse(schema, Object.fromEntries(url.searchParams));
}

async function readBody(request: IncomingMessage): Promise<Buffer> {
  if (Number(request.headers['content-length'] ?? 0) > MAX_BODY_BYTES) {
    request.resume(); throw new SimulationError(413, 'REQUEST_TOO_LARGE', '模拟请求体超过限制');
  }
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let length = 0, settled = false;
    request.on('data', (part: Buffer) => {
      if (settled) return;
      length += part.length;
      if (length > MAX_BODY_BYTES) {
        settled = true; chunks.length = 0; reject(new SimulationError(413, 'REQUEST_TOO_LARGE', '模拟请求体超过限制'));
      } else chunks.push(part);
    });
    request.on('end', () => { if (!settled) { settled = true; resolve(Buffer.concat(chunks)); } });
    request.on('error', () => { if (!settled) { settled = true; reject(new SimulationError(400, 'REQUEST_INTERRUPTED', '模拟请求已中断')); } });
    request.on('aborted', () => { if (!settled) { settled = true; reject(new SimulationError(400, 'REQUEST_INTERRUPTED', '模拟请求已中断')); } });
  });
}

async function jsonBody<T>(request: IncomingMessage, schema: z.ZodType<T>): Promise<T> {
  if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(request.headers['content-type'] ?? '')) {
    throw new SimulationError(415, 'VALIDATION_ERROR', '模拟请求必须为 JSON');
  }
  const bytes = await readBody(request);
  let value: unknown;
  try { value = JSON.parse(bytes.toString('utf8')); } catch { throw new SimulationError(422, 'VALIDATION_ERROR', '模拟 JSON 格式无效'); }
  return parse(schema, value);
}

function send(response: ServerResponse, status: number, body?: unknown): void {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    ...(status === 204 ? {} : { 'Content-Type': 'application/json; charset=utf-8' }),
    ...(status === 413 ? { Connection: 'close' } : {}) });
  response.end(status === 204 ? undefined : JSON.stringify(body));
}

function errorBody(error: unknown): { status: number; body: unknown } {
  const known = error instanceof SimulationError ? error : new SimulationError(500, 'SIMULATION_INTERNAL_ERROR', '本机模拟服务操作失败');
  return { status: known.status, body: { error: { code: known.code, message: known.message, retryable: false, details: known.details } } };
}

function accessToken(request: IncomingMessage): string {
  const match = /^Bearer ([A-Za-z0-9_-]{16,256})$/.exec(request.headers.authorization ?? '');
  if (!match) throw new SimulationError(401, 'AUTH_INVALID', '请先登录本机模拟账号');
  return match[1];
}

interface AudioConnection {
  sessionId: string;
  userId: string;
  socket: WebSocket;
  samples: number;
  lastSequence: number;
  shortFrame: boolean;
  phase: 'streaming' | 'finalizing' | 'ended';
  pending: Promise<void>;
  idleTimer: ReturnType<typeof setTimeout> | null;
}

function declaredRoute(method: string | undefined, path: string): boolean {
  const exact: Record<string, string[]> = {
    GET: ['/api/v1/capabilities', '/api/users/me', '/api/v1/settings', '/api/v1/history', '/api/v1/stats/me', '/api/v1/dictionary/terms'],
    POST: ['/api/auth/register', '/api/auth/login', '/api/auth/refresh', '/api/auth/logout', '/api/v1/sessions',
      '/api/v1/dictionary/terms', '/api/v1/dictionary/terms/batch-delete', '/api/v1/dictionary/import', '/api/v1/corrections'],
    PATCH: ['/api/v1/settings'],
  };
  if (exact[method ?? '']?.includes(path)) return true;
  if (method === 'GET' && /^\/api\/v1\/sessions\/[^/]+(?:\/insertions\/[^/]+)?$/.test(path)) return true;
  if (method === 'POST' && /^\/api\/v1\/sessions\/[^/]+\/(?:cancel|retry|insertions\/claim|insertions\/[^/]+\/receipt)$/.test(path)) return true;
  if (['PATCH', 'DELETE'].includes(method ?? '') && /^\/api\/v1\/dictionary\/terms\/[^/]+$/.test(path)) return true;
  return (method === 'GET' || method === 'POST') && /^\/api\/v1\/learning-events\/[^/]+(?:\/(?:confirm|undo))?$/.test(path);
}

export async function startSimulationHost(options: { directory: string; storageIdentity: string }): Promise<{ endpoint: ServiceEndpoint; close(): Promise<void> }> {
  const store = new SimulationStore(options.directory);
  const nonce = randomBytes(32).toString('base64url');
  const websocket = new WebSocketServer({ noServer: true, maxPayload: 8008, perMessageDeflate: false });
  const connections = new Map<string, AudioConnection>();
  let origin = '', host = '', closing = false;

  const guard = (request: IncomingMessage): URL => {
    if (closing) throw new SimulationError(503, 'SIMULATION_CLOSING', '本机模拟服务正在退出');
    if (request.socket.remoteAddress !== '127.0.0.1' || request.headers.host !== host || request.headers.origin !== undefined
      || request.headers['x-smartvoice-simulation'] !== nonce || request.headers['sec-fetch-site'] === 'cross-site') {
      throw new SimulationError(403, 'SIMULATION_FORBIDDEN', '拒绝非本机专用模拟连接');
    }
    const url = new URL(request.url ?? '', origin);
    if (url.origin !== origin || !request.url?.startsWith('/')) throw new SimulationError(400, 'VALIDATION_ERROR', '模拟请求地址无效');
    return url;
  };
  const stopConnection = (sessionId: string, userId: string) => {
    const connection = connections.get(sessionId);
    if (connection && connection.userId === userId) {
      connection.phase = 'ended';
      if (connection.idleTimer) clearTimeout(connection.idleTimer);
      connection.idleTimer = null;
      if (connection.socket.readyState === WebSocket.OPEN) {
        connection.socket.send(JSON.stringify({ type: 'cancelled', session_id: sessionId, attempt_no: 1 }));
        connection.socket.close(1000);
      }
    }
  };
  const route = async (request: IncomingMessage, response: ServerResponse) => {
    const url = guard(request), path = url.pathname, method = request.method;
    if (!declaredRoute(method, path)) throw new SimulationError(404, 'NOT_FOUND', '本机模拟版没有此接口');
    const hasQueryContract = method === 'GET' && ['/api/v1/history', '/api/v1/stats/me', '/api/v1/dictionary/terms'].includes(path)
      || method === 'DELETE' && /^\/api\/v1\/dictionary\/terms\/[^/]+$/.test(path);
    if (url.search && !hasQueryContract) throw new SimulationError(422, 'VALIDATION_ERROR', '此模拟接口不接受查询参数');
    if (method === 'GET' && path === '/api/v1/capabilities') {
      query(z.object({}).strict(), url);
      send(response, 200, { api_version: 'v1', ready: true, degraded: false, unavailable_reason: null,
        asr: { provider: 'simulation', model_id: 'simulation-fixed-text', stable_partial: false },
        llm: { available: false, provider: 'simulation', model_id: 'simulation-fixed-text' },
        limits: { global_sessions: 16, active_sessions_per_user: 1 } }); return;
    }
    if (method === 'POST' && path === '/api/auth/register') {
      const body = await jsonBody(request, credentials);
      send(response, 201, await store.register(body.email, body.password)); return;
    }
    if (method === 'POST' && path === '/api/auth/login') {
      const body = await jsonBody(request, loginSchema);
      send(response, 200, await store.login(body.email, body.password, body.device.installation_id)); return;
    }
    if (method === 'POST' && path === '/api/auth/refresh') {
      const body = await jsonBody(request, z.object({ refresh_token: z.string().min(16).max(256) }).strict());
      send(response, 200, await store.refresh(body.refresh_token)); return;
    }
    const access = accessToken(request);
    if (method === 'POST' && path === '/api/auth/logout') {
      await jsonBody(request, z.object({}).strict());
      const identity = await store.authenticate(access);
      await store.logout(access);
      for (const connection of connections.values()) if (connection.userId === identity.userId) stopConnection(connection.sessionId, identity.userId);
      send(response, 204); return;
    }
    if (method === 'GET' && path === '/api/users/me') { send(response, 200, await store.me(access)); return; }
    if (method === 'GET' && path === '/api/v1/settings') { send(response, 200, await store.settings(access)); return; }
    if (method === 'PATCH' && path === '/api/v1/settings') {
      const body = await jsonBody(request, z.object({ version,
        history_retention_days: z.union([z.literal(7), z.literal(30), z.literal(90)]).nullable().optional(),
        learning_enabled: z.boolean().optional(), locale: z.enum(['zh-CN', 'en']).optional() }).strict());
      send(response, 200, await store.patchSettings(access, body)); return;
    }
    if (method === 'POST' && path === '/api/v1/sessions') {
      const body = await jsonBody(request, z.object({ client_request_id: uuid, mode: z.literal('dictation'), learning_enabled: z.boolean() }).strict());
      if (parse(uuid, request.headers['idempotency-key']) !== body.client_request_id) throw new SimulationError(409, 'IDEMPOTENCY_CONFLICT', '会话请求标识不一致');
      send(response, 201, await store.createSession(access, body.client_request_id, body.learning_enabled)); return;
    }
    const sessionRoute = /^\/api\/v1\/sessions\/([^/]+)(?:\/(.*))?$/.exec(path);
    if (sessionRoute) {
      const id = parse(uuid, sessionRoute[1]), action = sessionRoute[2];
      if (method === 'GET' && !action) { send(response, 200, await store.getSession(access, id)); return; }
      if (method === 'POST' && action === 'cancel') {
        await jsonBody(request, z.object({}).strict());
        const identity = await store.authenticate(access);
        const current = connections.get(id);
        const session = await store.endSession(access, id, 'cancelled', null, current?.userId === identity.userId ? current.samples : undefined);
        stopConnection(id, identity.userId); send(response, 200, session); return;
      }
      if (method === 'POST' && action === 'retry') {
        await jsonBody(request, z.object({ retry_kind: z.enum(['asr', 'refine']) }).strict());
        await store.getSession(access, id);
        throw new SimulationError(409, 'RETRY_UNAVAILABLE', '本机模拟版不运行真实识别或整理重试');
      }
      if (method === 'POST' && action === 'insertions/claim') {
        const body = await jsonBody(request, z.object({ result_id: uuid, insertion_id: uuid, text_hash: z.string().regex(/^[0-9a-f]{64}$/), review_confirmed: z.boolean() }).strict());
        const insertion = await store.claim(access, id, body); send(response, insertion.replayed ? 200 : 201, insertion); return;
      }
      const insertionRoute = /^insertions\/([^/]+)(\/receipt)?$/.exec(action ?? '');
      if (insertionRoute) {
        const insertionId = parse(uuid, insertionRoute[1]);
        if (method === 'GET' && !insertionRoute[2]) { send(response, 200, await store.getInsertion(access, id, insertionId)); return; }
        if (method === 'POST' && insertionRoute[2]) { send(response, 200, await store.receipt(access, id, insertionId, await jsonBody(request, receiptSchema))); return; }
      }
    }
    if (method === 'GET' && path === '/api/v1/history') { send(response, 200, await store.history(access, query(pageSchema, url))); return; }
    if (method === 'GET' && path === '/api/v1/stats/me') {
      const dates = query(z.object({ date_from: date, date_to: date }).strict(), url);
      const days = (Date.parse(dates.date_to) - Date.parse(dates.date_from)) / 86_400_000;
      if (days < 0 || days > 3660) throw new SimulationError(422, 'VALIDATION_ERROR', '统计日期范围无效');
      send(response, 200, await store.stats(access, dates.date_from, dates.date_to)); return;
    }
    if (method === 'GET' && path === '/api/v1/dictionary/terms') {
      const input = query(pageSchema.extend({ q: z.string().max(256).optional(), source: z.enum(['all', 'manual', 'auto']).optional() }).strict(), url);
      send(response, 200, await store.dictionary(access, input)); return;
    }
    const key = () => parse(uuid, request.headers['idempotency-key']);
    if (method === 'POST' && path === '/api/v1/dictionary/terms') {
      const body = await jsonBody(request, z.object({ term, aliases, scope: z.literal('default') }).strict());
      const result = await store.createTerm(access, key(), body.term, body.aliases); send(response, result.replayed ? 200 : 201, result); return;
    }
    if (method === 'POST' && path === '/api/v1/dictionary/terms/batch-delete') {
      const body = await jsonBody(request, z.object({ items: z.array(z.object({ id: uuid, version }).strict()).min(1).max(200) }).strict());
      send(response, 200, await store.deleteTerms(access, key(), body.items)); return;
    }
    const termRoute = /^\/api\/v1\/dictionary\/terms\/([^/]+)$/.exec(path);
    if (termRoute) {
      const id = parse(uuid, termRoute[1]);
      if (method === 'PATCH') {
        const body = await jsonBody(request, z.object({ version, term: term.optional(), aliases: aliases.optional() }).strict());
        send(response, 200, await store.updateTerm(access, key(), id, body)); return;
      }
      if (method === 'DELETE') {
        const input = query(z.object({ version: z.coerce.number().int().positive() }).strict(), url);
        send(response, 200, await store.deleteTerm(access, key(), id, input.version)); return;
      }
    }
    if (method === 'POST' && path === '/api/v1/dictionary/import') {
      await store.authenticate(access);
      const file = multipartCsv(await readBody(request), request.headers['content-type'] ?? '');
      const records = parseCsv(file);
      send(response, 200, await store.importCsv(access, key(), records.digest, records.rows)); return;
    }
    await store.authenticate(access);
    if (path === '/api/v1/corrections' || path.startsWith('/api/v1/learning-events/')) {
      throw new SimulationError(501, 'SIMULATION_UNSUPPORTED', '本机模拟版不执行自动学习');
    }
    throw new SimulationError(404, 'NOT_FOUND', '本机模拟版没有此接口');
  };

  const server = createServer((request, response) => {
    void route(request, response).catch(error => { const result = errorBody(error); send(response, result.status, result.body); });
  });
  server.requestTimeout = 15_000; server.headersTimeout = 10_000; server.keepAliveTimeout = 1000; server.maxHeadersCount = 32;
  server.on('clientError', (_error, socket) => { if (!socket.destroyed) socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'); });

  const wire = (connection: AudioConnection, value: unknown) => {
    if (connection.socket.readyState === WebSocket.OPEN) connection.socket.send(JSON.stringify(value));
  };
  const failAudio = async (connection: AudioConnection, code: string) => {
    if (connection.phase === 'ended') return;
    connection.phase = 'ended';
    if (connection.idleTimer) clearTimeout(connection.idleTimer); connection.idleTimer = null;
    try { await store.interruptSession(connection.userId, connection.sessionId, code, connection.samples, 'failed'); }
    catch { code = 'SIMULATION_STORAGE_UNAVAILABLE'; }
    wire(connection, { type: 'error', session_id: connection.sessionId, attempt_no: 1,
      error: { code, message: '本机模拟音频会话已停止', retryable: false } });
    connection.socket.close(1008);
  };
  const armIdle = (connection: AudioConnection) => {
    if (connection.idleTimer) clearTimeout(connection.idleTimer);
    connection.idleTimer = setTimeout(() => { connection.pending = connection.pending.then(() => failAudio(connection, 'AUDIO_INTERRUPTED')); }, 15_000);
  };
  const acceptAudio = (socket: WebSocket, access: string, sessionId: string, userId: string) => {
    const connection: AudioConnection = { socket, sessionId, userId, samples: 0, lastSequence: -1, shortFrame: false,
      phase: 'streaming', pending: Promise.resolve(), idleTimer: null };
    connections.set(sessionId, connection);
    socket.on('error', () => undefined);
    wire(connection, { type: 'ready', session_id: sessionId, attempt_no: 1,
      audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1, chunk_samples: 4000, max_samples: SIMULATION_MAX_SAMPLES } });
    armIdle(connection);
    socket.on('message', (data, binary) => {
      connection.pending = connection.pending.then(async () => {
        if (connection.phase !== 'streaming') return;
        await store.authenticate(access);
        if (connection.phase !== 'streaming' || closing) return;
        const bytes = Array.isArray(data) ? Buffer.concat(data) : Buffer.isBuffer(data) ? data : Buffer.from(data);
        if (binary) {
          if (bytes.length < 10 || bytes.length > 8008) throw new SimulationError(422, 'AUDIO_PROTOCOL_ERROR', '模拟帧长度错误');
          const sequence = bytes.readUInt32LE(0), samples = bytes.readUInt32LE(4);
          if (sequence !== connection.lastSequence + 1 || samples < 1 || samples > 4000 || bytes.length !== samples * 2 + 8
            || connection.shortFrame || connection.samples + samples > SIMULATION_MAX_SAMPLES || bytes.subarray(8).some(value => value !== 0)) {
            throw new SimulationError(422, 'AUDIO_PROTOCOL_ERROR', '模拟帧身份或计数错误');
          }
          connection.lastSequence = sequence; connection.samples += samples; connection.shortFrame = samples < 4000;
          wire(connection, { type: 'audio.ack', session_id: sessionId, attempt_no: 1, seq: sequence, total_samples: connection.samples });
          armIdle(connection); return;
        }
        let value: unknown;
        try { value = JSON.parse(bytes.toString('utf8')); } catch { throw new SimulationError(422, 'AUDIO_PROTOCOL_ERROR', '模拟控制消息格式错误'); }
        const control = parse(z.discriminatedUnion('type', [
          z.object({ type: z.literal('audio.end'), last_seq: z.number().int().min(-1), total_samples: z.number().int().nonnegative() }).strict(),
          z.object({ type: z.literal('cancel') }).strict(),
        ]), value);
        if (control.type === 'cancel') {
          await store.endSession(access, sessionId, 'cancelled', null, connection.samples);
          stopConnection(sessionId, userId); return;
        }
        if (control.last_seq !== connection.lastSequence || control.total_samples !== connection.samples || connection.samples < 1) {
          throw new SimulationError(422, 'AUDIO_PROTOCOL_ERROR', '模拟尾帧计数不匹配');
        }
        connection.phase = 'finalizing';
        const stillFinalizing = () => connection.phase === 'finalizing';
        if (connection.idleTimer) clearTimeout(connection.idleTimer); connection.idleTimer = null;
        const started = Date.now();
        const completed = await store.completeAudio(access, sessionId, connection.samples, Math.max(0, Date.now() - started));
        if (!stillFinalizing() || closing) return;
        connection.phase = 'ended';
        wire(connection, { type: 'audio.end_ack', session_id: sessionId, attempt_no: 1, last_seq: connection.lastSequence, total_samples: connection.samples });
        wire(connection, { type: 'asr.final', session_id: sessionId, attempt_no: 1, text: completed.result!.raw_text });
        wire(connection, { type: 'result.ready', session_id: sessionId, attempt_no: 1, result: completed.result });
      }).catch(error => failAudio(connection, error instanceof SimulationError ? error.code : 'AUDIO_PROTOCOL_ERROR'));
    });
    socket.on('close', () => {
      if (connection.idleTimer) clearTimeout(connection.idleTimer); connection.idleTimer = null;
      if (connections.get(sessionId) === connection) connections.delete(sessionId);
      connection.pending = connection.pending.then(async () => {
        if (connection.phase !== 'ended') { connection.phase = 'ended'; await store.interruptSession(userId, sessionId, 'AUDIO_INTERRUPTED', connection.samples); }
      }).catch(() => undefined);
    });
  };

  const rejectUpgrade = (socket: Duplex, error: unknown) => {
    const result = errorBody(error);
    if (!socket.destroyed) socket.end(`HTTP/1.1 ${result.status} Rejected\r\nConnection: close\r\nContent-Type: application/json\r\n\r\n${JSON.stringify(result.body)}`);
  };
  server.on('upgrade', (request, socket, head) => {
    socket.on('error', () => undefined);
    void (async () => {
      const url = guard(request), match = /^\/ws\/v1\/sessions\/([^/]+)\/audio$/.exec(url.pathname);
      if (!match || url.search) throw new SimulationError(404, 'NOT_FOUND', '模拟音频接口不存在');
      if (request.method !== 'GET' || request.headers.upgrade?.toLowerCase() !== 'websocket'
        || request.headers['sec-websocket-version'] !== '13'
        || !/^[A-Za-z0-9+/]{22}==$/.test(String(request.headers['sec-websocket-key'] ?? ''))
        || request.headers['sec-websocket-protocol'] !== undefined) throw new SimulationError(400, 'AUDIO_PROTOCOL_ERROR', '模拟音频握手无效');
      const sessionId = parse(uuid, match[1]), access = accessToken(request);
      const identity = await store.authenticate(access);
      await store.beginAudio(access, sessionId);
      if (closing || socket.destroyed) { await store.interruptSession(identity.userId, sessionId, 'CLIENT_INTERRUPTED', 0); return; }
      websocket.handleUpgrade(request, socket, head, accepted => acceptAudio(accepted, access, sessionId, identity.userId));
    })().catch(error => rejectUpgrade(socket, error));
  });

  try {
    await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', () => { server.off('error', reject); resolve(); }); });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('SIMULATION_BIND_FAILED');
    host = `127.0.0.1:${address.port}`; origin = `http://${host}`;
  } catch (error) { await store.close(); websocket.close(); throw error; }

  let closePromise: Promise<void> | null = null;
  return {
    endpoint: Object.freeze({ origin, storageIdentity: options.storageIdentity, headers: Object.freeze({ 'X-SmartVoice-Simulation': nonce }) }),
    close: () => {
      if (closePromise) return closePromise;
      closing = true;
      closePromise = (async () => {
        const owned = [...connections.values()];
        for (const connection of owned) {
          if (connection.idleTimer) clearTimeout(connection.idleTimer); connection.idleTimer = null;
          connection.socket.terminate();
        }
        const interruptions = await Promise.allSettled(owned.map(async connection => {
          await connection.pending;
          await store.interruptSession(connection.userId, connection.sessionId, 'CLIENT_INTERRUPTED', connection.samples);
        }));
        await new Promise<void>(resolve => websocket.close(() => resolve()));
        await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); });
        await store.close();
        const failed = interruptions.find(value => value.status === 'rejected');
        if (failed?.status === 'rejected') throw failed.reason;
      })();
      return closePromise;
    },
  };
}
