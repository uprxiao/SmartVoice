// Prepared offline. Real api/http/config modules; fetch and clock are controlled.
// No server/model/browser, persisted credentials, or product-state substitution.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceRoot = path.resolve(process.env.SVI_ADMIN_SOURCE_ROOT ?? path.join(root, 'src'));
const dependencyRoot = path.resolve(process.env.SVI_ADMIN_DEPENDENCY_ROOT ?? root);
const ts = createRequire(path.join(dependencyRoot, 'package.json'))('typescript');
const digest = (value) => createHash('sha256').update(value).digest('hex');
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const denied = (status = 401) => json({ error: { code: status === 403 ? 'FORBIDDEN' : 'AUTH_INVALID', message: 'controlled fixture' } }, status);
const access = (actor, generation = 0) => `synthetic-access-${actor}-${generation}`;
const refreshToken = (actor) => `synthetic-refresh-${actor}`;
const pair = (actor, generation = 0) => ({ access_token: access(actor, generation), refresh_token: refreshToken(actor),
  token_type: 'Bearer', expires_in: 900, refresh_expires_at: '2026-11-17T00:00:00Z',
  user: { id: `synthetic-user-${actor}`, email: `${actor}@example.invalid`, display_name: actor,
    role: 'admin', status: 'active', locale: 'zh-CN', created_at: '2026-10-18T00:00:00Z', last_login_at: null } });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const watched = (promise) => { void promise.catch(() => undefined); return promise; };
const flush = () => new Promise((resolve) => setImmediate(resolve));
const codeIs = (code) => (error) => error?.code === code;

function harness(respond) {
  let clock = 0;
  const calls = [];
  const fetchBoundary = async (url, options) => {
    const endpoint = new URL(url).pathname;
    const auth = options.headers?.Authorization;
    const actor = ['A', 'B'].find((id) => [0, 1, 2].some((n) => auth === `Bearer ${access(id, n)}`)) ?? null;
    const generation = actor === null ? null : [0, 1, 2].find((n) => auth === `Bearer ${access(actor, n)}`);
    const body = options.body === undefined ? undefined : JSON.parse(options.body);
    const refreshOwner = ['A', 'B'].find((id) => body?.refresh_token === refreshToken(id)) ?? null;
    const safe = { endpoint, method: options.method, actor, generation, refreshOwner, hasAuthorization: Boolean(auth),
      bodyHash: digest(options.body ?? ''), keyHash: digest(options.headers?.['Idempotency-Key'] ?? '') };
    calls.push(safe); // Only hashes/anonymous labels ever enter assertion output.
    const controlled = await respond({ ...safe, body, signal: options.signal });
    if (controlled !== undefined) return controlled;
    if (endpoint === '/api/auth/login') {
      const id = ['A', 'B'].find((value) => body.email === `${value}@example.invalid`);
      assert.ok(id, 'Only declared synthetic users may log in');
      return json(pair(id));
    }
    throw new Error('Unexpected endpoint in controlled fetch boundary');
  };
  const context = vm.createContext({ URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    performance: { now: () => clock }, fetch: fetchBoundary });
  const modules = new Map();
  function load(name) {
    if (modules.has(name)) return modules.get(name).exports;
    assert.ok(['api.ts', 'http.ts', 'config.ts'].includes(name), 'Only production request modules are loaded');
    const file = path.join(sourceRoot, name);
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), { fileName: file, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true } });
    assert.equal((output.diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error).length, 0);
    const module = { exports: {} }; modules.set(name, module);
    const requireBoundary = (specifier) => {
      assert.ok(['./config', './http'].includes(specifier), 'Unexpected production dependency');
      return load(`${specifier.slice(2)}.ts`);
    };
    const factory = new vm.Script(`(function(require,module,exports){\n${output.outputText}\n})`, { filename: file })
      .runInContext(context, { timeout: 250 });
    factory(requireBoundary, module, module.exports);
    return module.exports;
  }
  const production = load('api.ts');
  return { ...production, calls, advance: (ms) => { clock += ms; },
    login: (id) => production.api.login(`${id}@example.invalid`, 'Synthetic-only-password'),
    read: () => production.api.overview('2026-10-17', '2026-10-17'),
    count: (endpoint) => calls.filter((row) => row.endpoint === endpoint).length };
}

