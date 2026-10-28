import test from 'node:test';
import assert from 'node:assert/strict';
import { UsageReader } from '../src/usage-reader.ts';
import { ApiError } from '../src/http.ts';

const query = { from: '2026-10-16', to: '2026-10-16', userId: 'fixture-u1' };
const u1 = { date: '2026-10-16', user_id: 'fixture-u1', sessions: 3,
  received_audio_ms: 35000, result_sessions: 2, chars: 13, successful_audio_ms: 30000,
  fallback_sessions: 0, inserted_sessions: 1, learning_events: 0 };
const u2 = { ...u1, user_id: 'fixture-u2', sessions: 1, received_audio_ms: 3000,
  result_sessions: 1, chars: 10, successful_audio_ms: 3000 };
const page = (items, next_cursor = null) => ({ timezone: 'Asia/Shanghai', items, next_cursor });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
function harness(usage) {
  const states = [], errors = [];
  const reader = new UsageReader({ usage }, (state) => states.push(state), (error) => errors.push(error));
  return { reader, states, errors, current: () => states.at(-1) };
}

test('fixed range/user reaches the API and keeps received/successful fields distinct', async () => {
  const calls = [];
  const h = harness(async (...args) => { calls.push(args); return page([u1]); });
  await h.reader.replace(query);
  assert.deepEqual(calls, [['2026-10-16', '2026-10-16', 'fixture-u1', undefined]]);
  assert.deepEqual(h.current().items, [u1]);
  assert.equal(h.current().items[0].received_audio_ms, 35000);
  assert.equal(h.current().items[0].successful_audio_ms, 30000);
  assert.equal(h.current().loaded, true);
  assert.equal(h.current().loading, false);
  assert.deepEqual(h.errors, []);
});

test('new filter clears old rows and a late older first page cannot cross users', async () => {
  const old = deferred();
  const h = harness(async (_from, _to, user) => user === 'fixture-u1' ? old.promise : page([u2]));
  const pending = h.reader.replace(query);
  const latest = h.reader.replace({ ...query, userId: 'fixture-u2' });
  assert.deepEqual(h.current().items, []);
  await latest;
  old.resolve(page([u1]));
  await pending;
  assert.deepEqual(h.current().items, [u2]);
  assert.deepEqual(h.errors, []);
});

test('pagination uses frozen query/cursor, blocks duplicate requests, and invalidates old pages', async () => {
  const more = deferred(), calls = [];
  const h = harness(async (...args) => {
    calls.push(args);
    return args[3] ? more.promise : page(args[2] === 'fixture-u1' ? [u1] : [u2], 'next-page');
  });
  await h.reader.replace(query);
  const pending = h.reader.more();
  await h.reader.more();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], ['2026-10-16', '2026-10-16', 'fixture-u1', 'next-page']);
  await h.reader.replace({ ...query, userId: 'fixture-u2' });
  more.resolve(page([{ ...u1, date: '2026-10-15' }]));
  await pending;
  assert.deepEqual(h.current().items, [u2]);
});

test('failed later page retains prior rows and retries the same cursor without duplication', async () => {
  const problem = new ApiError(503, 'SERVICE_UNAVAILABLE', '读取暂不可用');
  let moreCalls = 0;
  const h = harness(async (_from, _to, _user, cursor) => {
    if (!cursor) return page([u1], 'next');
    assert.equal(cursor, 'next');
    if (++moreCalls === 1) throw problem;
    return page([u2]);
  });
  await h.reader.replace({ ...query, userId: undefined });
  await h.reader.more();
  assert.deepEqual(h.current().items, [u1]);
  assert.equal(h.current().error, problem);
  assert.equal(h.current().nextCursor, 'next');
  await h.reader.more();
  assert.deepEqual(h.current().items, [u1, u2]);
  assert.equal(h.current().error, null);
  assert.equal(h.current().nextCursor, null);
  await h.reader.more();
  assert.equal(moreCalls, 2);
});

test('invalid dates never request; a valid empty response is distinct from failure', async () => {
  let calls = 0;
  const h = harness(async () => { calls++; return page([]); });
  await h.reader.replace({ ...query, from: '2026-10-17' });
  assert.equal(calls, 0);
  assert.equal(h.current().loaded, false);
  assert.match(h.current().error.message, /开始日期/);
  await h.reader.replace(query);
  assert.equal(calls, 1);
  assert.equal(h.current().loaded, true);
  assert.equal(h.current().error, null);
  assert.deepEqual(h.current().items, []);
});

test('current auth and programmer failures keep their identity; stale ordinary errors cannot clear a new view', async () => {
  const forbidden = new ApiError(403, 'FORBIDDEN', '角色已撤销');
  const bug = new TypeError('controlled test fault');
  for (const error of [forbidden, bug]) {
    const h = harness(async () => { throw error; });
    await h.reader.replace(query);
    assert.equal(h.current().error, error);
    assert.deepEqual(h.errors, [error]);
  }
  const old = deferred();
  const h = harness(async (_from, _to, user) => user === 'fixture-u1' ? old.promise : page([u2]));
  const pending = h.reader.replace(query);
  await h.reader.replace({ ...query, userId: 'fixture-u2' });
  old.reject(new ApiError(503, 'SERVICE_UNAVAILABLE', '旧筛选查询失败'));
  await pending;
  assert.deepEqual(h.current().items, [u2]);
  assert.deepEqual(h.errors, []);
});

test('unmount/account boundary invalidation suppresses delayed response and error callbacks', async () => {
  for (const reject of [false, true]) {
    const pending = deferred();
    const h = harness(async () => pending.promise);
    const request = h.reader.replace(query);
    h.reader.invalidate();
    const before = h.states.length;
    if (reject) pending.reject(new ApiError(401, 'AUTH_INVALID', '旧账号已退出'));
    else pending.resolve(page([u1]));
    await request;
    assert.equal(h.states.length, before);
    assert.deepEqual(h.errors, []);
  }
});


test('an old filter authorization failure still revokes the current account lifetime', async () => {
  for (const status of [401, 403]) {
    const old = deferred();
    const problem = new ApiError(status, status === 401 ? 'AUTH_INVALID' : 'FORBIDDEN', '权限已失效');
    const h = harness(async (_from, _to, user) => user === 'fixture-u1' ? old.promise : page([u2]));
    const pending = h.reader.replace(query);
    await h.reader.replace({ ...query, userId: 'fixture-u2' });
    old.reject(problem);
    await pending;
    assert.deepEqual(h.current().items, [u2]);
    assert.deepEqual(h.errors, [problem]);
  }
});
