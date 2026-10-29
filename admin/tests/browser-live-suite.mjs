// Explicit source-driven UI adapter for the owned server/tests/admin_browser_environment.py fixture.
// Copy into a frozen bundle with fixtures.json; credentials stay in the private environment file.
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);
const bundle = dirname(fileURLToPath(import.meta.url));
let fixture, environment, credentials, environmentPath, python, controller;
let disposeNetwork;
const statuses = new Map();
const statsExpression = `Object.fromEntries([...document.querySelectorAll('.stat-label')].map(e=>[e.textContent.trim(),e.parentElement.querySelector('.stat-value')?.textContent.trim()]))`;
const usageExpression = `(()=>{const matches=[...document.querySelectorAll('table')].filter(t=>[...t.querySelectorAll('thead th')].map(e=>e.textContent.trim()).join('|')==='日期|用户|会话数|有结果|接收总时长|成功口述时长|口述字数');if(matches.length!==1)return null;return [...matches[0].querySelectorAll('tbody tr')].map(r=>[...r.cells].map(e=>e.textContent.trim()));})()`;

async function button(ctx, text, scope = 'body') {
  const selector = await ctx.evaluate(`(()=>{const roots=document.querySelectorAll(${JSON.stringify(scope)});if(roots.length!==1)throw Error('Button scope is ambiguous');const nodes=[...roots[0].querySelectorAll('button')].filter(e=>e.textContent.trim()===${JSON.stringify(text)});if(nodes.length!==1)throw Error('Expected one exact button, found '+nodes.length);let e=nodes[0],p=[];while(e&&e.nodeType===1){if(e.id){p.unshift('#'+CSS.escape(e.id));break;}const t=e.tagName.toLowerCase();const n=[...e.parentElement?.children||[]].filter(x=>x.tagName===e.tagName).indexOf(e)+1;p.unshift(t+':nth-of-type('+n+')');e=e.parentElement;}return p.join(' > ');})()`);
  return ctx.click(selector);
}

async function login(ctx, email) {
  await ctx.fill('#email', email);
  await ctx.fill('#password', credentials.password, { sensitive: true });
  const response = await ctx.responseFor(() => ctx.click('form.login-card button[type=submit]'), {
    matchesRequest: request => request.method === 'POST' && new URL(request.url).pathname === '/api/auth/login',
  });
  return { status: statuses.get(response.requestId), body: JSON.parse(response.body) };
}

async function overview(ctx, { reset = false } = {}) {
  if (await ctx.evaluate("Boolean(document.querySelector('#email'))")) {
    const response = await login(ctx, fixture.admin_email);
    if (response.status !== 200) throw new Error('Fixture admin login did not succeed');
    await ctx.waitFor("Boolean(document.querySelector('nav'))");
  }
  if (reset) await button(ctx, '用户管理', 'nav');
  await button(ctx, '概览', 'nav');
  await ctx.waitFor(`(${statsExpression})['会话数']==='3'&&(${statsExpression})['已插入']==='1'`);
}

async function nativeKey(ctx, key, code, windowsVirtualKeyCode) {
  for (const type of ['keyDown', 'keyUp']) await ctx.cdp('Input.dispatchKeyEvent', { type, key, code, windowsVirtualKeyCode });
}

async function selectFirstUser(ctx) {
  // Tab into the native select without opening its separate popup surface.
  await ctx.click('[aria-label="搜索统计用户"]');
  for (let n = 0; n < 5; n++) {
    await ctx.key('Tab', 'Tab');
    if (await ctx.evaluate("document.activeElement===document.querySelector('select[aria-label=\"使用统计用户\"]')")) break;
  }
  await ctx.waitFor("document.activeElement===document.querySelector('select[aria-label=\"使用统计用户\"]')", 1000);
  const matches = await ctx.evaluate("[...document.querySelector('select[aria-label=\"使用统计用户\"]').options].filter(o=>o.textContent.toLowerCase().startsWith('w')).length");
  if (matches !== 1) throw new Error('Type-ahead option is not unique');
  // The headless Mac native widget ignores arrow navigation. Real character
  // type-ahead selects the sole searched fixture user and emits its change.
  for (const type of ['keyDown', 'keyUp']) await ctx.cdp('Input.dispatchKeyEvent', {
    type, key: 'w', code: 'KeyW', windowsVirtualKeyCode: 87,
    ...(type === 'keyDown' ? { text: 'w' } : {}),
  });
  await ctx.key('Tab', 'Tab');
}

