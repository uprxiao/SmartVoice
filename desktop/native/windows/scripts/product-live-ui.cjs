// Actual DOM input against an owned development Electron/Chrome profile and real HTTP/PG.
// No store setters, business RPC substitutes, synthetic permission return or deployed account.
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const WebSocket=require('ws');
const {spawn}=require('node:child_process');
const {createInterface}=require('node:readline');
const [port,environmentFile,output,phase]=process.argv.slice(2);
if(process.env.GITHUB_ACTIONS!=='true'||!/^\d{1,5}$/.test(port||'')||!['register','boot','logout','admin','history','admin-history','controls','crash','crash-recover','controls-restart','journal-arm','journal-restore','admin-journal'].includes(phase))throw Error('Owned CI phase required');
const root=path.resolve(__dirname,'../../../..');
const fixture=JSON.parse(fs.readFileSync(environmentFile,'utf8'));
if(fixture.schema!=='svi-desktop-live-environment-v1'||path.resolve(fixture.source_root)!==root||path.resolve(fixture.run_dir)!==path.dirname(path.resolve(environmentFile))||fixture.origin!=='http://127.0.0.1:17863'||path.resolve(fixture.private_file)!==path.join(path.resolve(fixture.run_dir),'credentials.private.json'))throw Error('Owned fixture mismatch');
const credentials=JSON.parse(fs.readFileSync(fixture.private_file,'utf8'));
const email='desktop-live@example.test';
const planned=phase==='register'?['LIVE-DESKTOP-01','LIVE-DESKTOP-02','LIVE-DESKTOP-03','LIVE-DESKTOP-04','LIVE-DESKTOP-05']:phase==='boot'?['LIVE-DESKTOP-06']:phase==='logout'?['LIVE-DESKTOP-07']:phase==='history'?['LIVE-HISTORY-01','LIVE-HISTORY-02','LIVE-HISTORY-03','LIVE-HISTORY-04']:phase==='admin-history'?['LIVE-ADMIN-HISTORY-01']:['LIVE-ADMIN-01','LIVE-ADMIN-02','LIVE-ADMIN-03'];
if(phase==='controls')planned.splice(0,planned.length,'LIVE-CONTROL-01','LIVE-CONTROL-02');
if(phase==='crash')planned.splice(0,planned.length);
if(phase==='crash-recover')planned.splice(0,planned.length,'LIVE-CONTROL-03');
if(phase==='controls-restart')planned.splice(0,planned.length,'LIVE-CONTROL-04');
if(phase==='journal-arm')planned.splice(0,planned.length);
if(phase==='journal-restore')planned.splice(0,planned.length,'LIVE-JOURNAL-02','LIVE-JOURNAL-03');
if(phase==='admin-journal')planned.splice(0,planned.length,'LIVE-JOURNAL-ADMIN');
const report={phase,scope:'actual Main/preload + loopback HTTP/PostgreSQL; real DOM input, mock providers, no product audio/model/S5 pass',planned:planned.length,cases:planned.map(id=>({id,status:'not_run'}))};
let socket,sequence=0,native,nativeExit,nativeSequence=0,actualCrashEvent=false;const pending=new Map(),nativePending=new Map();const loginResponses=[];
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const check=(ok,message)=>{if(!ok)throw Error(message);};
async function cdp(method,params={}){
  const id=++sequence;
  return new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{pending.delete(id);reject(Error('Owned CDP timeout'));},12000);
    pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));
  });
}
async function evaluate(expression){
  const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);
  return result.result.value;
}
async function until(expression,timeout=15000){
  const end=Date.now()+timeout;
  while(Date.now()<end){if(await evaluate(expression))return;await pause(100);}
  throw Error('Owned DOM condition not met');
}
async function click(selector){
  let point;const end=Date.now()+15000;
  do{
    point=await evaluate(`(()=>{const nodes=[...document.querySelectorAll(${JSON.stringify(selector)})].filter(e=>e.getClientRects().length);if(nodes.length!==1)throw Error('Ambiguous visible target');const e=nodes[0];e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();if(e.disabled)throw Error('Disabled target');const x=r.x+r.width/2,y=r.y+r.height/2,hit=document.elementFromPoint(x,y);return {x,y,hit:hit===e||e.contains(hit),target:e.textContent.trim(),hit_text:hit?.textContent?.trim().slice(0,500),hit_aria:hit?.getAttribute('aria-label')};})()`);
    if((phase!=='history'&&!phase.startsWith('journal'))||point.hit)break;
    report.occluded_pointer_targets??=[];
    if(report.occluded_pointer_targets.length<40)report.occluded_pointer_targets.push({...point,at:new Date().toISOString()});
    if(Date.now()>=end)throw Error('History control remained covered; no click dispatched');
    await pause(100);
  }while(true);
  if(phase==='history'){report.pointer_actions??=[];report.pointer_actions.push({...point,at:new Date().toISOString()});}
  for(const type of ['mouseMoved','mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,x:point.x,y:point.y,button:type==='mouseMoved'?'none':'left',clickCount:type==='mouseMoved'?0:1});
}
async function button(text,scope='body'){
  const css=await evaluate(`(()=>{const matches=[...document.querySelectorAll(${JSON.stringify(scope+' button')})].filter(e=>e.textContent.trim()===${JSON.stringify(text)}&&e.getClientRects().length);if(matches.length!==1)throw Error('Ambiguous exact button');let e=matches[0],parts=[];while(e&&e.tagName!=='HTML'){let tag=e.tagName.toLowerCase();parts.unshift(tag+':nth-of-type('+([...e.parentElement.children].filter(n=>n.tagName===e.tagName).indexOf(e)+1)+')');e=e.parentElement;}return parts.join(' > ');})()`);
  await click(css);
}
async function fill(selector,value){
  await click(selector);
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers:2});
  await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',windowsVirtualKeyCode:65,modifiers:2});
  await cdp('Input.insertText',{text:value});
}
async function caseRun(id,body){
  const row=report.cases.find(x=>x.id===id);row.started_at=new Date().toISOString();
  try{row.observations=await body();row.status='pass';}
  catch(error){
    row.status=error.message.startsWith('BLOCKED:')?'blocked':'fail';row.error=error.message.replaceAll(credentials.password,'[redacted]').replace(/eyJ[A-Za-z0-9_.-]+/g,'[redacted-token]');
    try{
      const snapshot=await evaluate(`({ready_state:document.readyState,visibility:document.visibilityState,nav_count:document.querySelectorAll('nav').length,email_input_present:Boolean(document.querySelector('input[type=email]')),settings_present:Boolean(document.querySelector('[aria-label="关闭设置"]')),text:document.body.textContent?.slice(0,9000),rendered_text:document.body.innerText?.slice(0,9000)})`);
      row.failure_ui=JSON.parse(JSON.stringify(snapshot).replaceAll(credentials.password,'[redacted]').replace(/eyJ[A-Za-z0-9_.-]+/g,'[redacted-token]'));
    }catch{row.failure_snapshot_unavailable=true;}
  }
  try{
    const safe=await evaluate(`(()=>{const text=document.body.innerText;return !text.includes(${JSON.stringify(credentials.password)})&&!/eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/.test(text)&&![...document.querySelectorAll('input')].some(e=>e.type!=='password'&&e.getClientRects().length&&e.value.includes(${JSON.stringify(credentials.password)}));})()`);
    check(safe,'Screenshot privacy precondition failed');
    const folder=path.resolve(path.dirname(output),'screenshots');check(folder===path.join(path.resolve(fixture.run_dir),'screenshots'),'Screenshot outside owned fixture');fs.mkdirSync(folder,{recursive:true});
    const data=await cdp('Page.captureScreenshot',{format:'png',fromSurface:true,captureBeyondViewport:false});
    const bytes=Buffer.from(data.data,'base64');check(bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])),'Screenshot is not PNG');
    const file=row.id+'.png';fs.writeFileSync(path.join(folder,file),bytes,{flag:'wx'});
    row.screenshot={status:'captured',file:'screenshots/'+file,scope:'owned fictional client page only; supporting image, assertions decide pass'};
  }catch{row.screenshot={status:'unavailable',scope:'no screenshot pass inferred; test assertion outcome retained'};}
  row.ended_at=new Date().toISOString();
}
async function closeSettings(){if(await evaluate(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`))await click('[aria-label="关闭设置"]');}
async function shortcutSettings(){
 await closeSettings();await click('button[title="设置"]');await button('设置');
 await until(`document.querySelectorAll('button.settings-control').length===1`);
}
async function shortcutKey(key,code,virtualKey,modifiers){
 await click('button.settings-control');
 await until(`document.querySelector('button.settings-control')?.innerText.includes('请按下快捷键')`);
 await cdp('Input.dispatchKeyEvent',{type:'keyDown',key,code,windowsVirtualKeyCode:virtualKey,modifiers});
 await cdp('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode:virtualKey,modifiers:0});
 await until(`!document.querySelector('button.settings-control')?.disabled&&!document.querySelector('button.settings-control')?.innerText.includes('请按下快捷键')`);
}
async function shortcutOwner(key,body){
 const folder=path.join(fixture.run_dir,'shortcut-controls',String(++sequence));fs.mkdirSync(folder,{recursive:true});
 const executable=path.join(root,'desktop/node_modules/electron/dist/electron.exe');
 const child=spawn(executable,[path.join(root,'desktop/native/windows/fixtures/shortcut-owner.cjs'),folder,key],{windowsHide:true,env:{...process.env,SVI_SHORTCUT_CONTROL_ROOT:path.dirname(folder)},stdio:'ignore'});
 let exited=false;const stopped=new Promise(resolve=>child.once('exit',(code,signal)=>{exited=true;resolve({code,signal});}));
 try{
  const readyFile=path.join(folder,'ready.json'),end=Date.now()+10000;
  while(!fs.existsSync(readyFile)){check(!exited&&Date.now()<end,'BLOCKED: native shortcut owner not ready');await pause(30);}
  const facts=JSON.parse(fs.readFileSync(readyFile,'utf8'));check(facts.pid===child.pid&&facts.key===key,'Wrong owned shortcut process');
  return await body(facts);
 }finally{
  fs.writeFileSync(path.join(folder,'stop.json'),'{}',{flag:'wx'});
  const result=await Promise.race([stopped,pause(3000).then(()=>null)]);
  if(!result){child.kill();await stopped;throw Error('Owned shortcut helper required forced cleanup');}
  check(result.code===0,'Owned shortcut helper did not quit normally');
 }
}
function localShortcutFile(){const file=path.join(fixture.run_dir,'desktop-profile/settings.json');return fs.existsSync(file)?fs.readFileSync(file,'utf8'):null;}
async function startOwnedProbe(){
  const targets=JSON.parse(fs.readFileSync(path.join(fixture.run_dir,phase.startsWith('journal')?'journal-targets.json':'history-targets.json'),'utf8'));
  check(process.platform==='win32'&&targets.schema==='svi-history-targets-v1'&&targets.notepad_image_verified===true&&targets.controller_owns_process_objects===true&&Number.isInteger(targets.main_pid)&&Number.isInteger(targets.notepad_pid)&&targets.main_pid>0&&targets.notepad_pid>0&&targets.main_pid!==targets.notepad_pid,'Explicit owned OS targets required');
  const executable=path.join(process.env.CARGO_TARGET_DIR,'release/svi-windows-probe.exe');
  check(fs.statSync(executable).isFile(),'Independent compiled observer unavailable');
  native=spawn(executable,[],{windowsHide:true,env:{...process.env,SVI_WINDOWS_PROBE:'1'},stdio:['pipe','pipe','ignore']});
  const rejectNative=()=>{for(const request of nativePending.values()){clearTimeout(request.timer);request.reject(Error('Independent probe closed'));}nativePending.clear();};
  nativeExit=new Promise(resolve=>native.once('close',(code,signal)=>{rejectNative();resolve({code,signal});}));
  native.on('error',rejectNative);native.stdin.on('error',rejectNative);
  createInterface({input:native.stdout}).on('line',line=>{let value;try{value=JSON.parse(line);}catch{return;}const request=nativePending.get(value.id);if(!request)return;clearTimeout(request.timer);nativePending.delete(value.id);value.error?request.reject(Error('Independent OS preparation/readback failed: '+value.error.message)):request.resolve(value);});
  report.native_actions=[];
  return targets;
}
async function observeOS(action,data={}){
  check(['ping','foreground','probeFocus','probeSelect','capture','probeSeedClipboard','probeClipboardState'].includes(action),'OS control action outside frozen preparation/readback scope');
  const request={id:++nativeSequence,action,...data};
  const response=await new Promise((resolve,reject)=>{const timer=setTimeout(()=>{nativePending.delete(request.id);reject(Error('Independent OS preparation/readback timeout'));},6500);nativePending.set(request.id,{resolve,reject,timer});native.stdin.write(JSON.stringify(request)+'\n');});
  report.native_actions.push({request,response});return response;
}
async function notepadFacts(targets){
  check((await observeOS('probeFocus',{fixture_pid:targets.notepad_pid})).focused,'Owned Notepad did not become foreground');
  return (await observeOS('capture',{fixture_pid:targets.notepad_pid})).readback;
}
async function focusMainPage(targets){
  check((await observeOS('probeFocus',{fixture_pid:targets.main_pid})).focused,'Owned application did not become foreground');
  // A PID also owns the HUD. Bring this exact, already validated Main page to
  // front, then independently confirm the top-level title and process.
  await cdp('Page.bringToFront');
  const title=await evaluate('document.title');
  const end=Date.now()+1000;
  do{const foreground=await observeOS('foreground');if(foreground.root?.pid===targets.main_pid&&foreground.root.title===title)return;await pause(20);}while(Date.now()<end);
  throw Error('Exact owned Main page did not become foreground');
}
async function waitRetarget(targets){
  const end=Date.now()+15000;report.retarget_ui_checks=[];let previous;
  while(Date.now()<end){
    const state=await evaluate(`({side:document.querySelector('aside.home-sidebar')?.innerText,buttons:[...document.querySelectorAll('button')].filter(e=>['插入到当前输入框','重新指定目标'].includes(e.textContent.trim())).map(e=>({text:e.textContent.trim(),disabled:e.disabled})),toasts:[...document.querySelectorAll('[aria-label="关闭提示"]')].map(e=>e.parentElement.innerText)})`);
    const key=JSON.stringify(state);if(key!==previous){report.retarget_ui_checks.push({...state,at:new Date().toISOString()});previous=key;}
    if(state.side?.includes('PID '+targets.notepad_pid)&&state.buttons.some(b=>b.text==='插入到当前输入框'&&!b.disabled))return;
    await pause(100);
  }
  throw Error('Owned retarget readiness not obtained; actual UI/toast trace retained');
}
function exactNative(actual,text,caret,selection){check(actual?.text===text&&actual.caret_utf16===caret&&actual.selection_utf16_len===selection,'Independent Notepad full text/caret/selection differs');}
const storageExpression=`(()=>{const localKeys=Object.keys(localStorage),sessionKeys=Object.keys(sessionStorage);const cookieNames=document.cookie.split(';').map(item=>item.split('=')[0].trim()).filter(Boolean);const values=[...localKeys.map(key=>localStorage.getItem(key)),...sessionKeys.map(key=>sessionStorage.getItem(key)),document.cookie];return {localKeys,sessionKeys,cookieNames,credentialValueDetected:values.some(value=>/eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/.test(String(value)))};})()`;
async function privacy(){
  const state=await evaluate(storageExpression);
  check(![...state.localKeys,...state.sessionKeys].some(k=>/auth|token|password|credential/i.test(k))&&state.cookieNames.length===0&&!state.credentialValueDetected,'Renderer contains persisted authentication material');
  return state;
}
(async()=>{
  let page;
  const adminPhase=phase==='admin'||phase==='admin-history'||phase==='admin-journal';
  const expected=adminPhase?fixture.origin+'/admin/':pathToFileURL(path.join(root,'desktop/out/renderer/index.html')).href;
  for(let i=0;i<200;i++){
    try{page=(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(p=>p.type==='page'&&p.url===expected);if(page)break;}catch{}
    await pause(100);
  }
  check(page,'Owned exact renderer unavailable');
  socket=new WebSocket(page.webSocketDebuggerUrl);
  socket.on('message',bytes=>{const data=JSON.parse(bytes.toString());if(data.method==='Inspector.targetCrashed')actualCrashEvent=true;if(data.method==='Network.responseReceived'&&new URL(data.params.response.url).pathname==='/api/auth/login')loginResponses.push(data.params.response.status);const request=pending.get(data.id);if(!request)return;clearTimeout(request.timer);pending.delete(data.id);data.error?request.reject(Error(data.error.message)):request.resolve(data.result);});
  await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject);});
  let info;
  if(!adminPhase){
    info=await evaluate(`window.electronAPI.invoke('app.info')`); // Read-only Main API precondition before credentials.
    check(info.platform==='win32'&&info.apiBaseUrl===fixture.origin,'Refusing credentials outside the owned loopback Main service');
  }
  if(phase==='journal-arm'){
    const seeded=JSON.parse(fs.readFileSync(path.join(fixture.run_dir,'journal-seed.json'),'utf8')),targets=await startOwnedProbe();
    await until(`Boolean(document.querySelector('input[type=email]'))`);await fill('input[type=email]',email);await fill('input[type=password]',credentials.password);await click('form button[type=submit]');
    await until(`Boolean(document.querySelector('nav'))`);if(info.microphone.status==='denied')await until(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`);await closeSettings();await button('历史记录','nav');await until(`[...document.querySelectorAll('button')].filter(e=>e.textContent.trim()==='重新整理'&&!e.disabled).length===1`);await button('重新整理');
    await until(`document.body.innerText.includes(${JSON.stringify(seeded.refined_text)})&&[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='插入到当前输入框'&&!e.disabled)`);
    await focusMainPage(targets);await button('插入到当前输入框');await until(`[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='重新指定目标'&&!e.disabled)`);
    await button('重新指定目标');check((await observeOS('probeFocus',{fixture_pid:targets.notepad_pid})).focused,'Owned journal Notepad unavailable');await observeOS('probeSelect',{fixture_pid:targets.notepad_pid,start:6,length:5});
    exactNative((await observeOS('capture',{fixture_pid:targets.notepad_pid})).readback,'BEGIN React END',6,5);await waitRetarget(targets);await observeOS('probeSeedClipboard');await focusMainPage(targets);
    fs.writeFileSync(path.join(fixture.run_dir,'journal-retarget-ready.json'),JSON.stringify({main_pid:targets.main_pid,notepad_pid:targets.notepad_pid,ready:true}),{flag:'wx'});
    const end=Date.now()+15000;while(!fs.existsSync(path.join(fixture.run_dir,'journal-click-go.json'))){check(Date.now()<end,'Owned crash controller did not arm');await pause(10);}
    await button('插入到当前输入框');report.preparation={armed:true,session_id:seeded.session_id,scope:'actual UI click dispatched; OS/SQL controller evaluates hard-exit outcome, no screenshot delay here'};
  }else if(phase==='journal-restore'){
    const seeded=JSON.parse(fs.readFileSync(path.join(fixture.run_dir,'journal-seed.json'),'utf8'));
    await caseRun('LIVE-JOURNAL-02',async()=>{
      await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);check(!await evaluate(`document.body.innerText.includes('仅本次内存登录')`),'Restored account fell back to memory only');
      await closeSettings();await button('历史记录','nav');await until(`document.body.innerText.includes(${JSON.stringify(seeded.refined_text)})`);
      check(await evaluate(`[...document.querySelectorAll('button')].filter(e=>e.textContent.trim()==='重新整理'&&!e.disabled).length===0`),'Uncertain insertion offered automatic model retry');
      return{account_restored:true,result_retained_in_actual_ui:true,storage:await privacy()};
    });
    await caseRun('LIVE-JOURNAL-03',async()=>{await click('button[title="退出登录"]');await until(`Boolean(document.querySelector('input[type=email]'))&&!document.querySelector('nav')`);return{actual_logout:true,storage:await privacy()};});
  }else if(phase==='admin-journal'){
    await caseRun('LIVE-JOURNAL-ADMIN',async()=>{
      await until(`Boolean(document.querySelector('#email'))`);await fill('#email','admin-live@example.test');await fill('#password',credentials.password);await click('form button[type=submit]');await until(`Boolean(document.querySelector('nav'))`);
      const stats=`Object.fromEntries([...document.querySelectorAll('.stat-label')].map(e=>[e.textContent.trim(),e.parentElement.querySelector('.stat-value')?.textContent.trim()]))`;
      await until(`(${stats})['会话数']==='2'&&(${stats})['已插入']==='1'`);const actual=await evaluate(stats),expected={'注册用户':'2','会话数':'2','结果会话':'2','整理字数':'28','已插入':'1','插入成功率':'50.0%','学习事件':'0','接收总时长':'2 秒','成功口述时长':'2 秒'};
      check(Object.entries(expected).every(([key,value])=>actual[key]===value),'Admin counted uncertain insertion as success');await button('退出登录');await until(`Boolean(document.querySelector('#email'))&&!document.querySelector('nav')`);return{actual,expected};
    });
  }else if(phase==='controls'){
    await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);
    await shortcutSettings();
    await caseRun('LIVE-CONTROL-01',async()=>{
      const original=info.shortcut,file=localShortcutFile(),observations=[];
      for(const [key,code,vk,modifiers] of [[' ','Space',32,8],[' ','Space',32,2],['Escape','Escape',27,0]]){
        await shortcutKey(key,code,vk,modifiers);
        const current=await evaluate(`window.electronAPI.invoke('app.info')`);
        check(current.shortcut===original&&localShortcutFile()===file,'Reserved key or Escape changed saved shortcut');
        observations.push({key,modifiers,actual:current.shortcut,ui:await evaluate(`document.querySelector('button.settings-control').innerText`)});
      }
      return{original,observations,scope:'real DOM key input and actual Main native policy; not physical IME verification'};
    });
    await caseRun('LIVE-CONTROL-02',async()=>{
      const before=await evaluate(`window.electronAPI.invoke('app.info')`),file=localShortcutFile();
      const conflict=await shortcutOwner('Control+Alt+F10',async facts=>{
        check(facts.registered,'BLOCKED: independent F10 registration unavailable');await shortcutKey('F10','F10',121,3);
        const current=await evaluate(`window.electronAPI.invoke('app.info')`);
        check(current.shortcut===before.shortcut&&localShortcutFile()===file,'Real native registration conflict replaced original shortcut');
        await until(`document.body.innerText.includes('不可用')`);return{owner:facts,actual:current.shortcut};
      });
      await shortcutOwner('Control+Alt+F9',async facts=>{check(facts.registered,'BLOCKED: candidate F9 not initially free');});
      await shortcutKey('F9','F9',120,3);
      const current=await evaluate(`window.electronAPI.invoke('app.info')`),saved=JSON.parse(localShortcutFile());
      check(current.shortcut==='Control+Alt+F9'&&saved.shortcut===current.shortcut,'Successful shortcut not saved/read back');
      const actualOwner=await shortcutOwner('Control+Alt+F9',async facts=>{check(!facts.registered,'Product did not hold the native global registration');return facts;});
      await closeSettings();return{conflict,actual:current.shortcut,saved,independent_native_occupied:actualOwner};
    });
  }else if(phase==='crash'){
    await cdp('Inspector.enable');
    const attempted=cdp('Page.crash').catch(error=>({transport_error:error.message}));
    const end=Date.now()+14000;while(!actualCrashEvent&&Date.now()<end)await pause(30);
    const transport=await attempted;check(actualCrashEvent,'Actual renderer crash event unavailable');
    report.preparation={actual_crash_event:true,old_target_id:page.id,transport,scope:'actual owned Renderer crash control; recovery evaluated separately'};
  }else if(phase==='crash-recover'||phase==='controls-restart'){
    await caseRun(phase==='crash-recover'?'LIVE-CONTROL-03':'LIVE-CONTROL-04',async()=>{
      await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);
      check(info.shortcut==='Control+Alt+F9','Shortcut or account lost on actual recovery/restart');
      if(phase==='crash-recover'){
        const previous=JSON.parse(fs.readFileSync(path.join(fixture.run_dir,'renderer-crash-preparation.json'),'utf8'));
        check(previous.preparation?.actual_crash_event&&previous.preparation.old_target_id!==page.id,'Crashed renderer was reused');
        if(info.microphone.status==='denied'){
          await click('[aria-label="开始录音"]');await until(`document.body.innerText.includes('麦克风')&&(document.body.innerText.includes('隐私')||document.body.innerText.includes('权限'))`);
          check(await evaluate(`!document.querySelector('[aria-label="停止录音"]')`),'New renderer sender entered unexpected recording');
        }
      }else{
        await shortcutSettings();check(await evaluate(`document.querySelector('button.settings-control').innerText.includes('F9')`),'Restart UI did not show persisted shortcut');await closeSettings();
      }
      const owner=await shortcutOwner('Control+Alt+F9',async facts=>{check(!facts.registered,'Restart did not hold saved native key');return facts;});
      return{actual_target_id:page.id,account_restored:true,shortcut:info.shortcut,native_owner:owner,storage:await privacy(),scope:'actual Main/preload/UI and native global registration; denied microphone only'};
    });
  }else if(phase==='register'){
    await caseRun('LIVE-DESKTOP-01',async()=>{await until(`Boolean(document.querySelector('input[type=email]'))`);check(info.nativeInputSupported&&!info.accessibilitySupported&&!info.correctionObservationSupported,'Windows capability boundary differs');return{platform:info.platform,microphone:info.microphone,api_origin:info.apiBaseUrl,storage:await privacy()};});
    await caseRun('LIVE-DESKTOP-02',async()=>{await fill('input[type=email]',email);await fill('input[type=password]',credentials.password+'wrong');await click('form button[type=submit]');await until(`document.body.innerText.includes('邮箱或密码错误')`);check(await evaluate(`!document.querySelector('nav')`),'Invalid credentials authenticated');return{rejected:true};});
    await caseRun('LIVE-DESKTOP-03',async()=>{await button('注册');await fill('input[type=email]',email);await fill('input[autocomplete="new-password"][placeholder="至少 8 个字符"]',credentials.password);await fill('input[placeholder="再次输入密码"]',credentials.password);await click('form button[type=submit]');await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);return{email,automatic_login:true,storage:await privacy()};});
    await caseRun('LIVE-DESKTOP-04',async()=>{
      if(info.microphone.status==='denied')await until(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`);
      else if(!await evaluate(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`))await click('button[title="设置"]');
      await button('设置');
      const modal=`document.querySelector('[aria-label="关闭设置"]')?.closest('.fixed')`;
      report.permission_ui_before_ready=await evaluate(`(()=>{const root=${modal};return {text:root?.innerText,checking:root?.innerText.includes('检查中')};})()`);
      // The group exists while platformChecking is still true. Wait for this
      // actual modal's resolved Windows row, rather than background page text.
      await until(`(()=>{const root=${modal};return root?.innerText.includes('系统权限')&&root.innerText.includes('普通权限窗口')&&[...root.querySelectorAll('button')].some(b=>b.textContent.trim()==='重新检查'&&!b.disabled);})()`);
      const text=await evaluate(`(${modal}).innerText`);
      report.permission_ui_after_ready={windows_visible:text.includes('Windows'),ax_visible:text.includes('辅助功能'),denied_visible:text.includes('已被系统禁用')};
      check(text.includes('Windows')&&!text.includes('辅助功能'),'Resolved Windows permissions UI differs');
      if(info.microphone.status==='denied')check(text.includes('已被系统禁用'),'Actual denied microphone status not visible');
      return{microphone:info.microphone,windows_permissions_visible:true,automatic_guide_required:info.microphone.status==='denied'};
    });
    await caseRun('LIVE-DESKTOP-05',async()=>{await closeSettings();if(info.microphone.status!=='denied')throw Error('BLOCKED: normal hosted microphone is not denied; no permission return is forced');await click('[aria-label="开始录音"]');await until(`document.body.innerText.includes('麦克风')&&(document.body.innerText.includes('隐私')||document.body.innerText.includes('权限'))`);check(await evaluate(`Boolean(document.querySelector('[aria-label="开始录音"]'))&&!document.querySelector('[aria-label="停止录音"]')`),'Denied microphone entered recording');return{denied_before_recording:true,storage:await privacy()};});
  }else if(phase==='boot'){
    await caseRun('LIVE-DESKTOP-06',async()=>{await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);check(!await evaluate(`document.body.innerText.includes('仅本次内存登录')`),'Restored account fell back to memory only');check(!await evaluate(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`),'Restoring an account unexpectedly opened a permission guide');return{restored_without_retyping_password:true,email,storage:await privacy()};});
  }else if(phase==='logout'){
    await caseRun('LIVE-DESKTOP-07',async()=>{await closeSettings();await click('button[title="退出登录"]');await until(`Boolean(document.querySelector('input[type=email]'))&&!document.querySelector('nav')`);return{logged_out:true,storage:await privacy()};});
  }else if(phase==='history'){
    const seeded=JSON.parse(fs.readFileSync(path.join(fixture.run_dir,'history-seed.json'),'utf8'));
    check(seeded.review_flag_prepared_by_fixture===true&&seeded.refined_text==='帮我加一个按钮，不要修改数据库。','Frozen fictional history fixture differs');
    const targets=await startOwnedProbe();
    check((await observeOS('ping')).integrity_rid===8192,'Independent observer must be Medium');
    await caseRun('LIVE-HISTORY-01',async()=>{
      await until(`Boolean(document.querySelector('input[type=email]'))`);
      await fill('input[type=email]',email);await fill('input[type=password]',credentials.password);await click('form button[type=submit]');
      await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);
      if(info.microphone.status==='denied')await until(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`);
      await closeSettings();await button('历史记录','nav');
      await until(`[...document.querySelectorAll('button')].filter(e=>e.textContent.trim()==='重新整理').length===1`);
      await button('重新整理');
      await until(`document.body.innerText.includes(${JSON.stringify(seeded.refined_text)})&&[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='插入到当前输入框'&&!e.disabled)&&document.body.innerText.includes('重新指定输入目标')`);
      return{real_history_refine_retry:true,automatic_insertion:false,scope:'synthetic history prepared through owned HTTP/WS; explicit review flag fixture, no microphone/quality pass',storage:await privacy()};
    });
    await caseRun('LIVE-HISTORY-02',async()=>{
      if(report.cases[0].status!=='pass')throw Error('BLOCKED: actual history retry did not establish the ready result');
      check((await observeOS('probeFocus',{fixture_pid:targets.notepad_pid})).focused,'Owned Notepad did not become foreground');
      await observeOS('probeSelect',{fixture_pid:targets.notepad_pid,start:0,length:0});
      exactNative(await notepadFacts(targets),'BEGIN React END',0,0);
      await focusMainPage(targets);
      await button('插入到当前输入框');
      await until(`[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='重新指定目标'&&!e.disabled)`);
      exactNative(await notepadFacts(targets),'BEGIN React END',0,0);
      await focusMainPage(targets);
      await button('重新指定目标');
      check((await observeOS('probeFocus',{fixture_pid:targets.notepad_pid})).focused,'Owned Notepad did not become foreground');
      await observeOS('probeSelect',{fixture_pid:targets.notepad_pid,start:6,length:5});
      exactNative((await observeOS('capture',{fixture_pid:targets.notepad_pid})).readback,'BEGIN React END',6,5);
      await waitRetarget(targets);
      await observeOS('probeSeedClipboard');
      await focusMainPage(targets);
      await button('插入到当前输入框');
      await until(`document.body.innerText.includes('已插入到目标输入框')&&[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='插入到当前输入框'&&!e.disabled)`);
      const actual=await notepadFacts(targets);exactNative(actual,seeded.expected_native_text,seeded.expected_caret_utf16,0);
      const clip=await observeOS('probeClipboardState');check(clip.text==='SVI original clipboard','Product did not restore original owned clipboard');
      return{real_dom_manual_insert:true,independent_notepad:actual,clipboard_restored:true,exact_target_restored_from_owned_main:true};
    });
    await caseRun('LIVE-HISTORY-03',async()=>{
      if(report.cases[1].status!=='pass')throw Error('BLOCKED: first actual insertion did not pass');
      await focusMainPage(targets);
      await button('插入到当前输入框');await pause(200);
      await until(`[...document.querySelectorAll('button')].some(e=>e.textContent.trim()==='插入到当前输入框'&&!e.disabled)`);
      const actual=await notepadFacts(targets);exactNative(actual,seeded.expected_native_text,seeded.expected_caret_utf16,0);
      return{repeat_actual_click_did_not_rewrite:true,independent_notepad:actual};
    });
    await caseRun('LIVE-HISTORY-04',async()=>{await closeSettings();await click('button[title="退出登录"]');await until(`Boolean(document.querySelector('input[type=email]'))&&!document.querySelector('nav')`);return{logged_out:true,storage:await privacy()};});
  }else if(phase==='admin-history'){
    await caseRun('LIVE-ADMIN-HISTORY-01',async()=>{
      await until(`Boolean(document.querySelector('#email'))`);await fill('#email','admin-live@example.test');await fill('#password',credentials.password);await click('form button[type=submit]');await until(`Boolean(document.querySelector('nav'))`);
      const stats=`Object.fromEntries([...document.querySelectorAll('.stat-label')].map(e=>[e.textContent.trim(),e.parentElement.querySelector('.stat-value')?.textContent.trim()]))`;
      await until(`(${stats})['会话数']==='1'&&(${stats})['已插入']==='1'`);const actual=await evaluate(stats);
      const expected={'注册用户':'2','会话数':'1','结果会话':'1','整理字数':'14','已插入':'1','插入成功率':'100.0%','学习事件':'0','接收总时长':'1 秒','成功口述时长':'1 秒'};
      check(Object.entries(expected).every(([key,value])=>actual[key]===value),'Admin history totals differ from frozen synthetic session/native insertion oracle');
      await button('退出登录');await until(`Boolean(document.querySelector('#email'))&&!document.querySelector('nav')`);return{metrics:actual,expected,storage:await privacy()};
    });
  }else{
    await cdp('Network.enable'); // Status only; never request bodies, headers or response tokens.
    async function login(who){await fill('#email',who);await fill('#password',credentials.password);await click('form button[type=submit]');}
    await caseRun('LIVE-ADMIN-01',async()=>{await until(`Boolean(document.querySelector('#email'))`);await login(email);await until(`document.body.innerText.includes('需要管理员权限')&&!document.querySelector('nav')`);check(loginResponses.at(-1)===403,'Ordinary user was not actually refused by HTTP403');return{ordinary_user_rejected:true,http_status:403};});
    await caseRun('LIVE-ADMIN-02',async()=>{await login('admin-live@example.test');await until(`Boolean(document.querySelector('nav'))`);const stats=`Object.fromEntries([...document.querySelectorAll('.stat-label')].map(e=>[e.textContent.trim(),e.parentElement.querySelector('.stat-value')?.textContent.trim()]))`;await until(`(${stats})['会话数']==='0'&&(${stats})['注册用户']==='2'`);const actual=await evaluate(stats);check(actual['已插入']==='0','Admin invented an insertion');return{metrics:actual,oracle:'one seeded admin + one real desktop registration, zero audio sessions'};});
    await caseRun('LIVE-ADMIN-03',async()=>{await button('退出登录');await until(`Boolean(document.querySelector('#email'))&&!document.querySelector('nav')`);return{logged_out:true,storage:await privacy()};});
  }
})().catch(error=>{report.infrastructure_error=error.message.replaceAll(credentials.password,'[redacted]');process.exitCode=1;}).finally(async()=>{
  if(native){native.stdin.end();const exited=await Promise.race([nativeExit,pause(2500).then(()=>null)]);if(!exited){native.kill();report.observer_cleanup=await nativeExit;process.exitCode=1;}else report.observer_cleanup=exited;}
  socket?.terminate();
  for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('Owned session closed'));}
  fs.writeFileSync(output,JSON.stringify(report,null,2),{flag:'wx'});
  if(report.cases.some(x=>x.status!=='pass'))process.exitCode=1;
});
