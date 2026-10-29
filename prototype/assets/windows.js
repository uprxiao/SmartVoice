/* S0 supplement only: fictional state, no OS calls, account, recording or clipboard access. */
'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const scenes = {
  settings: {default:'默认', 'mic-denied':'麦克风被禁用', 'mic-unknown':'麦克风状态未知', checking:'检查中', 'check-error':'检查失败', 'autostart-on':'自启已开启', 'autostart-blocked':'自启被系统禁用', 'autostart-saving':'自启保存中', 'autostart-error':'自启保存失败', 'shortcut-reserved':'输入法保留键', 'shortcut-error':'快捷键注册失败'},
  onboarding: {default:'初次使用', 'mic-denied':'麦克风被禁用', 'mic-unknown':'麦克风状态未知', checking:'检查中', granted:'麦克风已允许'},
  hud: {recording:'录音中', 'higher-integrity':'管理员窗口', protected:'受保护输入框', 'target-changed':'写入前目标变化', 'helper-unavailable':'输入组件不可用', unverified:'无法核验', dismissed:'提示已关闭'},
  tray: {default:'主窗口已打开', 'first-close':'首次关闭', hidden:'已隐藏到托盘', autostart:'开机进入托盘', exited:'已退出'}
};
let route;
let state = {autostart:false, mic:'granted', access:false, closedOnce:false, menu:false, tab:'settings'};
let toastTimer, saveTimer;
const win = () => route.platform === 'win32';
const button = (label, action, disabled=false) => `<button class="small-btn" data-action="${action}" ${disabled?'disabled':''}>${label}</button>`;
const pill = (text, tone='') => `<span class="pill ${tone}" role="status">${text}</span>`;
const row = (title, hint, control='') => `<div class="setting-row"><div><b>${title}</b><p>${hint}</p></div><div class="platform-control">${control}</div></div>`;
function go(page=route.page, scene='default', platform=route.platform) {
  const next=`${page}?platform=${platform}&state=${scene}`;
  if (location.hash.slice(1) === next) render(); else location.hash=next;
}
function toast(message) {
  clearTimeout(toastTimer); $('#toast').textContent=message; $('#toast').classList.add('show');
  toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),3200);
}
function microphone() {
  if (route.scene==='checking') return {label:'检查中',hint:'正在读取麦克风状态，请稍候。',tone:'',busy:true};
  if (route.scene==='check-error') return {label:'检查失败',hint:'无法读取系统状态，请检查系统设置后重新检查。',tone:'warn'};
  if (route.scene==='mic-denied') return {label:'已被系统禁用',hint:win()?'请在 Windows 隐私设置中允许麦克风和桌面应用访问。改动后可能需要重启应用。':'请在系统设置中允许 SmartVoiceInput 使用麦克风。',tone:'warn'};
  if (route.scene==='mic-unknown' || (route.page==='onboarding' && route.scene==='default')) return {label:'尚未确认',hint:'请重新检查麦克风权限和设备；确认可用后再开始试说。',tone:'warn'};
  return {label:'已允许',hint:'可以使用麦克风录音。',tone:'good'};
}
function permissions() {
  const mic=microphone();
  const microphoneRow=row('麦克风',mic.hint,pill(mic.label,mic.tone)+button('重新检查','check-mic',mic.busy)+button(win()?'打开麦克风隐私设置':'打开系统设置','mic-settings',mic.busy));
  return `<section class="platform-group"><h2>权限</h2>${microphoneRow}${win()
    ? row('输入到其他应用','无需额外授权。以管理员身份运行的窗口无法自动输入，结果会保留，请点“复制结果”后手动粘贴。',pill('普通权限窗口'))
    : row('辅助功能','允许 SmartVoiceInput 将结果输入到其他应用。未授权时仍可录音并复制结果。',pill(state.access?'已授权':'未授权',state.access?'good':'warn')+button('请求权限','access')+button('打开系统设置','access-settings'))}</section>`;
}
function settings() {
  if (state.tab==='help') return `<h1>帮助</h1><section class="plain-card"><div><b>权限与输入</b><p>${win()?'请在 Windows 麦克风隐私设置中允许桌面应用访问。管理员窗口和受保护输入框无法自动输入，请复制结果后手动粘贴。':'请允许麦克风和辅助功能权限，返回应用后重新检查。'}</p></div></section><section class="plain-card"><div><b>结果未能确认输入</b><p>先检查目标中是否已经有文字，再按需复制。应用不会自动重试，也不会自动回车。</p></div></section>${win()?`<section class="plain-card"><div><b>安装提示</b><p>本版 Windows 安装包未签名。如果出现 SmartScreen 提示，请先核对下载来源，再查看“更多信息 → 仍要运行”。</p></div></section>`:''}`;
  const busy=route.scene==='autostart-saving';
  const enabled=['autostart-on','autostart-blocked'].includes(route.scene)||state.autostart;
  return `<h1>设置</h1><section class="platform-group"><h2>常规</h2>${row('语音输入快捷键','按一下开始，再按一下停止。',`<button class="platform-shortcut" data-action="shortcut">${win()?'Ctrl':'⌘'} + Shift + Space <span class="muted">⌨</span></button>`)}${['shortcut-reserved','shortcut-error'].includes(route.scene)?`<p class="error-text" role="alert">${route.scene==='shortcut-reserved'?'此快捷键用于切换输入法或全角/半角，请选择其他组合。':'无法注册该快捷键，已保留原设置。'}</p>`:''}${win()?row('开机自启','启动 Windows 后在托盘运行。Windows 的“启动应用”系统开关可单独关闭本应用。',`<label class="switch"><input id="autostart" type="checkbox" aria-label="开机自启" ${enabled?'checked':''} ${busy?'disabled':''}><i></i></label>`):''}${win()&&busy?`<p class="platform-note" role="status">正在保存…</p>`:''}${win()&&route.scene==='autostart-error'?`<p class="error-text" role="alert">未能保存开机自启设置，已保留原值。请重试。</p>`:''}${win()&&route.scene==='autostart-blocked'?row('系统已禁用启动','应用仍保留开启偏好；请在 Windows“启动应用”中重新允许。',pill('已被 Windows 禁用','warn')+button('打开启动应用设置','startup-settings')):''}</section>${permissions()}${win()?`<section class="platform-group"><h2>纠正学习</h2>${row('自动纠正学习','Windows 本版暂不观察输入后的修改，可在词库手动添加。已有词条仍用于后续识别。',pill('本版暂不支持'))}</section>`:''}`;
}
function onboarding() {
  const mic=microphone(), blocked=['default','mic-unknown','mic-denied','checking','check-error'].includes(route.scene);
  return `<h1>让语音输入准备就绪</h1><p class="lede">麦克风用于录音。${win()?'结果可以输入到普通权限的应用窗口。':'辅助功能权限用于输入到其他应用。'}</p>${permissions()}<section class="platform-group"><h2>试说一句</h2>${row('按一下开始，再按一下结束',`${win()?'Ctrl':'⌘'} + Shift + Space`,button('试说一句','try-recording',blocked))}</section><div class="actions">${button('进入首页','home',blocked)}</div>${mic.busy?'<p class="platform-note">检查完成后可继续。</p>':''}`;
}
const failureText = {
  'higher-integrity':'目标窗口以管理员运行，Windows 不允许普通程序向它输入。请点“复制结果”后手动粘贴。',
  protected:'当前输入框受保护、只读或已禁用，无法自动输入。结果已保留。',
  'target-changed':'写入前目标已变化，本次没有输入。请复制结果，或在结果页重新指定目标。',
  'helper-unavailable':'输入组件暂时不可用，结果已保留。请点“复制结果”后手动粘贴。',
  unverified:'无法确认是否已输入，请先检查目标。不会自动重试，也不会再粘贴一次。'
};
const shortFailureText = {
  'higher-integrity':'目标窗口以管理员运行，无法自动输入。请复制结果后手动粘贴。',
  protected:'当前输入框受保护、只读或已禁用，结果已保留。',
  'target-changed':'写入前目标已变化，本次没有输入。结果已保留。',
  'helper-unavailable':'输入组件暂不可用，结果已保留。请复制后手动粘贴。'
};
function hud() {
  if (route.scene==='recording') return `<h1>正在聆听</h1><div class="platform-hud-stage"><div class="hud-preview" data-phase="recording"><button data-action="dismiss" aria-label="取消录音">×</button><span class="demo-wave">${'<i></i>'.repeat(10)}</span><button class="confirm" data-action="finish" aria-label="结束录音">✓</button></div></div><p class="platform-note">录音胶囊不显示“未开启辅助功能”的 Windows 提示。主窗口可以保持隐藏。</p>`;
  const dismissed=route.scene==='dismissed', unknown=route.scene==='unverified';
  const message=!win()&&route.scene==='higher-integrity'?'尚未开启辅助功能权限，结果已保留。可复制结果，或在系统设置中授权。':failureText[route.scene]||failureText['higher-integrity'];
  const shortMessage=!win()&&route.scene==='higher-integrity'?'未开启辅助功能，结果未输入。':shortFailureText[route.scene]||shortFailureText['higher-integrity'];
  return `<h1>口述结果</h1><div class="platform-hud-stage">${dismissed||unknown?'<p class="platform-note">胶囊已收起，结果保留在历史。</p>':`<div class="platform-failure" role="alert"><p>${esc(shortMessage)}</p><div class="actions">${button('复制结果','copy')}${!win()&&route.scene==='higher-integrity'?button('去授权','access-settings'):''}<button data-action="dismiss" aria-label="关闭提示">×</button></div></div>`}</div>${dismissed?'':`<p class="lede">${esc(message)}</p>`}<section class="result-card platform-result"><h3>${unknown?'不可核验 · 请检查目标':'结果已保留'}</h3><p class="result-text">今天 review 一下 React 和 Docker 的配置。</p>${button('复制结果','copy')}</section><div class="actions">${button('新的一段','new-recording')}${button('查看历史','history')}</div>`;
}
function tray() {
  if (!win()) return `<h1>macOS 行为对照</h1><p class="lede">关闭主窗口后继续运行，可通过 Dock 或应用菜单重新打开。本版不增加 macOS 开机自启。</p>`;
  const hidden=['hidden','autostart'].includes(route.scene), exited=route.scene==='exited';
  return `<h1>后台运行</h1><div class="tray-stage">${!hidden&&!exited?`<div class="plain-card"><div><b>SmartVoiceInput</b><p>主窗口已打开</p>${button('关闭主窗口','close-main')}</div></div>`:exited?'<p class="platform-note">SmartVoiceInput 已退出。</p>':'<p class="platform-note">主窗口已隐藏，应用继续在托盘运行。</p>'}${!exited?`<div class="tray-taskbar"><button data-action="tray-menu" aria-label="SmartVoiceInput 托盘菜单">◉ SmartVoiceInput</button></div>${state.menu?`<div class="tray-menu" role="menu"><button role="menuitem" data-action="open-main">打开</button><button role="menuitem" data-action="exit">退出</button></div>`:''}`:''}</div>${route.scene==='first-close'?`<div class="notice" role="dialog" aria-label="关闭后继续运行"><p>SmartVoiceInput 仍在后台运行，可从托盘图标打开或退出。</p>${button('知道了','ack-close')}</div>`:''}<div class="actions">${button('再次启动应用','second-launch',exited)}${button('打开主窗口','open-main',exited)}${button('退出','exit',exited)}</div><p class="platform-note">${route.scene==='autostart'?'开机自启直接进入托盘，不弹出主窗口。':'首次关闭提示在本次演示中只显示一次；重复启动复用同一实例并调出主窗口。'}</p>`;
}
function render() {
  const [page,query='']=location.hash.slice(1).split('?'), params=new URLSearchParams(query);
  const selectedPage=scenes[page]?page:'settings';
  const selectedScene=params.get('state')||'default';
  route={page:selectedPage,platform:params.get('platform')==='darwin'?'darwin':'win32',scene:scenes[selectedPage][selectedScene]?selectedScene:Object.keys(scenes[selectedPage])[0]};
  $('#platform').value=route.platform; $('#page').value=route.page;
  $('#scene').innerHTML=Object.entries(scenes[route.page]).map(([id,label])=>`<option value="${id}" ${route.scene===id?'selected':''}>${label}</option>`).join('');
  $('#titlebar').className=win()?'titlebar win-title':'titlebar';
  $('#titlebar').innerHTML=win()?`<span>SmartVoiceInput</span><div class="win-controls"><button data-action="minimize" aria-label="最小化">−</button><button data-action="maximize" aria-label="最大化">□</button><button data-action="close-main" aria-label="关闭主窗口">×</button></div>`:'<div class="traffic" aria-hidden="true"><span class="dot red"></span><span class="dot yellow"></span><span class="dot green"></span></div><span id="windowLabel">SmartVoiceInput</span>';
  const navigation=`<aside class="platform-nav"><span class="brand-name">SmartVoiceInput</span>${[['account','账户'],['settings','设置'],['appearance','个性化'],['about','关于'],['help','帮助']].map(([id,label])=>`<button data-tab="${id}" class="${id===state.tab?'selected':''}" ${['account','appearance','about'].includes(id)?'disabled':''}>${label}</button>`).join('')}</aside>`;
  const content=route.page==='settings'?settings():route.page==='onboarding'?onboarding():route.page==='hud'?hud():tray();
  $('#appBody').innerHTML=(route.page==='settings'?navigation:'')+`<main class="platform-content">${content}</main>`;
  $('#reviewNote').textContent=route.page==='hud'?'评审说明：失败卡片沿用 284px 宽与两行文案，正式产品 8 秒收起；新录音立即让位。不可核验不弹失败卡片、不自动重试。':'沿用方案 B 视觉与范围。权限、麦克风、剪贴板、前台窗口、自启和安装包均需真实环境验证。';
}
document.addEventListener('click',event=>{
  const target=event.target.closest('button'); if (!target||target.disabled) return;
  if (target.dataset.tab) {state.tab=target.dataset.tab;render();return;}
  const action=target.dataset.action;
  if (action==='close-main') {state.menu=false;go('tray',state.closedOnce?'hidden':'first-close');}
  else if (action==='ack-close') {state.closedOnce=true;go('tray','hidden');}
  else if (['open-main','second-launch'].includes(action)) {state.menu=false;go('tray');toast('已调出主窗口（模拟，仍为同一实例）');}
  else if (action==='tray-menu') {state.menu=!state.menu;render();}
  else if (action==='exit') {state.menu=false;go('tray','exited');}
  else if (action==='check-mic') {
    const page=route.page, platform=route.platform;go(page,'checking');const expected=location.hash;
    clearTimeout(saveTimer);saveTimer=setTimeout(()=>{if(location.hash===expected)go(page,page==='onboarding'?'granted':'default',platform);},650);
  }
  else if (action==='access') {state.access=true;render();toast('已授权（模拟）');}
  else if (['mic-settings','startup-settings','access-settings'].includes(action)) toast('将打开对应的系统设置页（原型不操作系统）');
  else if (action==='shortcut') go('settings','shortcut-reserved');
  else if (action==='copy') toast('已复制结果（模拟，未写入系统剪贴板）');
  else if (action==='dismiss') go('hud','dismissed');
  else if (['try-recording','new-recording'].includes(action)) go('hud','recording');
  else if (action==='finish') go('hud','higher-integrity');
  else if (action==='home') {state.tab='settings';go('settings');toast('已进入桌面端（演示）');}
  else if (action==='history') toast('结果保留在历史（演示）');
  else if (['minimize','maximize'].includes(action)) toast('窗口操作示意，不改变浏览器窗口');
});
document.addEventListener('change',event=>{
  if (event.target.id==='platform') {state.tab='settings';go(route.page,'default',event.target.value);}
  else if (event.target.id==='page') {state.tab='settings';go(event.target.value);}
  else if (event.target.id==='scene') go(route.page,event.target.value);
  else if (event.target.id==='autostart') {
    const next=event.target.checked, platform=route.platform;go('settings','autostart-saving');const expected=location.hash;clearTimeout(saveTimer);
    saveTimer=setTimeout(()=>{if(location.hash!==expected)return;state.autostart=next;go('settings',next?'autostart-on':'default',platform);toast('开机自启已保存并读回（模拟）');},650);
  }
});
$('#reset').addEventListener('click',()=>{clearTimeout(saveTimer);state={autostart:false,mic:'granted',access:false,closedOnce:false,menu:false,tab:'settings'};go('settings');});
addEventListener('hashchange',render);
render();
