// Actual component callbacks with controlled hooks/timers/API; not DOM/capacity evidence.
import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';import path from 'node:path';import vm from 'node:vm';import {createRequire} from 'node:module';import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),ts=createRequire(path.join(root,'package.json'))('typescript');
const sourceRoot=process.env.SVI_ADMIN_SOURCE_ROOT??path.join(root,'src');
const flush=()=>new Promise(r=>setImmediate(r));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const sample=(version=1,max=20)=>({config:{version,max_active_sessions:max,updated_at:'2026-10-19T00:00:00Z',updated_by:null},deployment:{admission_ceiling:100,effective_ceiling:100,active_pcm_budget_bytes:576000000,retry_pcm_budget_bytes:134217728,asr_execution_slots:1,llm_max_inflight:4},runtime:null,validation:{status:'unverified',report:null}});
function harness(){
 const cells=[],effects=[],timers=new Map(),reads=[],writes=[],errors=[];let cursor=0,id=0,stateWrites=0;
 const react={useState(v){const i=cursor++;if(!(i in cells))cells[i]=v;return[cells[i],v=>{stateWrites++;cells[i]=typeof v==='function'?v(cells[i]):v;}];},useRef(v){const i=cursor++;if(!(i in cells))cells[i]={current:v};return cells[i];},useEffect(callback,deps){const i=cursor++,old=cells[i];if(!old||deps.some((v,n)=>v!==old.deps[n])){const cell={deps:[...deps],cleanup:null};cells[i]=cell;effects.push(()=>{old?.cleanup?.();cell.cleanup=callback();});}}};
 const api={capacity(){const d=deferred();reads.push(d);return d.promise;},updateCapacity(version,value){const d=deferred();writes.push({version,value,...d});return d.promise;}};
 const window={confirm:()=>true,setInterval(callback,ms){const i=++id;timers.set(i,{callback,ms});return i;},clearInterval(i){timers.delete(i);}};
 const context=vm.createContext({window,Error}),modules=new Map(),allowed=new Set(['views/CapacityView.tsx','capacity.ts','lib.ts','date-range.ts','http.ts']);
 function load(name){if(modules.has(name))return modules.get(name).exports;assert.ok(allowed.has(name),name);
  const file=path.join(sourceRoot,name),out=ts.transpileModule(fs.readFileSync(file,'utf8'),{fileName:file,reportDiagnostics:true,compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}});assert.equal((out.diagnostics??[]).filter(d=>d.category===ts.DiagnosticCategory.Error).length,0);
  const m={exports:{}};modules.set(name,m);const require=s=>{if(s==='react')return react;if(s==='react/jsx-runtime')return{jsx:(type,props)=>({type,props:props??{}}),jsxs:(type,props)=>({type,props:props??{}})};if(s==='lucide-react')return new Proxy({},{get:(_,k)=>k});if(s==='../api')return{api};const n=path.posix.normalize(path.posix.join(path.posix.dirname(name),s));return load(n.endsWith('.ts')?n:n+'.ts');};
  new vm.Script('(function(require,module,exports){\n'+out.outputText+'\n})',{filename:file}).runInContext(context,{timeout:250})(require,m,m.exports);return m.exports;}
 const {CapacityView}=load('views/CapacityView.tsx');
 const all=n=>Array.isArray(n)?n.flatMap(all):n&&typeof n==='object'?[n,...all(n.props?.children)]:[];
 const texts=n=>Array.isArray(n)?n.map(texts).join(''):typeof n==='string'||typeof n==='number'?String(n):n&&typeof n==='object'?texts(n.props?.children):'';
 const render=()=>{cursor=0;const tree=CapacityView({onError:e=>errors.push(e)});while(effects.length)effects.shift()();return tree;};
 const button=(tree,label)=>all(tree).find(n=>n.type==='button'&&texts(n).trim()===label);
 const tick=()=>{assert.equal(timers.size,1,'One capacity poll timer is required');for(const t of timers.values()){assert.equal(t.ms,5000);t.callback();}};
 const close=()=>{for(const c of cells)if(c&&typeof c==='object'&&'cleanup'in c)c.cleanup?.();};return{render,all,texts,button,tick,close,reads,writes,timers,get stateWrites(){return stateWrites;}};
}
test('re-reads every 5 seconds without overlapping a pending request',async()=>{const h=harness();h.render();assert.equal(h.reads.length,1);h.tick();assert.equal(h.reads.length,1);h.reads[0].resolve(sample());await flush();h.render();h.tick();assert.equal(h.reads.length,2);h.tick();h.tick();assert.equal(h.reads.length,2);h.reads[1].resolve(sample(2,30));await flush();assert.ok(h.all(h.render()).some(n=>n.props?.label==='当前配置'&&n.props.value==='30 路'));h.close();assert.equal(h.timers.size,0);});
test('failed poll retains stale values and next timer recovers',async()=>{const h=harness();h.render();h.reads[0].resolve(sample());await flush();h.render();h.tick();h.reads[1].reject(new Error('owned poll failure'));await flush();let tree=h.render();assert.match(h.texts(tree),/以下为上次读取的配置/);assert.match(h.texts(tree),/owned poll failure/);assert.ok(h.all(tree).some(n=>n.props?.label==='当前配置'&&n.props.value==='20 路'));assert.equal(h.button(tree,'调整上限').props.disabled,true);h.tick();h.reads[2].resolve(sample(2,30));await flush();tree=h.render();assert.doesNotMatch(h.texts(tree),/owned poll failure/);assert.equal(h.button(tree,'调整上限').props.disabled,false);h.close();});
test('poll preserves draft and original CAS version and pauses during save',async()=>{const h=harness();h.render();h.reads[0].resolve(sample());await flush();let tree=h.render();h.button(tree,'调整上限').props.onClick();tree=h.render();h.all(tree).find(n=>n.type==='input'&&n.props.id==='capacity-value').props.onChange({target:{value:'37'}});h.render();h.tick();h.reads[1].resolve(sample(2,30));await flush();tree=h.render();assert.equal(h.all(tree).find(n=>n.type==='input'&&n.props.id==='capacity-value').props.value,'37');assert.match(h.texts(tree),/编辑基于版本 1、上限 20 路/);const p=h.all(tree).find(n=>n.type==='form').props.onSubmit({preventDefault(){}});assert.equal(h.writes.length,1);assert.equal(h.writes[0].version,1);assert.equal(h.writes[0].value,37);h.tick();assert.equal(h.reads.length,2);h.writes[0].resolve(sample(3,37));await p;h.render();h.tick();assert.equal(h.reads.length,3);h.reads[2].resolve(sample(3,37));await flush();h.close();});
test('unmount removes timer and ignores late result',async()=>{const h=harness();h.render();h.close();assert.equal(h.timers.size,0);const n=h.stateWrites;h.reads[0].resolve(sample());await flush();assert.equal(h.stateWrites,n);});

