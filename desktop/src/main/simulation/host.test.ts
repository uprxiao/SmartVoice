import { randomUUID, createHash } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { request as httpRequest } from 'node:http';
import { WebSocket } from 'ws';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, type DeviceIdentity } from '../core/api-client';
import { AuthService } from '../core/auth-service';
import { InsertionJournal } from '../core/insertion-journal';
import type { NativeVoiceInput } from '../core/native-input';
import { SessionService } from '../core/session-service';
import { TokenStore } from '../core/token-store';
import { startSimulationHost } from './host';

vi.mock('electron', () => ({ clipboard: { writeText: vi.fn() }, app: { getPath: () => '' }, safeStorage: {} }));

const TEXT = '这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。';
type Host = Awaited<ReturnType<typeof startSimulationHost>>;
const hosts: Host[] = [], directories: string[] = [], sockets: WebSocket[] = [], services: SessionService[] = [];
const folder = () => { const path = mkdtempSync(join(tmpdir(), 'svi-simulation-host-')); directories.push(path); return path; };
const device: DeviceIdentity = { installation_id: randomUUID(), name: 'Fictional Test Desktop', platform: 'windows', client_version: '0.0.2' };
async function start(directory = folder()) {
  const host = await startSimulationHost({ directory, storageIdentity: 'smartvoiceinput:windows-simulation:v1' });
  hosts.push(host); return host;
}
const client = (host: Host) => new ApiClient({ set: () => undefined, clear: () => undefined }, device, () => undefined, host.endpoint);
const signIn = async (host: Host) => { const api = client(host); await api.login('tester@example.test', 'Simulation123!'); return api; };
async function request(host: Host, path: string, init: RequestInit = {}) {
  return fetch(`${host.endpoint.origin}${path}`, { ...init, headers: { ...host.endpoint.headers, ...init.headers } });
}
interface Message { type: string; [key: string]: unknown }
function mailbox(socket: WebSocket) {
  const queue: Message[] = [];
  const waiters: Array<{ resolve: (message: Message) => void; reject: (reason: Error) => void; timer: ReturnType<typeof setTimeout> }> = [];
  socket.on('message', bytes => {
    const value = JSON.parse(bytes.toString()) as Message;
    const waiter = waiters.shift();
    if (waiter) { clearTimeout(waiter.timer); waiter.resolve(value); } else queue.push(value);
  });
  return {
    next: (): Promise<Message> => {
      const message = queue.shift();
      if (message) return Promise.resolve(message);
      return new Promise((resolve, reject) => {
        const waiter = { resolve, reject, timer: setTimeout(() => {
          const index = waiters.indexOf(waiter); if (index >= 0) waiters.splice(index, 1);
          reject(new Error('Simulation protocol message timeout'));
        }, 3000) };
        waiters.push(waiter);
      });
    },
    queued: () => [...queue],
  };
}
async function openAudio(api: ApiClient, sessionId: string) {
  const endpoint = api.audioSocket(`/ws/v1/sessions/${sessionId}/audio`);
  const socket = new WebSocket(endpoint.url, { headers: endpoint.headers }); sockets.push(socket);
  const messages = mailbox(socket);
  const ready = await messages.next();
  expect(ready).toMatchObject({ type: 'ready', session_id: sessionId, attempt_no: 1,
    audio: { encoding: 'pcm_s16le', sample_rate_hz: 16000, channels: 1, chunk_samples: 4000, max_samples: 2_880_000 } });
  return { socket, messages };
}
const frame = (sequence: number, samples = 4000) => {
  const bytes = Buffer.alloc(samples * 2 + 8); bytes.writeUInt32LE(sequence, 0); bytes.writeUInt32LE(samples, 4); return bytes;
};
afterEach(async () => {
  for (const service of services.splice(0)) service.dispose();
  for (const socket of sockets.splice(0)) socket.terminate();
  for (const host of hosts.splice(0)) await host.close();
  for (const path of directories.splice(0)) rmSync(path, { recursive: true, force: true });
});

