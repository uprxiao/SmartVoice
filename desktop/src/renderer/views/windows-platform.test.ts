// L1: render the actual reviewed UI with frozen local RPC data. No OS/permission claim.
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { AppInfo } from '@shared/types';
const f=vi.hoisted(()=>({info:null as AppInfo|null,locale:'zh-CN' as 'zh-CN'|'en',section:'settings',nullReads:0}));
vi.mock('react',async importOriginal=>{
  const real=await importOriginal<typeof import('react')>();
  return {...real,useState:(initial:unknown)=>real.useState(initial===null && f.nullReads++===0 ? f.info : initial==='settings'? f.section : initial)};
});
vi.mock('../stores',()=>({stores:{
  auth:{get settings(){return {learning_enabled:true,locale:f.locale,version:1};},user:{email:'fixture@example.test'}},
  session:{},ui:{get appInfo(){return f.info;},localShortcut:'Control+Shift+Space',autoStartBusy:false},
}}));
vi.mock('../lib/ipc',()=>({rpc:{app:{}}}));
import { SettingsView } from './SettingsView';
afterEach(() => vi.unstubAllGlobals());
beforeEach(()=>{
  vi.stubGlobal('localStorage',{getItem:()=>null});f.nullReads=0;f.locale='zh-CN';f.section='settings';
  f.info={version:'0.0.2',electron:'42',platform:'win32',accessibilityTrusted:false,accessibilitySupported:false,
    nativeInputSupported:true,correctionObservationSupported:false,microphone:{status:'denied',error:false},
    autoStart:{supported:true,configurable:true,registered:false,enabled:false,systemDisabled:false,error:false},apiBaseUrl:'http://127.0.0.1:17863',shortcut:'Control+Shift+Space'};
});
function render(){f.nullReads=0;return renderToStaticMarkup(createElement(SettingsView,{onClose:()=>undefined}));}
describe('Windows reviewed permission and capability presentation',()=>{
  it.each(['zh-CN','en'] as const)('%s has microphone/startup, no Mac grant action, and no cross-device learning toggle',locale=>{
    f.locale=locale;const html=render();
    expect(html).toContain(locale==='en'?'Start with Windows':'开机自启');
    expect(html).toContain('Ctrl + Shift + Space');
    expect(html).toContain(locale==='en'?'Unavailable in this version':'本版暂不支持');
    expect(html).not.toContain('请求权限');expect(html).not.toContain('Request permission');
    expect(html).not.toContain('辅助功能权限');expect(html).not.toContain('Accessibility permission');
    expect((html.match(/role="switch"/g)??[])).toHaveLength(1);
    const directory=process.env.SVI_WINDOWS_UI_SNAPSHOT_DIR;
    if(directory){mkdirSync(directory,{recursive:true});writeFileSync(join(directory,`windows-settings-${locale}.html`),html);}
  });
  it('shows the Windows help and unsigned install explanation instead of AX guidance',()=>{
    f.section='help';const html=render();expect(html).toContain('未签名');expect(html).toContain('未知控件');expect(html).not.toContain('辅助功能权限');
  });
  it('retains Mac AX/learning and omits Windows startup controls',()=>{
    f.info={...f.info!,platform:'darwin',accessibilitySupported:true,correctionObservationSupported:true};
    const html=render();expect(html).toContain('请求权限');expect(html).not.toContain('开机自启');expect(html).not.toContain('本版暂不支持');
  });
});
