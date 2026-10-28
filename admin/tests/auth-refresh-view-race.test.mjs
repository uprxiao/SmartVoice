// Root-run only. Actual App/ModelsView/api/http/lib/date-range callbacks.
// React hook scheduling, JSX children and fetch are controlled; this is not DOM/GUI evidence.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceRoot = path.resolve(process.env.SVI_ADMIN_SOURCE_ROOT ?? path.join(root, 'src'));
const dependencyRoot = path.resolve(process.env.SVI_ADMIN_DEPENDENCY_ROOT ?? root);
const ts = createRequire(path.join(dependencyRoot, 'package.json'))('typescript');
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const denied = (status) => json({ error: { code: status === 401 ? 'AUTH_INVALID' : 'FORBIDDEN',
  message: 'synthetic authorization failure' } }, status);
const pair = (actor) => ({ access_token: 'synthetic-access-' + actor, refresh_token: 'synthetic-refresh-' + actor,
  token_type: 'Bearer', expires_in: 900, refresh_expires_at: '2026-11-17T00:00:00Z',
  user: { id: 'synthetic-' + actor, email: actor + '@example.invalid', display_name: actor,
    role: 'admin', status: 'active', locale: 'zh-CN', created_at: '2026-10-18T00:00:00Z', last_login_at: null } });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise((yes) => { resolve = yes; });
  return { promise, resolve };
}