async function editDate(ctx, selector, value) {
  await ctx.click(selector); // Scroll the real control into view before measuring its native fields.
  const { root } = await ctx.cdp('DOM.getDocument', { depth: 0 });
  const { nodeId } = await ctx.cdp('DOM.querySelector', { nodeId: root.nodeId, selector });
  const { node } = await ctx.cdp('DOM.describeNode', { nodeId, depth: -1, pierce: true });
  const fields = new Map();
  function visit(n) {
    const attrs = Object.fromEntries(Array.from({ length: (n.attributes?.length || 0) / 2 }, (_, i) => n.attributes.slice(i * 2, i * 2 + 2)));
    for (const part of ['year', 'month', 'day']) if (attrs.pseudo === `-webkit-datetime-edit-${part}-field`) fields.set(part, n.backendNodeId);
    for (const child of [...n.children || [], ...n.shadowRoots || []]) visit(child);
  }
  visit(node);
  if (fields.size !== 3) throw new Error('Native date segments unavailable; no DOM value substitution');
  const values = Object.fromEntries(['year', 'month', 'day'].map((part, i) => [part, value.split('-')[i]]));
  for (const part of ['year', 'month', 'day']) {
    const { model } = await ctx.cdp('DOM.getBoxModel', { backendNodeId: fields.get(part) });
    const q = model.border, x = (q[0] + q[2] + q[4] + q[6]) / 4, y = (q[1] + q[3] + q[5] + q[7]) / 4;
    for (const type of ['mousePressed', 'mouseReleased']) await ctx.cdp('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1 });
    for (const digit of values[part]) {
      for (const type of ['keyDown', 'keyUp']) await ctx.cdp('Input.dispatchKeyEvent', {
        type, key: digit, code: `Digit${digit}`, text: type === 'keyDown' ? digit : undefined,
        windowsVirtualKeyCode: digit.charCodeAt(0),
      });
    }
    ctx.step('native-date-segment', { part, actual: await ctx.evaluate(`document.querySelector(${JSON.stringify(selector)}).value`) });
  }
  await ctx.key('Tab', 'Tab');
  await ctx.waitFor(`document.querySelector(${JSON.stringify(selector)}).value===${JSON.stringify(value)}`);
}

async function control(action) {
  const { stdout } = await run(python, [controller, action, '--environment', environmentPath], { timeout: 30000 });
  return JSON.parse(stdout.trim());
}

async function responseStatusFor(ctx, action, pathname) {
  let candidate, timer, resolveResult, rejectResult;
  const promise = new Promise((resolve, reject) => { resolveResult = resolve; rejectResult = reject; });
  const offRequest = ctx.on('Network.requestWillBeSent', p => {
    if (!candidate && p.request.method === 'POST' && new URL(p.request.url).pathname === pathname) candidate = p.requestId;
  });
  const offResponse = ctx.on('Network.responseReceived', p => {
    if (candidate === p.requestId) resolveResult(p.response.status);
  });
  timer = setTimeout(() => rejectResult(new Error('No response status for the action request')), 10000);
  promise.catch(() => {});
  try { await action(); return await promise; }
  finally { clearTimeout(timer); offRequest(); offResponse(); }
}

export async function setup(ctx) {
  fixture = JSON.parse(await readFile(resolve(bundle, 'fixtures.json'), 'utf8'));
  environmentPath = process.env.SVI_ADMIN_UI_ENVIRONMENT;
  if (!environmentPath || !process.env.SVI_ADMIN_UI_PYTHON) throw new Error('Explicit owned fixture environment is required');
  environment = JSON.parse(await readFile(environmentPath, 'utf8'));
  if (environment.schema !== 'svi-admin-browser-environment-v1' || environment.origin !== fixture.origin) throw new Error('Fixture environment mismatch');
  credentials = JSON.parse(await readFile(environment.private_file, 'utf8'));
  python = process.env.SVI_ADMIN_UI_PYTHON;
  controller = resolve(environment.source_root, 'server/tests/admin_browser_environment.py');
  disposeNetwork = ctx.on('Network.responseReceived', p => statuses.set(p.requestId, p.response.status));
  await ctx.cdp('Network.enable');
  await ctx.cdp('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await ctx.waitFor("document.readyState==='complete'&&Boolean(document.querySelector('#email'))");
}

export async function runCase(ctx, test) {
  if (test.id === 'WIN35-01') {
    const response = await login(ctx, fixture.users[0].email);
    await ctx.waitFor("Boolean(document.querySelector('[role=alert]'))");
    ctx.check(response.status === 403 && response.body.error?.code === 'FORBIDDEN', 'Real service rejects ordinary admin_web login', { actual: { status: response.status, code: response.body.error?.code } });
    ctx.check(await ctx.evaluate("Boolean(document.querySelector('#email'))&&!document.querySelector('nav')"), 'No admin rows or navigation are shown');
    return { status: 'passed', actual: { http_status: response.status, admin_visible: false } };
  }
  if (test.id === 'WIN35-02') {
    await overview(ctx);
    const actual = await ctx.evaluate(statsExpression);
    const expected = { '注册用户': '3', '启用账号': '3', '区间活跃用户': '2', '会话数': '3', '整理字数': '42', '结果会话': '3', '失败会话': '0', '已插入': '1', '插入成功率': '33.3%', '学习事件': '0', '接收总时长': '3 秒', '成功口述时长': '3 秒' };
    ctx.check(Object.entries(expected).every(([key, value]) => actual[key] === value), 'Rendered totals match frozen session plan and independent database snapshot', { expected, actual });
    await ctx.screenshot('overview');
    return { status: 'passed', actual };
  }
  if (test.id === 'WIN35-03') {
    await overview(ctx, { reset: true });
    await ctx.waitFor(`(${usageExpression})?.filter(r=>r.length===7).length===2`);
    const rows = await ctx.evaluate(usageExpression);
    for (const [i, user] of fixture.users.entries()) {
      const row = rows.find(row => row[1] === user.email);
      const count = i === 0 ? '2' : '1';
      ctx.check(row?.[2] === count && row?.[3] === count && row?.[4] === `${count} 秒`, 'Per-user rows are independent and deduplicated', { expected: { email: user.email, sessions: count }, actual: row });
    }
    await ctx.fill('[aria-label="搜索统计用户"]', fixture.users[0].email);
    await button(ctx, '查找用户', 'form.usage-toolbar');
    await ctx.waitFor(`document.querySelector('select[aria-label="使用统计用户"]').options.length===2`);
    await selectFirstUser(ctx);
    await ctx.waitFor(`document.querySelector('select[aria-label="使用统计用户"]').value===${JSON.stringify(fixture.users[0].id)}&&(${usageExpression})?.filter(r=>r.length===7).length===1`);
    const filtered = await ctx.evaluate(usageExpression);
    ctx.check(filtered.length === 1 && filtered[0][1] === fixture.users[0].email && filtered[0][2] === '2', 'UI filter reaches the selected real user only', { actual: filtered });
    ctx.check((await ctx.evaluate(statsExpression))['会话数'] === '3', 'Local usage filter does not alter global overview');
    return { status: 'passed', actual: { rows, filtered } };
  }
  if (test.id === 'WIN35-04') {
    if (!await ctx.evaluate(`document.querySelector('select[aria-label="使用统计用户"]')?.value===${JSON.stringify(fixture.users[0].id)}`)) {
      return { status: 'blocked', reason: 'WIN35-03 did not establish the selected-user precondition' };
    }
    await ctx.fill('[aria-label="搜索统计用户"]', 'absent-user@example.test');
    await button(ctx, '查找用户', 'form.usage-toolbar');
    await ctx.waitFor("document.body.textContent.includes('没有匹配的用户')&&!document.body.textContent.includes('正在读取可选用户')");
    const rows = await ctx.evaluate(usageExpression);
    ctx.check(rows.length === 1 && rows[0][1] === fixture.users[0].email && rows[0][2] === '2', 'Empty option search does not fabricate users or silently change the selected usage filter', { actual: rows });
    return { status: 'passed', actual: { empty_user_search: true, retained_usage_rows: rows.length } };
  }
  if (test.id === 'WIN35-09') {
    await overview(ctx, { reset: true });
    await editDate(ctx, '[aria-label="统计开始日期"]', '2001-01-01');
    await editDate(ctx, '[aria-label="统计结束日期"]', '2001-01-01');
    await ctx.waitFor(`(${statsExpression})['会话数']==='0'&&document.body.textContent.includes('所选日期和用户没有使用记录')`);
    const actual = await ctx.evaluate(statsExpression);
    ctx.check(actual['已插入'] === '0' && actual['结果会话'] === '0' && actual['插入成功率'] === '0.0%', 'Genuine empty date range is a successful zero result', { actual });
    await overview(ctx, { reset: true });
    return { status: 'passed', actual: { empty_date: '2001-01-01', totals: actual } };
  }
  if (test.id === 'WIN35-05') {
    await overview(ctx, { reset: true });
    await button(ctx, '用户管理', 'nav');
    await ctx.waitFor("document.querySelectorAll('table tbody tr').length===3");
    const text = await ctx.evaluate("document.querySelector('table').textContent");
    const visibleEmails = [fixture.admin_email, ...fixture.users.map(u => u.email)].filter(email => text.includes(email));
    ctx.check(visibleEmails.length === 3, 'Real users page contains exactly the isolated fixture accounts', { actual: { visibleEmails, rows: await ctx.evaluate("document.querySelectorAll('table tbody tr').length") } });
    return { status: 'passed', actual: { user_rows: 3 } };
  }
  if (test.id === 'WIN35-06') {
    await overview(ctx, { reset: true });
    let paused = false;
    try {
      const stopped = await control('pause'); paused = true;
      ctx.check(stopped.server_stopped === true, 'Owned real service is confirmed stopped before the request');
      await button(ctx, '刷新概览');
      await ctx.waitFor(`(${statsExpression})['会话数']==='—'&&document.querySelectorAll('.error-box').length>0`);
      const offline = await ctx.evaluate(statsExpression);
      ctx.check(offline['已插入'] === '—' && offline['结果会话'] === '—', 'HTTP transport failure shows an error and unavailable values, not successful zeros', { actual: offline });
      await ctx.screenshot('service-offline');
    } finally { if (paused) await control('resume'); }
    await button(ctx, '刷新概览');
    await ctx.waitFor(`(${statsExpression})['会话数']==='3'&&(${statsExpression})['已插入']==='1'`);
    ctx.check((await ctx.evaluate(statsExpression))['插入成功率'] === '33.3%', 'Real server recovery preserves database counts and session identity');
    return { status: 'passed', actual: { offline_not_zero: true, recovered_sessions: 3, inserted_sessions: 1 } };
  }
  if (test.id === 'WIN35-07') {
    await ctx.reload("Boolean(document.querySelector('#email'))");
    const state = await ctx.evaluate("({login:!!document.querySelector('#email'),nav:!!document.querySelector('nav'),localKeys:Object.keys(localStorage),sessionKeys:Object.keys(sessionStorage),cookieCount:document.cookie?document.cookie.split(';').length:0})");
    ctx.check(state.login && !state.nav && state.localKeys.length === 0 && state.sessionKeys.length === 0 && state.cookieCount === 0, 'Hard reload requires login; tokens are not persisted', { actual: state });
    await overview(ctx);
    ctx.check((await ctx.evaluate(statsExpression))['会话数'] === '3', 'Relogin and reads do not duplicate business statistics');
    return { status: 'passed', actual: { login_required_after_reload: true, persistent_auth: false, sessions_after_relogin: 3 } };
  }
  if (test.id === 'WIN35-08') {
    if (await ctx.evaluate("Boolean(document.querySelector('#email'))")) await overview(ctx);
    // A 204 has no body. Observe this request's actual response headers instead
    // of requiring a response-body loading event that Chromium may abort.
    const status = await responseStatusFor(ctx, () => button(ctx, '退出登录'), '/api/auth/logout');
    await ctx.waitFor("Boolean(document.querySelector('#email'))&&!document.querySelector('nav')");
    ctx.check(status === 204, 'Logout reaches the real service and receives 204', { actual: { status } });
    return { status: 'passed', actual: { logout_status: status, admin_visible: false } };
  }
  throw new Error('Unimplemented case: ' + test.id);
}

export async function teardown(ctx) {
  if (await ctx.evaluate("Boolean(document.querySelector('nav'))")) {
    await button(ctx, '退出登录');
    await ctx.waitFor("Boolean(document.querySelector('#email'))");
  }
  disposeNetwork?.();
  credentials = null;
  statuses.clear();
}
