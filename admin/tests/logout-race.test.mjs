// Prepared only. Actual App callbacks and api/http execute when root runs this;
// React render/children and fetch are controlled boundaries, not a DOM test.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const adminRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceRoot = path.resolve(process.env.SVI_ADMIN_SOURCE_ROOT ?? path.join(adminRoot, 'src'));
const dependencyRoot = path.resolve(process.env.SVI_ADMIN_DEPENDENCY_ROOT ?? adminRoot);
const ts = createRequire(path.join(dependencyRoot, 'package.json'))('typescript');
const user = (name) => ({ id: `fixture-${name}`, email: `${name}@example.invalid`,
  display_name: name, role: 'admin', status: 'active', locale: 'zh-CN',
  created_at: '2026-10-18T00:00:00Z', last_login_at: null });
const pair = (name) => ({ access_token: `fixture-access-${name}`, refresh_token: `fixture-refresh-${name}`,
  token_type: 'Bearer', expires_in: 900, refresh_expires_at: '2026-11-17T00:00:00Z', user: user(name) });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  const cells = [];
  let cursor = 0;
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in cells)) cells[index] = typeof initial === 'function' ? initial() : initial;
      return [cells[index], (next) => { cells[index] = typeof next === 'function' ? next(cells[index]) : next; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in cells)) cells[index] = { current: initial };
      return cells[index];
    },
    useCallback(callback) { return callback; },
    useEffect() {}, // App hash listener is irrelevant to this callback ordering.
  };
  const views = Object.fromEntries(['LoginView', 'OverviewView', 'UsersView', 'ModelsView', 'ProvidersView', 'CapacityView']
    .map((name) => [name, function ChildBoundary() {}]));
  const jsx = (type, props) => ({ type, props: props ?? {} });
  const logout = deferred();
  const requests = [];
  const actorOf = (header) => header === 'Bearer fixture-access-A' ? 'A'
    : header === 'Bearer fixture-access-B' ? 'B' : 'none';
  let logoutStarted = false;
  const fetchBoundary = async (url, options) => {
    const endpoint = new URL(url).pathname;
    requests.push({ endpoint, method: options.method, actor: actorOf(options.headers.Authorization) });
    if (endpoint === '/api/auth/login') {
      const body = JSON.parse(options.body);
      const name = body.email === 'A@example.invalid' ? 'A' : body.email === 'B@example.invalid' ? 'B' : null;
      assert.ok(name, 'Only the two synthetic accounts may be used');
      assert.equal(body.client_type, 'admin_web');
      return new Response(JSON.stringify(pair(name)), { status: 200 });
    }
    if (endpoint === '/api/auth/logout') {
      assert.equal(options.method, 'POST');
      logoutStarted = true;
      return logout.promise;
    }
    if (endpoint === '/api/admin/stats/overview') {
      return new Response('{}', { status: 200 });
    }
    throw new Error('Unexpected endpoint in isolated callback experiment');
  };
  const context = vm.createContext({ window: { location: { hash: '' }, addEventListener() {}, removeEventListener() {} },
    URL, URLSearchParams, AbortController, performance, setTimeout, clearTimeout, fetch: fetchBoundary });
  const modules = new Map();
  const allowed = new Set(['App.tsx', 'api.ts', 'http.ts', 'config.ts']);
  // Prepare compiler work before any timed VM invocation, retaining fresh
  // module state and the existing callback/test deadlines for every case.
  const factories = new Map([...allowed].map((name) => {
    const file = path.join(sourceRoot, name);
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), { fileName: file, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } });
    assert.equal((output.diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error).length, 0,
      `Transpile diagnostics in ${name}`);
    return [name, new vm.Script(`(function(require,module,exports){\n${output.outputText}\n})`, { filename: file })];
  }));
  function invoke(callback, ...args) {
    context.__callback = callback;
    context.__args = args;
    return new vm.Script('__callback(...__args)').runInContext(context, { timeout: 250 });
  }
  function load(name) {
    if (modules.has(name)) return modules.get(name).exports;
    assert.ok(allowed.has(name), 'The loader only runs the four declared production modules');
    const module = { exports: {} };
    modules.set(name, module);
    const requireBoundary = (specifier) => {
      if (specifier === 'react') return react;
      if (specifier === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: Symbol.for('fragment') };
      if (specifier === 'lucide-react') return Object.fromEntries(['Activity', 'Gauge', 'LogOut', 'ServerCog', 'SlidersHorizontal', 'Users']
        .map((icon) => [icon, function IconBoundary() {}]));
      if (specifier === './assets/logo.png') return 'synthetic-logo';
      if (specifier.startsWith('./views/')) {
        const component = specifier.slice('./views/'.length);
        assert.ok(views[component], 'Unexpected child component');
        return { [component]: views[component] };
      }
      if (['./api', './http', './config'].includes(specifier)) return load(`${specifier.slice(2)}.ts`);
      throw new Error(`Unexpected module boundary: ${specifier}`);
    };
    const factory = factories.get(name).runInContext(context, { timeout: 250 });
    invoke(factory, requireBoundary, module, module.exports);
    return module.exports;
  }
  const { App } = load('App.tsx');
  const { api, ApiError } = load('api.ts');
  function render() { cursor = 0; return invoke(App); }
  function all(node) {
    if (Array.isArray(node)) return node.flatMap(all);
    if (!node || typeof node !== 'object') return [];
    return [node, ...all(node.props?.children)];
  }
  function texts(node) {
    if (Array.isArray(node)) return node.map(texts).join('');
    if (typeof node === 'string' || typeof node === 'number') return String(node);
    return node && typeof node === 'object' ? texts(node.props?.children) : '';
  }
  function child(tree, name) { return all(tree).find((node) => node.type === views[name]); }
  return { render, invoke, child, texts, api, ApiError, requests, logout,
    started: () => logoutStarted,
    logoutButton(tree) { return all(tree).find((node) => node.type === 'button' && texts(node).includes('退出登录')); },
  };
}

