import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { runInAction } from 'mobx';
import type { AuthSnapshot, InsertionOutcome, SessionProgress, SessionResult, SessionStartResult } from '@shared/types';
const doubles=vi.hoisted(()=>({listeners:new Map<string,(value:unknown)=>void>(), start:vi.fn().mockResolvedValue(undefined), stop:vi.fn().mockResolvedValue(undefined), acknowledge:vi.fn()}));
vi.mock('../audio/recorder',async importOriginal=>({...await importOriginal<typeof import('../audio/recorder')>(),PcmRecorder:class {start=doubles.start;stop=doubles.stop;acknowledge=doubles.acknowledge;}}));
vi.mock('../lib/ipc',()=>({
  events:{on:(event:{name:string},callback:(value:unknown)=>void)=>{doubles.listeners.set(event.name,callback);return ()=>doubles.listeners.delete(event.name);},emit:vi.fn()},
  rpc:{session:{start:vi.fn(),captureReady:vi.fn(),cancelStart:vi.fn().mockResolvedValue(undefined),cancel:vi.fn(),status:vi.fn(),finish:vi.fn(),insert:vi.fn(),retry:vi.fn(),reset:vi.fn().mockResolvedValue(undefined)},history:{selectForRetry:vi.fn()},auth:{},settings:{}}
}));
import { rpc } from '../lib/ipc';
import { RecorderError } from '../audio/recorder';
import { AuthStore } from './auth-store';
import { SessionStore } from './session-store';
import type { UiStore } from './ui-store';
const USER='11111111-1111-4111-8111-111111111111',SESSION='22222222-2222-4222-8222-222222222222';
const RESULT={result_id:'33333333-3333-4333-8333-333333333333',raw_text:'测试',refined_text:'测试',review_required:false} as SessionResult;
const progress=(patch:Partial<SessionProgress>={}):SessionProgress=>({acknowledged_samples:0,session_id:SESSION,attempt_no:1,revision:1,auto_insert_allowed:false,capture_ready:true,state:'streaming',audio_ms:0,stable_text:'',provisional_text:'',error_code:null,result:null,...patch});
const snapshot=()=>({user:{id:USER},settings:{learning_enabled:true,locale:'zh-CN'}} as AuthSnapshot);
const started={session_id:SESSION,attempt_no:1,insertion_available:true,target_app:'TestEdit',target_error:null} satisfies SessionStartResult;
const pending=<T>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>{resolve=done;});return {promise,resolve};};
const flush=async()=>{for(let n=0;n<12;n++)await Promise.resolve();};
const stores:SessionStore[]=[];
function fixture(loggedIn=true){const auth=new AuthStore();if(loggedIn)auth.snapshot=snapshot();const ui={toast:vi.fn()};const store=new SessionStore(auth,ui as unknown as UiStore);stores.push(store);store.init();return {auth,ui,store};}
beforeEach(()=>{
 vi.clearAllMocks(); doubles.listeners.clear();
 vi.mocked(rpc.session.status).mockResolvedValue(progress({session_id:null,attempt_no:null,state:'created',revision:0}));
 vi.mocked(rpc.session.captureReady).mockImplementation(async (sessionId, attemptNo) => progress({ session_id: sessionId, attempt_no: attemptNo, capture_ready: true, revision: 2 }));
 vi.mocked(rpc.session.start).mockResolvedValue(started); vi.mocked(rpc.session.cancel).mockResolvedValue(progress({state:'cancelled',revision:4}));
});
afterEach(()=>{for(const store of stores.splice(0))store.invalidateLocal();});
describe('result-time target metadata', () => {
 it('requests automatic insertion without a recording-time target and adopts later Main metadata', async () => {
  const {store} = fixture();
  vi.mocked(rpc.session.start).mockResolvedValueOnce({...started,insertion_available:false,target_app:null});
  vi.mocked(rpc.session.insert).mockResolvedValueOnce({status:'inserted',observable:true,error_code:null,message:'已输入',insertion_id:'test',review_required:false});
  await store.start();
  const result = progress({state:'ready',result:RESULT,revision:10,auto_insert_allowed:true});
  doubles.listeners.get('session.result')?.(result); await flush();
  expect(rpc.session.insert).toHaveBeenCalledExactlyOnceWith(SESSION,false,false);
  doubles.listeners.get('session.progress')?.({...result,revision:11,target:{insertion_available:true,target_app:'Chrome',target_error:null}});
  expect(store.targetApp).toBe('Chrome'); expect(store.insertionAvailable).toBe(true);
  expect(rpc.session.insert).toHaveBeenCalledTimes(1);
 });
});
describe('VOICE-15 consecutive dictations keep completed results in history', () => {
 it.each([false, true])('starts again without copying or confirming the previous result (review=%s)', async review => {
  const {store,ui}=fixture(); await store.start();
  doubles.listeners.get('session.result')?.(progress({state:'ready',result:{...RESULT,review_required:review},revision:10}));
  const next='88888888-8888-4888-8888-888888888888';
  const creation=pending<SessionStartResult>();
  vi.mocked(rpc.session.start).mockReturnValueOnce(creation.promise);
  const starting=store.toggle();
  expect.soft(rpc.session.start).toHaveBeenCalledTimes(2);
  expect.soft(store.phase).toBe('starting');
  expect.soft(store.result).toBeNull();
  expect.soft(store.insertionAvailable).toBe(false);
  expect.soft(store.targetApp).toBeNull();
  // A late result for the completed dictation cannot become the new target/result.
  doubles.listeners.get('session.result')?.(progress({state:'ready',result:RESULT,revision:999,auto_insert_allowed:true}));
  creation.resolve({...started,session_id:next,insertion_available:false,target_app:null,target_error:'没有输入目标'});
  await starting;
  expect(store.phase).toBe('recording'); expect(store.progress.session_id).toBe(next);
  expect(store.result).toBeNull(); expect(store.insertionAvailable).toBe(false);
  expect(rpc.session.insert).not.toHaveBeenCalled();
  expect(ui.toast).not.toHaveBeenCalledWith('请先处理当前结果，再开始录音','warning');
 });
});
describe('microphone denial stays local and requires an explicit new recording', () => {
 it.each([
  {locale:'zh-CN',code:'MICROPHONE_PERMISSION_DENIED',text:'麦克风权限被拒绝'},
  {locale:'en',code:'MICROPHONE_PERMISSION_DENIED',text:'Microphone access was denied'},
  {locale:'zh-CN',code:'MICROPHONE_PERMISSION_UNAVAILABLE',text:'无法确认麦克风权限'},
  {locale:'en',code:'MICROPHONE_PERMISSION_UNAVAILABLE',text:'Microphone access could not be verified'},
 ] as const)('explains Main preflight $code in $locale before any recorder/session exists',async({locale,code,text})=>{
  const {auth,store,ui}=fixture();
  runInAction(()=>{auth.snapshot={...snapshot(),settings:{...snapshot().settings,locale}};});
  vi.mocked(rpc.session.start).mockRejectedValueOnce(new Error(`Error invoking remote method 'session.start': Error: ${code}`));
  await store.start();
  expect(store.phase).toBe('failed');expect(store.error).toContain(text);
  expect(store.error).not.toContain(code);expect(ui.toast).toHaveBeenLastCalledWith(store.error,'error');
  expect(doubles.start).not.toHaveBeenCalled();expect(rpc.session.cancel).not.toHaveBeenCalled();
  expect(rpc.session.start).toHaveBeenCalledTimes(1);
 });
 it.each(['zh-CN', 'en'] as const)('explains the system privacy allowance path in %s', async locale => {
  const { auth, store, ui } = fixture();
  runInAction(() => { auth.snapshot = { ...snapshot(), settings: { ...snapshot().settings, locale } }; });
  doubles.start.mockRejectedValueOnce(new RecorderError('MICROPHONE_PERMISSION_DENIED', '麦克风权限被拒绝'));
  await store.start();
  expect(store.phase).toBe('failed');
  expect(store.error).toContain(locale === 'en' ? 'Microphone access was denied' : '麦克风权限被拒绝');
  expect(store.error).toContain(locale === 'en' ? 'system privacy settings' : '系统隐私设置');
  expect(store.error).toContain(locale === 'en' ? 'Start recording' : '开始录音');
  expect(ui.toast).toHaveBeenLastCalledWith(store.error, 'error');
  expect(rpc.session.cancel).toHaveBeenCalledExactlyOnceWith(SESSION);
  expect(rpc.session.start).toHaveBeenCalledTimes(1);
  const next = '77777777-7777-4777-8777-777777777777';
  vi.mocked(rpc.session.start).mockResolvedValueOnce({ ...started, session_id: next });
  await store.start();
  expect(store.phase).toBe('recording'); expect(store.progress.session_id).toBe(next);
  expect(store.error).toBeNull();
 });

 it.each(['cancel', 'account'] as const)('ignores late microphone denial after %s invalidates the operation', async operation => {
  const { auth, store, ui } = fixture();
  let reject!: (error: Error) => void;
  doubles.start.mockReturnValueOnce(new Promise<void>((_resolve, fail) => { reject = fail; }));
  const starting = store.start(); await flush();
  if (operation === 'cancel') await store.cancel();
  else runInAction(() => { auth.snapshot = null; });
  reject(new RecorderError('MICROPHONE_PERMISSION_DENIED', '麦克风权限被拒绝')); await starting;
  expect(store.phase).toBe(operation === 'cancel' ? 'cancelled' : 'idle');
  expect(store.error).toBeNull(); expect(ui.toast).not.toHaveBeenCalled();
 });
});
describe('renderer operation identity and authoritative snapshots (AUTH-16/VOICE-07/API-19)',()=>{
 it('cancel during create sends a bound cancellation without waiting for a session ID',async()=>{
  const {store}=fixture();const creation=pending<SessionStartResult>();vi.mocked(rpc.session.start).mockReturnValueOnce(creation.promise);
  const starting=store.start();const operationId=vi.mocked(rpc.session.start).mock.calls[0][1];await store.cancel();
  expect(rpc.session.cancelStart).toHaveBeenCalledWith(operationId);creation.resolve(started);await starting;
  expect(doubles.start).not.toHaveBeenCalled();expect(store.phase).toBe('cancelled');
 });
 it('an old cancelled start cannot replace a later recording',async()=>{
  const {store}=fixture();const old=pending<SessionStartResult>();vi.mocked(rpc.session.start).mockReturnValueOnce(old.promise);
  const first=store.start();await store.cancel();await store.start();old.resolve({...started,session_id:'44444444-4444-4444-8444-444444444444'});await first;
  expect(store.progress.session_id).toBe(SESSION);expect(store.phase).toBe('recording');expect(doubles.start).toHaveBeenCalledTimes(1);
 });
 it('restores main results only after login is known, without automatically inserting them',async()=>{
  const {auth,store}=fixture(false);const status=pending<SessionProgress>();vi.mocked(rpc.session.status).mockReturnValueOnce(status.promise);
  runInAction(()=>{auth.snapshot=snapshot();});status.resolve(progress({state:'ready',result:RESULT,revision:20,auto_insert_allowed:true}));await flush();
  expect(store.result?.result_id).toBe(RESULT.result_id);expect(store.progress.auto_insert_allowed).toBe(false);expect(rpc.session.insert).not.toHaveBeenCalled();
 });
 it('late finish snapshots cannot replace newer ready state or clear insertion busy',async()=>{
  const {store}=fixture();await store.start();const finishing=pending<SessionProgress>();vi.mocked(rpc.session.finish).mockReturnValueOnce(finishing.promise);
  const stopping=store.stop();await flush();doubles.listeners.get('session.progress')?.(progress({state:'ready',result:RESULT,revision:20}));
  const insertion=pending<InsertionOutcome>();vi.mocked(rpc.session.insert).mockReturnValueOnce(insertion.promise);const inserting=store.insert(false);
  expect(store.busy).toBe(true);finishing.resolve(progress({state:'refining',revision:10}));await stopping;
  expect(store.progress.revision).toBe(20);expect(store.phase).toBe('ready');expect(store.busy).toBe(true);
  insertion.resolve({status:'inserted',observable:true,error_code:null,message:'ok',insertion_id:null,review_required:false});await inserting;expect(store.busy).toBe(false);
 });
 it('returns server ACK credit to the worklet for the matching session only',async()=>{
  const {store}=fixture();await store.start();doubles.listeners.get('session.progress')?.(progress({revision:10,acknowledged_samples:4000}));
  doubles.listeners.get('session.progress')?.(progress({session_id:'old-session',revision:11,acknowledged_samples:8000}));
  expect(doubles.acknowledge).toHaveBeenCalledTimes(1);expect(doubles.acknowledge).toHaveBeenCalledWith(4000);
 });
});


