import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const f=vi.hoisted(()=>({files:new Map<string,string>(),dialog:vi.fn(),write:vi.fn(),tray:{setToolTip:vi.fn(),setContextMenu:vi.fn(),on:vi.fn(),destroy:vi.fn(),isDestroyed:()=>false},menus:[] as unknown[]}));
vi.mock('electron',()=>({app:{getPath:()=>'/tmp/svi-tray-fixture',quit:vi.fn()},dialog:{showMessageBox:f.dialog},
  Tray:class {constructor(){return f.tray;}},Menu:{buildFromTemplate:(items:unknown[])=>{f.menus.push(items);return items;}}}));
vi.mock('node:fs',()=>({
  readFileSync:(file:string)=>{if(!f.files.has(file))throw new Error('missing');return f.files.get(file);},
  writeFileSync:(file:string,text:string)=>{f.write(file,text);f.files.set(file,text);},
  renameSync:(from:string,to:string)=>{f.files.set(to,f.files.get(from)!);f.files.delete(from);},
}));
import { WindowsTray } from './windows-tray';
import type { BrowserWindow } from 'electron';
let controller:WindowsTray;
const win=()=>({isDestroyed:()=>false,hide:vi.fn(),show:vi.fn()});
beforeEach(()=>{vi.clearAllMocks();f.files.clear();f.write.mockReset();f.dialog.mockResolvedValue({response:0});});
afterEach(()=>controller?.dispose());
describe('Windows close hint acknowledgement and lifetime',()=>{
  it('waits for acknowledgement, coalesces duplicate close, persists once and hides without destroying',async()=>{
    let resolve!:(v:unknown)=>void;f.dialog.mockReturnValueOnce(new Promise(done=>{resolve=done;}));
    const w=win();controller=new WindowsTray('owned.png',async()=>undefined,()=> 'zh-CN');
    const closing=controller.close(w as unknown as BrowserWindow);
    await controller.close(w as unknown as BrowserWindow);
    expect(w.hide).not.toHaveBeenCalled();expect(f.dialog).toHaveBeenCalledTimes(1);expect(f.write).not.toHaveBeenCalled();
    resolve({response:0});await closing;expect(w.hide).toHaveBeenCalledTimes(1);expect(f.write).toHaveBeenCalledTimes(1);
    await controller.close(w as unknown as BrowserWindow);expect(w.hide).toHaveBeenCalledTimes(2);expect(f.dialog).toHaveBeenCalledTimes(1);
    controller.dispose();controller=new WindowsTray('owned.png',async()=>undefined,()=> 'en');
    await controller.close(w as unknown as BrowserWindow);expect(f.dialog).toHaveBeenCalledTimes(1);
    expect(f.menus.at(-1)).toMatchObject([{label:'Open'},{label:'Exit'}]);
  });
  it('does not hide, write or revive a window after exit during the pending notice',async()=>{
    let resolve!:(v:unknown)=>void;f.dialog.mockReturnValueOnce(new Promise(done=>{resolve=done;}));
    const w=win();controller=new WindowsTray('owned.png',async()=>undefined,()=> 'en');
    const closing=controller.close(w as unknown as BrowserWindow);controller.dispose();resolve({response:0});await closing;
    expect(w.hide).not.toHaveBeenCalled();expect(w.show).not.toHaveBeenCalled();expect(f.write).not.toHaveBeenCalled();
  });
  it('keeps the window reachable when acknowledgement persistence fails',async()=>{
    f.write.mockImplementation(()=>{throw new Error('disk failure');});
    const w=win();controller=new WindowsTray('owned.png',async()=>undefined,()=> 'en');
    await controller.close(w as unknown as BrowserWindow);
    expect(w.hide).not.toHaveBeenCalled();expect(w.show).toHaveBeenCalledOnce();
    expect(f.dialog).toHaveBeenLastCalledWith(w,expect.objectContaining({type:'error'}));
  });
});