test('business 401 refreshes without access, then replays once under the same account', { timeout: 4000 }, async () => {
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/refresh') return json(pair('A', 1));
    if (r.endpoint.endsWith('/overview')) return r.generation === 0 ? denied() : json({ observed: r.actor });
  });
  await h.login('A');
  assert.equal((await h.read()).observed, 'A');
  assert.equal(h.count('/api/auth/refresh'), 1);
  assert.equal(h.count('/api/admin/stats/overview'), 2);
  const refresh = h.calls.find((r) => r.endpoint === '/api/auth/refresh');
  assert.equal(refresh.hasAuthorization, false);
  assert.equal(refresh.refreshOwner, 'A');
});

test('mutation replay freezes payload and preserves its one idempotency key', { timeout: 4000 }, async () => {
  const first = deferred();
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/refresh') return json(pair('A', 1));
    if (r.endpoint === '/api/admin/users') return r.generation === 0 ? first.promise : json({ user: pair('B').user, replayed: false });
  });
  await h.login('A');
  const payload = { email: 'new@example.invalid', password: 'Synthetic-create-password' };
  const pending = watched(h.api.createUser(payload, '11111111-1111-4111-8111-111111111111'));
  try {
    await flush(); payload.email = 'changed@example.invalid';
    first.resolve(denied()); await pending;
    const writes = h.calls.filter((r) => r.endpoint === '/api/admin/users');
    assert.equal(writes.length, 2);
    assert.equal(writes.every((r) => r.method === 'POST'), true);
    assert.equal(writes[0].bodyHash === writes[1].bodyHash, true);
    assert.equal(writes[0].keyHash === writes[1].keyHash, true);
  } finally { first.resolve(denied()); await Promise.allSettled([pending]); }
});

test('parallel and delayed old 401 responses share one refresh and each replay once', { timeout: 4000 }, async () => {
  const refreshing = deferred(), third401 = deferred(); let originals = 0;
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/refresh') return refreshing.promise;
    if (r.endpoint.endsWith('/overview')) {
      if (r.generation === 0) return ++originals === 3 ? third401.promise : denied();
      return json({ observed: r.actor });
    }
  });
  await h.login('A'); const pending = [watched(h.read()), watched(h.read()), watched(h.read())];
  try {
    await flush(); assert.equal(h.count('/api/auth/refresh'), 1);
    refreshing.resolve(json(pair('A', 1))); await Promise.all(pending.slice(0, 2));
    third401.resolve(denied()); await pending[2];
    assert.equal(h.count('/api/auth/refresh'), 1);
    assert.equal(h.count('/api/admin/stats/overview'), 6);
  } finally { refreshing.resolve(json(pair('A', 1))); third401.resolve(denied()); await Promise.allSettled(pending); }
});

test('a second business 401 ends the operation without a third dispatch or refresh', { timeout: 4000 }, async () => {
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/refresh') return json(pair('A', 1));
    if (r.endpoint.endsWith('/overview')) return denied();
  });
  await h.login('A'); await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
  assert.equal(h.count('/api/admin/stats/overview'), 2);
  assert.equal(h.count('/api/auth/refresh'), 1);
  await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
  assert.equal(h.calls.at(-1).hasAuthorization, false);
  assert.equal(h.count('/api/auth/refresh'), 1);
});

test('login failure neither sends nor refreshes the former account credentials', { timeout: 4000 }, async () => {
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/login' && r.body.email === 'B@example.invalid') return denied();
    if (r.endpoint.endsWith('/overview')) return denied();
  });
  await h.login('A'); await assert.rejects(h.login('B'), codeIs('AUTH_INVALID'));
  assert.equal(h.calls.filter((r) => r.endpoint === '/api/auth/login').every((r) => !r.hasAuthorization), true);
  await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
  assert.equal(h.calls.at(-1).hasAuthorization, false);
  assert.equal(h.count('/api/auth/refresh'), 0);
});

