import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('electron',()=>({app:{},dialog:{},shell:{},systemPreferences:{getMediaAccessStatus:vi.fn(),askForMediaAccess:vi.fn()}}));
import { systemPreferences } from 'electron';
import { createRpcRouter, type RpcDeps } from './rpc';
const OP_A='11111111-1111-4111-8111-111111111111';
const OP_B='22222222-2222-4222-8222-222222222222';
const platform=Object.getOwnPropertyDescriptor(process,'platform')!;
const pending=<T>()=>{let resolve!:(value:T)=>void;const promise=new Promise<T>(yes=>{resolve=yes;});return {promise,resolve};};
function fixture(){
  const auth={current:{user:{id:'account-a'}},contextVersion:1,ensure:()=>auth.current};
  const sessions={isBusy:vi.fn(()=>false),start:vi.fn(async()=>({session_id:'session-a'})),reset:vi.fn()};
  return {auth,sessions,router:createRpcRouter({auth,sessions} as unknown as RpcDeps)};
}
beforeEach(()=>{
  vi.resetAllMocks();Object.defineProperty(process,'platform',{...platform,value:'darwin'});
  vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('granted');
});
afterEach(()=>{vi.useRealTimers();Object.defineProperty(process,'platform',platform);});
describe('microphone authorization precedes creation of a server audio session',()=>{
  it('does not consume the server audio-idle budget while the OS prompt is pending over ten seconds',async()=>{
    vi.useFakeTimers();const f=fixture(),permission=pending<boolean>();
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockReturnValue(permission.promise);
    const start=f.router.session.start(true,OP_A);
    await vi.advanceTimersByTimeAsync(11_000);expect(f.sessions.start).not.toHaveBeenCalled();
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('granted');permission.resolve(true);
    await expect(start).resolves.toMatchObject({session_id:'session-a'});
    expect(systemPreferences.askForMediaAccess).toHaveBeenCalledExactlyOnceWith('microphone');
    expect(f.sessions.start).toHaveBeenCalledExactlyOnceWith(true);
  });
  it.each(['denied','restricted'] as const)('rejects %s without creating a session or trying to bypass system settings',async status=>{
    const f=fixture();vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue(status);
    await expect(f.router.session.start(true,OP_A)).rejects.toThrow('MICROPHONE_PERMISSION_DENIED');
    expect(systemPreferences.askForMediaAccess).not.toHaveBeenCalled();expect(f.sessions.start).not.toHaveBeenCalled();
  });
  it('rejects an explicitly declined OS prompt without creating a server session',async()=>{
    const f=fixture();vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockResolvedValue(false);
    await expect(f.router.session.start(true,OP_A)).rejects.toThrow('MICROPHONE_PERMISSION_DENIED');
    expect(f.sessions.start).not.toHaveBeenCalled();
  });
  it('does not treat a granted response as proof when the fresh system status is still unknown',async()=>{
    const f=fixture();vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockResolvedValue(true);
    await expect(f.router.session.start(true,OP_A)).rejects.toThrow('MICROPHONE_PERMISSION_UNAVAILABLE');
    expect(f.sessions.start).not.toHaveBeenCalled();
  });
  it('a permission grant arriving after cancel cannot create an orphan recording',async()=>{
    const f=fixture(),permission=pending<boolean>();
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockReturnValue(permission.promise);
    const start=f.router.session.start(true,OP_A);const rejection=expect(start).rejects.toThrow('录音操作已取消');
    f.router.session.cancelStart(OP_A);vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('granted');permission.resolve(true);
    await rejection;expect(f.sessions.start).not.toHaveBeenCalled();
  });
  it('an old account permission response cannot create a session for the new account',async()=>{
    const f=fixture(),permission=pending<boolean>();
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockReturnValue(permission.promise);
    const start=f.router.session.start(true,OP_A);const rejection=expect(start).rejects.toThrow('录音操作已取消');
    f.auth.current={user:{id:'account-b'}};f.auth.contextVersion++;
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('granted');permission.resolve(true);
    await rejection;expect(f.sessions.start).not.toHaveBeenCalled();
  });
  it('a stale permission response neither creates nor resets the newer start',async()=>{
    const f=fixture(),permission=pending<boolean>();
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockReturnValue(permission.promise);
    const old=f.router.session.start(true,OP_A);const rejection=expect(old).rejects.toThrow('录音操作已取消');
    f.router.session.cancelStart(OP_A);
    vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('granted');await f.router.session.start(false,OP_B);
    const resets=f.sessions.reset.mock.calls.length;permission.resolve(true);await rejection;
    f.router.session.cancelStart(OP_A);expect(f.sessions.reset).toHaveBeenCalledTimes(resets);
    expect(f.sessions.start).toHaveBeenCalledExactlyOnceWith(false);
  });
  it('granted access preserves the normal service error without showing another permission prompt',async()=>{
    const f=fixture(),unavailable=new Error('service unavailable');f.sessions.start.mockRejectedValue(unavailable);
    await expect(f.router.session.start(true,OP_A)).rejects.toBe(unavailable);
    expect(systemPreferences.askForMediaAccess).not.toHaveBeenCalled();
  });
  it('fails closed for an unknown microphone status without starting audio work',async()=>{
    const f=fixture();vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('unknown');
    await expect(f.router.session.start(true,OP_A)).rejects.toThrow('MICROPHONE_PERMISSION_UNAVAILABLE');
    expect(f.sessions.start).not.toHaveBeenCalled();expect(systemPreferences.askForMediaAccess).not.toHaveBeenCalled();
  });
  it('turns a failed OS status query into a specific check-permission error',async()=>{
    const f=fixture();vi.mocked(systemPreferences.getMediaAccessStatus).mockImplementation(()=>{throw new Error('OS query failed');});
    await expect(f.router.session.start(true,OP_A)).rejects.toThrow('MICROPHONE_PERMISSION_UNAVAILABLE');
    expect(f.sessions.start).not.toHaveBeenCalled();
  });
  it('does not call a failed OS request a user denial or start a session',async()=>{
    const f=fixture();vi.mocked(systemPreferences.getMediaAccessStatus).mockReturnValue('not-determined');
    vi.mocked(systemPreferences.askForMediaAccess).mockRejectedValue(new Error('OS request failed'));
    await expect(f.router.session.start(true,OP_A)).rejects.toThrow('MICROPHONE_PERMISSION_UNAVAILABLE');
    expect(f.sessions.start).not.toHaveBeenCalled();
  });
});
