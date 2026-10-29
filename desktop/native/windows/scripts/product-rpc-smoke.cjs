// Automated CI boundary test of the actual packaged Main/preload. No app mock mode or account.
const fs = require('node:fs');
const WebSocket = require('ws');
const [port, evidence] = process.argv.slice(2);
if (process.env.GITHUB_ACTIONS !== 'true' || !/^\d{1,5}$/.test(port ?? '') || !evidence) throw new Error('Disposable CI arguments required');
const report = { scope: 'actual packaged local RPC/system startup read-back; no system-login/audio/S5', status: 'not_run' };
let socket, seq = 0;
const pending = new Map();
function call(method, params) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('CDP response timeout')); }, 12000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
}
(async () => {
  let page;
  for (let n = 0; n < 100; n++) {
    try {
      const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      page = pages.find(p => p.type === 'page' && p.url.endsWith('/index.html'));
      if (page) break;
    } catch { /* owned app may still be starting */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (!page) throw new Error('Owned product renderer unavailable');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  socket.on('message', bytes => {
    const data = JSON.parse(bytes.toString()); const request = pending.get(data.id);
    if (!request) return;
    clearTimeout(request.timer); pending.delete(data.id);
    if (data.error) request.reject(new Error(data.error.message)); else request.resolve(data.result);
  });
  await new Promise((resolve, reject) => { socket.once('open', resolve); socket.once('error', reject); });
  const result = await call('Runtime.evaluate', { awaitPromise: true, returnByValue: true, expression: `(async () => {
    const invoke = (action, ...args) => window.electronAPI.invoke(action, ...args);
    const initial = await invoke('app.info');
    if (initial.platform !== 'win32' || initial.accessibilitySupported || initial.accessibilityTrusted || initial.correctionObservationSupported || !initial.nativeInputSupported) throw new Error('Incorrect Windows capabilities');
    if (!initial.autoStart.configurable || initial.autoStart.error || initial.autoStart.registered) throw new Error('Expected a clean installed app with startup off');
    let invalidRejected = false, axRejected = false;
    try { await invoke('app.updateAutoStart', 'yes'); } catch (e) { invalidRejected = String(e).includes('AUTOSTART_INVALID'); }
    try { await invoke('app.openAccessibilitySettings'); } catch (e) { axRejected = String(e).includes('SYSTEM_SETTINGS_UNAVAILABLE'); }
    const ax = await invoke('app.requestAccessibility');
    let enabled, disabled;
    try {
      enabled = await invoke('app.updateAutoStart', true);
      if (!enabled.registered || !enabled.enabled || enabled.error) throw new Error('Actual startup enable/read-back failed');
    } finally { disabled = await invoke('app.updateAutoStart', false); }
    const final = await invoke('app.info');
    if (!invalidRejected || !axRejected || ax.supported || disabled.registered || final.autoStart.registered || final.autoStart.error) throw new Error('RPC boundary or startup cleanup failed');
    return { initial, enabled, disabled, final, invalidRejected, axRejected };
  })()` });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  report.observations = result.result.value;
  report.status = 'pass';
})().catch(error => { report.status = 'fail'; report.error = error.message; process.exitCode = 1; }).finally(() => {
  socket?.terminate();
  fs.writeFileSync(evidence, JSON.stringify(report, null, 2), { flag: 'wx' });
});
