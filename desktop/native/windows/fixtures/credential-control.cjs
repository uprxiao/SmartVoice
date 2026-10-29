// Standalone real Electron safeStorage control, never loaded by the product.
const {app,safeStorage}=require('electron');
const fs=require('node:fs'),path=require('node:path');
const option=name=>{const index=process.argv.indexOf(name);return index>=0?process.argv[index+1]:undefined;};
const owned=option('--control-dir'),phase=option('--control-phase'),exitKind=option('--control-exit');
if(process.platform!=='win32'||process.env.GITHUB_ACTIONS!=='true'||!['encrypt','decrypt'].includes(phase)||!['hard','graceful'].includes(exitKind))throw Error('Disposable Windows crypto control required');
const directory=path.resolve(owned||'');
const relative=path.relative(path.resolve(process.env.RUNNER_TEMP),directory);
const owner=JSON.parse(fs.readFileSync(path.join(directory,'owner.json'),'utf8'));
if(relative.startsWith('..')||path.isAbsolute(relative)||fs.lstatSync(directory).isSymbolicLink()||owner.schema!=='svi-safe-storage-control-v1'||owner.exit_kind!==exitKind)throw Error('Fresh owned crypto directory required');
app.setName('SVI Credential Control');
app.setPath('userData',directory);app.setPath('sessionData',directory);
const canary='SmartVoiceInput fictional safeStorage fixture';
function write(name,value){
  const target=path.join(directory,name),temporary=target+'.tmp';
  if(fs.existsSync(target))throw Error('Control result already exists');
  fs.writeFileSync(temporary,JSON.stringify(value,null,2),{flag:'wx'});fs.renameSync(temporary,target);
}
function facts(){
  const local=path.join(app.getPath('sessionData'),'Local State');
  let state;try{state=JSON.parse(fs.readFileSync(local,'utf8'));}catch{}
  return {user_data_is_owned:path.resolve(app.getPath('userData'))===directory,session_data_is_owned:path.resolve(app.getPath('sessionData'))===directory,local_state_present:fs.existsSync(local),local_state_parseable:Boolean(state),encrypted_key_present:Boolean(state?.os_crypt?.encrypted_key),app_bound_key_present:Boolean(state?.os_crypt?.app_bound_encrypted_key)};
}
app.whenReady().then(()=>{
  const report={scope:'standalone real safeStorage control; no account or product/UI pass',phase,exit_kind:exitKind,electron_version:process.versions.electron,arch:process.arch,available:safeStorage.isEncryptionAvailable(),observed_at:new Date().toISOString(),secret_values_recorded:false};
  try{
    if(!report.available)report.status='unavailable';
    else if(phase==='encrypt'){
      const cipher=safeStorage.encryptString(canary);fs.writeFileSync(path.join(directory,'cipher.private.bin'),cipher,{flag:'wx'});
      report.encrypted_bytes=cipher.length;report.status='encrypted';
    }else{
      report.decrypted_matches=safeStorage.decryptString(fs.readFileSync(path.join(directory,'cipher.private.bin')))===canary;
      report.status=report.decrypted_matches?'matches':'mismatch';
    }
  }catch(error){report.status='error';report.error_type=error.name;}
  report.profile=facts();write(phase+'.json',report);
  if(phase==='encrypt'&&exitKind==='hard'&&report.status==='encrypted')setInterval(()=>{},1000);
  else app.quit();
}).catch(()=>{app.exit(1);});
