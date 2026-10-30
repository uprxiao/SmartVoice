// Independent native registration owner/observer, never a product RPC double.
const {app,globalShortcut}=require('electron');
const fs=require('node:fs'),path=require('node:path');
const [directory,key]=process.argv.slice(2);
if(process.env.GITHUB_ACTIONS!=='true'||process.platform!=='win32'||!['Control+Alt+F9','Control+Alt+F10'].includes(key))throw Error('Owned Windows shortcut control required');
const folder=path.resolve(directory),root=path.resolve(process.env.SVI_SHORTCUT_CONTROL_ROOT||'');
if(!folder.startsWith(root+path.sep)||!fs.statSync(folder).isDirectory())throw Error('Explicit disposable control root required');
app.setPath('userData',folder);
app.whenReady().then(()=>{
 const registered=globalShortcut.register(key,()=>{});
 fs.writeFileSync(path.join(folder,'ready.json'),JSON.stringify({pid:process.pid,key,registered}),{flag:'wx'});
 setInterval(()=>{if(fs.existsSync(path.join(folder,'stop.json')))app.quit();},30);
 setTimeout(()=>app.quit(),45000);
});
app.on('will-quit',()=>globalShortcut.unregisterAll());