for (const status of [200, 401, 403]) {
  test(`old account response ${status} cannot affect a new login`, { timeout: 4000 }, async () => {
    const old = deferred();
    const h = harness((r) => r.endpoint.endsWith('/overview')
      ? r.actor === 'A' ? old.promise : json({ observed: r.actor }) : undefined);
    await h.login('A'); const pending = watched(h.read());
    try {
      await flush(); h.setToken(null); await h.login('B');
      old.resolve(status === 200 ? json({ old: true }) : denied(status));
      await assert.rejects(pending, codeIs('AUTH_CONTEXT_CHANGED'));
      assert.equal((await h.read()).observed, 'B');
      assert.equal(h.count('/api/auth/refresh'), 0);
    } finally { old.resolve(json({})); await Promise.allSettled([pending]); }
  });
}

test('refresh completing after account switch cannot install old credentials or replay', { timeout: 4000 }, async () => {
  const old = deferred();
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/refresh') return old.promise;
    if (r.endpoint.endsWith('/overview')) return r.actor === 'A' ? denied() : json({ observed: r.actor });
  });
  await h.login('A'); const pending = watched(h.read());
  try {
    await flush(); assert.equal(h.count('/api/auth/refresh'), 1);
    h.setToken(null); await h.login('B'); old.resolve(json(pair('A', 1)));
    await assert.rejects(pending, codeIs('AUTH_CONTEXT_CHANGED'));
    assert.equal((await h.read()).observed, 'B');
    assert.equal(h.calls.filter((r) => r.endpoint.endsWith('/overview') && r.actor === 'A').length, 1);
  } finally { old.resolve(json(pair('A', 1))); await Promise.allSettled([pending]); }
});

test('clear while login is pending prevents that login from installing a session', { timeout: 4000 }, async () => {
  const login = deferred();
  const h = harness((r) => r.endpoint === '/api/auth/login' ? login.promise
    : r.endpoint.endsWith('/overview') ? denied() : undefined);
  const pending = watched(h.login('A'));
  try {
    await flush(); h.setToken(null); login.resolve(json(pair('A')));
    await assert.rejects(pending, codeIs('AUTH_CONTEXT_CHANGED'));
    await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
    assert.equal(h.calls.at(-1).hasAuthorization, false);
  } finally { login.resolve(json(pair('A'))); await Promise.allSettled([pending]); }
});

test('detached logout may refresh old credentials solely to revoke them after a new login', { timeout: 4000 }, async () => {
  const first = deferred(), refresh = deferred();
  const h = harness((r) => {
    if (r.endpoint === '/api/auth/logout') return r.generation === 0 ? first.promise : new Response(null, { status: 204 });
    if (r.endpoint === '/api/auth/refresh') return refresh.promise;
    if (r.endpoint.endsWith('/overview')) return json({ observed: r.actor });
  });
  await h.login('A'); const pending = watched(h.api.logout());
  try {
    h.setToken(null); await h.login('B'); first.resolve(denied()); await flush();
    assert.equal(h.count('/api/auth/refresh'), 1);
    assert.equal(h.calls.find((r) => r.endpoint === '/api/auth/refresh').refreshOwner, 'A');
    refresh.resolve(json(pair('A', 1))); await pending;
    const logouts = h.calls.filter((r) => r.endpoint === '/api/auth/logout');
    assert.deepEqual(logouts.map((r) => [r.actor, r.generation]), [['A', 0], ['A', 1]]);
    assert.equal((await h.read()).observed, 'B');
  } finally { first.resolve(denied()); refresh.resolve(json(pair('A', 1))); await Promise.allSettled([pending]); }
});

for (const status of [401, 403]) {
  test(`refresh rejection ${status} clears the current session without a business replay`, { timeout: 4000 }, async () => {
    const h = harness((r) => r.endpoint === '/api/auth/refresh' ? denied(status)
      : r.endpoint.endsWith('/overview') ? denied() : undefined);
    await h.login('A'); await assert.rejects(h.read(), (error) => error.status === status);
    assert.equal(h.count('/api/admin/stats/overview'), 1);
    await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
    assert.equal(h.calls.at(-1).hasAuthorization, false);
    assert.equal(h.count('/api/auth/refresh'), 1);
  });
}