function harness() {
  const frames = { app: { cells: [] }, models: { cells: [] } };
  let active, cursor;
  const effects = [];
  const react = {
    useState(initial) {
      const frame = active, index = cursor++;
      if (!(index in frame.cells)) frame.cells[index] = typeof initial === 'function' ? initial() : initial;
      return [frame.cells[index], (value) => {
        frame.cells[index] = typeof value === 'function' ? value(frame.cells[index]) : value;
      }];
    },
    useRef(initial) {
      const frame = active, index = cursor++;
      if (!(index in frame.cells)) frame.cells[index] = { current: initial };
      return frame.cells[index];
    },
    useCallback(callback) { cursor++; return callback; },
    useEffect(callback, deps) {
      const frame = active, index = cursor++, old = frame.cells[index];
      if (!old || deps.some((value, n) => value !== old.deps[n])) {
        const effect = { deps: [...deps], cleanup: null, effect: true };
        frame.cells[index] = effect;
        effects.push(() => { old?.cleanup?.(); effect.cleanup = callback(); });
      }
    },
  };
  const views = Object.fromEntries(['LoginView', 'OverviewView', 'UsersView', 'ProvidersView', 'CapacityView']
    .map((name) => [name, function ChildBoundary() {}]));
  const jsx = (type, props) => ({ type, props: props ?? {} });
  const gates = [deferred(), deferred()], requests = [], pending = [], errors = [];
  let aReads = 0, refreshes = 0;
  const fetchBoundary = async (url, options) => {
    const endpoint = new URL(url).pathname;
    const actor = ['A', 'B'].find((id) => options.headers.Authorization === 'Bearer synthetic-access-' + id) ?? null;
    requests.push({ endpoint, actor });
    if (endpoint === '/api/auth/login') {
      const body = JSON.parse(options.body);
      const id = ['A', 'B'].find((id) => body.email === id + '@example.invalid');
      assert.ok(id, 'Declared synthetic login only');
      assert.equal(options.headers.Authorization, undefined);
      return json(pair(id));
    }
    if (endpoint === '/api/auth/refresh') {
      refreshes++;
      assert.equal(options.headers.Authorization, undefined);
      return denied(401);
    }
    if (endpoint === '/api/auth/logout') return new Response(null, { status: 204 });
    if (endpoint === '/api/admin/stats/models') {
      if (actor === 'A' && aReads < 2) return gates[aReads++].promise;
      return actor === 'B' ? json({ timezone: 'Asia/Shanghai', items: [] }) : denied(401);
    }
    throw new Error('Unexpected controlled endpoint');
  };
  const context = vm.createContext({ window: { location: { hash: '#/models' }, addEventListener() {}, removeEventListener() {} },
    URL, URLSearchParams, AbortController, performance, setTimeout, clearTimeout, fetch: fetchBoundary });
  const modules = new Map();
  const allowed = new Set(['App.tsx', 'views/ModelsView.tsx', 'api.ts', 'http.ts', 'config.ts', 'lib.ts', 'date-range.ts']);
  function invoke(callback, ...args) {
    context.__callback = callback; context.__args = args;
    return new vm.Script('__callback(...__args)').runInContext(context, { timeout: 250 });
  }
  function load(name) {
    if (modules.has(name)) return modules.get(name).exports;
    assert.ok(allowed.has(name), 'Only declared production modules are loaded');
    const file = path.join(sourceRoot, name);
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), { fileName: file, reportDiagnostics: true,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } });
    assert.equal((output.diagnostics ?? []).filter((d) => d.category === ts.DiagnosticCategory.Error).length, 0);
    const module = { exports: {} }; modules.set(name, module);
    const boundary = (specifier) => {
      if (specifier === 'react') return react;
      if (specifier === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: Symbol.for('fragment') };
      if (specifier === 'lucide-react') return Object.fromEntries(
        ['Activity', 'Gauge', 'LogOut', 'ServerCog', 'SlidersHorizontal', 'Users', 'RefreshCw'].map((icon) => [icon, function Icon() {}]));
      if (specifier === './assets/logo.png') return 'synthetic-logo';
      if (specifier.startsWith('./views/') && specifier !== './views/ModelsView') {
        const child = specifier.slice('./views/'.length);
        assert.ok(views[child]); return { [child]: views[child] };
      }
      const relative = path.posix.normalize(path.posix.join(path.posix.dirname(name), specifier));
      return load(relative + (relative.endsWith('ModelsView') ? '.tsx' : '.ts'));
    };
    const factory = new vm.Script('(function(require,module,exports){\n' + output.outputText + '\n})', { filename: file })
      .runInContext(context, { timeout: 250 });
    invoke(factory, boundary, module, module.exports);
    return module.exports;
  }
  const { App } = load('App.tsx'), { ModelsView } = load('views/ModelsView.tsx'), { api } = load('api.ts');
  const models = api.models;
  api.models = (...args) => {
    const request = models(...args), index = pending.length;
    pending.push(request);
    void request.catch((error) => { errors[index] = error; });
    return request; // Observe the actual production promise, without replacing its outcome.
  };
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
  function render(frame, component, props) {
    active = frame; cursor = 0;
    const tree = invoke(component, props);
    while (effects.length) invoke(effects.shift());
    return tree;
  }
  const renderApp = () => render(frames.app, App);
  const child = (tree, type) => all(tree).find((node) => node.type === type);
  const login = async (actor) => {
    const tree = renderApp(), loginNode = child(tree, views.LoginView);
    assert.ok(loginNode, 'The real App must currently show LoginView');
    const response = await api.login(actor + '@example.invalid', 'Synthetic-only-password');
    invoke(loginNode.props.onLogin, response.user);
    return renderApp();
  };
  function twoReads(tree) {
    const model = child(tree, ModelsView);
    assert.ok(model);
    const first = render(frames.models, ModelsView, model.props);
    const dateInput = all(first).find((node) => node.type === 'input' && node.props.type === 'date');
    const date = new Date(dateInput.props.value + 'T00:00:00Z');
    date.setUTCDate(date.getUTCDate() - 1);
    invoke(dateInput.props.onChange, { target: { value: date.toISOString().slice(0, 10) } });
    render(frames.models, ModelsView, model.props); // Real effect cleanup + new filter load.
    assert.equal(pending.length, 2);
    return model.props.onError;
  }
  const settle = async (index, response) => {
    gates[index].resolve(response);
    await Promise.allSettled([pending[index]]);
    await flush();
  };
  function unmountModels() {
    for (const slot of frames.models.cells) if (slot?.effect) { slot.cleanup?.(); slot.cleanup = null; }
  }
  async function close() {
    gates.forEach((gate) => gate.resolve(json({ items: [] })));
    await Promise.allSettled(pending); await flush();
    for (const frame of Object.values(frames)) for (const slot of frame.cells) if (slot?.effect) slot.cleanup?.();
  }
  return { api, requests, pending, errors, login, twoReads, settle, renderApp, unmountModels, close, invoke,
    loginVisible: (tree) => Boolean(child(tree, views.LoginView)), texts,
    logoutButton: (tree) => all(tree).find((node) => node.type === 'button' && texts(node).includes('退出登录')),
    refreshes: () => refreshes };
}

