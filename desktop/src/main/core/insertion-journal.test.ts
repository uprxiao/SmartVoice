import { afterEach, describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { InsertionJournal, type InsertionIntent } from './insertion-journal';
const paths: string[] = [];
const setup = () => {
  const path = mkdtempSync(join(tmpdir(), 'svi-insertion-journal-')); paths.push(path);
  const key: InsertionIntent = { user_id: randomUUID(), session_id: randomUUID(), result_id: randomUUID(), insertion_id: randomUUID(), text_hash: 'a'.repeat(64) };
  return { path, key, journal: new InsertionJournal(path) };
};
afterEach(() => { for (const path of paths.splice(0)) rmSync(path, { recursive: true, force: true }); });

describe('durable exactly-once native intent (INSERT-12/15/16/17/18)', () => {
  it('a repeated claim/intent never grants another native execution', () => {
    const { key, journal } = setup(); expect(journal.begin(key).created).toBe(true);
    expect(journal.begin(key).created).toBe(false);
  });
  it('crash recovery converts started to unverified without executing or forgetting it', () => {
    const { path, key, journal } = setup(); journal.begin(key);
    const recovered = new InsertionJournal(path).unconfirmed(key.user_id);
    expect(recovered).toHaveLength(1); expect(recovered[0].phase).toBe('unverified');
    expect(recovered[0].error_code).toBe('CLIENT_INTERRUPTED');
    expect(new InsertionJournal(path).begin(key).created).toBe(false);
  });
  it('receipt loss recovers the same inserted outcome and same insertion ID', () => {
    const { path, key, journal } = setup(); journal.begin(key); journal.finish(key, 'inserted', true, null);
    const restarted = new InsertionJournal(path); const [pending] = restarted.unconfirmed(key.user_id);
    expect(pending.insertion_id).toBe(key.insertion_id); expect(pending.phase).toBe('inserted');
    restarted.confirmReceipt(pending); expect(restarted.unconfirmed(key.user_id)).toEqual([]);
    expect(restarted.begin(key).created).toBe(false);
  });
  it('a terminal outcome cannot be rewritten to make an uncertain operation retryable', () => {
    const { key, journal } = setup(); journal.begin(key); journal.finish(key, 'unverified', false, 'VERIFY_FAILED');
    expect(() => journal.finish(key, 'failed', false, 'TARGET_CHANGED')).toThrow('禁止更改');
  });
  it('a changed account cannot recover another account pending receipt', () => {
    const { key, journal } = setup(); journal.begin(key);
    expect(journal.unconfirmed(randomUUID())).toEqual([]);
  });
  it('persists only whitelisted metadata without result or input-box text', () => {
    const { path, key, journal } = setup(); journal.begin({ ...key, result_text: 'result-secret', target_text: 'target-secret' } as InsertionIntent);
    const dir = join(path, readdirSync(path)[0]); const raw = readFileSync(join(dir, readdirSync(dir)[0]), 'utf8');
    expect(raw).not.toContain('result-secret'); expect(raw).not.toContain('target-secret');
  });
  it('a torn intent remains uncertain rather than being silently overwritten', () => {
    const { path, key, journal } = setup(); journal.begin(key);
    const dir = join(path, readdirSync(path)[0]); writeFileSync(join(dir, readdirSync(dir)[0]), '{partial');
    expect(() => new InsertionJournal(path).begin(key)).toThrow('不会重复输入');
  });
});
