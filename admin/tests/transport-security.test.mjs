// Prepared only. Root runs against actual Admin modules; no real network.
// Browser location is varied; the production URL module is not rewritten.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const localAdminRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const adminRoot = path.resolve(process.env.SVI_ADMIN_DEPENDENCY_ROOT
  ?? localAdminRoot);
const sourceRoot = path.resolve(process.env.SVI_ADMIN_SOURCE_ROOT ?? path.join(localAdminRoot, 'src'));
const ts = createRequire(path.join(adminRoot, 'package.json'))('typescript');
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const synthetic = {
  password: 'Synthetic-NFR05-password-only', access: 'synthetic-access',
  refreshed: 'synthetic-refreshed', refresh: 'synthetic-refresh',
};
const pair = (refreshed = false) => ({
  access_token: refreshed ? synthetic.refreshed : synthetic.access,
  refresh_token: synthetic.refresh,
  user: { id: 'synthetic-admin', email: 'nfr05@example.invalid', role: 'admin' },
});

function harness(base, respond = () => json(pair())) {
  const calls = [];
  const fetchBoundary = async (url, options) => {
    const parsed = new URL(url);
    const body = options.body === undefined ? null : JSON.parse(options.body);
    const safe = {
      scheme: parsed.protocol, hostname: parsed.hostname, endpoint: parsed.pathname,
      method: options.method, redirect: options.redirect ?? 'follow',
      hasPassword: body?.password === synthetic.password,
      hasRefresh: body?.refresh_token === synthetic.refresh,
      hasAccess: options.headers?.Authorization === `Bearer ${synthetic.access}`,
      hasRefreshedAccess: options.headers?.Authorization === `Bearer ${synthetic.refreshed}`,
    };
    calls.push(safe); // No credential values or full bodies enter observations.
    return respond(safe, options);
  };
  const context = vm.createContext({ URL, URLSearchParams, AbortController,
    setTimeout, clearTimeout, performance, fetch: fetchBoundary,
    location: { origin: new URL(base).origin, protocol: new URL(base).protocol } });
  const modules = new Map();
  function load(name) {
    if (modules.has(name)) return modules.get(name).exports;
    assert.ok(['api.ts', 'http.ts', 'config.ts'].includes(name));
    const file = path.join(sourceRoot, name);
    const source = fs.readFileSync(file, 'utf8');
    const output = ts.transpileModule(source, { fileName: file, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } });
    assert.equal((output.diagnostics ?? []).filter((item) => item.category === ts.DiagnosticCategory.Error).length, 0);
    const module = { exports: {} }; modules.set(name, module);
    const requireBoundary = (specifier) => {
      assert.ok(['./config', './http'].includes(specifier), 'Unexpected dependency; review before extending');
      return load(`${specifier.slice(2)}.ts`);
    };
    new vm.Script(`(function(require,module,exports){\n${output.outputText}\n})`, { filename: file })
      .runInContext(context, { timeout: 250 })(requireBoundary, module, module.exports);
    return module.exports;
  }
  return { ...load('api.ts'), ...load('http.ts'), calls };
}

async function refusedBeforeFetch(h, operation) {
  const result = await Promise.allSettled([operation()]);
  assert.equal(h.calls.length, 0, 'Credentials must be rejected before the fetch boundary');
  assert.equal(result[0].status, 'rejected', 'Insecure transport must not report success');
  const message = String(result[0].reason?.message ?? '');
  assert.equal(Object.values(synthetic).some((value) => message.includes(value)), false);
}

for (const base of ['http://fixture.invalid', 'http://192.0.2.10', 'http://localhost.fixture.invalid']) {
  test(`Admin login refuses remote cleartext: ${base}`, async () => {
    const h = harness(base);
    await refusedBeforeFetch(h, () => h.api.login('nfr05@example.invalid', synthetic.password));
  });
}

test('Admin authenticated read refuses remote HTTP before exposing its Bearer token', async () => {
  const h = harness('http://fixture.invalid'); h.setToken(synthetic.access);
  await refusedBeforeFetch(h, () => h.api.overview('2026-10-18', '2026-10-18'));
});

test('Admin create-user refuses remote HTTP before exposing either credential', async () => {
  const h = harness('http://fixture.invalid'); h.setToken(synthetic.access);
  await refusedBeforeFetch(h, () => h.api.createUser({ email: 'new@example.invalid', password: synthetic.password },
    '11111111-1111-4111-8111-111111111111'));
});

test('the actual shared request path refuses a remote cleartext refresh body', async () => {
  const h = harness('https://fixture.invalid');
  await refusedBeforeFetch(h, () => h.requestJson({ url: 'http://fixture.invalid/api/auth/refresh',
    method: 'POST', body: { refresh_token: synthetic.refresh } }));
});