test('reserved PCM is visible separately from actual PCM and can be zero with active sessions', async () => {
 const h=harness();h.render();
 const first=sample();first.runtime={sampled_at:'2026-10-19T00:00:00Z',active_sessions:1,available_sessions:19,draining:false,
  asr_running:0,asr_partial_waiting:0,asr_finish_waiting:0,llm_running:1,llm_foreground_waiting:0,llm_classify_waiting:0,
  active_pcm_bytes:0,reserved_pcm_bytes:5760000,retry_pcm_bytes:0,window_seconds:300,sample_count:0,rejected_count:0,failed_count:0,fallback_count:0,stop_to_result_p50_ms:null,stop_to_result_p95_ms:null};
 h.reads[0].resolve(first);await flush();let tree=h.render();
 const row=()=>h.all(tree).find(n=>n.type==='tr'&&h.texts(n).startsWith('预留 PCM 字节'));
 assert.ok(row(),'the resource table must expose reserved PCM');assert.equal(h.texts(row()),'预留 PCM 字节576.0 万');
 h.tick();h.reads[1].resolve({...first,runtime:{...first.runtime,reserved_pcm_bytes:0}});await flush();tree=h.render();
 assert.equal(h.texts(row()),'预留 PCM 字节0');
 assert.ok(h.all(tree).some(n=>n.props?.label==='运行中'&&n.props.value==='1 路'));
 h.close();
});
