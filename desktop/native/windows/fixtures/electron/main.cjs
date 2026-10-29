const { app, BrowserWindow, desktopCapturer, globalShortcut, screen } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const { performance } = require('node:perf_hooks');

const index = process.argv.indexOf('--probe-dir');
if (index < 0 || !process.argv[index + 1]) throw new Error('--probe-dir required');
const folder = path.resolve(process.argv[index + 1]);
const helperPath = path.join(process.resourcesPath, 'voice', 'smartvoiceinput-input.exe');
const seen = new Set();
const children = new Set();
let main;
let hud;
let processing = false;
let quitting = false;

function writeNew(name, data) {
  const target = path.join(folder, name);
  if (fs.existsSync(target)) throw new Error('probe response already exists');
  const temporary = `${target}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(data, null, 2), { flag: 'wx' });
  fs.renameSync(temporary, target);
}

async function stop(child) {
  if (child.exitCode !== null) return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('probe child exit not confirmed')), 3000);
    child.once('exit', () => { clearTimeout(timer); children.delete(child); resolve(); });
    child.kill();
  });
}

async function pingNewHelper() {
  const started = performance.now();
  const child = spawn(helperPath, [], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  children.add(child);
  child.once('exit', () => children.delete(child));
  child.stderr.resume();
  const response = await new Promise((resolve, reject) => {
    let buffered = '';
    const timer = setTimeout(() => reject(new Error('probe ping timeout')), 5000);
    const fail = error => { clearTimeout(timer); reject(error); };
    child.once('error', fail);
    child.once('exit', () => fail(new Error('helper exited before ping')));
    child.stdin.on('error', fail);
    child.stdout.on('data', bytes => {
      buffered += bytes.toString('utf8');
      if (buffered.length > 1048576) return fail(new Error('probe protocol exceeds limit'));
      const end = buffered.indexOf('\n');
      if (end < 0) return;
      try {
        const value = JSON.parse(buffered.slice(0, end));
        if (value.id !== 1 || value.error || value.probe_only !== true) throw new Error('invalid ping response');
        clearTimeout(timer); resolve(value);
      } catch (error) { fail(error); }
    });
    child.stdin.write(`${JSON.stringify({ id: 1, action: 'ping' })}\n`);
  }).catch(async error => { await stop(child); throw error; });
  return { child, response, cold_start_ms: performance.now() - started };
}

async function command(request) {
  switch (request.action) {
    case 'startup-diagnostics': {
      if (process.env.GITHUB_ACTIONS !== 'true' || path.basename(request.productExe || '') !== 'SmartVoiceInput.exe'
        || !fs.existsSync(request.productExe)) throw new Error('disposable product fixture required');
      const options={path:request.productExe,args:['--background']};
      const quoted={...options,path:`"${request.productExe}"`};
      const runKey='HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';
      const approvedKey='HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run';
      const query=(key,entry)=>{
        const args=['query',key];if(entry)args.push('/v','SmartVoiceInput');
        const result=spawnSync('reg.exe',args,{encoding:'utf8',windowsHide:true,timeout:3000});
        return {status:result.status,error:result.error?.message,output:entry?result.stdout:undefined,stderr:result.stderr};
      };
      const registry=()=>({runKey:query(runKey,false),ownRun:query(runKey,true),ownApproved:query(approvedKey,true)});
      const initial=app.getLoginItemSettings(options);
      const initialQuoted=app.getLoginItemSettings(quoted);
      const before=registry();
      if(initial.openAtLogin || initialQuoted.openAtLogin || before.ownRun.status===0 || before.ownApproved.status===0
        || initialQuoted.launchItems.some(item=>item.name==='SmartVoiceInput')) throw new Error('refusing to replace an existing startup entry');
      let enabled,enabledQuoted,after;
      try {
        app.setLoginItemSettings({...options,name:'SmartVoiceInput',openAtLogin:true,enabled:true});
        enabled=app.getLoginItemSettings(options);
        enabledQuoted=app.getLoginItemSettings(quoted);
        after=registry();
      } finally {app.setLoginItemSettings({...options,name:'SmartVoiceInput',openAtLogin:false,enabled:false});}
      return {initial,initialQuoted,before,enabled,enabledQuoted,after,final:app.getLoginItemSettings(quoted),cleanup:registry(),
        scope:'independent Electron control using the actual product path; own entry only; not a product fix'};
    }
    case 'hud': {
      if (!hud) {
        // Same non-activating Windows options as desktop/src/main/app/window.ts.
        hud = new BrowserWindow({ width: 300, height: 104, x: 20, y: 20,
          frame: false, transparent: true, resizable: false, movable: true,
          minimizable: false, maximizable: false, fullscreenable: false,
          skipTaskbar: true, focusable: false, alwaysOnTop: true, show: false,
          hasShadow: false, webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } });
        hud.setAlwaysOnTop(true, 'screen-saver');
        await hud.loadURL('data:text/html;charset=utf-8,<body style="background:transparent"><p style="background:rgb(40,40,50);color:white;padding:12px;border-radius:20px">SmartVoiceInput 技术验证</p>');
      }
      if (request.state === 'hide') hud.hide();
      else {
        await hud.webContents.executeJavaScript(`document.querySelector('p').textContent=${JSON.stringify(request.state)}`);
        hud.showInactive();
      }
      return { visible: hud.isVisible(), focused: hud.isFocused() };
    }
    case 'field': {
      const id = ['plain', 'editor', 'password', 'readonly', 'disabled', 'unreadable'].includes(request.field) ? request.field : 'editor';
      main.show(); main.focus();
      const rect = await main.webContents.executeJavaScript(`(() => {
        const el=document.getElementById(${JSON.stringify(id)});
        if('value' in el && !el.readOnly && !el.disabled && el.type!=='password') el.value=${JSON.stringify(typeof request.initial === 'string' ? request.initial : 'BEGIN React END')};
        window.pasteChords=[]; window.canvasValue=''; window.canvasPasteCount=0; el.focus();
        el.scrollIntoView({block:'center'});
        if(el.setSelectionRange && !el.disabled) el.setSelectionRange(${Number.isSafeInteger(request.start) ? request.start : 6},${Number.isSafeInteger(request.end) ? request.end : 11});
        const r=el.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};
      })()`);
      const bounds = main.getContentBounds();
      return { pid: process.pid, point: screen.dipToScreenPoint({ x: Math.round(bounds.x + rect.x), y: Math.round(bounds.y + rect.y) }) };
    }
    case 'fieldState':
      return main.webContents.executeJavaScript(`(() => {
        const e=document.activeElement; return {value:e.value, start:e.selectionStart, end:e.selectionEnd, chords:window.pasteChords,
          canvasValue:window.canvasValue, canvasPasteCount:window.canvasPasteCount};
      })()`);
    case 'hotkeys': {
      const values = [];
      for (const accelerator of ['Control+Shift+Space', 'Control+Alt+Space']) {
        const registered = globalShortcut.register(accelerator, () => {});
        values.push({ accelerator, registered });
        if (registered) globalShortcut.unregister(accelerator);
      }
      return { values, ime_verified: false };
    }
    case 'helpers': {
      const attempts = [];
      for (let round = 0; round < 3; round++) {
        const { child, response, cold_start_ms } = await pingNewHelper();
        attempts.push({ ...response, cold_start_ms });
        await stop(child); // Deliberate crash; no write request is replayed.
      }
      const restarted = await pingNewHelper();
      attempts.push({ ...restarted.response, cold_start_ms: restarted.cold_start_ms });
      await stop(restarted.child);
      const verified = app.isPackaged && fs.existsSync(helperPath) && attempts.every(a =>
        path.resolve(a.executable).toLowerCase() === path.resolve(helperPath).toLowerCase() && a.integrity_rid === 8192);
      return { packaged: app.isPackaged, helper_path_verified: verified, attempts, crash_restart_attempted: true };
    }
    case 'screenshot': {
      if (!/^V-\d\d(?:-[a-z0-9]+)?$/.test(request.label)) throw new Error('invalid screenshot label');
      const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 1920, height: 1080 } });
      for (let n = 0; n < sources.length; n++) fs.writeFileSync(path.join(folder, `${request.label}-${n}.png`), sources[n].thumbnail.toPNG(), { flag: 'wx' });
      return { count: sources.length };
    }
    case 'quit':
      quitting = true;
      setTimeout(() => app.quit(), 200);
      return { quitting: true };
    default: throw new Error('unknown fixture command');
  }
}

app.whenReady().then(async () => {
  app.setPath('userData', path.join(folder, 'electron-profile'));
  main = new BrowserWindow({ width: 800, height: 750, show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false } });
  await main.loadFile(path.join(__dirname, 'index.html'));
  writeNew('ready.json', { pid: process.pid, packaged: app.isPackaged, electron: process.versions.electron,
    resources_path: process.resourcesPath, helper_exists: fs.existsSync(helperPath) });
  const timer = setInterval(async () => {
    if (processing || quitting) return;
    processing = true;
    try {
      for (const filename of fs.readdirSync(folder).filter(n => /^\d+\.request\.json$/.test(n)).sort((a, b) => parseInt(a) - parseInt(b))) {
        if (seen.has(filename)) continue;
        seen.add(filename);
        let response;
        try { response = await command(JSON.parse(fs.readFileSync(path.join(folder, filename), 'utf8'))); }
        catch (error) { response = { error: error.message }; }
        writeNew(filename.replace('.request.', '.response.'), response);
      }
    } finally { processing = false; }
  }, 40);
  app.on('before-quit', () => { clearInterval(timer); globalShortcut.unregisterAll(); for (const child of children) child.kill(); });
});