// TC-API-19: public Store methods + the existing test-only IPC callback adapter.
// These are not Electron IPC/DOM or native-write acceptance evidence.
describe('API-19 local cancellation fences delayed result events', () => {
 it('ignores an old operation ready event while its cancel RPC is still pending', async () => {
  const { store } = fixture();
  await store.start();
  doubles.listeners.get('session.progress')?.(progress({ revision: 10, state: 'refining', stable_text: '当前部分稿' }));
  const cancelled = pending<SessionProgress>();
  vi.mocked(rpc.session.cancel).mockReturnValueOnce(cancelled.promise);
  vi.mocked(rpc.session.insert).mockResolvedValue({ status: 'unavailable', observable: false, error_code: 'TARGET_UNSUPPORTED', message: '测试替身不执行输入', insertion_id: null, review_required: false });
  const cancelling = store.cancel();
  await flush();
  try {
   expect(rpc.session.cancel).toHaveBeenCalledWith(SESSION);
   expect(store.phase).toBe('cancelled');
   // This snapshot was emitted by the operation cancelled above. Its revision
   // exceeds the last delivered one, so a revision check alone cannot fence it.
   doubles.listeners.get('session.result')?.(progress({ revision: 11, state: 'ready', result: RESULT, auto_insert_allowed: true }));
   await flush();
   expect.soft(store.phase).toBe('cancelled');
   expect.soft(store.result).toBeNull();
   expect.soft(store.partialText).toBe('当前部分稿');
   expect.soft(rpc.session.insert).not.toHaveBeenCalled();
  } finally {
   cancelled.resolve(progress({ revision: 12, state: 'cancelled' }));
   await cancelling;
  }
  expect(store.phase).toBe('cancelled');
  expect(store.result).toBeNull();
 });

 it('accepts a new recording after cancellation and dispatches its fresh result only once', async () => {
  const { store } = fixture();
  await store.start();
  await store.cancel();
  const nextSession = '55555555-5555-4555-8555-555555555555';
  vi.mocked(rpc.session.start).mockResolvedValueOnce({ ...started, session_id: nextSession });
  vi.mocked(rpc.session.insert).mockResolvedValue({ status: 'unavailable', observable: false, error_code: 'TARGET_UNSUPPORTED', message: '测试替身不执行输入', insertion_id: null, review_required: false });
  await store.start();
  // A high revision cannot authorize a different session's delayed result.
  doubles.listeners.get('session.result')?.(progress({ revision: 999, state: 'ready', result: RESULT, auto_insert_allowed: true }));
  expect(store.result).toBeNull();
  expect(rpc.session.insert).not.toHaveBeenCalled();
  const fresh = progress({ session_id: nextSession, revision: 20, state: 'ready', result: RESULT, auto_insert_allowed: true });
  doubles.listeners.get('session.progress')?.(fresh);
  doubles.listeners.get('session.result')?.(fresh);
  await flush();
  expect(store.phase).toBe('ready');
  expect(store.result?.result_id).toBe(RESULT.result_id);
  expect(rpc.session.insert).toHaveBeenCalledTimes(1);
  expect(rpc.session.insert).toHaveBeenCalledWith(nextSession, false, false);
  // Same-revision progress/result is intentional in production publish();
  // replaying the result must not dispatch a second automatic insert command.
  doubles.listeners.get('session.result')?.(fresh);
  await flush();
  expect(rpc.session.insert).toHaveBeenCalledTimes(1);
 });
});


