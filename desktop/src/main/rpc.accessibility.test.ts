import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('electron',()=>({app:{},dialog:{},shell:{},systemPreferences:{getMediaAccessStatus:vi.fn(),askForMediaAccess:vi.fn()}}));
import { systemPreferences } from 'electron';
import { ACCESSIBILITY_MISSING_HINT, createRpcRouter, type RpcDeps } from './rpc';
// TC-INSERT-22: Accessibility hint at recording start and visible auto-insert failure.
const OP='11111111-1111-4111-8111-111111111111';
const platform=Object.getOwnPropertyDescriptor(process,'platform')!;
function fixture(trusted:boolean,outcome:Record<string,unknown>={status:'inserted',error_code:null,message:''}){
  const auth={current:{user:{id:'account-a'}},contextVersion:1,ensure:()=>auth.current};
  let busy=false;
  const sessions={isBusy:vi.fn(()=>busy),start:vi.fn(async()=>{busy=true;return {session_id:'session-a'};}),reset:vi.fn(),
    currentSessionId:()=> 'session-a',insert:vi.fn(async()=>outcome)};
  const native={call:vi.fn(async()=>({supported:true,trusted}))};
  const notifyHud=vi.fn();
  return {sessions,native,notifyHud,router:createRpcRouter({auth,sessions,native,notifyHud} as unknown as RpcDeps)};
}
beforeEach(()=>{
  vi.resetAllMocks();Object.defineProperty(process,'platform',{...platform,value:'darwin'});
  vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('granted');
});
afterEach(()=>{Object.defineProperty(process,'platform',platform);});

describe('Accessibility hint when recording starts',()=>{
  it('queries without a system prompt and shows the HUD hint, never blocking the recording',async()=>{
    const f=fixture(false);
    await expect(f.router.session.start(true,OP)).resolves.toMatchObject({session_id:'session-a'});
    await vi.waitFor(()=>expect(f.notifyHud).toHaveBeenLastCalledWith({kind:'permission',message:ACCESSIBILITY_MISSING_HINT,permission:true}));
    expect(f.native.call).toHaveBeenCalledExactlyOnceWith('permission',{prompt:false});
    expect(f.notifyHud).toHaveBeenNthCalledWith(1,null);
  });
  it('shows nothing when Accessibility is already granted',async()=>{
    const f=fixture(true);
    await f.router.session.start(true,OP);
    await vi.waitFor(()=>expect(f.native.call).toHaveBeenCalled());
    expect(f.notifyHud).toHaveBeenCalledExactlyOnceWith(null);
  });
});

describe('automatic insertion failure is surfaced on the HUD',()=>{
  it('Windows administrator denial preserves the platform reason without an AX action',async()=>{
    Object.defineProperty(process,'platform',{...platform,value:'win32'});
    const f=fixture(true,{status:'failed',error_code:'PERMISSION_DENIED',message:'目标窗口以管理员运行'});
    await f.router.session.insert('session-a',false,false);
    expect(f.notifyHud).toHaveBeenCalledWith({kind:'insert-failed',permission:false,message:'目标窗口以管理员运行',locale:'zh-CN'});
  });
  it('permission zero-write shows the failure card with the settings action',async()=>{
    const f=fixture(true,{status:'failed',error_code:'PERMISSION_DENIED',message:'外部输入需要辅助功能权限'});
    await f.router.session.insert('session-a',false,false);
    expect(f.notifyHud).toHaveBeenCalledExactlyOnceWith({kind:'insert-failed',permission:true,message:'未开启辅助功能，结果未输入'});
  });
  it('other zero-write keeps the native reason without the settings action',async()=>{
    const f=fixture(true,{status:'failed',error_code:'TARGET_CHANGED',message:'输入位置已改变'});
    await f.router.session.insert('session-a',false,false);
    expect(f.notifyHud).toHaveBeenCalledExactlyOnceWith({kind:'insert-failed',permission:false,message:'输入位置已改变'});
  });
  it.each([
    ['unverified may already have written',{status:'unverified',error_code:'VERIFY_FAILED',message:''},false],
    ['inserted',{status:'inserted',error_code:null,message:''},false],
    ['explicit insertion from the main window',{status:'failed',error_code:'PERMISSION_DENIED',message:''},true],
  ])('no HUD card: %s',async(_name,outcome,explicit)=>{
    const f=fixture(true,outcome as Record<string,unknown>);
    await f.router.session.insert('session-a',false,explicit as boolean);
    expect(f.notifyHud).not.toHaveBeenCalled();
  });
});