for (const base of ['http://127.0.0.1:17863', 'http://localhost:17863', 'http://[::1]:17863', 'https://fixture.invalid']) {
  test(`normal login, refresh/replay and logout still work: ${base}`, async () => {
    const h = harness(base, (r) => {
      if (r.endpoint === '/api/auth/login') return json(pair());
      if (r.endpoint === '/api/auth/refresh') return json(pair(true));
      if (r.endpoint === '/api/auth/logout') return new Response(null, { status: 204 });
      if (r.endpoint === '/api/admin/stats/overview') return r.hasAccess
        ? json({ error: { code: 'AUTH_INVALID', message: 'Synthetic expiry' } }, 401)
        : json({ synthetic_ok: r.hasRefreshedAccess });
      throw new Error('Undeclared endpoint');
    });
    await h.api.login('nfr05@example.invalid', synthetic.password);
    assert.equal((await h.api.overview('2026-10-18', '2026-10-18')).synthetic_ok, true);
    await h.api.logout();
    assert.equal(h.calls.length, 5);
    assert.equal(h.calls[0].hasPassword, true);
    assert.equal(h.calls[0].hasAccess, false);
    assert.equal(h.calls[2].hasRefresh, true);
    assert.equal(h.calls[2].hasAccess, false);
    assert.equal(h.calls[4].hasRefreshedAccess, true);
  });
}

// A terminal Fetch-policy trap, not a browser/HTTP redirect implementation.
// It requires the selected smallest fix, redirect:'error', before any redirect.
// No destination fetch is made. The shared helper also protects 307/308 bodies.
for (const kind of ['login-password', 'create-user-password-and-access', 'refresh-body', 'bearer-read']) {
  test(`HTTPS ${kind} forbids redirect following at the Fetch boundary`, async () => {
    let wouldFollow = 0;
    const h = harness('https://fixture.invalid', (r) => {
      if (r.redirect === 'error') throw new TypeError('Synthetic redirect denied by fetch policy');
      wouldFollow++; // Safe boolean/count only; never contact a redirect target.
      return json(pair());
    });
    h.setToken(synthetic.access);
    const operation = kind === 'login-password'
      ? () => h.api.login('nfr05@example.invalid', synthetic.password)
      : kind === 'create-user-password-and-access'
        ? () => h.api.createUser({ email: 'new@example.invalid', password: synthetic.password },
          '11111111-1111-4111-8111-111111111111')
        : kind === 'refresh-body'
          ? () => h.requestJson({ url: 'https://fixture.invalid/api/auth/refresh', method: 'POST',
            body: { refresh_token: synthetic.refresh } })
          : () => h.api.overview('2026-10-18', '2026-10-18');
    const result = await Promise.allSettled([operation()]);
    assert.equal(h.calls.length, 1, 'Redirect failure must not retry or refresh');
    assert.equal(wouldFollow, 0, 'A credential-bearing request must not use default follow');
    assert.equal(result[0].status, 'rejected');
    assert.equal(result[0].reason?.code, 'NETWORK_ERROR');
  });
}

for (const [label, url] of [
  ['URL userinfo', 'https://synthetic-user:synthetic-password@fixture.invalid/api/auth/login'],
  ['URL fragment', 'https://fixture.invalid/api/auth/login#synthetic-fragment'],
  ['empty URL fragment', 'https://fixture.invalid/api/auth/login#'],
  ['malformed absolute URL', 'https://'],
]) {
  test(`${label} is rejected with a readable safe transport error before Fetch`, async () => {
    const h = harness('https://fixture.invalid');
    const result = await Promise.allSettled([h.requestJson({ url, method: 'POST',
      token: synthetic.access, body: { password: synthetic.password } })]);
    assert.equal(h.calls.length, 0);
    assert.equal(result[0].status, 'rejected');
    assert.equal(result[0].reason instanceof h.ApiError, true);
    assert.equal(result[0].reason?.code, 'INSECURE_TRANSPORT');
    assert.equal(result[0].reason?.status, 0);
    assert.equal(result[0].reason?.message, '服务地址必须使用 HTTPS（本机开发除外）');
  });
}

test('a normal HTTPS endpoint query is preserved and dispatched once', async () => {
  const h = harness('https://fixture.invalid');
  let calls = 0;
  const result = await h.requestJson({
    url: 'https://fixture.invalid/api/admin/users?q=synthetic%23term&limit=20', method: 'GET', token: synthetic.access,
    fetchImpl: async (url, options) => {
      calls++;
      assert.equal(new URL(url).searchParams.get('q'), 'synthetic#term');
      assert.equal(new URL(url).hash, '');
      assert.equal(options.redirect, 'error');
      assert.equal(options.headers.Authorization === `Bearer ${synthetic.access}`, true);
      return json({ items: [], next_cursor: null });
    },
  });
  assert.equal(calls, 1);
  assert.equal(result.items.length, 0);
  assert.equal(result.next_cursor, null);
});
