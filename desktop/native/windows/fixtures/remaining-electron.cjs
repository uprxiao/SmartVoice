// Real production modules in a disposable Electron host. No account, provider,
// network call, permission override or business RPC substitute is supplied.
const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path');
const {performance}=require('node:perf_hooks');
const [folderArg,profileArg,mode]=process.argv.slice(2);
if(process.platform!=='win32'||process.env.GITHUB_ACTIONS!=='true'||!['normal','restart','persist-failure'].includes(mode))throw Error('Owned Windows integration host required');
const root=path.resolve(__dirname,'../../..');
const folder=path.resolve(folderArg),profile=path.resolve(profileArg);
if(!folder.startsWith(path.resolve(process.env.SVI_REMAINING_ROOT)+path.sep)||!profile.startsWith(path.resolve(process.env.SVI_REMAINING_ROOT)+path.sep))throw Error('Disposable owned paths required');
app.setName('SmartVoiceInput');app.setPath('userData',profile);app.setAppPath(root);
const api=require(path.join(path.resolve(process.env.SVI_REMAINING_MODULE_DIR),'out/main/remaining-modules.cjs'));
let tray,win,bridge,pending,sessions,working=false,stopping=false,seq=0;
const seen=new Set(),children=new Set();
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const check=(ok,message)=>{if(!ok)throw Error(message);};
function write(name,value){const file=path.join(folder,name);fs.writeFileSync(file+'.tmp',JSON.stringify(value,null,2),{flag:'wx'});fs.renameSync(file+'.tmp',file);}
function track(child){if(!child||children.has(child))return;children.add(child);child.once('exit',()=>{child.ownedExitConfirmed=true;});}
function state(){
 const main=api.getMainWindow(),hud=api.getHudWindow();
 const ack=path.join(profile,'tray-state.json');let acknowledged=false;
 try{acknowledged=JSON.parse(fs.readFileSync(ack,'utf8')).closeHintAcknowledged===true;}catch{}
 return {pid:process.pid,mode,main:main?{hwnd:Number(main.getNativeWindowHandle().readBigUInt64LE()),visible:main.isVisible(),focused:main.isFocused(),destroyed:main.isDestroyed(),renderer_crashed:main.webContents.isCrashed(),renderer_id:main.webContents.id,background_throttling:main.webContents.getLastWebPreferences().backgroundThrottling}:null,
 hud:hud?{hwnd:Number(hud.getNativeWindowHandle().readBigUInt64LE()),visible:hud.isVisible(),focused:hud.isFocused()}:null,
 main_window_count:BrowserWindow.getAllWindows().filter(w=>w!==hud).length,
 acknowledged,ack_file_present:fs.existsSync(ack),pending,children:[...children].map(c=>({pid:c.pid,exit_confirmed:c.ownedExitConfirmed===true,exit_code:c.exitCode,signal:c.signalCode}))};
}
async function stopBridge(){if(bridge){bridge.dispose();bridge=null;}const end=Date.now()+3500;while([...children].some(c=>c.exitCode===null&&c.signalCode===null)&&Date.now()<end)await wait(20);check([...children].every(c=>c.exitCode!==null||c.signalCode!==null),'Owned real helper exit not confirmed');}
async function command(request){
 if(request.action.startsWith('session')){
  sessions??=await require('./remaining-sessions.cjs')(api,root,folder,track);
  return sessions.command(request);
 }
 switch(request.action){
  case 'state':return state();
  case 'show':win=await api.createMainWindow({closeToTray:w=>void tray.close(w)});win.show();win.focus();return state();
  case 'concurrentWindows':{const windows=await Promise.all(Array.from({length:6},()=>api.createMainWindow({closeToTray:w=>void tray.close(w)})));return{ids:windows.map(w=>w.webContents.id),state:state()};}
  case 'destroyAndRebuild':{const old=api.getMainWindow();check(old&&!old.isDestroyed(),'Owned main missing');const oldId=old.webContents.id;old.destroy();const windows=await Promise.all(Array.from({length:6},()=>api.createMainWindow({closeToTray:w=>void tray.close(w)})));win=windows[0];win.show();return{old_id:oldId,ids:windows.map(w=>w.webContents.id),state:state()};}
  case 'crashAndReopen':{
   const old=api.getMainWindow();check(old&&!old.isDestroyed(),'Owned main missing');const oldId=old.webContents.id;
   const gone=new Promise(resolve=>old.webContents.once('render-process-gone',(_,details)=>resolve(details)));
   old.webContents.forcefullyCrashRenderer();
   const details=await Promise.race([gone,wait(4000).then(()=>{throw Error('Actual renderer crash event not observed');})]);
   const crashed={renderer_id:oldId,is_crashed:old.webContents.isCrashed(),event:details};
   const windows=await Promise.all(Array.from({length:6},()=>api.createMainWindow({closeToTray:w=>void tray.close(w)})));
   win=windows[0];win.show();return{crashed,ids:windows.map(w=>w.webContents.id),state:state()};
  }
  case 'hud':{check(['show','hide','move'].includes(request.state),'HUD state outside fixture');const hud=await api.createHudWindow();if(request.state==='hide')api.hideHud();else{if(request.state==='move')hud.setPosition(30+(request.iteration%5)*10,30+(request.iteration%3)*10,false);api.showHud();}return state();}
  case 'bridgePrepare':{await stopBridge();pending=null;bridge=new api.NativeVoiceInput();const permission=await bridge.call('permission');track(bridge.child);return{permission,pid:bridge.child?.pid,parent_pid:process.pid};}
  case 'bridgeWrite':{
   check(bridge?.child&&!pending,'Fresh real helper needed');check(['crash','timeout','invalidate'].includes(request.mode),'Unknown transport control');
   const child=bridge.child;child.stdout.pause(); // Deliberately lose transport delivery, not a fabricated helper result.
   const started=performance.now();pending={status:'pending',mode:request.mode,child_pid:child.pid,started_at:new Date().toISOString()};
   const current=pending;
   void bridge.call('insertCurrent',{insertion_id:'remaining-'+request.mode+'-'+(++seq),text:'中文🙂'}).then(result=>{if(pending===current)pending={...current,status:'resolved',result,elapsed_ms:performance.now()-started};},error=>{if(pending===current)pending={...current,status:'rejected',code:error.code,effect:error.effect,message:error.message,elapsed_ms:performance.now()-started};});
   return state();
  }
  case 'bridgeKill':{check(pending?.status==='pending'&&pending.mode==='crash'&&bridge?.child?.pid===pending.child_pid,'Owned pending crash target required');return{killed:bridge.child.kill(),pid:pending.child_pid};}
  case 'bridgeInvalidate':{check(pending?.status==='pending'&&pending.mode==='invalidate','Pending invalidation required');bridge.invalidate('owned integration cancellation');return state();}
  case 'bridgeRestart':{check(pending?.status==='rejected','Old write must have settled');const result=await bridge.call('permission');track(bridge.child);const old=[...children].find(c=>c.pid===pending.child_pid);old?.stdout.resume();await wait(50);return{permission:result,pid:bridge.child?.pid,late_old_stdout_resumed:true,state:state()};}
  case 'bridgeOwnWindow':{check(bridge,'Bridge required');const result=await bridge.call('insertCurrent',{insertion_id:'remaining-own-'+(++seq),text:'MUST NOT PASTE'});return{result,state:state()};}
  case 'bridgeDone':{await stopBridge();const result=state();pending=null;return result;}
  case 'quit':{await sessions?.close();await stopBridge();tray.dispose();api.markAppQuitting();stopping=true;setImmediate(()=>app.quit());return state();}
  default:throw Error('Unknown owned integration action');
 }
}
app.on('window-all-closed',()=>{});
app.whenReady().then(async()=>{
 // First-close failure is an actual filesystem error in this new profile.
 if(mode==='persist-failure')fs.mkdirSync(path.join(profile,'tray-state.json.tmp'));
 tray=new api.WindowsTray(path.join(root,'resources/icon.png'),async()=>{win=await api.createMainWindow({closeToTray:w=>void tray.close(w)});win.show();},()=> 'zh-CN');
 const mains=await Promise.all(Array.from({length:6},()=>api.createMainWindow({closeToTray:w=>void tray.close(w)})));
 win=mains[0];write('ready.json',{...state(),initial_ids:mains.map(w=>w.webContents.id),module_scope:'actual production window/tray/native bridge modules; no full Main login/audio/S5 pass'});
 setInterval(async()=>{if(working||stopping)return;working=true;try{for(const name of fs.readdirSync(folder).filter(n=>/^\d+\.request\.json$/.test(n)).sort((a,b)=>parseInt(a)-parseInt(b))){if(seen.has(name))continue;seen.add(name);const request=JSON.parse(fs.readFileSync(path.join(folder,name),'utf8'));let value;try{value=await command(request);}catch(error){value={error:error.message};}write(name.replace('.request.','.response.'),value);}}finally{working=false;}},30);
}).catch(error=>{write('host-failure.json',{error:error.message});app.exit(1);});
