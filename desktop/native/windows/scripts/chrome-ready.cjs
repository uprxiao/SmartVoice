// Disposable fixture preparation only: no UIA query, value setter or paste.
const fs=require('node:fs'),path=require('node:path');
const {fileURLToPath}=require('node:url');
const WebSocket=require('ws');
const [rawPort,expectedUrl,rawBudget,output]=process.argv.slice(2);
const port=Number(rawPort),budget=Number(rawBudget),url=new URL(expectedUrl);
const kind=url.searchParams.get('kind');
if(process.platform!=='win32'||process.env.GITHUB_ACTIONS!=='true'||!Number.isInteger(port)||port<1||port>65535||!Number.isInteger(budget)||budget<1||budget>45000||url.protocol!=='file:'||path.resolve(fileURLToPath(url))!==path.resolve(__dirname,'../fixtures/chrome.html')||!['input','textarea'].includes(kind))throw Error('Owned Chrome fixture required');
const started=performance.now(),end=started+budget;
const report={scope:'owned file DOM readiness only; no UIA warmup, paste or product pass',status:'not_run',kind,budget_ms:budget};
let socket,sequence=0;const pending=new Map();
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function cdp(method,params={}){return new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(Error('Fixture CDP deadline'));},Math.max(1,Math.min(2000,end-performance.now())));pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});}
(async()=>{
 let page;
 while(performance.now()<end){
  try{page=(await(await fetch(`http://127.0.0.1:${port}/json/list`,{signal:AbortSignal.timeout(1000)})).json()).find(p=>p.type==='page'&&p.url===expectedUrl);if(page)break;}catch{}
  await pause(100);
 }
 if(!page)throw Error('Owned exact fixture page not discovered');
 socket=new WebSocket(page.webSocketDebuggerUrl);
 socket.on('message',bytes=>{const value=JSON.parse(bytes.toString()),request=pending.get(value.id);if(!request)return;clearTimeout(request.timer);pending.delete(value.id);value.error?request.reject(Error(value.error.message)):request.resolve(value.result);});
 await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('Fixture socket not ready')),Math.max(1,Math.min(2000,end-performance.now())));socket.once('open',()=>{clearTimeout(timer);resolve();});socket.once('error',error=>{clearTimeout(timer);reject(error);});});
 await cdp('Page.bringToFront');
 while(performance.now()<end){
  const result=await cdp('Runtime.evaluate',{expression:`({ready:document.readyState,active:document.activeElement?.tagName?.toLowerCase(),label:document.activeElement?.getAttribute('aria-label'),value:document.activeElement?.value,has_focus:document.hasFocus()})`,returnByValue:true});
  if(result.exceptionDetails)throw Error('Fixture readiness evaluation failed');
  const state=result.result.value;report.last_dom=state;
  if(state.ready==='complete'&&state.active===kind&&state.label==='SVI fictional probe field'&&state.value==='BEGIN React END'){report.status='ready';return;}
  await pause(100);
 }
 throw Error('Fixture DOM input readiness deadline exceeded');
})().catch(error=>{report.status='blocked';report.error=error.message;process.exitCode=1;}).finally(()=>{
 socket?.terminate();for(const request of pending.values()){clearTimeout(request.timer);request.reject(Error('Fixture connection closed'));}
 report.elapsed_ms=performance.now()-started;fs.writeFileSync(output,JSON.stringify(report,null,2),{flag:'wx'});
});
