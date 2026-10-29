import { EventEmitter } from 'node:events';
import { afterEach,beforeEach,describe,expect,it,vi } from 'vitest';
const f=vi.hoisted(()=>({windows:[] as any[],load:vi.fn()}));
vi.mock('electron',()=>({app:{isPackaged:false},screen:{},BrowserWindow:class extends EventEmitter {
  destroyed=false;show=vi.fn();hide=vi.fn();webContents=new EventEmitter();
  constructor(readonly options:unknown){super();f.windows.push(this);}
  isDestroyed(){return this.destroyed;}
  destroy(){this.destroyed=true;this.emit('closed');}
  async loadFile(){await f.load();this.emit('ready-to-show');}
  async loadURL(){await this.loadFile();}
}}));
const platform=Object.getOwnPropertyDescriptor(process,'platform')!;
beforeEach(()=>{vi.resetModules();f.windows=[];f.load.mockReset().mockResolvedValue(undefined);vi.stubEnv('SVI_HIDDEN','');Object.defineProperty(process,'platform',{...platform,value:'win32'});});
afterEach(()=>{Object.defineProperty(process,'platform',platform);vi.unstubAllEnvs();});
describe('single main window including hidden audio renderer',()=>{
  it('coalesces concurrent creation, keeps background startup hidden and routes close to the tray',async()=>{
    const api=await import('./window');const close=vi.fn();
    const a=api.createMainWindow({hidden:true,closeToTray:close});const b=api.createMainWindow({hidden:true});
    expect(a).toBe(b);const w=await a;expect(f.windows).toHaveLength(1);expect(f.windows[0].show).not.toHaveBeenCalled();
    const preventDefault=vi.fn();w.emit('close',{preventDefault});expect(preventDefault).toHaveBeenCalledOnce();expect(close).toHaveBeenCalledWith(w);
    expect(f.windows[0].options.webPreferences.backgroundThrottling).toBe(false);
    expect(api.getMainWindow()).toBe(w);
  });
  it('recreates an unexpectedly destroyed main window even when other windows exist',async()=>{
    const api=await import('./window');const a=await api.createMainWindow();f.windows[0].destroy();
    const b=await api.createMainWindow();expect(a).not.toBe(b);expect(f.windows).toHaveLength(2);expect(api.getMainWindow()).toBe(b);
  });
  it('preserves Mac close-to-hide and allows a real quit to close',async()=>{
    Object.defineProperty(process,'platform',{...platform,value:'darwin'});
    const api=await import('./window');const w=await api.createMainWindow();const preventDefault=vi.fn();
    w.emit('close',{preventDefault});expect(f.windows[0].hide).toHaveBeenCalledOnce();
    api.markAppQuitting();w.emit('close',{preventDefault});expect(preventDefault).toHaveBeenCalledTimes(1);
  });
  it('does not reuse a broken window after a renderer load failure',async()=>{
    f.load.mockRejectedValueOnce(new Error('load failed'));
    const api=await import('./window');await expect(api.createMainWindow()).rejects.toThrow('load failed');
    expect(api.getMainWindow()).toBeNull();await api.createMainWindow();expect(f.windows).toHaveLength(2);
  });
});
