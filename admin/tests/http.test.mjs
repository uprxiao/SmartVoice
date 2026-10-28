import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, requestJson } from '../src/http.ts';

const json = (body, status = 200) => new Response(JSON.stringify(body), { status });

test('request sends explicit CAS body and in-memory authorization without retries', async () => {
  let calls = 0;
  const result = await requestJson({
    url: 'https://fixture.invalid/capacity', method: 'PATCH', token: 'fixture-token',
    body: { version: 2, max_active_sessions: 50 },
    fetchImpl: async (url, options) => {
      calls += 1;
      assert.equal(url, 'https://fixture.invalid/capacity');
      assert.equal(options.headers.Authorization, 'Bearer fixture-token');
      assert.deepEqual(JSON.parse(options.body), { version: 2, max_active_sessions: 50 });
      assert.equal(options.signal.aborted, false);
      return json({ version: 3 });
    },
  });
  assert.deepEqual(result, { version: 3 });
  assert.equal(calls, 1);
});

test('structured error preserves current_version when present; missing details also supported', async () => {
  for (const details of [undefined, { current_version: 8 }]) {
    await assert.rejects(requestJson({
      url: 'https://fixture.invalid/capacity', method: 'PATCH',
      fetchImpl: async () => json({ error: { code: 'VERSION_CONFLICT', message: 'changed', details } }, 409),
    }), (error) => error instanceof ApiError && error.status === 409
      && error.code === 'VERSION_CONFLICT' && error.details.current_version === details?.current_version);
  }
});

test('timeout aborts exactly one request and reports uncertain delivery', async () => {
  let calls = 0;
  await assert.rejects(requestJson({
    url: 'https://fixture.invalid/capacity', method: 'PATCH', timeoutMs: 1,
    fetchImpl: async (_url, options) => {
      calls += 1;
      return new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true }));
    },
  }), (error) => error.code === 'REQUEST_TIMEOUT' && error.status === 0);
  assert.equal(calls, 1);
});

test('invalid success response is distinguishable from a definite HTTP rejection', async () => {
  await assert.rejects(requestJson({
    url: 'https://fixture.invalid/capacity', method: 'PATCH',
    fetchImpl: async () => new Response('<invalid>', { status: 200 }),
  }), (error) => error.status === 200 && error.code === 'INVALID_RESPONSE');
});

test('network exceptions do not expose transport payloads or retry', async () => {
  await assert.rejects(requestJson({
    url: 'https://fixture.invalid/users', method: 'POST',
    fetchImpl: async () => { throw new Error('fixture secret transport contents'); },
  }), (error) => error.code === 'NETWORK_ERROR' && !error.message.includes('secret'));
});

test('logout 204 does not require a JSON body', async () => {
  const result = await requestJson({
    url: 'https://fixture.invalid/logout', method: 'POST',
    fetchImpl: async () => new Response(null, { status: 204 }),
  });
  assert.equal(result, undefined);
});