// Positive controls for the cancellation epoch fence: restore and explicit
// retry stay usable through the public Store methods and IPC event adapter.
describe('API-19 restore and retry remain live after adding an event epoch fence', () => {
 it('binds a restored active session and accepts its later result without auto insert', async () => {
  const { auth, store } = fixture(false);
  const restored = pending<SessionProgress>();
  vi.mocked(rpc.session.status).mockReturnValueOnce(restored.promise);
  runInAction(() => { auth.snapshot = snapshot(); });
  restored.resolve(progress({ revision: 10, state: 'refining', auto_insert_allowed: true }));
  await flush();
  expect(store.progress.session_id).toBe(SESSION);
  expect(store.phase).toBe('refining');
  expect(store.progress.auto_insert_allowed).toBe(false);
  const fresh = progress({ revision: 11, state: 'ready', result: RESULT, auto_insert_allowed: false });
  doubles.listeners.get('session.progress')?.(fresh);
  doubles.listeners.get('session.result')?.(fresh);
  await flush();
  expect(store.phase).toBe('ready');
  expect(store.result?.result_id).toBe(RESULT.result_id);
  expect(store.progress.auto_insert_allowed).toBe(false);
  expect(rpc.session.insert).not.toHaveBeenCalled();
 });

 it('keeps explicit retry after failure live and rejects the prior attempt result', async () => {
  const { store } = fixture();
  await store.start();
  doubles.listeners.get('session.result')?.(progress({ revision: 10, state: 'failed', error_code: 'ASR_FAILED' }));
  const retried = pending<SessionProgress>();
  vi.mocked(rpc.session.retry).mockReturnValueOnce(retried.promise);
  const retrying = store.retry('asr');
  expect(rpc.session.retry).toHaveBeenCalledWith(SESSION, 'asr');
  doubles.listeners.get('session.progress')?.(progress({ revision: 11, attempt_no: 2, state: 'finalizing', auto_insert_allowed: false }));
  const result2 = { ...RESULT, result_id: '66666666-6666-4666-8666-666666666666', attempt_no: 2 };
  const fresh = progress({ revision: 12, attempt_no: 2, state: 'ready', result: result2, auto_insert_allowed: false });
  try {
   doubles.listeners.get('session.result')?.(progress({ revision: 999, attempt_no: 1, state: 'ready', result: RESULT, auto_insert_allowed: true }));
   expect.soft(store.progress.attempt_no).toBe(2);
   expect.soft(store.phase).toBe('finalizing');
   expect.soft(store.result).toBeNull();
   expect.soft(rpc.session.insert).not.toHaveBeenCalled();
   doubles.listeners.get('session.progress')?.(fresh);
   doubles.listeners.get('session.result')?.(fresh);
  } finally {
   retried.resolve(fresh);
   await retrying;
  }
  expect(store.phase).toBe('ready');
  expect(store.progress.attempt_no).toBe(2);
  expect(store.result?.result_id).toBe(result2.result_id);
  expect(store.progress.auto_insert_allowed).toBe(false);
  expect(rpc.session.insert).not.toHaveBeenCalled();
 });
});

