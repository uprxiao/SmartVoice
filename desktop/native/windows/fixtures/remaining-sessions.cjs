// Controlled metadata/zero-PCM protocol fixture, with real production session,
// journal, bridge and native Windows effects. No microphone/model/S5 claim.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {performance}=require('node:perf_hooks');
const ts=require('typescript'),{WebSocketServer}=require('ws');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const check=(ok,message)=>{if(!ok)throw Error(message);};
const deferred=()=>{let resolve;const promise=new Promise(yes=>{resolve=yes;});return{promise,resolve};};
module.exports=async function(api,root,folder,track){
 check(api.WS_BASE_URL==='ws://127.0.0.1:17863','Refusing session fixture outside its owned loopback endpoint');
 const source=fs.readFileSync(path.join(root,'src/main/index.ts'),'utf8');
 const parsed=ts.createSourceFile('index.ts',source,ts.ScriptTarget.Latest,true),nodes=[];
 function visit(node){if(ts.isVariableDeclaration(node)&&node.name.getText(parsed)==='interrupt')nodes.push(node);ts.forEachChild(node,visit);}visit(parsed);
 check(nodes.length===1&&ts.isArrowFunction(nodes[0].initializer),'Exact production Main interrupt callback unavailable');
 const expression=nodes[0].initializer.getText(parsed);
 // Execute this frozen source expression unchanged, with actual production
 // objects. This injects a software boundary, not a physical OS sleep.
 const makeInterrupt=new Function('sessionService','native','hudNative','hudVisibility',`return ${expression};`);
 const server=new WebSocketServer({host:'127.0.0.1',port:17863});
 await new Promise((resolve,reject)=>{server.once('listening',resolve);server.once('error',reject);});
 let current=null;
 server.on('connection',(socket,request)=>{
  const scenario=current;
  if(!scenario||request.url!==`/ws/v1/sessions/${scenario.sessionId}/audio`||request.headers.authorization!=='Bearer fixture-only-no-secret'){socket.close(4403);return;}
  let samples=0;scenario.sockets.add(socket);
  const send=value=>{if(socket.readyState===1)socket.send(JSON.stringify(value));};
  send({type:'ready',session_id:scenario.sessionId,attempt_no:1,audio:{encoding:'pcm_s16le',sample_rate_hz:16000,channels:1,chunk_samples:4000,max_samples:2880000}});
  socket.on('message',(bytes,binary)=>{
   if(binary){check(bytes.length===8008&&bytes.readUInt32LE(4)===4000,'Unexpected fictional PCM frame');samples+=4000;send({type:'audio.ack',seq:bytes.readUInt32LE(0),total_samples:samples});return;}
   const message=JSON.parse(bytes.toString());
   if(message.type==='audio.end'){
    send({type:'audio.end_ack',attempt_no:1,last_seq:message.last_seq,total_samples:message.total_samples});
    send({type:'result.ready',session_id:scenario.sessionId,attempt_no:1,result:scenario.result});
   }else if(message.type==='cancel')send({type:'cancelled'});
  });
 });
 function facts(){
  const s=current;if(!s)return null;
  return {stage:s.stage,boundary:s.boundary,signal:s.signal,boundary_applied:s.applied,pending:s.pending,status:s.service.current(),claims:s.claims,writes:s.writes,receipts:s.receipts,marks:s.claims.map(claim=>s.journal.read(s.user,claim.insertion_id)).filter(Boolean),actual_write_response_held:s.actualWriteResponseHeld===true,buffered_response_bytes:s.oldChild?.stdout.readableLength??0,children:s.children.map(child=>({pid:child.pid,exit_code:child.exitCode,signal:child.signalCode})),interrupt_source_sha256:crypto.createHash('sha256').update(expression).digest('hex')};
 }
 function apply(s){
  check(!s.applied,'Boundary already applied');s.applied=true;
  if(s.boundary==='cancel')void s.service.cancel();
  else if(s.boundary==='account'){
   s.snapshot={user:{id:crypto.randomUUID()},settings:{learning_enabled:true,locale:'zh-CN'}};
   s.service.invalidateAccount();s.native.invalidate();
  }else s.interrupt();
 }
 async function dispose(){
  const s=current;if(!s)return;
  s.service.dispose();s.native.dispose();s.gate.resolve();
  for(const socket of s.sockets)socket.terminate();
  const end=Date.now()+3500;while(s.children.some(c=>c.exitCode===null&&c.signalCode===null)&&Date.now()<end)await pause(20);
  check(s.children.every(c=>c.exitCode!==null||c.signalCode!==null),'Session helper exit unconfirmed');current=null;
 }
 return {
  async command(request){
   switch(request.action){
    case 'sessionPrepare':{
     await dispose();check(['prepare','claim','journal','helper','late'].includes(request.stage)&&['cancel','account','suspend'].includes(request.boundary),'Unknown frozen session parameter');
     const user=crypto.randomUUID(),sessionId=crypto.randomUUID(),directory=path.join(folder,'journal-'+crypto.randomUUID());fs.mkdirSync(directory);
     const s={stage:request.stage,boundary:request.boundary,user,sessionId,snapshot:{user:{id:user},settings:{learning_enabled:true,locale:'zh-CN'}},gate:deferred(),signal:null,applied:false,pending:null,claims:[],writes:[],receipts:[],children:[],sockets:new Set(),gateUsed:false};current=s;
     s.result={result_id:crypto.randomUUID(),attempt_no:1,attempt_kind:'initial',raw_text:'中文🙂',refined_text:'中文🙂',review_required:false,review_reason:null,text_hash:crypto.createHash('sha256').update('中文🙂').digest('hex'),dictionary_version:0,stop_to_result_ms:1,created_at:new Date().toISOString()};
     const session=()=>({session_id:s.sessionId,mode:'dictation',state:'created',learning_enabled:false,current_attempt_no:1,current_result_id:null,audio_complete:false,received_samples:0,audio_ms:0,created_at:new Date().toISOString(),first_terminal_at:null,content_expires_at:null,content_expired:false,error_code:null,result:null,retry_kind:'none',retry_expires_at:null,retry_unavailable_reason:null,insertion_status:null});
     s.native=new api.NativeVoiceInput();const originalCall=s.native.call.bind(s.native);
     s.native.call=(action,data)=>{
      if(['insert','insertCurrent'].includes(action)){
       s.writes.push({action,insertion_id:data.insertion_id,at:new Date().toISOString()});
       if(['helper','late'].includes(s.stage)){s.oldChild=s.native.child;s.oldChild.stdout.pause();s.signal='dispatched';}
      }
      const response=originalCall(action,data);const child=s.native.child;
      if(['insert','insertCurrent'].includes(action))s.writeRequestId=s.native.seq;
      if(child&&!s.children.includes(child)){s.children.push(child);track(child);}return response;
     };
     const http={accessTokenValue:'fixture-only-no-secret',ensureFreshAccess:async()=>{
      if(s.stage==='prepare'&&!s.gateUsed){s.gateUsed=true;s.signal='prepare';await s.gate.promise;}
     },createSession:async()=>session(),cancelSession:async()=>({...session(),state:'cancelled'}),getSession:async()=>session(),
      claimInsertion:async(sid,body)=>{
       const claim={...body,session_id:sid,status:'reserved',observable:false,error_code:null,replayed:false,claimed_at:'',completed_at:null};s.claims.push(claim);
       if(s.stage==='claim'&&!s.gateUsed){s.gateUsed=true;s.signal='claim';await s.gate.promise;}return claim;
      },insertionReceipt:async(sid,id,body)=>{const receipt={...body,session_id:sid,insertion_id:id,result_id:s.result.result_id,replayed:false,claimed_at:'',completed_at:null};s.receipts.push(receipt);return receipt;},clearTokens:()=>{}};
     const auth={get current(){return s.snapshot;},get learningEnabled(){return s.snapshot.settings.learning_enabled;},ensure:()=>s.snapshot};
     s.journal=new api.InsertionJournal(directory);const begin=s.journal.begin.bind(s.journal);
     s.journal.begin=intent=>{const actual=begin(intent);if(s.stage==='journal'&&!s.gateUsed){s.gateUsed=true;s.signal='journal';apply(s);}return actual;};
     s.service=new api.SessionService(http,s.native,auth,s.journal,()=>{},()=>{});
     s.interrupt=makeInterrupt(s.service,s.native,{invalidate:()=>{}},{reset:()=>{}});
     await s.native.call('permission');return facts();
    }
    case 'sessionBegin':{
     const s=current;check(s&&!s.pending,'Fresh scenario required');s.pending={status:'pending'};
     void(async()=>{
      await s.service.start(false);
      if(s.stage==='prepare')return;
      s.service.sendAudio(new ArrayBuffer(8000),s.sessionId,performance.timeOrigin+performance.now());
      await s.service.finish();
      check(s.service.status.state==='ready','Controlled result was not ready');
      return await s.service.insert(false,false);
     })().then(value=>{s.pending={status:'resolved',value};},error=>{s.pending={status:'rejected',message:error.message};});return facts();
    }
    case 'sessionState':return facts();
    case 'sessionCollectResponse':{
     const s=current;check(s.stage==='late'&&s.oldChild,'Withheld real response control required');s.heldBytes??=[];
     let bytes;while((bytes=s.oldChild.stdout.read())!==null)s.heldBytes.push(bytes);
     const actual=Buffer.concat(s.heldBytes).toString('utf8').split('\n').filter(Boolean).flatMap(line=>{try{return[JSON.parse(line)];}catch{return[];}});
     s.actualWriteResponseHeld=actual.some(value=>value.id===s.writeRequestId&&['inserted','unverified','failed'].includes(value.status));return facts();
    }
    case 'sessionBoundary':{const s=current;apply(s);s.gate.resolve();if(s.stage==='late'){
     check(s.actualWriteResponseHeld,'Actual write response was not acquired');
     // Deliver exactly the bytes read from this owned old helper, unchanged.
     // The real bridge's child ownership guard must reject stale delivery.
     s.oldChild.stdout.emit('data',Buffer.concat(s.heldBytes));s.oldChild.stdout.resume();
    }return facts();}
    case 'sessionNewOperation':{
     const s=current;check(s.pending?.status!=='pending','Old operation still pending');s.service.reset();s.gateUsed=true;s.sessionId=crypto.randomUUID();
     const result=await s.service.start(false);check(s.service.status.result===null,'Old result entered new operation');await s.service.cancel();return{started:result,state:facts()};
    }
    case 'sessionDispose':{const before=facts();await dispose();return{before,exit_confirmed:true};}
    default:throw Error('Unknown session fixture action');
   }
  },
  async close(){await dispose();for(const client of server.clients)client.terminate();await new Promise(resolve=>server.close(resolve));}
 };
};
