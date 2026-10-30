import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('electron', () => ({app: {}, dialog: {showOpenDialog: vi.fn()}, shell: {}, systemPreferences: {getMediaAccessStatus:vi.fn(()=> 'granted')}}));
vi.mock('node:fs/promises', () => ({open: vi.fn()}));
import { dialog } from 'electron';
import { open } from 'node:fs/promises';
import { createRpcRouter, type RpcDeps } from './rpc';
const pending = <T>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return {promise, resolve}; };
function fixture() {
  const auth = {current: {user: {id: 'account-a'}}, contextVersion: 1, ensure: () => auth.current};
  const sessions = {isBusy: vi.fn(() => false), reset: vi.fn(), start: vi.fn(async () => ({session_id: 'session-a'})),
    currentSessionId:()=> '33333333-3333-4333-8333-333333333333',cancel:vi.fn(async()=>({state:'cancelled'}))};
  const native={invalidate:vi.fn()};
  const api = {importCsv: vi.fn().mockResolvedValue({created: 1})};
  return {auth, sessions, api,native, router: createRpcRouter({auth, sessions, api,native} as unknown as RpcDeps)};
}
beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());
describe('main commands preserve initiating account and operation identity (AUTH-16/VOICE-07)', () => {
  it('Windows cancellation invalidates its already-dispatched helper after checking the current session',async()=>{
    const platform=Object.getOwnPropertyDescriptor(process,'platform')!;
    Object.defineProperty(process,'platform',{...platform,value:'win32'});
    try{
      const f=fixture();expect(()=>f.router.session.cancel('44444444-4444-4444-8444-444444444444')).toThrow();
      expect(f.native.invalidate).not.toHaveBeenCalled();expect(f.sessions.cancel).not.toHaveBeenCalled();
      const cancelled=f.router.session.cancel('33333333-3333-4333-8333-333333333333');
      expect(f.sessions.cancel).toHaveBeenCalledOnce();expect(f.native.invalidate).toHaveBeenCalledOnce();
      expect(f.sessions.cancel.mock.invocationCallOrder[0]).toBeLessThan(f.native.invalidate.mock.invocationCallOrder[0]);
      await expect(cancelled).resolves.toEqual({state:'cancelled'});
    }finally{Object.defineProperty(process,'platform',platform);}
  });
  it('cancels the initiating start even before its session ID exists', async () => {
    const f=fixture(), creation=pending<{session_id: string}>(); f.sessions.start.mockReturnValueOnce(creation.promise);
    const starting=f.router.session.start(true, '11111111-1111-4111-8111-111111111111');
    f.router.session.cancelStart('11111111-1111-4111-8111-111111111111');
    expect(f.sessions.reset).toHaveBeenCalledOnce(); creation.resolve({session_id: 'session-a'}); await starting;
  });
  it('a cancelled old start command cannot reset a newer start binding', async () => {
    const f=fixture(); await f.router.session.start(true,'11111111-1111-4111-8111-111111111111');
    await f.router.session.start(true,'22222222-2222-4222-8222-222222222222');
    f.router.session.cancelStart('11111111-1111-4111-8111-111111111111'); expect(f.sessions.reset).not.toHaveBeenCalled();
  });
  it('a file dialog opened under an old account cannot import into a new account', async () => {
    const f=fixture(), selected=pending<{canceled: boolean; filePaths: string[]}>();
    vi.mocked(dialog.showOpenDialog).mockReturnValueOnce(selected.promise as never);
    const importing=f.router.dictionary.importCsv(); f.auth.current={user:{id:'account-b'}}; f.auth.contextVersion++;
    selected.resolve({canceled:false,filePaths:['/virtual/old.csv']}); await expect(importing).rejects.toThrow('账户已变化');
    expect(open).not.toHaveBeenCalled(); expect(f.api.importCsv).not.toHaveBeenCalled();
  });
  it('oversized files are rejected before their body is read and the handle is closed', async () => {
    const f=fixture(); vi.mocked(dialog.showOpenDialog).mockResolvedValueOnce({canceled:false,filePaths:['/virtual/large.csv']});
    const file={stat:vi.fn(async()=>({isFile:()=>true,size:1024*1024+1})),read:vi.fn(),close:vi.fn().mockResolvedValue(undefined)};
    vi.mocked(open).mockResolvedValueOnce(file as never);
    await expect(f.router.dictionary.importCsv()).rejects.toThrow('1 MiB'); expect(file.read).not.toHaveBeenCalled();
    expect(file.close).toHaveBeenCalledOnce(); expect(f.api.importCsv).not.toHaveBeenCalled();
  });
  it('a file growing after stat remains bounded to 1 MiB plus one byte', async () => {
    const f=fixture(); vi.mocked(dialog.showOpenDialog).mockResolvedValueOnce({canceled:false,filePaths:['/virtual/grows.csv']});
    const file={stat:vi.fn(async()=>({isFile:()=>true,size:0})),read:vi.fn(async(_buffer:Buffer,_offset:number,length:number)=>({bytesRead:length})),close:vi.fn().mockResolvedValue(undefined)};
    vi.mocked(open).mockResolvedValueOnce(file as never); await expect(f.router.dictionary.importCsv()).rejects.toThrow('1 MiB');
    expect(file.read).toHaveBeenCalledTimes(1); expect(file.read.mock.calls[0][2]).toBe(1024*1024+1); expect(file.close).toHaveBeenCalledOnce();
    expect(f.api.importCsv).not.toHaveBeenCalled();
  });
});