describe('Main-owned simulation HTTP and WS protocol', () => {
  it('uses the real ApiClient for auth, settings, dictionary mutations, CSV and explicit unknown-route errors', async () => {
    const host = await start(), api = await signIn(host);
    const me = await api.me();
    expect(me.user.email).toBe('tester@example.test');
    expect(await api.capabilities()).toMatchObject({ ready: true, asr: { provider: 'simulation', model_id: 'simulation-fixed-text' }, llm: { available: false } });
    await api.patchSettings({ version: 1, locale: 'en' });
    expect((await api.getSettings()).locale).toBe('en');
    const term = await api.createTerm('React', ['React.js'], randomUUID());
    const changed = await api.updateTerm(term.id, { version: term.version, aliases: ['ReactJS'] }, randomUUID());
    expect(changed.version).toBe(2);
    const key = randomUUID(), csv = Buffer.from('term,aliases\r\nDocker,容器\r\nTypeScript,TS\r\nDocker,容器\r\n');
    expect(await api.importCsv('fictional.csv', csv, key)).toMatchObject({ created: 2, skipped: 1, conflicts: 0, invalid: 0, replayed: false });
    expect(await api.importCsv('fictional.csv', csv, key)).toMatchObject({ created: 2, skipped: 1, replayed: true });
    expect((await api.dictionary({})).items.map(row => row.term).sort()).toEqual(['Docker', 'React', 'TypeScript']);
    await api.deleteTerm(term.id, changed.version, randomUUID());
    const remaining = await api.dictionary({});
    await api.deleteTerms(remaining.items.map(row => ({ id: row.id, version: row.version })), randomUUID());
    expect((await api.dictionary({})).items).toEqual([]);
    await expect(api.request('POST', '/api/v1/unknown', {})).rejects.toMatchObject({ status: 404, code: 'NOT_FOUND' });
    await api.logout();
    await expect(api.me()).rejects.toMatchObject({ status: 401, code: 'AUTH_INVALID' });
  });

  it('validates owner nonce, Host, Origin, size and unknown schemas before any side effect', async () => {
    const host = await start(), api = await signIn(host);
    const path = '/api/v1/capabilities';
    expect((await fetch(`${host.endpoint.origin}${path}`)).status).toBe(403);
    const wrongHost = await new Promise<number>((resolve, reject) => {
      const probe = httpRequest(`${host.endpoint.origin}${path}`, { headers: { ...host.endpoint.headers, Host: 'localhost:1' } }, response => {
        response.resume(); resolve(response.statusCode!);
      });
      probe.once('error', reject); probe.end();
    });
    expect(wrongHost).toBe(403);
    expect((await request(host, path, { headers: { Origin: 'https://example.test' } })).status).toBe(403);
    const oversized = await request(host, '/api/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: ' '.repeat(1024 * 1024 + 8193) });
    expect(oversized.status).toBe(413);
    expect(await oversized.json()).toMatchObject({ error: { code: 'REQUEST_TOO_LARGE' } });
    await expect(api.request('POST', '/api/v1/dictionary/terms', { term: 'React', aliases: [], scope: 'default', unknown: true }, { 'Idempotency-Key': randomUUID() }))
      .rejects.toMatchObject({ status: 422, code: 'VALIDATION_ERROR' });
    await expect(api.register('real@public.example', 'Simulation123!')).rejects.toMatchObject({ code: 'VALIDATION_ERROR' });
    expect((await api.dictionary({})).items).toEqual([]);
    const created = await api.createSession(randomUUID(), false);
    const socket = new WebSocket(host.endpoint.origin.replace(/^http/, 'ws') + `/ws/v1/sessions/${created.session_id}/audio`, {
      headers: { Authorization: `Bearer ${api.accessTokenValue}` },
    }); sockets.push(socket);
    const rejection = await new Promise<number>((resolve, reject) => {
      socket.once('unexpected-response', (_request, response) => { response.resume(); resolve(response.statusCode!); socket.terminate(); });
      socket.once('open', () => reject(new Error('Missing nonce opened a websocket')));
      socket.on('error', () => undefined);
    });
    expect(rejection).toBe(403);
    expect((await api.getSession(created.session_id)).state).toBe('created');
  });

  it('acknowledges actual frame totals and emits the fixed hashed result only after a valid end', async () => {
    const host = await start(), api = await signIn(host), created = await api.createSession(randomUUID(), false);
    const { socket, messages } = await openAudio(api, created.session_id);
    socket.send(frame(0));
    expect(await messages.next()).toMatchObject({ type: 'audio.ack', seq: 0, total_samples: 4000 });
    socket.send(frame(1, 17));
    expect(await messages.next()).toMatchObject({ type: 'audio.ack', seq: 1, total_samples: 4017 });
    socket.send(JSON.stringify({ type: 'audio.end', last_seq: 1, total_samples: 4017 }));
    expect(await messages.next()).toMatchObject({ type: 'audio.end_ack', last_seq: 1, total_samples: 4017 });
    expect(await messages.next()).toMatchObject({ type: 'asr.final', text: TEXT });
    const result = await messages.next();
    expect(result).toMatchObject({ type: 'result.ready', session_id: created.session_id, attempt_no: 1,
      result: { refined_text: TEXT, raw_text: TEXT, text_hash: createHash('sha256').update(TEXT).digest('hex'), attempt_no: 1, review_required: false } });
    const authoritative = await api.getSession(created.session_id);
    expect(authoritative).toMatchObject({ state: 'ready', received_samples: 4017, audio_complete: true, result: result.result });
    const claim = { result_id: authoritative.result!.result_id, insertion_id: randomUUID(), text_hash: authoritative.result!.text_hash, review_confirmed: false };
    expect(await api.claimInsertion(created.session_id, claim)).toMatchObject({ replayed: false, status: 'reserved' });
    expect(await api.claimInsertion(created.session_id, claim)).toMatchObject({ replayed: true, status: 'reserved' });
    await api.insertionReceipt(created.session_id, claim.insertion_id, { status: 'unverified', observable: false, error_code: 'VERIFY_FAILED' });
    expect((await api.history()).items[0].insertion_status).toBe('unverified');
    const date = new Date().toISOString().slice(0, 10);
    expect((await api.stats(date, date)).totals.inserted_sessions).toBe(0);
  });

  it.each(['empty', 'wrong-sequence', 'wrong-end', 'nonzero', 'after-short'] as const)('rejects %s audio without publishing a result', async scenario => {
    const host = await start(), api = await signIn(host), session = await api.createSession(randomUUID(), false);
    const { socket, messages } = await openAudio(api, session.session_id);
    if (scenario === 'empty') socket.send(JSON.stringify({ type: 'audio.end', last_seq: -1, total_samples: 0 }));
    if (scenario === 'wrong-sequence') socket.send(frame(1));
    if (scenario === 'wrong-end') {
      socket.send(frame(0)); await messages.next();
      socket.send(JSON.stringify({ type: 'audio.end', last_seq: 0, total_samples: 3999 }));
    }
    if (scenario === 'nonzero') { const bytes = frame(0); bytes[9] = 1; socket.send(bytes); }
    if (scenario === 'after-short') { socket.send(frame(0, 1)); await messages.next(); socket.send(frame(1)); }
    expect(await messages.next()).toMatchObject({ type: 'error', error: { code: 'AUDIO_PROTOCOL_ERROR' } });
    expect(await api.getSession(session.session_id)).toMatchObject({ state: 'failed', result: null, audio_complete: false });
    expect((await api.history()).items[0].result).toBeNull();
  });

  it('cancels an active websocket and allows a new session without late result resurrection', async () => {
    const host = await start(), api = await signIn(host), created = await api.createSession(randomUUID(), false);
    const { socket, messages } = await openAudio(api, created.session_id);
    socket.send(frame(0)); await messages.next();
    expect(await api.cancelSession(created.session_id)).toMatchObject({ state: 'cancelled', received_samples: 4000 });
    expect(await messages.next()).toMatchObject({ type: 'cancelled' });
    const next = await api.createSession(randomUUID(), false);
    expect(next.session_id).not.toBe(created.session_id);
    expect((await api.getSession(created.session_id)).result).toBeNull();
    expect(messages.queued().some(message => message.type === 'result.ready')).toBe(false);
  });

  it('preserves credentials and reserved receipt identities when the owned port changes', async () => {
    const directory = folder(), first = await start(directory), api = await signIn(first);
    const session = await api.createSession(randomUUID(), false), { socket, messages } = await openAudio(api, session.session_id);
    socket.send(frame(0)); await messages.next(); socket.send(JSON.stringify({ type: 'audio.end', last_seq: 0, total_samples: 4000 }));
    await messages.next(); await messages.next(); await messages.next();
    const completed = await api.getSession(session.session_id), access = api.accessTokenValue!;
    const claim = { result_id: completed.result!.result_id, insertion_id: randomUUID(), text_hash: completed.result!.text_hash, review_confirmed: false };
    await api.claimInsertion(session.session_id, claim);
    await first.close();
    const second = await start(directory);
    expect(second.endpoint.storageIdentity).toBe(first.endpoint.storageIdentity);
    expect(second.endpoint.headers).not.toEqual(first.endpoint.headers);
    const response = await request(second, `/api/v1/sessions/${session.session_id}/insertions/claim`, {
      method: 'POST', headers: { Authorization: `Bearer ${access}`, 'Content-Type': 'application/json' }, body: JSON.stringify(claim),
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ replayed: true, status: 'reserved', insertion_id: claim.insertion_id });
  });

  it('drives the production SessionService claim, durable journal and receipt chain without reimplementing insertion', async () => {
    const directory = folder(), serviceDirectory = folder(), host = await start(serviceDirectory), tokens = new TokenStore({
      isEncryptionAvailable: () => false, encryptString: () => { throw new Error('unavailable'); }, decryptString: () => { throw new Error('unavailable'); },
    }, directory, host.endpoint.storageIdentity);
    const api = new ApiClient(tokens, device, () => undefined, host.endpoint), auth = new AuthService(api, tokens, () => undefined);
    await auth.login('tester@example.test', 'Simulation123!');
    const journal = new InsertionJournal(join(directory, 'journal'));
    let writes = 0;
    const native = {
      supportsObservation: false,
      call: async (action: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> => {
        if (action === 'permission') return { supported: true, trusted: true };
        if (action === 'capture') return { target: 'candidate-only', app: 'Fixture' };
        if (action === 'insertCurrent') {
          const mark = journal.read(auth.ensure().user.id, String(payload.insertion_id));
          expect(mark?.phase).toBe('started');
          const saved = JSON.parse(readFileSync(join(serviceDirectory, 'state.json'), 'utf8'));
          expect(saved.insertions.find((row: { insertion: { insertion_id: string } }) => row.insertion.insertion_id === payload.insertion_id).insertion.status).toBe('reserved');
          expect(payload.text).toBe(TEXT); expect(payload.restoreFocus).toBe(false);
          writes++;
          return { status: 'inserted', effect: 'written', observable: false, target: 'latest-target', app: 'Fixture', message: 'Fixture write' };
        }
        return {};
      },
    } as unknown as NativeVoiceInput;
    const service = new SessionService(api, native, auth, journal, () => undefined, () => undefined); services.push(service);
    const started = await service.start(false);
    service.captureReady(started.session_id, started.attempt_no);
    service.sendAudio(new ArrayBuffer(8000), started.session_id, performance.timeOrigin + performance.now() - 1);
    expect((await service.finish()).state).toBe('ready');
    const outcome = await service.insert(false, false);
    expect(outcome.status).toBe('inserted');
    expect(await service.insert(false, false)).toMatchObject({ status: 'inserted', insertion_id: outcome.insertion_id });
    expect(writes).toBe(1);
    expect((await api.getSession(started.session_id)).insertion_status).toBe('inserted');
    expect(journal.read(auth.ensure().user.id, outcome.insertion_id!)?.receipt_confirmed).toBe(true);
  });
});
