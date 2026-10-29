// Actual DOM input against an owned development Electron/Chrome profile and real HTTP/PG.
// No store setters, business RPC substitutes, synthetic permission return or deployed account.
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const WebSocket=require('ws');
const [port,environmentFile,output,phase]=process.argv.slice(2);
if(process.env.GITHUB_ACTIONS!=='true'||!/^\d{1,5}$/.test(port||'')||!['register','boot','logout','admin'].includes(phase))throw Error('Owned CI phase required');
const root=path.resolve(__dirname,'../../../..');
const fixture=JSON.parse(fs.readFileSync(environmentFile,'utf8'));
if(fixture.schema!=='svi-desktop-live-environment-v1'||path.resolve(fixture.source_root)!==root||path.resolve(fixture.run_dir)!==path.dirname(path.resolve(environmentFile))||fixture.origin!=='http://127.0.0.1:17863'||path.resolve(fixture.private_file)!==path.join(path.resolve(fixture.run_dir),'credentials.private.json'))throw Error('Owned fixture mismatch');
const credentials=JSON.parse(fs.readFileSync(fixture.private_file,'utf8'));
const email='desktop-live@example.test';
const planned=phase==='register'?['LIVE-DESKTOP-01','LIVE-DESKTOP-02','LIVE-DESKTOP-03','LIVE-DESKTOP-04','LIVE-DESKTOP-05']:phase==='boot'?['LIVE-DESKTOP-06']:phase==='logout'?['LIVE-DESKTOP-07']:['LIVE-ADMIN-01','LIVE-ADMIN-02','LIVE-ADMIN-03'];
const report={phase,scope:'actual Main/preload + loopback HTTP/PostgreSQL; real DOM input, mock providers, no product audio/model/S5 pass',planned:planned.length,cases:planned.map(id=>({id,status:'not_run'}))};
let socket,sequence=0;const pending=new Map();const loginResponses=[];
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
  const point=await evaluate(`(()=>{const nodes=[...document.querySelectorAll(${JSON.stringify(selector)})].filter(e=>e.getClientRects().length);if(nodes.length!==1)throw Error('Ambiguous visible target');const e=nodes[0];e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();if(e.disabled)throw Error('Disabled target');return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  for(const type of ['mouseMoved','mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,...point,button:type==='mouseMoved'?'none':'left',clickCount:type==='mouseMoved'?0:1});
}
async function button(text,scope='body'){
  const selector=await evaluate(`(()=>{const matches=[...document.querySelectorAll(${JSON.stringify(scope+' button')})].filter(e=>e.textContent.trim()===${JSON.stringify(text)}&&e.getClientRects().length);if(matches.length!==1)throw Error('Ambiguous exact button');const e=matches[0];return [...document.querySelectorAll('button')].indexOf(e);})()`);
  const css=await evaluate(`(()=>{let e=document.querySelectorAll('button')[${selector}],parts=[];while(e&&e.tagName!=='HTML'){let tag=e.tagName.toLowerCase();parts.unshift(tag+':nth-of-type('+([...e.parentElement.children].filter(n=>n.tagName===e.tagName).indexOf(e)+1)+')');e=e.parentElement;}return parts.join(' > ');})()`);
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
  catch(error){row.status=error.message.startsWith('BLOCKED:')?'blocked':'fail';row.error=error.message.replaceAll(credentials.password,'[redacted]').replace(/eyJ[A-Za-z0-9_.-]+/g,'[redacted-token]');}
  row.ended_at=new Date().toISOString();
}
async function closeSettings(){if(await evaluate(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`))await click('[aria-label="关闭设置"]');}
const storageExpression=`(()=>{const localKeys=Object.keys(localStorage),sessionKeys=Object.keys(sessionStorage);const cookieNames=document.cookie.split(';').map(item=>item.split('=')[0].trim()).filter(Boolean);const values=[...localKeys.map(key=>localStorage.getItem(key)),...sessionKeys.map(key=>sessionStorage.getItem(key)),document.cookie];return {localKeys,sessionKeys,cookieNames,credentialValueDetected:values.some(value=>/eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/.test(String(value)))};})()`;
async function privacy(){
  const state=await evaluate(storageExpression);
  check(![...state.localKeys,...state.sessionKeys].some(k=>/auth|token|password|credential/i.test(k))&&state.cookieNames.length===0&&!state.credentialValueDetected,'Renderer contains persisted authentication material');
  return state;
}
(async()=>{
  let page;
  const expected=phase==='admin'?fixture.origin+'/admin/':pathToFileURL(path.join(root,'desktop/out/renderer/index.html')).href;
  for(let i=0;i<200;i++){
    try{page=(await(await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(p=>p.type==='page'&&p.url===expected);if(page)break;}catch{}
    await pause(100);
  }
  check(page,'Owned exact renderer unavailable');
  socket=new WebSocket(page.webSocketDebuggerUrl);
  socket.on('message',bytes=>{const data=JSON.parse(bytes.toString());if(data.method==='Network.responseReceived'&&new URL(data.params.response.url).pathname==='/api/auth/login')loginResponses.push(data.params.response.status);const request=pending.get(data.id);if(!request)return;clearTimeout(request.timer);pending.delete(data.id);data.error?request.reject(Error(data.error.message)):request.resolve(data.result);});
  await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject);});
  let info;
  if(phase!=='admin'){
    info=await evaluate(`window.electronAPI.invoke('app.info')`); // Read-only Main API precondition before credentials.
    check(info.platform==='win32'&&info.apiBaseUrl===fixture.origin,'Refusing credentials outside the owned loopback Main service');
  }
  if(phase==='register'){
    await caseRun('LIVE-DESKTOP-01',async()=>{await until(`Boolean(document.querySelector('input[type=email]'))`);check(info.nativeInputSupported&&!info.accessibilitySupported&&!info.correctionObservationSupported,'Windows capability boundary differs');return{platform:info.platform,microphone:info.microphone,api_origin:info.apiBaseUrl,storage:await privacy()};});
    await caseRun('LIVE-DESKTOP-02',async()=>{await fill('input[type=email]',email);await fill('input[type=password]',credentials.password+'wrong');await click('form button[type=submit]');await until(`document.body.innerText.includes('邮箱或密码错误')`);check(await evaluate(`!document.querySelector('nav')`),'Invalid credentials authenticated');return{rejected:true};});
    await caseRun('LIVE-DESKTOP-03',async()=>{await button('注册');await fill('input[type=email]',email);await fill('input[autocomplete="new-password"][placeholder="至少 8 个字符"]',credentials.password);await fill('input[placeholder="再次输入密码"]',credentials.password);await click('form button[type=submit]');await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);return{email,automatic_login:true,storage:await privacy()};});
    await caseRun('LIVE-DESKTOP-04',async()=>{if(info.microphone.status==='denied')await until(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`);else if(!await evaluate(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`))await click('button[title="设置"]');await button('设置');await until(`document.body.innerText.includes('系统权限')&&document.body.innerText.includes('麦克风')`);const text=await evaluate(`document.querySelector('[aria-label="关闭设置"]').closest('.fixed').innerText`);check(text.includes('Windows')&&!text.includes('辅助功能'),'Windows permissions UI still exposes AX');if(info.microphone.status==='denied')check(text.includes('已被系统禁用'),'Actual denied microphone status not visible');return{microphone:info.microphone,windows_permissions_visible:true,automatic_guide_required:info.microphone.status==='denied'};});
    await caseRun('LIVE-DESKTOP-05',async()=>{await closeSettings();if(info.microphone.status!=='denied')throw Error('BLOCKED: normal hosted microphone is not denied; no permission return is forced');await click('[aria-label="开始录音"]');await until(`document.body.innerText.includes('麦克风')&&(document.body.innerText.includes('隐私')||document.body.innerText.includes('权限'))`);check(await evaluate(`Boolean(document.querySelector('[aria-label="开始录音"]'))&&!document.querySelector('[aria-label="停止录音"]')`),'Denied microphone entered recording');return{denied_before_recording:true,storage:await privacy()};});
  }else if(phase==='boot'){
    await caseRun('LIVE-DESKTOP-06',async()=>{await until(`Boolean(document.querySelector('nav'))&&document.body.innerText.includes(${JSON.stringify(email)})`);check(!await evaluate(`document.body.innerText.includes('仅本次内存登录')`),'Restored account fell back to memory only');check(!await evaluate(`Boolean(document.querySelector('[aria-label="关闭设置"]'))`),'Restoring an account unexpectedly opened a permission guide');return{restored_without_retyping_password:true,email,storage:await privacy()};});
  }else if(phase==='logout'){
    await caseRun('LIVE-DESKTOP-07',async()=>{await closeSettings();await click('button[title="退出登录"]');await until(`Boolean(document.querySelector('input[type=email]'))&&!document.querySelector('nav')`);return{logged_out:true,storage:await privacy()};});
  }else{
    await cdp('Network.enable'); // Status only; never request bodies, headers or response tokens.
    async function login(who){await fill('#email',who);await fill('#password',credentials.password);await click('form button[type=submit]');}
    await caseRun('LIVE-ADMIN-01',async()=>{await until(`Boolean(document.querySelector('#email'))`);await login(email);await until(`document.body.innerText.includes('需要管理员权限')&&!document.querySelector('nav')`);check(loginResponses.at(-1)===403,'Ordinary user was not actually refused by HTTP403');return{ordinary_user_rejected:true,http_status:403};});
    await caseRun('LIVE-ADMIN-02',async()=>{await login('admin-live@example.test');await until(`Boolean(document.querySelector('nav'))`);const stats=`Object.fromEntries([...document.querySelectorAll('.stat-label')].map(e=>[e.textContent.trim(),e.parentElement.querySelector('.stat-value')?.textContent.trim()]))`;await until(`(${stats})['会话数']==='0'&&(${stats})['注册用户']==='2'`);const actual=await evaluate(stats);check(actual['已插入']==='0','Admin invented an insertion');return{metrics:actual,oracle:'one seeded admin + one real desktop registration, zero audio sessions'};});
    await caseRun('LIVE-ADMIN-03',async()=>{await button('退出登录');await until(`Boolean(document.querySelector('#email'))&&!document.querySelector('nav')`);return{logged_out:true,storage:await privacy()};});
  }
})().catch(error=>{report.infrastructure_error=error.message.replaceAll(credentials.password,'[redacted]');process.exitCode=1;}).finally(()=>{
  socket?.terminate();
  for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('Owned session closed'));}
  fs.writeFileSync(output,JSON.stringify(report,null,2),{flag:'wx'});
  if(report.cases.some(x=>x.status!=='pass'))process.exitCode=1;
});
