import { randomUUID } from 'node:crypto';
import { mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as durableFile from '../../../native/durable-file';
import { SimulationStore, type Claim } from './store';

const directories: string[] = [];
const stores: SimulationStore[] = [];
const folder = () => { const directory = mkdtempSync(join(tmpdir(), 'svi-simulation-state-')); directories.push(directory); return directory; };
const storeIn = (directory = folder()) => { const store = new SimulationStore(directory); stores.push(store); return store; };
const login = (store: SimulationStore, email = 'tester@example.test', device = randomUUID()) => store.login(email, 'Simulation123!', device);
async function ready(store: SimulationStore, access: string) {
  const created = await store.createSession(access, randomUUID(), false);
  await store.beginAudio(access, created.session_id);
  const completed = await store.completeAudio(access, created.session_id, 4000, 1);
  const claim: Claim = { insertion_id: randomUUID(), result_id: completed.result!.result_id,
    text_hash: completed.result!.text_hash, review_confirmed: false };
  return { session: completed, claim };
}
afterEach(async () => {
  vi.restoreAllMocks();
  for (const store of stores.splice(0)) await store.close();
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true });
});

describe('isolated simulation state', () => {
  it('persists only salted password hashes and token digests, and keeps settings across restart', async () => {
    const directory = folder(), first = storeIn(directory), pair = await login(first);
    const me = await first.me(pair.access_token);
    await first.patchSettings(pair.access_token, { version: me.settings.version, locale: 'en', history_retention_days: 7 });
    const raw = readFileSync(join(directory, 'state.json'), 'utf8');
    expect(raw).not.toContain('Simulation123!');
    expect(raw).not.toContain(pair.access_token);
    expect(raw).not.toContain(pair.refresh_token);
    expect(JSON.parse(raw).accounts[0].salt).toMatch(/^[0-9a-f]{32}$/);
    await first.close();
    const reopened = storeIn(directory);
    expect((await reopened.me(pair.access_token)).settings).toMatchObject({ locale: 'en', version: 2, history_retention_days: 7 });
    const renewed = await reopened.refresh(pair.refresh_token);
    expect(renewed.refresh_token).toBe(pair.refresh_token);
    await reopened.logout(renewed.access_token);
    await expect(reopened.me(pair.access_token)).rejects.toMatchObject({ code: 'AUTH_INVALID' });
    await expect(reopened.refresh(pair.refresh_token)).rejects.toMatchObject({ code: 'AUTH_INVALID' });
  });

  it('rejects non-fictional accounts and isolates account data and optimistic versions', async () => {
    const store = storeIn();
    await expect(store.register('person@gmail.com', 'Simulation123!')).rejects.toMatchObject({ code: 'VALIDATION_ERROR' });
    await store.register('second@example.test', 'Simulation123!');
    const a = await login(store), b = await login(store, 'second@example.test');
    const term = await store.createTerm(a.access_token, randomUUID(), 'React', ['React.js']);
    expect((await store.dictionary(b.access_token, { limit: 50 })).items).toEqual([]);
    await expect(store.updateTerm(b.access_token, randomUUID(), term.id, { version: 1, term: 'Other' })).rejects.toMatchObject({ code: 'NOT_FOUND' });
    const changed = await store.updateTerm(a.access_token, randomUUID(), term.id, { version: 1, aliases: ['ReactJS'] });
    expect(changed.version).toBe(2);
    await expect(store.deleteTerm(a.access_token, randomUUID(), term.id, 1)).rejects.toMatchObject({ code: 'VERSION_CONFLICT' });
    await store.patchSettings(a.access_token, { version: 1, locale: 'en' });
    await expect(store.patchSettings(a.access_token, { version: 1, locale: 'zh-CN' })).rejects.toMatchObject({ code: 'VERSION_CONFLICT' });
    expect((await store.settings(b.access_token)).locale).toBe('zh-CN');
    const session = await store.createSession(a.access_token, randomUUID(), false);
    await expect(store.getSession(b.access_token, session.session_id)).rejects.toMatchObject({ code: 'NOT_FOUND' });
  });

  it('serializes concurrent mutations and retries without duplicating dictionary effects', async () => {
    const store = storeIn(), access = (await login(store)).access_token, key = randomUUID();
    const results = await Promise.all([
      store.createTerm(access, key, 'Docker', ['容器']), store.createTerm(access, key, 'Docker', ['容器']),
    ]);
    expect(results.map(value => value.replayed)).toEqual([false, true]);
    expect(results[1].id).toBe(results[0].id);
    expect((await store.dictionary(access, { limit: 50 })).items).toHaveLength(1);
    await expect(store.createTerm(access, key, 'Different', [])).rejects.toMatchObject({ code: 'IDEMPOTENCY_CONFLICT' });
    const second = await store.createTerm(access, randomUUID(), 'TypeScript', []);
    await expect(store.deleteTerms(access, randomUUID(), [
      { id: results[0].id, version: 1 }, { id: second.id, version: 99 },
    ])).rejects.toMatchObject({ code: 'VERSION_CONFLICT' });
    expect((await store.dictionary(access, { limit: 50 })).items).toHaveLength(2);
    const removed = await store.deleteTerms(access, randomUUID(), [{ id: results[0].id, version: 1 }, { id: second.id, version: 1 }]);
    expect(removed.deleted_ids).toEqual([results[0].id, second.id]);
    expect((await store.dictionary(access, { limit: 50 })).items).toEqual([]);
  });

  it('rejects a changed dictionary cursor and classifies CSV rows without losing duplicates', async () => {
    const store = storeIn(), access = (await login(store)).access_token;
    await store.createTerm(access, randomUUID(), 'Docker', []);
    await store.createTerm(access, randomUUID(), 'React', ['React.js']);
    const first = await store.dictionary(access, { limit: 1 });
    const key = randomUUID();
    const rows = [{ row: 2, term: 'React', aliases: ['React.js'] }, { row: 3, term: 'React', aliases: ['different'] },
      { row: 4, term: '', aliases: [] }, { row: 5, term: 'TypeScript', aliases: [] }];
    const imported = await store.importCsv(access, key, 'fixture-digest', rows);
    expect(imported).toMatchObject({ created: 1, skipped: 1, conflicts: 1, invalid: 1, replayed: false });
    expect((await store.importCsv(access, key, 'fixture-digest', rows))).toMatchObject({ ...imported, replayed: true });
    await expect(store.dictionary(access, { limit: 1, cursor: first.next_cursor! })).rejects.toMatchObject({ code: 'DICTIONARY_CHANGED' });
    expect((await store.dictionary(access, { limit: 50 })).items).toHaveLength(3);
  });

  it('retains reserved and terminal insertion identities through restart and never upgrades an unverified receipt', async () => {
    const directory = folder(), first = storeIn(directory), pair = await login(first);
    const { session, claim } = await ready(first, pair.access_token);
    const reserved = await first.claim(pair.access_token, session.session_id, claim);
    expect(reserved.status).toBe('reserved');
    await first.close();
    const restarted = storeIn(directory);
    expect(await restarted.claim(pair.access_token, session.session_id, claim)).toMatchObject({ insertion_id: claim.insertion_id, status: 'reserved', replayed: true });
    await expect(restarted.claim(pair.access_token, session.session_id, { ...claim, insertion_id: randomUUID() }))
      .rejects.toMatchObject({ code: 'INSERTION_ALREADY_CLAIMED' });
    const receipt = { status: 'unverified' as const, observable: false, error_code: 'VERIFY_FAILED' };
    await restarted.receipt(pair.access_token, session.session_id, claim.insertion_id, receipt);
    await restarted.close();
    const again = storeIn(directory);
    expect(await again.receipt(pair.access_token, session.session_id, claim.insertion_id, receipt)).toMatchObject({ status: 'unverified', replayed: true });
    await expect(again.receipt(pair.access_token, session.session_id, claim.insertion_id, { status: 'inserted', observable: true, error_code: null }))
      .rejects.toMatchObject({ code: 'INSERTION_STATE_CONFLICT' });
    const date = new Date().toISOString().slice(0, 10);
    expect((await again.stats(pair.access_token, date, date)).totals.inserted_sessions).toBe(0);
  });

  it('allows a fresh claim only after a proven failed receipt, and counts inserted sessions once', async () => {
    const store = storeIn(), access = (await login(store)).access_token;
    const { session, claim } = await ready(store, access);
    await store.claim(access, session.session_id, claim);
    await store.receipt(access, session.session_id, claim.insertion_id, { status: 'failed', observable: false, error_code: 'ZERO_WRITE' });
    const next = { ...claim, insertion_id: randomUUID() };
    await store.claim(access, session.session_id, next);
    const receipt = { status: 'inserted' as const, observable: true, error_code: null };
    await store.receipt(access, session.session_id, next.insertion_id, receipt);
    await store.receipt(access, session.session_id, next.insertion_id, receipt);
    const date = new Date().toISOString().slice(0, 10), stats = await store.stats(access, date, date);
    expect(stats.totals).toMatchObject({ sessions: 1, inserted_sessions: 1, insertion_success_rate: 1, learning_events: 0 });
  });

  it('cancels unfinished sessions on restart and revokes active streams on logout', async () => {
    const directory = folder(), first = storeIn(directory), pair = await login(first);
    const old = await first.createSession(pair.access_token, randomUUID(), false);
    await first.beginAudio(pair.access_token, old.session_id);
    await first.close();
    const restarted = storeIn(directory);
    expect(await restarted.getSession(pair.access_token, old.session_id)).toMatchObject({ state: 'cancelled', error_code: 'CLIENT_INTERRUPTED', result: null });
    const next = await restarted.createSession(pair.access_token, randomUUID(), false);
    await restarted.beginAudio(pair.access_token, next.session_id);
    await restarted.logout(pair.access_token);
    const renewed = await login(restarted);
    expect(await restarted.getSession(renewed.access_token, next.session_id)).toMatchObject({ state: 'cancelled', result: null });
    await expect(restarted.completeAudio(renewed.access_token, next.session_id, 4000, 1)).rejects.toMatchObject({ code: 'SESSION_STATE_CONFLICT' });
  });

  it('does not acknowledge or publish a mutation when durable storage is unavailable', async () => {
    const directory = folder(), store = storeIn(directory), access = (await login(store)).access_token;
    const oldBytes = readFileSync(join(directory, 'state.json'));
    const moved = `${directory}-moved`;
    renameSync(directory, moved);
    try {
      await expect(store.createTerm(access, randomUUID(), 'React', [])).rejects.toMatchObject({ code: 'SIMULATION_STORAGE_UNAVAILABLE' });
      expect((await store.dictionary(access, { limit: 50 })).items).toEqual([]);
      expect(readFileSync(join(moved, 'state.json'))).toEqual(oldBytes);
    } finally { renameSync(moved, directory); }
    expect((await store.createTerm(access, randomUUID(), 'Docker', [])).version).toBe(1);
  });

  it('fails closed after an uncertain publish and recovers its reserved claim without granting another write', async () => {
    const directory = folder(), first = storeIn(directory), access = (await login(first)).access_token;
    const { session, claim } = await ready(first, access);
    vi.spyOn(durableFile, 'syncPublishedFile').mockImplementationOnce(() => { throw new Error('fictional flush failure'); });
    await expect(first.claim(access, session.session_id, claim)).rejects.toMatchObject({ code: 'SIMULATION_STORAGE_UNAVAILABLE' });
    await expect(first.getSession(access, session.session_id)).rejects.toMatchObject({ code: 'SIMULATION_STORAGE_UNAVAILABLE' });
    await first.close();
    const restarted = storeIn(directory);
    expect(await restarted.claim(access, session.session_id, claim)).toMatchObject({ replayed: true, status: 'reserved' });
    await expect(restarted.claim(access, session.session_id, { ...claim, insertion_id: randomUUID() })).rejects.toMatchObject({ code: 'INSERTION_ALREADY_CLAIMED' });
  });

  it('does not silently replace corrupted or duplicated persistent identities', async () => {
    const directory = folder(), first = storeIn(directory), access = (await login(first)).access_token;
    const { session, claim } = await ready(first, access);
    await first.claim(access, session.session_id, claim);
    await first.close();
    const file = join(directory, 'state.json'), data = JSON.parse(readFileSync(file, 'utf8'));
    data.insertions.push(data.insertions[0]); writeFileSync(file, JSON.stringify(data));
    expect(() => new SimulationStore(directory)).toThrow('SIMULATION_STATE_INVALID');
    expect(JSON.parse(readFileSync(file, 'utf8')).insertions).toHaveLength(2);
  });

  it('expires retained result text while preserving claim identities and anonymous counters', async () => {
    const directory = folder(), first = storeIn(directory), pair = await login(first);
    const { session, claim } = await ready(first, pair.access_token);
    await first.claim(pair.access_token, session.session_id, claim);
    await first.patchSettings(pair.access_token, { version: 1, history_retention_days: 7 });
    await first.close();
    const file = join(directory, 'state.json'), data = JSON.parse(readFileSync(file, 'utf8'));
    data.sessions[0].session.first_terminal_at = '2000-01-01T00:00:00.000Z';
    writeFileSync(file, JSON.stringify(data));
    const restarted = storeIn(directory);
    expect(await restarted.getSession(pair.access_token, session.session_id)).toMatchObject({ content_expired: true, result: null });
    expect(await restarted.claim(pair.access_token, session.session_id, claim)).toMatchObject({ replayed: true, status: 'reserved' });
    await expect(restarted.claim(pair.access_token, session.session_id, { ...claim, insertion_id: randomUUID() })).rejects.toMatchObject({ code: 'CONTENT_EXPIRED' });
    const date = new Date().toISOString().slice(0, 10);
    expect((await restarted.stats(pair.access_token, date, date)).totals.result_sessions).toBe(1);
  });
});
