import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { performance } from 'node:perf_hooks';

vi.mock('electron', () => ({ app: {}, safeStorage: {}, clipboard: { writeText: vi.fn() } }));
import { ApiClient } from '../core/api-client';
import { AuthService } from '../core/auth-service';
import { TokenStore } from '../core/token-store';
import { InsertionJournal } from '../core/insertion-journal';
import { SessionService } from '../core/session-service';
import type { NativeVoiceInput } from '../core/native-input';
import { startSimulationHost } from './host';

const cleanup: Array<() => Promise<void>> = [];
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close(); });

async function fixture() {
  const root = mkdtempSync(join(tmpdir(), 'svi-simulation-chain-'));
  cleanup.push(async () => { rmSync(root, { recursive: true, force: true }); });
  const host = await startSimulationHost({ directory: join(root, 'service'), storageIdentity: 'smartvoiceinput:fictional-simulation-test:v1' });
  cleanup.push(host.close);
  const profile = join(root, 'profile'); mkdirSync(profile);
  const tokens = new TokenStore({ isEncryptionAvailable: () => false, encryptString: () => { throw new Error('Unavailable'); }, decryptString: () => { throw new Error('Unavailable'); } }, profile, host.endpoint.storageIdentity);
  const device = { installation_id: '11111111-1111-4111-8111-111111111111', name: 'fictional-test-device', platform: 'windows' as const, client_version: '0.0.2' };
  const api = new ApiClient(tokens, device, () => {}, host.endpoint);
  const auth = new AuthService(api, tokens, () => {});
  await auth.login('tester@example.test', 'Simulation123!');
  const journal = new InsertionJournal(join(root, 'journal'));
  let latestTarget = 'caret-a';
  const effects: Array<{ target: string; text: unknown; insertionId: unknown }> = [];
  const native = { supportsObservation: false, call: vi.fn(async (action: string, body?: Record<string, unknown>) => {
    if (action === 'capture') return { target: latestTarget, app: 'Fictional editor', available: true };
    if (action === 'insertCurrent') {
      effects.push({ target: latestTarget, text: body?.text, insertionId: body?.insertion_id });
      return { status: 'inserted', effect: 'written', observable: true, target: latestTarget, app: 'Fictional editor' };
    }
    if (action === 'release') return {};
    throw new Error(`Unexpected native command ${action}`);
  }) };
  const service = new SessionService(api, native as unknown as NativeVoiceInput, auth, journal, () => {}, () => {});
  cleanup.push(async () => { service.dispose(); });
  return { api, auth, service, journal, effects, native, setTarget: (target: string) => { latestTarget = target; } };
}

describe('simulation transport with the production session and durable insertion chain', () => {
  it('uses the production claim, journal and receipt around one latest-target native command', async () => {
    const f = await fixture();
    const started = await f.service.start(false);
    f.service.sendAudio(new ArrayBuffer(8000), started.session_id, performance.timeOrigin + performance.now());
    f.service.captureReady(started.session_id, started.attempt_no);
    const ready = await f.service.finish();
    expect(ready.state).toBe('ready');
    expect(ready.result?.refined_text).toBe('这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。');
    f.setTarget('caret-b-with-new-selection');
    const outcome = await f.service.insert(false, false);
    expect(outcome.status).toBe('inserted');
    expect(f.effects).toEqual([{ target: 'caret-b-with-new-selection', text: ready.result!.refined_text, insertionId: outcome.insertion_id }]);
    const user = f.auth.ensure().user.id;
    expect(f.journal.read(user, outcome.insertion_id!)?.phase).toBe('inserted');
    await f.service.recoverReceipts();
    expect(f.journal.read(user, outcome.insertion_id!)?.receipt_confirmed).toBe(true);
    expect((await f.api.getSession(started.session_id)).insertion_status).toBe('inserted');
    await f.service.insert(false, false);
    expect(f.effects).toHaveLength(1);
  });

  it('cancels the production operation without dispatching a native insertion', async () => {
    const f = await fixture(); const started = await f.service.start(false);
    f.service.sendAudio(new ArrayBuffer(8000), started.session_id, performance.timeOrigin + performance.now());
    await f.service.cancel();
    expect(f.service.status.state).toBe('cancelled');
    expect((await f.api.getSession(started.session_id)).state).toBe('cancelled');
    expect(f.effects).toEqual([]);
  });
});