for (const firstStatus of [401, 403]) {
  test('superseded ModelsView ' + firstStatus + ' failure immediately invalidates the real App even when its view discards it',
    { timeout: 4000 }, async () => {
      const h = harness();
      try {
        const tree = await h.login('A'); h.twoReads(tree);
        await h.settle(0, denied(firstStatus));
        assert.equal(h.errors[0]?.status, firstStatus);
        assert.equal(h.refreshes(), firstStatus === 401 ? 1 : 0);
        assert.equal(h.loginVisible(h.renderApp()), true, 'Auth failure must notify App before any current read settles');
        await h.settle(1, json({ timezone: 'Asia/Shanghai', items: [] }));
        const final = h.renderApp();
        assert.equal(h.loginVisible(final), true, 'The later response must not restore the cleared identity');
        assert.equal(h.texts(final).includes('A@example.invalid'), false);
        assert.equal(h.errors[1]?.code, 'AUTH_CONTEXT_CHANGED');
        assert.equal(h.requests.filter((r) => r.endpoint.endsWith('/models')).length, 2, 'No retry after auth failure');
      } finally { await h.close(); }
    });
}

test('terminal old-login failure cannot clear B after automatic invalidation and a new real App login',
  { timeout: 4000 }, async () => {
    const h = harness();
    try {
      const tree = await h.login('A'), oldOnError = h.twoReads(tree);
      await h.settle(0, denied(401));
      const oldFailure = h.errors[0];
      assert.equal(oldFailure?.status, 401);
      assert.equal(h.loginVisible(h.renderApp()), true, 'Auth notification must already show LoginView');
      h.unmountModels();
      await h.login('B');
      h.invoke(oldOnError, oldFailure); // Real callback, real returned ApiError, delayed until B is current.
      await h.settle(1, denied(403));
      assert.equal(h.errors[1]?.code, 'AUTH_CONTEXT_CHANGED');
      const final = h.renderApp();
      assert.equal(h.loginVisible(final), false);
      assert.equal(h.texts(final).includes('B@example.invalid'), true);
      await h.api.models('2026-10-17', '2026-10-17');
      assert.equal(h.requests.at(-1).actor, 'B');
      assert.equal(h.refreshes(), 1);
    } finally { await h.close(); }
  });

for (const firstStatus of [401, 403]) {
  test('completed current read cannot hide a later superseded ModelsView ' + firstStatus + ' failure',
    { timeout: 4000 }, async () => {
      const h = harness();
      try {
        const tree = await h.login('A'); h.twoReads(tree);
        await h.settle(1, json({ timezone: 'Asia/Shanghai', items: [] }));
        assert.equal(h.errors[1], undefined, 'The current read really succeeded before the old failure');
        assert.equal(h.loginVisible(h.renderApp()), false);
        await h.settle(0, denied(firstStatus));
        assert.equal(h.errors[0]?.status, firstStatus);
        assert.equal(h.refreshes(), firstStatus === 401 ? 1 : 0);
        const final = h.renderApp();
        assert.equal(h.loginVisible(final), true, 'No in-flight read remains; App must receive auth invalidation directly');
        assert.equal(h.texts(final).includes('A@example.invalid'), false);
        assert.equal(h.requests.filter((r) => r.endpoint.endsWith('/models')).length, 2);
      } finally { await h.close(); }
    });
}