for (const fault of ['another-user', 'rotated-refresh', 'not-admin', 'missing-user']) {
  test(`refresh ${fault} response is rejected without switching identity`, { timeout: 4000 }, async () => {
    const response = pair(fault === 'another-user' ? 'B' : 'A', 1);
    if (fault === 'rotated-refresh') response.refresh_token = 'synthetic-unexpected-rotation';
    if (fault === 'not-admin') response.user.role = 'user';
    if (fault === 'missing-user') response.user = null;
    const h = harness((r) => r.endpoint === '/api/auth/refresh' ? json(response)
      : r.endpoint.endsWith('/overview') ? denied() : undefined);
    await h.login('A'); await assert.rejects(h.read(), (error) => [401, 403].includes(error.status));
    assert.equal(h.count('/api/admin/stats/overview'), 1);
    await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
    assert.equal(h.calls.at(-1).hasAuthorization, false);
  });
}

for (const fault of ['network', 'server-error', 'invalid-json']) {
  test(`refresh ${fault} failure clears the current session and is not retried`, { timeout: 4000 }, async () => {
    const h = harness((r) => {
      if (r.endpoint === '/api/auth/refresh') {
        if (fault === 'network') throw new TypeError('controlled transport failure');
        if (fault === 'server-error') return denied(500);
        return new Response('{', { status: 200 });
      }
      if (r.endpoint.endsWith('/overview')) return denied();
    });
    await h.login('A'); await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
    assert.equal(h.count('/api/admin/stats/overview'), 1);
    await assert.rejects(h.read(), codeIs('AUTH_INVALID'));
    assert.equal(h.calls.at(-1).hasAuthorization, false);
    assert.equal(h.count('/api/auth/refresh'), 1);
  });
}

for (const status of [403, 429, 500]) {
  test(`business ${status} does not trigger refresh or automatic replay`, { timeout: 4000 }, async () => {
    const h = harness((r) => r.endpoint.endsWith('/overview') ? denied(status) : undefined);
    await h.login('A'); await assert.rejects(h.read(), (error) => error.status === status);
    assert.equal(h.count('/api/admin/stats/overview'), 1);
    assert.equal(h.count('/api/auth/refresh'), 0);
  });
}

test('transport failure does not refresh or retry a mutation', { timeout: 4000 }, async () => {
  const h = harness((r) => { if (r.endpoint.endsWith('/capacity')) throw new TypeError('controlled transport failure'); });
  await h.login('A'); await assert.rejects(h.api.updateCapacity(1, 2), codeIs('NETWORK_ERROR'));
  assert.equal(h.count('/api/admin/capacity'), 1);
  assert.equal(h.count('/api/auth/refresh'), 0);
});

test('refresh cannot reset the original operation deadline', { timeout: 4000 }, async () => {
  let h;
  h = harness((r) => {
    if (r.endpoint.endsWith('/overview')) { h.advance(14_500); return denied(); }
    if (r.endpoint === '/api/auth/refresh') { h.advance(501); return json(pair('A', 1)); }
  });
  await h.login('A'); await assert.rejects(h.read(), (error) =>
    error.code === 'AUTH_INVALID' && error.details?.cause_code === 'REQUEST_TIMEOUT');
  assert.equal(h.count('/api/admin/stats/overview'), 1);
  assert.equal(h.count('/api/auth/refresh'), 1);
});

test('an auth error already returned before new login cannot later clear that new login', { timeout: 4000 }, async () => {
  const h = harness((r) => r.endpoint.endsWith('/overview')
    ? r.actor === 'A' ? denied(403) : json({ observed: r.actor }) : undefined);
  await h.login('A');
  let oldError;
  try { await h.read(); } catch (error) { oldError = error; }
  assert.ok(oldError);
  assert.equal(h.isCurrentAuthError(oldError), true);
  await h.login('B');
  assert.equal(h.isCurrentAuthError(oldError), false);
  assert.equal((await h.read()).observed, 'B');
});
