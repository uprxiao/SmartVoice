const fs = require('node:fs');
const path = require('node:path');
const { fileURLToPath } = require('node:url');
const { spawn, execFileSync } = require('node:child_process');
const { createInterface } = require('node:readline');
const WebSocket = require('ws');

const phases = {
  'normal-configure': ['NORMAL-IDENTITY', 'NORMAL-SHORTCUT', 'NORMAL-STARTUP'],
  'normal-quit': ['NORMAL-QUIT'],
  exercise: ['SIM-IDENTITY', 'SIM-LOGIN', 'SIM-HUD', 'SIM-NOTE-CARET', 'SIM-NOTE-SELECTION', 'SIM-NOTE-EMPTY', 'SIM-LATEST-TARGET', 'SIM-REPEAT', 'SIM-HUD-CANCEL', 'SIM-ESCAPE-CANCEL', ...['INPUT', 'TEXTAREA'].flatMap(type => ['CARET', 'SELECTION', 'EMPTY'].map(kind => `SIM-CHROME-${type}-${kind}`)), 'SIM-HISTORY-STATS', 'SIM-STARTUP', 'SIM-NO-AUDIO', 'SIM-QUIT'],
  resume: ['SIM-SAVED-ACCOUNT', 'SIM-NO-REPLAY', 'SIM-RESTART-NO-AUDIO', 'SIM-RESTART-QUIT'],
};
if (process.argv[2] === '--plan') { process.stdout.write(JSON.stringify(phases)); process.exit(0); }
const [manifestFile, portArgument, phase, output] = process.argv.slice(2);
if (process.env.GITHUB_ACTIONS !== 'true' || process.platform !== 'win32' || !phases[phase] || !/^\d{1,5}$/.test(portArgument || '')) throw Error('Owned Windows CI phase required');
const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
const inside = (file, directory) => { const relative = path.relative(path.resolve(directory), path.resolve(file)); return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative); };
if (manifest.schema !== 'svi-simulation-smoke-owner-v1' || !inside(manifestFile, process.env.RUNNER_TEMP) || !inside(output, manifest.evidence_dir) || fs.existsSync(output)) throw Error('Fresh owned manifest/output required');
const text = '这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。';
const email = 'tester@example.test', password = 'Simulation123!';
const report = { schema: 'svi-simulation-ui-v1', phase, status: 'not_run', scope: 'installed product, real OS global shortcut and production insertion chain; no physical keyboard/audio/IME/WeChat/backend/model/S5 acceptance', cases: phases[phase].map(id => ({ id, status: 'not_run', required: true })) };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const check = (value, message) => { if (!value) throw Error(message); };
const safeError = error => {
  let message = String(error.message || error).replaceAll(password, '[fixture-password]').replaceAll(email, '[fixture-account]').replaceAll(manifest.nonce, '[owner-nonce]').replace(/eyJ[A-Za-z0-9_.-]+/g, '[token]');
  for (const prefix of [manifest.evidence_dir, process.env.RUNNER_TEMP, process.env.USERPROFILE].filter(Boolean)) message = message.replaceAll(prefix, '[owned-directory]');
  return message;
};
const owned = role => { const rows = manifest.targets.filter(row => row.role === role); check(rows.length === 1, `Exactly one owned ${role} required`); return rows[0]; };
const mainTarget = owned(phase.startsWith('normal-') ? 'normal-main' : 'main');
let observer, observerStopped, observerSequence = 0, main, hud, chrome;
const observerPending = new Map(), connections = [];
const successful = [];
const noteState = new Map();
const noteCarets = new Map();
const noteRoles = ['note-a', 'note-b', 'note-empty'];
const noteInitial = ['Alpha React Omega', 'Second Docker Target', ''];
for (let i = 0; i < noteRoles.length; i++) {
  const target = manifest.targets.find(row => row.name === noteRoles[i]);
  if (target) noteState.set(target.pid, noteInitial[i]);
}