// Public Store + existing IPC test adapter, not real Electron/DOM proof.
describe('history selection binds a new renderer generation before existing retry', () => {
 it('preserves the current result on a denied selection and never starts recording', async () => {
  const {store}=fixture(); await store.start();
  doubles.listeners.get('session.result')?.(progress({state:'ready',result:RESULT,revision:10}));
  const before=store.progress;
  vi.mocked(rpc.history.selectForRetry).mockRejectedValueOnce(new Error('请先处理或关闭当前结果'));
  expect(await store.selectHistoryForRetry('55555555-5555-4555-8555-555555555555','refine')).toBe(false);
  expect(store.progress).toEqual(before); expect(store.busy).toBe(false);
  expect(rpc.session.retry).not.toHaveBeenCalled();
  expect(doubles.start).toHaveBeenCalledTimes(1); // Only the earlier public start.
 });

 it('selects a new snapshot, ignores old events and retries without automatic insert or microphone', async () => {
  const {store}=fixture(); await flush();
  const next='55555555-5555-4555-8555-555555555555';
  const selected=progress({session_id:next,state:'ready',result:RESULT,revision:20});
  vi.mocked(rpc.history.selectForRetry).mockResolvedValueOnce(selected);
  expect(await store.selectHistoryForRetry(next,'refine')).toBe(true);
  expect(store.progress.session_id).toBe(next); expect(store.progress.auto_insert_allowed).toBe(false);
  expect(store.insertionAvailable).toBe(false); expect(store.targetApp).toBeNull();
  doubles.listeners.get('session.result')?.(progress({state:'ready',result:RESULT,revision:999,auto_insert_allowed:true}));
  expect(store.progress.revision).toBe(20);
  const result2={...RESULT,attempt_no:2,attempt_kind:'refine' as const};
  const fresh=progress({session_id:next,state:'ready',result:result2,attempt_no:2,revision:22});
  vi.mocked(rpc.session.retry).mockResolvedValueOnce(fresh);
  await store.retry('refine');
  expect(rpc.session.retry).toHaveBeenCalledWith(next,'refine');
  expect(store.result?.attempt_no).toBe(2); expect(store.progress.auto_insert_allowed).toBe(false);
  expect(rpc.session.insert).not.toHaveBeenCalled(); expect(doubles.start).not.toHaveBeenCalled();
 });

 it('does not retry after a pending selection outlives account invalidation', async () => {
  const {auth,store}=fixture(); await flush(); const read=pending<SessionProgress>();
  vi.mocked(rpc.history.selectForRetry).mockReturnValueOnce(read.promise);
  const selecting=store.selectHistoryForRetry(SESSION,'refine');
  expect(store.busy).toBe(true);
  runInAction(()=>{auth.snapshot=null;});
  read.resolve(progress({state:'ready',result:RESULT,revision:20}));
  expect(await selecting).toBe(false);
  expect(store.result).toBeNull(); expect(store.progress.session_id).toBeNull();
  expect(rpc.session.retry).not.toHaveBeenCalled(); expect(doubles.start).not.toHaveBeenCalled();
 });

 it('blocks a duplicate click while selecting and does not authorize an unsafe response', async () => {
  const {store}=fixture(); await flush(); const read=pending<SessionProgress>();
  vi.mocked(rpc.history.selectForRetry).mockReturnValueOnce(read.promise);
  const selecting=store.selectHistoryForRetry(SESSION,'refine');
  expect(await store.selectHistoryForRetry(SESSION,'refine')).toBe(false);
  expect(rpc.history.selectForRetry).toHaveBeenCalledTimes(1);
  read.resolve(progress({state:'ready',result:RESULT,revision:20,auto_insert_allowed:true}));
  expect(await selecting).toBe(false); expect(store.result).toBeNull();
  expect(rpc.session.retry).not.toHaveBeenCalled(); expect(rpc.session.insert).not.toHaveBeenCalled();
 });
});