for (const outcome of ['204', 'network-failure']) {
  test(`late A logout ${outcome} cannot clear B identity`, { timeout: 4000 }, async () => {
    const h = harness();
    let oldClick;
    try {
      let tree = h.render();
      const a = await h.api.login('A@example.invalid', 'Synthetic-only-password-1');
      h.invoke(h.child(tree, 'LoginView').props.onLogin, a.user);
      tree = h.render();
      const onErrorA = h.child(tree, 'OverviewView').props.onError;
      oldClick = h.invoke(h.logoutButton(tree).props.onClick);
      assert.ok(h.started(), 'The real API must start logout before local identity changes');
      assert.equal(h.requests.find((r) => r.endpoint === '/api/auth/logout').actor, 'A');

      // A's separate read is rejected after logout revokes its SID but before
      // the delayed logout response arrives. Invoke the actual App callback.
      h.invoke(onErrorA, new h.ApiError(401, 'AUTH_INVALID', 'synthetic expired login'));
      tree = h.render();
      assert.ok(h.child(tree, 'LoginView'));
      const b = await h.api.login('B@example.invalid', 'Synthetic-only-password-2');
      h.invoke(h.child(tree, 'LoginView').props.onLogin, b.user);
      tree = h.render();
      assert.ok(h.texts(tree).includes('B@example.invalid'));

      if (outcome === '204') h.logout.resolve(new Response(null, { status: 204 }));
      else h.logout.reject(new TypeError('controlled synthetic network failure'));
      await oldClick;
      await new Promise((resolve) => setImmediate(resolve));
      tree = h.render();
      // Observe both the real API token closure and the real App user state.
      await h.api.overview('2026-10-16', '2026-10-16');
      const lastRead = h.requests.at(-1);
      assert.deepEqual({ loginVisible: Boolean(h.child(tree, 'LoginView')),
        userBVisible: h.texts(tree).includes('B@example.invalid'), requestActor: lastRead.actor },
      { loginVisible: false, userBVisible: true, requestActor: 'B' });
    } finally {
      h.logout.resolve(new Response(null, { status: 204 }));
      await Promise.resolve(oldClick).catch(() => undefined);
      await new Promise((resolve) => setImmediate(resolve));
    }
  });
}