class Page {
  constructor(socket) {
    this.socket = socket; this.sequence = 0; this.pending = new Map();
    socket.on('message', bytes => {
      const message = JSON.parse(bytes.toString());
      const request = this.pending.get(message.id);
      if (!request) return;
      this.pending.delete(message.id); clearTimeout(request.timer);
      message.error ? request.reject(Error(message.error.message)) : request.resolve(message.result);
    });
    socket.on('close', () => { for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(Error('Owned page closed')); } this.pending.clear(); });
  }
  call(method, params = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(Error('CDP observation deadline exceeded')); }, 10000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async value(expression) {
    const response = await this.call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (response.exceptionDetails) throw Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  }
}
async function connect(port, matches) {
  let target;
  const end = Date.now() + 25000;
  while (Date.now() < end) {
    try { const rows = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).filter(row => row.type === 'page' && matches(row.url)); check(rows.length <= 1, 'Ambiguous installed renderer'); target = rows[0]; if (target) break; } catch (error) { if (error.message.includes('Ambiguous')) throw error; }
    await pause(100);
  }
  check(target, 'Exact installed renderer was not exposed');
  const address = new URL(target.webSocketDebuggerUrl);
  check(address.protocol === 'ws:' && ['127.0.0.1', 'localhost'].includes(address.hostname) && address.port === String(port), 'CDP endpoint escaped owned loopback listener');
  const socket = new WebSocket(address);
  await new Promise((resolve, reject) => { socket.once('open', resolve); socket.once('error', reject); });
  const page = new Page(socket); connections.push(page);
  await page.call('Runtime.enable'); await page.call('Page.enable');
  await wait(async () => await page.value(`document.readyState === 'complete'`), 'Owned renderer did not become ready');
  return page;
}
function installedPage(url, name) {
  try {
    const parsed = new URL(url);
    const file = fileURLToPath(parsed);
    return parsed.protocol === 'file:' && !parsed.search && !parsed.hash && inside(file, path.join(mainTarget.install_dir, 'resources', 'app.asar')) && file.replaceAll('\\', '/').endsWith('/renderer/' + name);
  } catch { return false; }
}
async function wait(observe, message, milliseconds = 15000) {
  const end = Date.now() + milliseconds;
  while (Date.now() < end) { if (await observe()) return; await pause(60); }
  throw Error(message);
}
async function run(id, body, dependency) {
  const row = report.cases.find(item => item.id === id); check(row, 'Unplanned smoke case');
  if (dependency && !report.cases.some(item => item.id === dependency && item.status === 'pass')) { row.reason = 'Prerequisite did not pass'; return; }
  row.started_at = new Date().toISOString();
  try { row.observations = await body(); row.status = 'pass'; }
  catch (error) { row.status = 'fail'; row.error = safeError(error); }
  row.ended_at = new Date().toISOString();
}
async function rpc(channel, ...args) {
  check(['app.info', 'app.updateAutoStart', 'app.updateShortcut', 'session.status', 'history.list', 'stats.me', 'auth.me'].includes(channel), 'Business action outside the public observation/settings allowlist');
  return main.value(`window.electronAPI.invoke(${JSON.stringify(channel)}, ...${JSON.stringify(args)})`);
}
async function click(page, selector) {
  const point = await page.value(`(() => { const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})].filter(element => element.getClientRects().length); if (nodes.length !== 1 || nodes[0].disabled) throw Error('Unique enabled UI control required'); const element = nodes[0]; element.scrollIntoView({block:'center'}); const rectangle = element.getBoundingClientRect(); const x = rectangle.x + rectangle.width / 2, y = rectangle.y + rectangle.height / 2; const hit = document.elementFromPoint(x,y); if (hit !== element && !element.contains(hit)) throw Error('UI control is occluded'); return {x,y}; })()`);
  for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) await page.call('Input.dispatchMouseEvent', { type, ...point, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: type === 'mouseMoved' ? 0 : 1 });
}
async function button(page, label, scope = 'body') {
  const selector = await page.value(`(() => { const nodes = [...document.querySelectorAll(${JSON.stringify(scope + ' button')})].filter(element => element.textContent.trim() === ${JSON.stringify(label)} && element.getClientRects().length); if (nodes.length !== 1) throw Error('Exact UI button required'); let element = nodes[0]; const parts = []; while (element && element.tagName !== 'HTML') { parts.unshift(element.tagName.toLowerCase() + ':nth-of-type(' + ([...element.parentElement.children].filter(item => item.tagName === element.tagName).indexOf(element) + 1) + ')'); element = element.parentElement; } return parts.join(' > '); })()`);
  await click(page, selector);
}
async function fill(selector, value) {
  await click(main, selector);
  for (const type of ['keyDown', 'keyUp']) await main.call('Input.dispatchKeyEvent', { type, key: 'a', code: 'KeyA', windowsVirtualKeyCode: 65, modifiers: 2 });
  await main.call('Input.insertText', { text: value });
}
async function startObserver() {
  observer = spawn(manifest.probe_exe, [], { windowsHide: true, env: { ...process.env, SVI_WINDOWS_PROBE: '1' }, stdio: ['pipe', 'pipe', 'ignore'] });
  const rejectPending = () => { for (const request of observerPending.values()) { clearTimeout(request.timer); request.reject(Error('Independent Windows observer closed')); } observerPending.clear(); };
  observerStopped = new Promise(resolve => observer.once('close', (code, signal) => { rejectPending(); resolve({ code, signal }); }));
  observer.on('error', rejectPending); observer.stdin.on('error', rejectPending);
  createInterface({ input: observer.stdout }).on('line', line => {
    let message; try { message = JSON.parse(line); } catch { return; }
    const request = observerPending.get(message.id); if (!request) return;
    observerPending.delete(message.id); clearTimeout(request.timer);
    message.error ? request.reject(Error('Independent OS preparation/readback failed')) : request.resolve(message);
  });
  check((await observe('ping')).integrity_rid === 8192, 'Independent observer is not Medium');
}
async function observe(action, args = {}) {
  check(['ping', 'foreground', 'probeFocus', 'probeSelect', 'capture'].includes(action), 'Observer cannot perform product insertion or shortcut actions');
  const request = { id: ++observerSequence, action, ...args };
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { observerPending.delete(request.id); reject(Error('Independent Windows observation timed out')); }, 7000);
    observerPending.set(request.id, { resolve, reject, timer }); observer.stdin.write(JSON.stringify(request) + '\n');
  });
}
async function focus(target) {
  check((await observe('probeFocus', { fixture_pid: target.pid })).focused, 'Owned external target did not become foreground');
  const facts = await observe('foreground'); check(facts.root?.pid === target.pid, 'Owned foreground PID differs'); return facts.root;
}
async function focusMain() {
  // The same PID owns a non-activating HUD, so PID-only activation cannot select Main.
  await observe('probeFocus', { fixture_pid: mainTarget.pid }); await main.call('Page.bringToFront');
  let result;
  const title = await main.value('document.title');
  await wait(async () => { const facts = await observe('foreground'); result = facts.root; return result?.pid === mainTarget.pid && result.title === title; }, 'Exact Main root did not become foreground');
  return result;
}
function inject(target, foreground, action) {
  const bytes = execFileSync(manifest.injector_exe, ['--manifest', manifestFile, '--nonce', manifest.nonce, '--pid', String(target.pid), '--hwnd', String(foreground.hwnd), '--action', action], { windowsHide: true, timeout: 10000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  const facts = JSON.parse(bytes); check(facts.sent && facts.owned_foreground_verified && facts.integrity_rid === 8192, 'Independent OS injector did not confirm the fixed chord');
  return facts;
}
async function shortcut(target) {
  const facts = await observe('foreground'); check(facts.root?.pid === target.pid, 'Refusing a shortcut to a different foreground target'); return inject(target, facts.root, 'toggle');
}
async function nativeText(target, expected, caret, selection = 0) {
  const facts = (await observe('capture', { fixture_pid: target.pid })).readback;
  check(facts?.text === expected && facts.caret_utf16 === caret && facts.selection_utf16_len === selection, 'External full text, UTF-16 caret or selection differs');
  if (noteState.has(target.pid)) noteCarets.set(target.pid, { caret, selection });
  return { exact_text: true, exact_utf16_caret: true, exact_selection: true, text_utf16_length: expected.length, no_enter_or_duplicate: true };
}
async function prepareNote(name, start, length = 0) {
  const target = manifest.targets.find(item => item.name === name); check(target, 'Owned Notepad fixture unavailable');
  await focus(target); await observe('probeSelect', { fixture_pid: target.pid, start, length });
  await nativeText(target, noteState.get(target.pid), start, length); return target;
}
async function begin(target) {
  await shortcut(target);
  let status;
  await wait(async () => { status = await rpc('session.status'); return status.state === 'streaming' && status.capture_ready === true && status.acknowledged_samples > 0; }, 'Real global shortcut did not reach recording with a real protocol ACK');
  await wait(async () => await hud.value(`Boolean(document.querySelector('.hud-pill[data-phase="recording"]'))`), 'Original recording HUD did not appear');
  const foreground = (await observe('foreground')).root; check(foreground?.pid === target.pid, 'Recording HUD stole external foreground');
  return status.session_id;
}
async function finish(target, sessionId) {
  await shortcut(target);
  let result;
  await wait(async () => { result = (await rpc('history.list', null, 100)).items.find(row => row.session_id === sessionId); return result?.state === 'ready' && result.insertion_status !== null && result.insertion_status !== 'reserved'; }, 'Actual result/receipt did not reach a terminal insertion state');
  check(result.insertion_status === 'inserted' && result.result?.refined_text === text, 'Production insertion did not return an exact inserted receipt');
  check(!successful.includes(sessionId), 'A single session was counted twice'); successful.push(sessionId);
  return result;
}
async function scenario(name, start, length = 0) {
  const target = await prepareNote(name, start, length), before = noteState.get(target.pid);
  const sessionId = await begin(target); await finish(target, sessionId);
  const expected = before.slice(0, start) + text + before.slice(start + length);
  const result = await nativeText(target, expected, start + text.length);
  await pause(350); await nativeText(target, expected, start + text.length);
  noteState.set(target.pid, expected); return { ...result, inserted_receipt: true, fixed_text_once: true, global_shortcut_os_sendinput: true };
}
const traps = `(() => {
  if (window.__sviHardwareTrap) return;
  const counts = {}, installed = [];
  const trap = (owner, key, label) => { if (!owner || typeof owner[key] !== 'function') return; counts[label] = 0; Object.defineProperty(owner, key, {configurable:true,value:function(){ counts[label]++; throw Error('Simulation reached forbidden hardware API'); }}); installed.push(label); };
  trap(navigator.mediaDevices,'getUserMedia','getUserMedia');
  trap(navigator,'getUserMedia','legacyGetUserMedia'); trap(navigator,'webkitGetUserMedia','webkitGetUserMedia');
  for (const key of ['AudioContext','webkitAudioContext','AudioWorklet','AudioWorkletNode']) trap(window,key,key);
  if (window.Worklet) trap(window.Worklet.prototype,'addModule','AudioWorklet.addModule');
  Object.defineProperty(window,'__sviHardwareTrap',{value:{counts,installed}});
})()`;
async function armTraps(page) { await page.call('Page.addScriptToEvaluateOnNewDocument', { source: traps }); await page.value(traps); }
async function audioFacts() {
  const facts = [];
  for (const page of [main, hud]) {
    const trap = await page.value('window.__sviHardwareTrap');
    check(trap?.installed.includes('getUserMedia') && trap.installed.includes('AudioContext') && trap.installed.includes('AudioWorkletNode'), 'Hardware traps were not installed');
    check(Object.values(trap.counts).every(value => value === 0), 'Simulation attempted a real hardware API');
    facts.push({ installed_apis: trap.installed, total_calls: Object.values(trap.counts).reduce((sum, value) => sum + value, 0) });
  }
  return { main_and_hud: facts, no_permission_force_grant: true, timing: 'armed before any simulated session, includes all shortcut/cancel/result cases in this process' };
}
async function quit() {
  const foreground = await focusMain(); return inject(mainTarget, foreground, 'menu-quit');
}
async function historyStats(expectedSuccesses, expectedCancelled) {
  const history = await rpc('history.list', null, 100);
  check(history.items.length === expectedSuccesses + expectedCancelled && history.next_cursor === null, 'History omitted or duplicated a completed session');
  check(history.items.filter(row => row.insertion_status === 'inserted').length === expectedSuccesses && history.items.filter(row => row.state === 'cancelled').length === expectedCancelled, 'History receipt/cancellation counts differ');
  check(history.items.filter(row => row.state === 'ready').every(row => row.result?.refined_text === text), 'History contains a different simulation result');
  const now = new Date(), from = new Date(now.getTime() - 86400000).toISOString().slice(0, 10), to = new Date(now.getTime() + 86400000).toISOString().slice(0, 10);
  const stats = await rpc('stats.me', from, to);
  check(stats.totals.sessions === expectedSuccesses + expectedCancelled && stats.totals.inserted_sessions === expectedSuccesses && stats.totals.cancelled_sessions === expectedCancelled && stats.totals.learning_events === 0, 'Statistics do not match actual inserted receipts and cancelled sessions');
  await focusMain(); await button(main, '历史记录', 'nav');
  await wait(async () => await main.value(`document.body.innerText.includes(${JSON.stringify(text)})`), 'Actual history UI did not display the controlled result');
  return { successful_receipts: expectedSuccesses, cancelled_sessions: expectedCancelled, history_count: history.items.length, stats_match: true, actual_history_ui: true, production_backend_acceptance: false };
}

(async () => {
  try {
    main = await connect(portArgument, url => installedPage(url, 'index.html'));
    await wait(async () => await main.value(`typeof window.electronAPI?.invoke === 'function'`), 'Installed preload bridge unavailable');
    await startObserver();
    if (phase === 'normal-configure') {
      await run('NORMAL-IDENTITY', async () => { const info = await rpc('app.info'); check(info.platform === 'win32' && info.buildFlavor === 'normal' && info.microphone.status !== 'not-required', 'Normal control entered simulation'); return { actual_normal_package: true, simulation_disabled: true }; });
      await run('NORMAL-SHORTCUT', async () => { check(await rpc('app.updateShortcut', 'Control+Alt+F12') === 'Control+Alt+F12', 'Normal control shortcut did not persist'); check((await rpc('app.info')).shortcut === 'Control+Alt+F12', 'Normal control shortcut readback differs'); return { disjoint_shortcut_persisted: true }; }, 'NORMAL-IDENTITY');
      await run('NORMAL-STARTUP', async () => { check((await rpc('app.updateAutoStart', true)).enabled, 'Normal control startup did not enable'); const info = await rpc('app.info'); check(info.autoStart.registered && info.autoStart.enabled && !info.autoStart.error, 'Normal control startup readback differs'); return { own_startup_enabled: true }; }, 'NORMAL-IDENTITY');
    } else if (phase === 'normal-quit') {
      await run('NORMAL-QUIT', quit);
    } else {
      hud = await connect(portArgument, url => installedPage(url, 'hud.html'));
      await armTraps(main); await armTraps(hud);
      if (phase === 'exercise') {
        await run('SIM-IDENTITY', async () => {
          const info = await rpc('app.info');
          check(info.platform === 'win32' && info.buildFlavor === 'windows-simulation' && info.version === manifest.version && info.microphone.status === 'not-required' && !info.microphone.error && info.shortcut === 'CommandOrControl+Shift+Space', 'Installed simulation identity, version, microphone or shortcut differs');
          check(new URL(info.apiBaseUrl).hostname === '127.0.0.1', 'Simulation did not use its owned loopback service');
          check(await main.value(`Boolean(document.querySelector('[role="note"]')) && document.querySelector('[role="note"]').innerText.includes('模拟测试版') && document.querySelector('[role="note"]').innerText.includes('无需麦克风')`), 'Login page simulation banner missing');
          return { build_flavor: info.buildFlavor, version: info.version, microphone_status: info.microphone.status, simulation_banner: true };
        });
        await run('SIM-LOGIN', async () => {
          await wait(async () => await main.value(`Boolean(document.querySelector('input[type="email"]'))`), 'Actual login UI unavailable');
          await focusMain(); await fill('input[type="email"]', email); await fill('input[type="password"]', password); await click(main, 'form button[type="submit"]');
          await wait(async () => await main.value(`Boolean(document.querySelector('nav')) && document.body.innerText.includes(${JSON.stringify(email)})`), 'Actual UI fictional login failed');
          check(await main.value(`Boolean(document.querySelector('[role="note"]')) && document.querySelector('[role="note"]').innerText.includes('模拟测试版')`), 'Main page simulation banner missing');
          check((await rpc('auth.me')).user.email === email, 'Real AuthService did not retain the fictional account');
          return { actual_ui_login: true, fictional_account_only: true, main_banner: true };
        }, 'SIM-IDENTITY');
        let firstSession;
        await run('SIM-HUD', async () => { const target = await prepareNote('note-a', 6); firstSession = await begin(target); return { real_global_shortcut: true, hud_recording: true, acknowledged_samples_positive: true, foreground_preserved: true, ...(await audioFacts()) }; }, 'SIM-LOGIN');
        await run('SIM-NOTE-CARET', async () => { const target = manifest.targets.find(row => row.name === 'note-a'); await finish(target, firstSession); const expected = 'Alpha ' + text + 'React Omega'; const facts = await nativeText(target, expected, 6 + text.length); await pause(350); await nativeText(target, expected, 6 + text.length); noteState.set(target.pid, expected); return { ...facts, fixed_text_once: true, actual_inserted_receipt: true }; }, 'SIM-HUD');
        await run('SIM-NOTE-SELECTION', () => scenario('note-b', 7, 6), 'SIM-LOGIN');
        await run('SIM-NOTE-EMPTY', () => scenario('note-empty', 0), 'SIM-LOGIN');
        await run('SIM-LATEST-TARGET', async () => {
          const old = await prepareNote('note-a', 0), oldText = noteState.get(old.pid), sessionId = await begin(old);
          const latest = await prepareNote('note-b', 0, 6), before = noteState.get(latest.pid); await finish(latest, sessionId);
          const expected = text + before.slice(6); await nativeText(latest, expected, text.length); noteState.set(latest.pid, expected);
          await focus(old); await nativeText(old, oldText, 0);
          return { recording_target_unchanged: true, final_foreground_selection_used: true, inserted_exactly_once: true };
        }, 'SIM-LOGIN');
        await run('SIM-REPEAT', async () => { const target = manifest.targets.find(row => row.name === 'note-empty'); return scenario('note-empty', noteState.get(target.pid).length); }, 'SIM-LOGIN');
        await run('SIM-HUD-CANCEL', async () => {
          const target = await prepareNote('note-a', 0), before = noteState.get(target.pid), sessionId = await begin(target);
          await click(hud, '.hud-cancel');
          await wait(async () => (await rpc('session.status')).state === 'cancelled', 'Actual HUD cancel did not cancel the session');
          check((await observe('foreground')).root?.pid === target.pid, 'Non-activating HUD cancel stole foreground');
          await pause(500); await nativeText(target, before, 0);
          check((await rpc('history.list', null, 100)).items.find(row => row.session_id === sessionId)?.insertion_status === null, 'Cancelled HUD operation claimed an insertion');
          return { actual_hud_click: true, target_unchanged: true, no_late_insertion: true };
        }, 'SIM-LOGIN');
        await run('SIM-ESCAPE-CANCEL', async () => {
          const target = await prepareNote('note-a', 0), before = noteState.get(target.pid); await begin(target);
          inject(mainTarget, await focusMain(), 'escape');
          await wait(async () => (await rpc('session.status')).state === 'cancelled', 'Actual application-local Escape did not cancel');
          await focus(target); await pause(500); await nativeText(target, before, 0);
          return { actual_os_escape_to_main: true, target_unchanged: true, scope: 'application-local Escape only' };
        }, 'SIM-LOGIN');
        for (const type of ['INPUT', 'TEXTAREA']) for (const kind of ['CARET', 'SELECTION', 'EMPTY']) {
          await run(`SIM-CHROME-${type}-${kind}`, async () => {
            const target = owned('chrome');
            chrome ??= await connect(manifest.chrome_port, url => { try { return fileURLToPath(url) === manifest.chrome_fixture; } catch { return false; } });
            const selector = type === 'INPUT' ? '#input' : '#textarea', before = kind === 'EMPTY' ? '' : 'BEGIN React END', start = kind === 'EMPTY' ? 0 : 6, length = kind === 'SELECTION' ? 5 : 0;
            await focus(target); await chrome.call('Page.bringToFront'); await click(chrome, selector);
            await chrome.value(`(() => { const field = document.querySelector(${JSON.stringify(selector)}); field.value = ${JSON.stringify(before)}; field.focus(); field.setSelectionRange(${start}, ${start + length}); window.sviEvents = {pastes:0,enters:0}; })()`);
            await nativeText(target, before, start, length);
            const sessionId = await begin(target); await finish(target, sessionId);
            const expected = before.slice(0, start) + text + before.slice(start + length);
            const native = await nativeText(target, expected, start + text.length);
            const dom = await chrome.value(`(() => { const field = document.querySelector(${JSON.stringify(selector)}); return {value:field.value,start:field.selectionStart,end:field.selectionEnd,...window.sviEvents}; })()`);
            check(dom.value === expected && dom.start === start + text.length && dom.end === dom.start && dom.pastes === 1 && dom.enters === 0, 'Independent Chrome DOM saw wrong text/selection, duplicate paste or Enter');
            return { ...native, independent_dom_matches: true, actual_ctrl_v_count: dom.pastes, enter_count: dom.enters };
          }, 'SIM-LOGIN');
        }
        await run('SIM-HISTORY-STATS', () => historyStats(successful.length, 2), 'SIM-LOGIN');
        await run('SIM-STARTUP', async () => {
          await focusMain(); await click(main, 'button[title="设置"]'); await button(main, '设置');
          const selector = 'button[role="switch"][aria-label="开机自启"]';
          await wait(async () => await main.value(`Boolean(document.querySelector(${JSON.stringify(selector)})) && !document.querySelector(${JSON.stringify(selector)}).disabled`), 'Actual startup settings UI unavailable');
          for (const enabled of [true, false, true]) {
            const before = await rpc('app.info');
            if (before.autoStart.registered !== enabled) await click(main, selector);
            await wait(async () => { const after = (await rpc('app.info')).autoStart; return !after.error && after.registered === enabled && after.enabled === enabled; }, 'Startup setting did not read back from actual OS');
          }
          await click(main, '[aria-label="关闭设置"]');
          return { actual_ui_on_off_on: true, public_os_readback_confirmed: true, system_login_not_executed: true };
        }, 'SIM-LOGIN');
        await run('SIM-NO-AUDIO', audioFacts);
        report.expected_successes = successful.length;
        report.notepad_expectations = [...noteState].map(([pid, expected]) => ({ name: manifest.targets.find(row => row.pid === pid).name, text: expected, ...noteCarets.get(pid) }));
        await run('SIM-QUIT', quit);
      } else {
        await run('SIM-SAVED-ACCOUNT', async () => {
          await wait(async () => await main.value(`Boolean(document.querySelector('nav')) && document.body.innerText.includes(${JSON.stringify(email)})`), 'Saved fictional account did not restore through the real product');
          const info = await rpc('app.info'); check(info.buildFlavor === 'windows-simulation' && info.microphone.status === 'not-required', 'Restarted product identity differs');
          check((await rpc('auth.me')).user.email === email, 'Restored AuthService account differs');
          return { saved_fictional_account_restored: true, real_token_store: true, random_loopback_port_did_not_break_identity: true };
        });
        await run('SIM-NO-REPLAY', async () => {
          await pause(1000);
          for (const expected of manifest.restart_notes) { const target = manifest.targets.find(row => row.name === expected.name); await focus(target); const state = (await observe('capture', { fixture_pid: target.pid })).readback; check(state.text === expected.text && state.caret_utf16 === expected.caret && state.selection_utf16_len === expected.selection, 'Restart automatically replayed text or changed target caret/selection'); }
          const history = await rpc('history.list', null, 100);
          check(history.items.filter(row => row.insertion_status === 'inserted').length === manifest.expected_successes && history.items.length === manifest.expected_successes + 2, 'Restart duplicated or lost receipts');
          return { all_owned_notepads_unchanged: true, receipt_count_preserved: true, no_automatic_paste: true };
        }, 'SIM-SAVED-ACCOUNT');
        await run('SIM-RESTART-NO-AUDIO', audioFacts);
        await run('SIM-RESTART-QUIT', quit);
      }
    }
  } catch (error) { report.infrastructure_error = safeError(error); }
  finally {
    for (const row of report.cases) if (row.status === 'not_run') row.reason ??= 'Infrastructure or prior case prevented execution';
    for (const page of connections) page.socket.close();
    if (observer) {
      observer.stdin.end();
      const stopped = await Promise.race([observerStopped, pause(2500).then(() => null)]);
      if (!stopped) { observer.kill(); await observerStopped; report.observer_cleanup = 'forced'; } else report.observer_cleanup = stopped.code === 0 ? 'clean' : 'failed';
    }
    report.status = !report.infrastructure_error && report.cases.every(row => row.status === 'pass') && (!observer || report.observer_cleanup === 'clean') ? 'pass' : 'fail';
    const privateNotes = report.notepad_expectations; delete report.notepad_expectations;
    if (privateNotes) fs.writeFileSync(manifest.restart_file, JSON.stringify({ expected_successes: report.expected_successes, notes: privateNotes }), { flag: 'wx' });
    fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
    process.exitCode = report.status === 'pass' ? 0 : 1;
  }
})();