// The Recorder mock models its public begun promise; no Store private state is set.
describe('local capture start acknowledgement', () => {
 it('stays preparing until Recorder begun and the matching Main acknowledgement', async () => {
  const { store } = fixture(); const begun = pending<void>(); const mainReady = pending<SessionProgress>();
  doubles.start.mockReturnValueOnce(begun.promise);
  vi.mocked(rpc.session.captureReady).mockReturnValueOnce(mainReady.promise);
  const starting = store.start(); await flush();
  expect(store.phase).toBe('starting'); expect(rpc.session.captureReady).not.toHaveBeenCalled();
  begun.resolve(); await flush();
  expect(rpc.session.captureReady).toHaveBeenCalledExactlyOnceWith(SESSION, 1);
  expect(store.phase).toBe('starting');
  mainReady.resolve(progress({ capture_ready: true, revision: 2 })); await starting;
  expect(store.phase).toBe('recording'); expect(store.progress.capture_ready).toBe(true);
 });

 it('does not resurrect recording if a terminal event precedes begun', async () => {
  const { store } = fixture(); const begun = pending<void>(); doubles.start.mockReturnValueOnce(begun.promise);
  const starting = store.start(); await flush();
  doubles.listeners.get('session.progress')?.(progress({ state: 'failed', capture_ready: false, error_code: 'AUDIO_INTERRUPTED', revision: 20 }));
  begun.resolve(); await starting;
  expect(store.phase).toBe('failed'); expect(store.progress.capture_ready).toBe(false);
  expect(rpc.session.captureReady).not.toHaveBeenCalled();
 });

 it('does not resurrect recording if the Main acknowledgement arrives after terminal', async () => {
  const { store } = fixture(); const mainReady = pending<SessionProgress>();
  vi.mocked(rpc.session.captureReady).mockReturnValueOnce(mainReady.promise);
  const starting = store.start(); await flush();
  expect(rpc.session.captureReady).toHaveBeenCalledExactlyOnceWith(SESSION, 1);
  doubles.listeners.get('session.progress')?.(progress({ state: 'cancelled', capture_ready: false, revision: 20 }));
  mainReady.resolve(progress({ capture_ready: true, revision: 2 })); await starting;
  expect(store.phase).toBe('cancelled'); expect(store.progress.revision).toBe(20);
  expect(store.progress.capture_ready).toBe(false);
 });

 it('keeps the microphone-denial explanation after its own cancellation progress', async () => {
  const { store, ui } = fixture();
  doubles.start.mockRejectedValueOnce(new RecorderError('MICROPHONE_PERMISSION_DENIED', '麦克风权限被拒绝'));
  vi.mocked(rpc.session.cancel).mockImplementationOnce(async () => {
   const value = progress({ state: 'cancelled', capture_ready: false, revision: 20 });
   doubles.listeners.get('session.progress')?.(value); return value;
  });
  await store.start();
  expect(store.phase).toBe('failed'); expect(store.error).toContain('系统隐私设置');
  expect(ui.toast).toHaveBeenLastCalledWith(store.error, 'error');
 });
});
