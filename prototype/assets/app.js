/* 方案 B 离线评审原型：虚构数据、内存状态；不调用网络、麦克风或系统输入。 */
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const norm = s => s.trim().normalize('NFKC').toLowerCase();
const count = s => Array.from(s).length;
const t = (zh, en) => model.locale === 'en' ? en : zh;
const seeds = ['React', 'TypeScript', 'Docker', 'PostgreSQL', 'Electron', 'FastAPI', 'SQLite', 'AudioWorklet'];
const model = {
  capacity:{max:20,version:1,active:8,ceiling:100},
  email:'u1@example.test', locale:'zh-CN', retention:0, learning:true, learningCount:2, dictionaryVersion:8,
  shortcut:'CommandOrControl+Shift+Space', mic:false, access:false, source:'all', query:'', selectedTerms:[], userQuery:'', userFilter:'all',
  terms:seeds.map((term,i)=>({id:i+1,term,aliases:i===6?['SQL lite']:[],source:i===6?'auto':'manual',version:1,context:i===6?'这个项目使用 SQL lite 存储配置':''})),
  users:[{id:1,email:'admin0@example.test',role:'admin',status:'active',sessions:0},{id:2,email:'u1@example.test',role:'user',status:'active',sessions:3},{id:3,email:'u2@example.test',role:'user',status:'disabled',sessions:0}],
  applied:false, undone:false, resultFallback:false, resultSuggested:false, historyFilter:'all', settingsTab:'preferences'
};
const sceneNames = {
  default:'默认', loading:'加载中', empty:'空状态', error:'加载失败', conflict:'版本冲突', submitting:'提交中',
  invalid:'凭证错误', duplicate:'邮箱已注册', disabled:'账号禁用', limited:'请求频繁', forbidden:'无管理权限',
  'mic-denied':'麦克风未授权', 'access-denied':'输入权限未授权', granted:'权限就绪',
  preparing:'准备录音', recording:'录音中', finalizing:'尾音识别', refining:'整理中', 'processing-slow':'处理较久', ready:'正常结果',
  review:'原文回退待确认', 'target-changed':'目标已变化', 'no-target':'没有可用目标', inserted:'已确认输入',
  unverified:'写入结果不确定', cancelled:'已取消', busy:'服务繁忙', 'asr-failed':'完整音频可重识别',
  'audio-expired':'音频已过期', 'learn-observing':'学习：观察中', 'learn-learned':'学习：已学习', 'learn-final-unconfirmed':'学习：未确认最终修改', 'learn-unreadable':'学习：输入框不可读', 'learn-off':'学习已关闭', 'learned':'已自动学习', suggested:'本次未学习', 'undo-conflict':'撤销冲突',
  'refine-retry':'可重新整理', expired:'正文已清理', offline:'服务离线', draining:'降容排空', unavailable:'运行指标不可用', verified:'已验证示例',
  'test-failed':'连通测试失败', 'egress-denied':'地址不在白名单', fallback:'回落部署配置', 'no-provider':'无可用识别服务', 'linux-host':'Linux 服务器'
};
const pageScenes = {
  home:['default','loading','empty','error','learn-observing','learn-learned','learn-final-unconfirmed','learn-unreadable','learn-off'], history:['default','loading','empty','error','asr-failed','audio-expired','refine-retry','expired'],
  dictionary:['default','loading','empty','error','conflict'], newterm:['default','conflict'],
  login:['default','submitting','invalid','disabled','limited','offline'], register:['default','submitting','duplicate','offline'],
  'admin-login':['default','submitting','invalid','disabled','forbidden','limited','offline'],
  onboarding:['default','mic-denied','access-denied','granted'],
  result:['recording','preparing','finalizing','refining','processing-slow','ready','review','target-changed','no-target','inserted','unverified','empty','cancelled','error','busy','asr-failed','audio-expired','learned','suggested','undo-conflict'],
  settings:['default','loading','error','conflict','offline'],
  'admin-capacity':['default','loading','error','forbidden','conflict','offline','submitting','draining','unavailable','verified'],
  'admin-users':['default','loading','empty','error','forbidden','conflict'],
  'admin-stats':['default','loading','empty','error','forbidden'], 'admin-models':['default','loading','empty','error','forbidden'],
  'admin-providers':['default','loading','empty','error','forbidden','conflict','submitting','test-failed','egress-denied','fallback','no-provider','linux-host']
};
let route = {page:'home',scene:'default'}, toastTimer, transitionTimer, dialog = null, returnFocus = null;
function go(page, scene='default', extra='') { location.hash = page + (scene==='default' && !extra ? '' : `?state=${encodeURIComponent(scene)}${extra}`); }
function btn(label, action, cls='secondary-btn', attrs='') { return `<button class='${cls}' data-action='${action}' ${attrs}>${label}</button>`; }
function pill(text, cls='') { return `<span class='pill ${cls}'>${text}</span>`; }
function note(text, cls='') { return `<div class='notice ${cls}' role='status'>${text}</div>`; }
function head(title, sub='', actions='') { return `<header class='page-head'><div><h1>${title}</h1>${sub?`<p class='lede' style='margin-bottom:0'>${sub}</p>`:''}</div>${actions}</header>`; }
function toast(text) { clearTimeout(toastTimer); $('#toast').textContent=text; $('#toast').classList.add('show'); toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),3200); }
function closeDialog() { $('#modalLayer').classList.remove('show'); $('#modalLayer').innerHTML=''; dialog=null; if(returnFocus?.isConnected)returnFocus.focus(); }
function modal(title, body, actions='', wide=false) {
  returnFocus=document.activeElement; $('#modalLayer').innerHTML=`<section class='dialog-panel ${wide?'wide':''}' role='dialog' aria-modal='true' aria-labelledby='dialogTitle'><button class='modal-close' data-action='close-modal' aria-label='关闭'>×</button><h2 id='dialogTitle'>${title}</h2>${body}<p id='dialogError' class='error-text' role='alert'></p><div class='actions end'>${actions}</div></section>`;
  $('#modalLayer').classList.add('show'); const focusable=$('#modalLayer input:not([disabled]), #modalLayer textarea, #modalLayer button'); focusable?.focus();
}
function stateView(kind, options={}) {
  const messages={loading:['正在加载','请稍候，正在获取账号数据。'],empty:['这里还没有内容','完成一次口述，或添加你的常用词汇。'],error:['暂时无法加载','请检查服务连接后重试，已有数据不会被当作空列表。'],forbidden:['没有管理权限','当前账号无法访问管理后台，请使用管理员账号登录。']};
  const [title,desc]=messages[kind]||messages.error;
  return `<div class='state-card'>${kind==='loading'?`<div class='spinner' aria-label='加载中'></div>`:pill(kind==='error'?'连接异常':kind==='forbidden'?'权限不足':'暂无数据')}<h2>${options.title||title}</h2><p>${options.desc||desc}</p>${kind==='loading'?btn('加载中','noop','secondary-btn','disabled'):kind==='forbidden'?btn('返回管理员登录','admin-login'):btn(kind==='error'?'重试':'查看默认示例','reset-scene')}</div>`;
}
function navigation(admin=false) {
  const items=admin?[['admin-users','用户管理','♙'],['admin-stats','使用统计','▥'],['admin-models','模型用量','◈'],['admin-providers','模型配置','⚙'],['admin-capacity','并发容量','≋']]:[['home',t('首页','Home'),'⌂'],['history',t('历史记录','History'),'◷'],['dictionary',t('词库','Dictionary'),'▤']];
  return `<aside class='sidebar'><div class='brand'><img class='brand-mark' src='assets/logo.png' alt=''><span class='brand-name'>SmartVoiceInput</span></div><nav class='nav'>${items.map(([id,label,icon])=>`<button class='nav-item ${route.page===id?'active':''}' data-go='${id}'><span class='nav-glyph' aria-hidden='true'>${icon}</span>${label}</button>`).join('')}</nav>${admin?'':btn(t('开始口述','Start dictation'),'start-recording','primary-btn')}<div class='sidebar-bottom'><div class='sidebar-note'>${admin?'管理后台<br>admin0@example.test':`${esc(model.email)}<br>自有服务 · 已连接`}</div>${admin?btn('退出后台','admin-logout','nav-item'):`<button class='nav-item' data-go='settings'>⚙ ${t('设置','Settings')}</button><button class='nav-item' data-action='help'>? ${t('帮助','Help')}</button>`}</div></aside>`;
}
function authPage() {
  const register=route.page==='register', admin=route.page==='admin-login', submitting=route.scene==='submitting';
  const errors={invalid:'邮箱或密码错误',duplicate:'该邮箱已注册，请直接登录',disabled:'账号已禁用，请联系管理员',limited:'请求过于频繁，请稍后重试',forbidden:'当前账号没有管理员权限',offline:'无法连接服务，请稍后重试'};
  return `<div class='auth-layout'><section class='auth-story'><div class='brand-name'><img class='brand-mark' src='assets/logo.png' alt='' style='display:inline-block;vertical-align:-6px;margin-right:6px'>SmartVoiceInput</div><h1>${admin?'清晰了解<br>每一次使用':'说出想法，<br>专注表达。'}</h1><p>${admin?'管理账号、查看使用频率与模型用量。账号内容保持隔离，后台不提供用户正文浏览。':'按下快捷键说话，让语音成为整理好的文字。你的词库、历史和使用记录，归属于自己的账号。'}</p><div>${pill(admin?'自有服务 · 管理员入口':'语音输入 · 方案 B')}</div></section><form class='auth-form' id='authForm'><p class='eyebrow'>${admin?'ADMIN CONSOLE':'WELCOME'}</p><h2>${admin?'管理员登录':register?'创建账号':'登录 SmartVoiceInput'}</h2><p class='lede'>${register?'使用邮箱和密码注册，无需邮件验证。':'请输入账号信息以继续。'}</p>${errors[route.scene]?note(errors[route.scene],'error'):''}<label class='field'><span>邮箱</span><input class='text-input' name='email' type='email' required maxlength='254' autocomplete='off' value='${admin?'admin0@example.test':esc(model.email)}'></label><label class='field'><span>密码</span><input class='text-input' name='password' type='password' required autocomplete='off'><small>8–128 个字符，保留原始空格。请仅输入虚构演示密码。</small></label><p id='authError' class='error-text' role='alert'></p><button class='primary-btn' type='submit' ${submitting?'disabled':''}>${submitting?'正在提交…':register?'注册账号':'登录'}</button><div class='auth-foot'>${admin?'管理后台只允许管理员账号访问。':register?`已有账号？ <a href='#login'>返回登录</a>`:`还没有账号？ <a href='#register'>创建账号</a>`}</div></form></div>`;
}
function onboardingPage() {
  const mic=route.scene==='granted'||route.scene==='access-denied'||model.mic, access=route.scene==='granted'||model.access;
  return `<section class='onboarding'><p class='step-label'>首次使用 · 权限与试说</p><h1>让语音输入准备就绪</h1><p class='lede'>麦克风用于录音，辅助功能权限用于把结果放入当前输入框。缺少输入权限时，仍可复制结果。</p><div class='stack'><div class='permission-card'><span class='permission-icon'>◉</span><div class='grow'><h3>麦克风</h3><p>${mic?'已允许，可以采集语音。':'尚未允许。请在系统隐私设置中允许麦克风访问。'}</p></div>${pill(mic?'已允许':'未授权',mic?'good':'warn')}${btn('重新检查','check-mic','small-btn')}</div><div class='permission-card'><span class='permission-icon'>⌨</span><div class='grow'><h3>辅助功能 / 输入权限</h3><p>${access?'已允许，可在支持的输入框中插入文字。':'未授权。macOS 请启用辅助功能；Windows 请检查目标权限与安全桌面限制。'}</p></div>${pill(access?'已允许':'仅复制',access?'good':'warn')}${btn('查看授权路径','permission-help','small-btn')}${btn('重新检查','check-access','small-btn')}</div><div class='permission-card'><span class='permission-icon'>⌘</span><div class='grow'><h3>按一下开始，再按一下结束</h3><p><kbd>⌘ / Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>Space</kbd></p></div>${btn('试说一句','try-recording','small-btn',mic?'':'disabled')}</div></div>${route.scene==='mic-denied'?note('麦克风权限被拒绝。授权后点击“重新检查”，才能开始录音。','error'):''}<div class='actions end'>${btn(mic&&!access?'以仅复制方式继续':'进入首页','finish-onboarding','primary-btn',mic?'':'disabled')}</div></section>`;
}
// 2026-10-28 fast-correction learning (S0): the card states the latest observation outcome,
// never a live "learning continuously" claim. Fictional example term only.
function learnStatusLine() {
  const lines = {
    'learn-observing': '正在观察本次输入 · 改完正常发送即可，不用等待',
    'learn-learned': '最近一次：已学习“瑞艾克特 → React” · 可在词典中撤销',
    'learn-final-unconfirmed': '最近一次未学习：改完后立即发送，无法确认最终修改 · 可在词典中手动添加',
    'learn-unreadable': '最近一次未学习：当前应用的输入框无法读取 · 可在词典中手动添加',
    'learn-off': '自动学习已关闭 · 可在设置中开启',
  };
  return lines[route.scene] || (model.learning ? '已开启 · 修改识别错误后正常发送即可学习；撤销词条不会抹去已发生的学习次数' : lines['learn-off']);
}
function homePage() {
  if(['loading','empty','error'].includes(route.scene))return head('首页')+stateView(route.scene);
  const stats=[['13','字','口述字数'],['30','秒','总口述时长'],['26.0','字/分钟','平均口述速度'],[model.terms.length,'个','词条总数']];
  return `<div class='home-page'><h1 class='hero-title'>说话，不要打字</h1><div class='home-layout'><div class='home-main'><section class='insight-surface'><div class='insight-head'><h2>洞察</h2><span class='insight-label'>累计 · Asia/Shanghai</span></div><div class='stat-cards'>${stats.map(([n,u,label])=>`<div class='stat-card'><div><b>${n}</b><span>${u}</span></div><small>${label}</small></div>`).join('')}</div><div class='activity-card'><div class='activity-summary'>${['活跃天数','当前连续天数','最长连续天数'].map(label=>`<div><b>1</b><span>天</span><small>${label}</small></div>`).join('')}</div><div class='heatmap-wrap'><div class='heatmap-days'><span>S</span><span>M</span><span>T</span><span>W</span><span>T</span><span>F</span><span>S</span></div><div class='heatmap'>${Array.from({length:196},(_,i)=>`<i class='l${i===194?2:0}' title='${i===194?'2026-10-16 · 3 次':'0 次'}'></i>`).join('')}</div></div><div class='calendar-months-proto'><span>四月</span><span>五月</span><span>六月</span><span>七月</span><span>八月</span><span>九月</span><span>十月</span></div><div class='heatmap-footer'><span class='muted' style='font-size:10px'>最近 28 周 · 按会话次数</span><div class='heatmap-legend'><span>少</span>${[0,1,2,3,4].map(n=>`<i class='l${n}'></i>`).join('')}<span>多</span></div></div></div><div class='personalization-card'><div class='personalization-big'><b>${model.learningCount}</b><span>次学习</span></div><p class='muted'>${learnStatusLine()}</p></div></section></div><aside class='home-side'><div class='side-card shortcut-card'><div class='shortcut-group'><span>口述快捷键</span><kbd>⌘ / Ctrl</kbd><kbd>⇧</kbd><kbd>Space</kbd></div>${btn('试说一句','start-recording','small-btn')}</div><div class='side-card'><b style='font-size:13px'>表达自然，原意保留</b><p class='lede' style='font-size:12px;margin:10px 0 0'>整理口头禅与重复，保留数字、专名和你的要求。</p></div><div class='side-card'><b style='font-size:13px'>服务已连接</b><p class='lede' style='font-size:11px;margin:8px 0 0'>ASR 就绪<br>文本整理可用</p>${btn('查看设置','settings','text-btn')}</div><div class='side-version'><span>v0.0.1 · 原型</span></div></aside></div></div>`;
}
function historyPage() {
  const retention=`<select class='select history-retention' id='retentionSelect' aria-label='历史保留期限'>${[[0,'永远'],[7,'7 天'],[30,'30 天'],[90,'90 天']].map(([v,l])=>`<option value='${v}' ${model.retention===v?'selected':''}>${l}</option>`).join('')}</select>`;
  const top=head('历史记录')+`<section class='history-privacy'><div class='row-between'><div class='grow'><b>▱ 保存历史</b><p>选择账号历史正文与纠错片段的保留时间。</p></div>${retention}</div><div class='history-privacy-divider'><b>♙ 您的数据保持私密</b><p>语音由配置的识别与整理服务处理，历史文字保存到你的账号。${btn('数据说明','privacy','text-btn')}</p></div></section><div class='tabs filter-tabs'>${[['all','全部'],['dictation','听写']].map(([id,label])=>`<button class='tab ${model.historyFilter===id?'active':''}' data-history-filter='${id}'>${label}</button>`).join('')}</div>`;
  if(['loading','empty','error'].includes(route.scene))return top+stateView(route.scene);
  if(route.scene==='expired')return top+stateView('empty',{title:'旧正文已清理',desc:'已到期的正文不再出现在历史列表；统计和独立词库继续保留，延长期限不会恢复正文。'});
  const items=route.scene==='asr-failed'?[['11:56','识别失败','完整音频仍可重试，剩余 09:42。',btn('重新识别','retry-asr','small-btn'),'']]:route.scene==='audio-expired'?[['11:56','识别失败','音频已不可用，请重新录音。',btn('重新录音','start-recording','small-btn'),'']]:route.scene==='refine-retry'?[['11:56','原文回退 · 待确认','嗯那个，今天 review 一下 API 的 timeout。',btn('重新整理','retry-refine','small-btn'),'review']]:[['11:56','已取消','本次录音已取消','',''],['11:48','已输入 · 10 秒','你好 API','','history-detail-a'],['11:42','已完成 · 20 秒','版本 2 React','','history-detail-b']];
  return top+`<div class='history-day'>今天</div><div class='history-list'>${items.map(([time,status,text,retry,detail])=>`<article class='history-item'><div class='history-time'>${time}</div><div class='history-body'><p>${text}</p></div><div class='history-row-actions'>${retry}${detail?btn('⧉','copy-result','icon-btn',"aria-label='复制转录' title='复制转录'"):''}<details class='row-menu'><summary aria-label='更多历史操作' title='更多'>⋯</summary><div class='row-menu-popup'>${detail?btn('查看详情',detail,'text-btn'):''}<p>${status}</p></div></details></div></article>`).join('')}</div>`;
}
function dictionaryPage() {
  const top=head('词典','',btn('新词','new-term','primary-btn'))+(model.selectedTerms.length?`<div class='dictionary-toolbar selection-toolbar'>${btn('▣','select-loaded','icon-btn',"aria-label='全选或取消已加载词条'")}<span>已选择 ${model.selectedTerms.length} 个词</span>${btn('删除','bulk-delete','danger-btn')}${btn('取消选择','cancel-selection','text-btn')}</div>`:`<div class='dictionary-toolbar'><div class='tabs dictionary-source-filter'>${[['all','所有'],['auto','自动添加'],['manual','手动添加']].map(([id,label])=>`<button class='tab ${model.source===id?'active':''}' data-source='${id}'>${id==='auto'?"<i class='source-auto' aria-hidden='true'></i>":id==='manual'?"<i class='source-manual' aria-hidden='true'></i>":''}${label}</button>`).join('')}</div><input class='text-input search-input' id='termSearch' aria-label='搜索词条或别名' placeholder='搜索词条或别名' value='${esc(model.query)}'></div>`);
  if(['loading','empty','error'].includes(route.scene))return top+stateView(route.scene);
  const terms=model.terms.filter(x=>(model.source==='all'||x.source===model.source)&&norm(x.term+' '+x.aliases.join(' ')).includes(norm(model.query)));
  return top+(route.scene==='conflict'?note('词条已在其他操作中更新；删除整批取消，选择仍保留，请刷新后重新选择。','info'):'')+`<div class='term-grid ${model.selectedTerms.length?'selection-mode':''}'>${terms.map(x=>`<article class='term-card ${model.selectedTerms.includes(x.id)?'term-selected':''}'><i class='${x.source==='auto'?'source-auto':'source-manual'}' aria-label='${x.source==='auto'?'自动添加':'手动添加'}'></i><button class='term-name' data-edit='${x.id}'>${esc(x.term)}</button><button class='term-select' data-action='toggle-term' data-term='${x.id}' role='checkbox' aria-checked='${model.selectedTerms.includes(x.id)}' aria-label='选择词条 ${esc(x.term)}'>${model.selectedTerms.includes(x.id)?'☑':'□'}</button></article>`).join('')}</div>`+(terms.length?'':`<div class='state-card'><h2>没有匹配的词条</h2><p>试试其他关键词，或添加一个新词。</p>${btn('清除筛选','clear-search','small-btn')}</div>`)+`<div class='actions'>${btn('导入 CSV','import','text-btn')}<span class='muted' style='font-size:11px'>全选仅限当前筛选下已加载词条；词库版本 ${model.dictionaryVersion}</span></div>`;
}
function settingsPage() {
  const tabs=[['account',t('账户','Account')],['preferences',t('设置','Settings')],['personalization',t('个性化','Learning')],['about',t('关于','About')],['help',t('帮助','Help')]];
  const tab=model.settingsTab;
  let body='';
  if(tab==='preferences')body=`<h1>${t('设置','Settings')}</h1><div class='setting-row'><div><b>${t('语音输入快捷键','Dictation shortcut')}</b><p>${t('按一下开始，再按一下停止。','Press once to start and again to stop.')}</p></div><div><input class='text-input' id='shortcutInput' aria-label='语音输入快捷键' value='${esc(model.shortcut)}'><div style='text-align:right;margin-top:8px'>${btn(t('保存','Save'),'save-shortcut','small-btn')}</div></div></div><div class='setting-row'><div><b>${t('界面语言','Interface language')}</b><p>${t('保存到本机和账号偏好。','Saved locally and to account preferences.')}</p></div><select class='select' id='localeSelect' aria-label='界面语言'><option value='zh-CN' ${model.locale==='zh-CN'?'selected':''}>简体中文</option><option value='en' ${model.locale==='en'?'selected':''}>English</option></select></div><div class='setting-row'><div><b>${t('自动学习','Learn from corrections')}</b><p>${t('只观察本次插入片段的局部修正，可随时关闭。','Observe only local corrections to the inserted text.')}</p></div><label class='switch'><input type='checkbox' id='learningToggle' aria-label='自动学习' ${model.learning?'checked':''}><i></i></label></div><div class='setting-row'><div><b>${t('服务连接','Service connection')}</b><p>http://127.0.0.1:17863</p><p>${t('地址由安装包配置，部署时统一设置。','Configured when the application is built.')}</p></div>${pill(route.scene==='offline'?t('未连接','Offline'):t('已连接','Connected'),route.scene==='offline'?'warn':'good')}${btn(t('重新检测','Check again'),'recheck-service','small-btn')}</div>${route.scene==='conflict'?note('设置已变化，请重新读取后保存。原设置未被覆盖。','error'):''}${route.scene==='offline'?note('服务不可用，请检查网络；当前无法开始录音。','error'):''}`;
  if(tab==='account')body=`<h1>账户</h1><div class='setting-row'><div><b>邮箱</b><p>${esc(model.email)}</p></div>${btn('退出当前登录','logout','small-btn')}</div><h3 class='settings-group-title'>已登录设备</h3><div class='setting-row'><div><b>演示 Mac</b><p>macOS · 最近活跃：今天</p></div>${pill('当前设备')}</div><div class='setting-row'><div><b>演示 PC</b><p>Windows · 最近活跃：昨天</p></div>${pill('其他设备')}</div><p class='lede'>退出仅结束本次登录，其他设备保持登录。</p>`;
  if(tab==='personalization')body=`<h1>个性化</h1><div class='personalization-big'><b>${model.learningCount}</b><span>次成功学习</span></div><p class='lede'>持续学习中。修改你刚刚输入的词语，我们会识别明确的词语纠正；有歧义时请你确认。</p>${note('只观察本次片段：静止 15 秒或最多 60 秒后结束。整段改写、清空或切换输入框不学习。','info')}${btn('查看词库','dictionary','primary-btn')}`;
  if(tab==='about')body=`<h1>关于</h1><div class='setting-row'><b>SmartVoiceInput</b><span>v0.0.1 · 原型版本</span></div><p class='lede'>语音输入、忠实整理与个人词库。</p>${privacyText()}`;
  if(tab==='help')body=`<h1>帮助</h1><div class='stack'><div class='plain-card'><div><b>权限与快捷键</b><p>允许麦克风后即可录音；输入权限缺失时结果供复制。</p>${btn('检查权限','onboarding','text-btn')}</div></div><div class='plain-card'><div><b>结果没有进入输入框</b><p>确认目标是否改变。保留结果并重新指定；如果无法确认是否写入，请先检查目标再手动复制。</p>${btn('查看结果处理示例','review','text-btn')}</div></div><div class='plain-card'><div><b>失败后如何恢复</b><p>完整音频短期可重新识别；原文仍在可重新整理；音频过期需要重新录音。</p></div></div></div>`;
  if(['loading','error'].includes(route.scene))body=stateView(route.scene);
  return `<div class='settings-panel inline'><aside class='settings-nav'>${btn('← '+t('返回首页','Home'),'home','settings-nav-item')}${tabs.map(([id,label])=>`<button class='settings-nav-item ${tab===id?'active':''}' data-settings='${id}'>${label}</button>`).join('')}</aside><section class='settings-content'>${body}</section></div>`;
}
function privacyText() { return `<div class='privacy-copy'><h3>数据如何处理</h3><p>音频仅在本机或获准内网 ASR 处理。成功、取消、无语音或上传中断后释放；完整识别失败仅在内存保留最多 600 秒供重试。</p><p>本次原文、整理稿和有限学习片段由自有服务端保存；需要时发至已配置 LLM。输入框原有全文不上传，日志不记录正文与凭据。</p><p>正文默认永久，可选 7 / 30 / 90 天。到期立即不可读，1 小时内清理。统计及已经形成的词条与有限语境独立保留。</p><p>默认加密备份保留 7 天，在线删除不会改写旧备份。恢复开放前清空恢复来的历史正文与学习原片段。备份仅供受限运维恢复使用。</p></div>`; }
function resultPage() {
  const s=route.scene==='default'?'recording':route.scene;
  const processing=['preparing','recording','finalizing','refining','processing-slow'].includes(s);
  if(processing){
    const phase=s==='preparing'?'preparing':s==='recording'?'recording':'processing';
    const capsule=phase==='recording'?`<button data-action='cancel-recording' aria-label='取消录音'>×</button><span class='demo-wave' aria-label='模拟波形'>${'<i></i>'.repeat(10)}</span><button class='confirm' data-action='finish-recording' aria-label='完成录音'>✓</button>`:phase==='processing'?`<span class='hud-demo-progress' aria-hidden='true'></span><span class='hud-demo-loading'>正在处理</span>`:'';
    return head('语音输入','原型胶囊与过程状态预览')+`<div class='state-card'><p class='eyebrow'>${{preparing:'正在准备',recording:'正在聆听',finalizing:'正在识别尾音',refining:'正在整理文字','processing-slow':'处理耗时较长'}[s]}</p>${s==='processing-slow'?`<div class='hud-slow-preview' role='status'>处理耗时比平时更长…</div>`:''}<div class='hud-preview ${s==='processing-slow'?'is-slow':''}' data-phase='${phase}' aria-label='${phase==='preparing'?'准备录音':phase==='recording'?'正在录音':'正在处理'}'>${capsule}</div><p>${s==='recording'?'模拟波形；胶囊内不显示转写正文。':'等待真实结果后收起；示意动画不是服务端完成比例。'}</p>${btn('取消','cancel-recording')}</div>`;
  }
  const terminal={empty:['未识别到语音','请靠近麦克风后重试。'],cancelled:['本次已取消','音频和待处理结果已丢弃，不能恢复本轮。'],error:['连接中断','音频未完整上传，请重新录音。'],busy:['服务繁忙','当前没有空闲处理容量，请稍后重试。'],'asr-failed':['识别失败','完整音频仍在，剩余 09:42，可重新识别。'],'audio-expired':['音频已不可用','失败音频已过期，请重新录音。']};
  if(terminal[s])return head('语音输入')+`<div class='state-card'><h2>${terminal[s][0]}</h2><p>${terminal[s][1]}</p><div class='actions'>${btn(s==='asr-failed'?'重新识别':'重新录音',s==='asr-failed'?'retry-asr':'start-recording','primary-btn')}${btn('返回首页','home')}</div></div>`;
  const raw=(s==='suggested'||model.resultSuggested&&s==='inserted')?'云':'嗯那个，今天 review 一下 API 的 timeout，然后然后记得不要改数据库。';
  const refined=(s==='review'||s==='inserted'&&model.resultFallback||s==='suggested'||s==='inserted'&&model.resultSuggested)?raw:'今天 review 一下 API 的 timeout，记得不要改数据库。';
  const notices={ready:'结果已生成。正常口述结果返回后，会获取当时当前输入位置并校验插入。',review:'整理未通过忠实性检查，已保留原文。请确认后再输入。','target-changed':'结果返回后捕获的目标又发生变化，本次不自动写入。可重新指定或复制。','no-target':'没有可用的输入目标，或输入权限不足。结果保留在历史，可复制，也可直接开始下一段。',inserted:'已输入，并已读回确认。没有自动按下 Enter。',unverified:'无法确认是否已输入，请检查目标。不会自动重试，也不会再粘贴一次。',learned:model.undone?'已撤销本次学习，累计学习次数保留。':'已输入。你将 SQL lite 改为 SQLite，已加入词库。',suggested:model.applied?'已确认「云」→「芸」并加入词库。':!model.learning?'自动学习已关闭，不能提交本次建议。':'已输入。本次改词不确定，暂不学习；不弹加入确认卡，可到词典手动添加。','undo-conflict':'词条已有后续修改，无法撤销本次学习。请到词库手动处理。'};
  const learned=['learned','undo-conflict'].includes(s);
  return head('口述结果',pill(['inserted','learned','suggested'].includes(s)?'已确认输入':s==='review'?'待确认':'结果已保留',s==='review'?'warn':''))+note(notices[s]||notices.ready,s==='review'||s==='unverified'?'':'info')+`<div class='result-columns'><section class='result-card'><h3>原始转写</h3><p class='result-text'>${learned?'这个项目使用 SQL lite 存储配置。':raw}</p>${btn('复制原文','copy-raw','text-btn')}</section><section class='result-card'><h3>${s==='review'?'待确认原文':'整理结果'}</h3><p class='result-text'>${learned?'这个项目使用 SQL lite 存储配置。':refined}</p>${btn('复制结果','copy-result','text-btn')}</section></div><div class='actions'>${s==='review'?btn('确认并输入','confirm-result','primary-btn')+btn('重新整理','retry-refine'):''}${['ready','target-changed'].includes(s)?btn('重新指定输入框','retarget','primary-btn'):''}${s==='no-target'?btn('检查权限','onboarding'):''}${s==='learned'?btn(model.undone?'已撤销':'撤销学习','undo-learn','small-btn',model.undone?'disabled':''):''}${s==='undo-conflict'?btn('打开词库','dictionary'):''}${btn('新的一段','start-recording')}${btn('关闭结果','home')}</div>${['inserted','learned'].includes(s)?`<div class='plain-card' style='margin-top:22px'><div class='grow'><b>模拟目标读回</b><p>前缀 + 本次输入 + 后缀</p><p class='target-preview'>前缀　${learned?'这个项目使用 SQLite 存储配置。':refined}　后缀</p></div></div>`:''}`;
}

function capacitySnapshot() {
  return route.scene==='draining'?{...model.capacity,max:5,active:8}:model.capacity;
}
function capacityPage() {
  const c=capacitySnapshot(), unknown=route.scene==='unavailable', busy=route.scene==='submitting';
  const title=head('并发容量','控制同时处理的口述数量。调整后立即用于新会话。',btn('刷新状态','refresh-capacity'));
  if(['loading','error','forbidden'].includes(route.scene))return title+stateView(route.scene);
  const draining=c.active>c.max, verified=route.scene==='verified';
  return title+note(unknown?'暂时无法获取运行指标，当前负载未知。配置仍可查看，保存前请确认运行情况。':draining?`正在降容：当前 ${c.active} 路，上限 ${c.max} 路。在途口述继续完成，活动数低于 ${c.max} 后恢复接纳。`:'当前容量目标 20 路。可调上限与实际处理能力分别记录。',unknown?'error':draining?'':'info')+
    `<div class='capacity-layout'><section class='capacity-settings'><h2>会话上限</h2><form id='capacityForm' novalidate><label class='field'><span>最多同时处理</span><div class='capacity-number'><input class='text-input' id='capacityInput' type='number' min='1' max='${c.ceiling}' step='1' value='${c.max}' ${busy?'disabled':''}><span>路</span></div><small>部署允许 1–${c.ceiling} 路；每个账号最多 1 路。</small></label><p class='capacity-meta'>已生效 <strong id='capacityEffective'>${c.max}</strong> 路 · 版本 ${c.version}</p><p class='lede capacity-copy'>调高上限不会增加模型算力。调低时已有口述继续完成，不会被中断。</p><p id='capacityError' class='error-text' role='alert'>${route.scene==='conflict'?'配置已被其他管理员更新。保留输入，刷新后比较并重新提交。':route.scene==='offline'?'无法确认上次保存结果。请刷新读取当前生效值，不要连续提交。':''}</p><button class='primary-btn' type='submit' ${busy?'disabled':''}>${busy?'正在保存…':'保存上限'}</button></form><div class='capacity-validation'><h3>容量验证</h3>${pill(verified?'已验证示例：20 路':'未验证',verified?'good':'warn')}<p>${verified?'示例报告 CAP-DEMO-20 · 2026-10-17。仅覆盖当时的模型、硬件和测试负载。':'尚无此部署的真实压测报告。当前设置不代表已经达到目标。'}</p>${verified&&c.max>20?note('当前设置超过已验证的 20 路，请完成对应负载测试。'):''}</div></section><section class='capacity-runtime'><div class='capacity-runtime-head'><h2>运行状态</h2><span>${unknown?'采样失败':'采样于 14:32:10'}</span></div><div class='capacity-metrics'>${[[unknown?'—':c.active,'当前活动'],[unknown?'—':Math.max(0,c.max-c.active),'可接纳'],[unknown?'—':'1 / 1','ASR 运行 / 执行上限'],[unknown?'—':'2 / 4','LLM 运行 / 调用上限']].map(([n,l])=>`<div class='stat-card'><b>${n}</b><small>${l}</small></div>`).join('')}</div><div class='capacity-rows'>${[['ASR 等待 · 窗口 / 收尾','3 / 0'],['LLM 等待 · 整理审核 / 学习','1 / 0'],['最近 5 分钟 · 拒绝 / 失败 / 回退','0 / 0 / 0'],['停止至结果 · p50 / p95','1.2 s / 2.4 s · 40 个样本'],['音频 · 实占 / 预留 / 失败缓存','12.8 / 46.08 / 0 MB']].map(([k,v])=>`<div><span>${k}</span><strong>${unknown?'未知':v}</strong></div>`).join('')}</div><p class='capacity-meta'>模型执行资源由部署配置管理。运行指标不包含用户正文。</p></section></div>`;
}
function saveCapacity() {
  const c=capacitySnapshot(), raw=$('#capacityInput').value, next=Number(raw);
  if(!raw||!Number.isSafeInteger(next)||next<1||next>c.ceiling){$('#capacityError').textContent=`请输入 1–${c.ceiling} 的整数。`;return;}
  if(route.scene==='conflict'){$('#capacityError').textContent='版本冲突，当前修改未保存。请先刷新，再比较输入并重新提交。';return;}
  if(route.scene==='offline'){$('#capacityError').textContent='保存结果待确认。请刷新读取当前生效值，不要自动重试。';return;}
  const pending={next,version:c.version,old:c.max};
  if(next<c.max){dialog={type:'capacity',...pending};modal('调低并发上限',`<p>从 ${c.max} 路调整为 ${next} 路。</p><p>已有 ${route.scene==='unavailable'?'未知数量的':c.active+' 路'}口述继续完成，活动数低于新上限后才接纳新会话。</p>`,btn('取消','close-modal')+btn('确认并保存','confirm-capacity','primary-btn'));return;}
  commitCapacity(pending);
}
function commitCapacity(pending) {
  closeDialog();const button=$('#capacityForm [type=submit]'), input=$('#capacityInput'), hash=location.hash;
  button.disabled=true;input.disabled=true;button.textContent='正在保存…';
  setTimeout(()=>{
    if(model.capacity.version!==pending.version){if(location.hash===hash){button.disabled=false;input.disabled=false;button.textContent='保存上限';$('#capacityError').textContent='版本已变化，未保存。请刷新后重试。';}return;}
    if(model.capacity.max!==pending.next){model.capacity.max=pending.next;model.capacity.version++;}
    if(location.hash===hash){if(route.scene!=='default'&&route.scene!=='verified')go('admin-capacity');else render();toast('已读回生效上限：'+model.capacity.max+' 路（原型模拟）');}
  },450);
}

function adminPage() {
  if(route.page==='admin-capacity')return capacityPage();
  if(route.page==='admin-providers')return providersPage();
  const p=route.page; const title=p==='admin-users'?'用户管理':p==='admin-stats'?'使用统计':'模型与用量';
  let top=head(title,p==='admin-users'?'账号创建、启用与禁用；仅管理必要信息。':'固定示例期间：2026-10-01 至 2026-10-17 · Asia/Shanghai',p==='admin-users'?btn('创建用户','create-user','primary-btn'):'');
  if(['loading','empty','error','forbidden'].includes(route.scene))return top+stateView(route.scene);
  if(p==='admin-users') {
    const users=model.users.filter(u=>u.email.includes(model.userQuery.toLowerCase())&&(model.userFilter==='all'||u.status===model.userFilter));
    return top+(route.scene==='conflict'?note('账号状态已变化，请刷新列表后重试。','error'):'')+`<div class='dictionary-toolbar'><input class='text-input search-input' id='userSearch' aria-label='搜索用户邮箱' value='${esc(model.userQuery)}' placeholder='搜索邮箱'><select class='select' id='userStatus' aria-label='用户状态'>${[['all','全部状态'],['active','已启用'],['disabled','已禁用']].map(([v,l])=>`<option value='${v}' ${v===model.userFilter?'selected':''}>${l}</option>`).join('')}</select></div><div class='table-wrap'><table class='admin-table'><thead><tr><th>邮箱 / 角色</th><th>状态</th><th>会话数</th><th>操作</th></tr></thead><tbody>${users.map(u=>`<tr><td>${esc(u.email)}<small>${u.role==='admin'?'管理员 · 当前账号':'普通用户'}</small></td><td>${pill(u.status==='active'?'已启用':'已禁用',u.status==='active'?'good':'')}</td><td>${u.sessions}</td><td>${btn(u.status==='active'?'禁用':'启用','toggle-user','small-btn',`data-user='${u.id}' ${u.role==='admin'?"disabled title='不能禁用自己或最后一个有效管理员'":''}`)}</td></tr>`).join('')||`<tr><td colspan='4'>没有匹配用户</td></tr>`}</tbody></table></div><p class='lede' style='font-size:11px'>不提供用户正文、词条语境或密码浏览。</p>`;
  }
  top+=`<form id='dateForm' class='date-controls'><label>开始 <input type='date' name='from' value='2026-10-01' required></label><label>结束 <input type='date' name='to' value='2026-10-17' required></label><button class='small-btn' type='submit'>查询</button><span id='dateError' class='error-text' role='status'></span></form>`;
  if(p==='admin-stats')return top+`<div class='metrics'>${[['1','10/16 日活用户'],['3','录音会话'],['35 秒','接收音频'],['100%','处理成功率'],['50%','插入成功率'],['30 秒','成功口述时长']].map(([n,l])=>`<div class='stat-card'><b>${n}</b><small>${l}</small></div>`).join('')}</div><div class='activity-card'><b style='font-size:13px'>每日会话</b><div class='mini-chart'>${['10/11','10/12','10/13','10/14','10/15','10/16','10/17'].map((d,i)=>`<div><i style='height:${i===5?65:2}px'></i><span>${d}</span></div>`).join('')}</div></div><div class='table-wrap'><table class='admin-table'><thead><tr><th>用户</th><th>会话数</th><th>接收音频</th><th>口述字数</th></tr></thead><tbody><tr><td>u1@example.test</td><td>3</td><td>35 秒</td><td>13</td></tr></tbody></table></div>`;
  return top+note('供应商未返回的 token 标记为未知。已知小计不代表完整消耗。','info')+`<div class='table-wrap'><table class='admin-table'><thead><tr><th>模型 / 用途</th><th>调用次数</th><th>已知输入 token</th><th>已知输出 token</th><th>未知用量调用</th></tr></thead><tbody><tr><td>示例内网 ASR<small>窗口解码 / 收尾</small></td><td>3</td><td>—</td><td>—</td><td>不适用</td></tr><tr><td>示例文本模型<small>整理（含一次重试）</small></td><td>2</td><td>100</td><td>20</td><td>1</td></tr><tr><td>示例文本模型<small>忠实性审核</small></td><td>1</td><td>40</td><td>5</td><td>0</td></tr><tr><td>示例文本模型<small>词语分类</small></td><td>1</td><td>30</td><td>3</td><td>0</td></tr></tbody></table></div><p class='lede'>ASR 处理窗口累计 6 秒，用户录音 3 秒；重叠窗口不能累计成用户录音时长。本页是独立模型调用夹具，不与使用统计页的会话样本混算。</p>`;
}
function render() {
  clearTimeout(transitionTimer); closeDialog();
  const [page,query='']=location.hash.slice(1).split('?');
  route={page:pageScenes[page]?page:'home',scene:new URLSearchParams(query).get('state')||'default'};
  if(!['default',...pageScenes[route.page]].includes(route.scene))route.scene='default';
  $('#sceneSelect').innerHTML=pageScenes[route.page].map(s=>`<option value='${s}' ${s===route.scene?'selected':''}>${sceneNames[s]}</option>`).join('');
  $('#windowLabel').textContent=route.page.startsWith('admin-')?'SmartVoiceInput · 管理后台':'SmartVoiceInput';
  let content;
  if(['login','register','admin-login'].includes(route.page))content=authPage();
  else if(route.page==='onboarding')content=onboardingPage();
  else if(route.page==='settings')content=settingsPage();
  else { const admin=route.page.startsWith('admin-'); const body=admin?adminPage():route.page==='home'?homePage():route.page==='history'?historyPage():['dictionary','newterm'].includes(route.page)?dictionaryPage():resultPage(); content=navigation(admin)+`<main class='content'><section class='page'>${body}</section></main>`; }
  $('#appBody').innerHTML=content;
  if(route.page==='newterm')termDialog();
}
function termDialog(id) {
  const term=model.terms.find(x=>x.id===id); dialog={type:'term',id,version:term?.version};
  modal(term?'编辑词条':'添加到词库',`<label class='field'><span>正确词</span><input class='text-input' id='termName' value='${esc(term?.term||'')}' placeholder='例如 TypeScript'><small>1–64 个字符；不可与已有正确词重复。</small></label><label class='field'><span>别名</span><input class='text-input' id='termAliases' value='${esc(term?.aliases.join('; ')||'')}' placeholder='例如 type script; typescript'><small>分号分隔，最多 10 个。作为语境提示，不进行全局替换。</small></label>${term?`<p>来源：${term.source==='auto'?'自动学习':'手动添加'} · 版本 ${term.version}</p><p>学习语境：${esc(term.context||'暂无')}</p>`:''}`,`${term?btn('删除词条','delete-term','text-btn'):btn('导入 CSV','import','text-btn')}${btn('取消','close-modal')}${btn(term?'保存':'添加词条','save-term','primary-btn')}`);
  if(!term) $('[data-action="save-term"]').disabled=true;
}
function termValidation(name, aliases) {
  if(!name||count(name)>64||/[\p{Cc}\p{Zl}\p{Zp}]/u.test(name))return '正确词须为 1–64 个字符，不能含换行或控制字符。';
  if(aliases.length>10||aliases.some(a=>count(a)>64||/[\p{Cc}\p{Zl}\p{Zp}]/u.test(a)))return '别名最多 10 个，每个 1–64 个字符，不能含换行或控制字符。';
  if(aliases.some(a=>norm(a)===norm(name)))return '别名不能与正确词相同。';
  return '';
}
const aliasList = value => [...new Map(value.split(';').map(x=>x.trim()).filter(Boolean).map(x=>[norm(x),x])).values()];
function saveTerm() {
  const name=$('#termName').value.trim(), aliases=aliasList($('#termAliases').value), old=model.terms.find(x=>x.id===dialog.id);
  let error=termValidation(name,aliases);
  if(!error&&model.terms.some(x=>x.id!==dialog.id&&norm(x.term)===norm(name)))error='正确词已存在，请编辑已有词条。';
  if(!error&&route.scene==='conflict')error='词条已更新，当前修改未保存。请关闭弹窗并刷新列表后重试。';
  if(error){$('#dialogError').textContent=error;return;}
  if(old) { const changed=old.term!==name||JSON.stringify(old.aliases)!==JSON.stringify(aliases); if(changed){old.context=old.aliases.every(a=>aliases.includes(a))?old.context:''; Object.assign(old,{term:name,aliases,source:'manual',version:old.version+1});model.dictionaryVersion++;} }
  else {model.terms.unshift({id:Date.now(),term:name,aliases,source:'manual',version:1,context:''});model.dictionaryVersion++;}
  closeDialog(); if(route.page==='newterm')go('dictionary');else render(); toast('词条已保存（本次原型演示）');
}
function importDialog() {
  dialog={type:'import'};
  modal('导入词库',`<p>UTF-8 CSV，表头为 term 与可选 aliases；最多 1 MiB / 1000 行。合法行导入，重复行跳过，冲突不覆盖。</p><label class='field'><span>选择 CSV 文件</span><input type='file' id='csvFile' accept='.csv,text/csv'></label><label class='field'><span>CSV 内容预览 · 默认使用固定示例</span><textarea class='text-input' id='csvText'>term,aliases
Kubernetes,k8s
React,
React,react framework
,empty
Example Tool,example kit</textarea></label><div id='csvSummary'></div>`,btn('取消','close-modal')+btn('导入','run-import','primary-btn'),true);
}
function parseCSV(text) {
  const rows=[]; let row=[],field='',quoted=false,closed=false;
  text=text.replace(/^\uFEFF/,'');
  for(let i=0;i<text.length;i++) {
    const c=text[i];
    if(quoted){if(c==='"'&&text[i+1]==='"'){field+='"';i++;}else if(c==='"'){quoted=false;closed=true;}else field+=c;continue;}
    if(c==='"'){if(field||closed)throw Error('CSV 引号位置不正确');quoted=true;}
    else if(c===','){row.push(field);field='';closed=false;}
    else if(c==='\r'||c==='\n'){if(c==='\r'&&text[i+1]==='\n')i++;row.push(field);rows.push(row);row=[];field='';closed=false;}
    else {if(closed)throw Error('CSV 引号结束后只能接分隔符');field+=c;}
  }
  if(quoted)throw Error('CSV 引号未闭合');
  if(field||row.length||closed){row.push(field);rows.push(row);}
  const header=rows.shift()||[];
  if(!header.includes('term')||new Set(header).size!==header.length||header.some(x=>!['term','aliases'].includes(x)))throw Error('需要唯一的 term 列，只有 aliases 是可选列');
  if(rows.length>1000)throw Error('数据行超过 1000 行');
  if(rows.some(r=>r.length!==header.length))throw Error('CSV 列数与表头不一致');
  return rows.map((r,i)=>({line:i+2,term:r[header.indexOf('term')].trim(),aliases:header.includes('aliases')?aliasList(r[header.indexOf('aliases')]):[]}));
}
function runImport() {
  try {
    const text=$('#csvText').value;if(new TextEncoder().encode(text).length>1048576)throw Error('CSV 文件不能超过 1 MiB');
    const rows=parseCSV(text), totals={created:0,skipped:0,conflict:0,invalid:0};
    const details=rows.map(row=>{
      let status='created',error=termValidation(row.term,row.aliases); const old=model.terms.find(t=>norm(t.term)===norm(row.term));
      if(error)status='invalid';else if(old)status=JSON.stringify(old.aliases.map(norm).sort())===JSON.stringify(row.aliases.map(norm).sort())?'skipped':'conflict';
      if(status==='created'){model.terms.push({id:Date.now()+row.line,term:row.term,aliases:row.aliases,source:'manual',version:1,context:''});model.dictionaryVersion++;}
      totals[status]++;return `<tr><td>${row.line}</td><td>${{created:'已导入',skipped:'已跳过',conflict:'冲突，未覆盖',invalid:'无效行'}[status]}</td><td>${esc(error||row.term)}</td></tr>`;
    });
    $('#csvSummary').innerHTML=note(`导入 ${totals.created}，跳过 ${totals.skipped}，冲突 ${totals.conflict}，无效 ${totals.invalid}。`,'info')+`<div class='table-wrap'><table class='admin-table'><thead><tr><th>行号</th><th>结果</th><th>说明</th></tr></thead><tbody>${details.join('')}</tbody></table></div>`;
    $('#dialogError').textContent='';$('[data-action="run-import"]').disabled=true;
  }catch(e){$('#dialogError').textContent=e.message;}
}
function userDialog() {
  dialog={type:'user'};
  modal('创建用户',`<p>创建普通用户，可立即登录，无需邮件验证。</p><label class='field'><span>邮箱</span><input class='text-input' type='email' id='userEmail' placeholder='u3@example.test'></label><label class='field'><span>初始密码</span><input class='text-input' type='password' id='userPassword' autocomplete='off'><small>8–128 个字符。原型不保存或传输密码。</small></label>`,btn('取消','close-modal')+btn('创建用户','save-user','primary-btn','disabled'));
}
function startTransition(stage, target, delay=700) {
  go('result',stage);
  setTimeout(()=>{if(route.page==='result'&&route.scene===stage){transitionTimer=setTimeout(()=>{if(route.page==='result'&&route.scene===stage)go('result',target);},delay);}},30);
}
const actions={
  home:()=>go('home'),settings:()=>go('settings'),dictionary:()=>go('dictionary'),onboarding:()=>go('onboarding'),'admin-login':()=>go('admin-login'),review:()=>go('result','review'),
  help:()=>{model.settingsTab='help';go('settings');if(route.page==='settings')render();},
  'start-recording':()=>{model.resultFallback=false;model.resultSuggested=false;go('result','recording');},'try-recording':()=>actions['start-recording'](),
  'finish-recording':()=>{model.resultFallback=false;model.resultSuggested=false;startTransition('finalizing','inserted',800);},
  'cancel-recording':()=>{clearTimeout(transitionTimer);go('result','cancelled');},
  'retry-asr':()=>startTransition('finalizing','target-changed'), 'retry-refine':()=>startTransition('refining','target-changed'),
  'confirm-result':()=>{model.resultFallback=true;model.resultSuggested=false;go('result','inserted');toast('模拟：确认原文后输入并读回成功');},
  retarget:()=>{dialog={type:'target'};modal('重新指定输入框',`<p>请把光标放入目标输入框。确认后重新捕获并校验目标；不会恢复旧焦点后直接写入。</p><label class='field'><span>本次模拟目标</span><textarea class='text-input' readonly>前缀　[光标]　后缀</textarea></label>`,btn('取消','close-modal')+btn('确认目标并输入','apply-target','primary-btn'));},
  'apply-target':()=>{model.resultFallback=false;model.resultSuggested=false;closeDialog();go('result','inserted');},
  'copy-raw':()=>toast('原型演示：原文复制反馈（未写入系统剪贴板）'), 'copy-result':()=>toast('原型演示：结果复制反馈（未写入系统剪贴板）'),
  'check-mic':()=>{model.mic=true;go('onboarding',model.access?'granted':'access-denied');},
  'check-access':()=>{model.access=true;go('onboarding',model.mic?'granted':'mic-denied');},
  'finish-onboarding':()=>go('home'),
  'permission-help':()=>{dialog={type:'permission'};modal('输入权限',`<p>macOS：系统设置 → 隐私与安全性 → 辅助功能 → 允许 SmartVoiceInput。</p><p>Windows：确认应用和目标处于可访问的桌面与权限级别；受保护或不可验证的目标使用复制。</p><p>原型不会请求真实系统权限，返回后可点击“重新检查”演示授权完成。</p>`,btn('知道了','close-modal','primary-btn'));},
  'reset-scene':()=>{go(route.page);if(route.scene==='default')render();},
  'new-term':()=>termDialog(), 'save-term':saveTerm, 'close-modal':()=>{closeDialog();if(route.page==='newterm')go('dictionary');else if(route.page==='dictionary')render();},
  import:importDialog,'run-import':runImport,
  'clear-search':()=>{model.query='';model.source='all';render();},
  'delete-term':()=>{const id=dialog.id;dialog={type:'delete',id};modal('删除词条',`<p>此词条及其别名、学习语境将从词库移除。已经发生的学习次数不会减少。</p>`,btn('取消','close-modal')+btn('确认删除','confirm-delete','primary-btn'));},
  'confirm-delete':()=>{model.terms=model.terms.filter(x=>x.id!==dialog.id);model.dictionaryVersion++;closeDialog();render();toast('词条已删除');},
  privacy:()=>{dialog={type:'privacy'};modal('数据与保留',privacyText(),btn('关闭','close-modal','primary-btn'),true);},
  'confirm-retention':()=>{model.retention=dialog.next;closeDialog();render();toast('保留期限已更新；过期正文立即不可读，1 小时内清理');},
  logout:()=>{dialog={type:'logout'};modal('退出当前登录',`<p>本机界面缓存和修改观察将结束，其他设备保持登录。</p>`,btn('取消','close-modal')+btn('退出登录','confirm-logout','primary-btn'));},
  'confirm-logout':()=>{model.query='';model.source='all';closeDialog();go('login');},'admin-logout':()=>go('admin-login'),
  'save-shortcut':()=>{const value=$('#shortcutInput').value.trim();if(route.scene==='conflict'||!value.includes('+')||/^(Fn|Option)$/i.test(value)){toast('快捷键不可用或冲突，已保留旧设置');$('#shortcutInput').value=model.shortcut;}else {model.shortcut=value;toast('快捷键已保存（原型不注册系统快捷键）');}},
  'recheck-service':()=>{go('settings');toast('服务连接恢复（模拟）');},
  'history-detail-a':()=>historyDetail('你好 API','已输入 · 10 秒'), 'history-detail-b':()=>historyDetail('版本 2 React','已完成 · 20 秒'),
  'create-user':userDialog,
  'save-user':()=>{
    const email=$('#userEmail').value.trim().toLowerCase(), password=$('#userPassword').value;
    let error=!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)||email.length>254?'请输入有效邮箱':count(password)<8||count(password)>128?'密码须为 8–128 个字符':model.users.some(u=>u.email===email)?'该邮箱已注册':'';
    if(error){$('#dialogError').textContent=error;return;}model.users.push({id:Date.now(),email,role:'user',status:'active',sessions:0});closeDialog();render();toast('普通用户已创建（模拟）');
  },
  'toggle-user':el=>{
    const user=model.users.find(u=>u.id===Number(el.dataset.user));if(!user||user.role==='admin')return;
    dialog={type:'toggle-user',id:user.id};modal(user.status==='active'?'禁用用户':'启用用户',`<p>${esc(user.email)}</p><p>${user.status==='active'?'禁用将撤销全部登录；进行中的会话最迟 5 秒终止。':'启用后仍需重新登录，旧凭据不会恢复。'}</p>`,btn('取消','close-modal')+btn('确认','confirm-user-status','primary-btn'));
  },
  'confirm-user-status':()=>{if(route.scene==='conflict'){$('#dialogError').textContent='状态已变化，请刷新后重试。';return;}const u=model.users.find(x=>x.id===dialog.id);u.status=u.status==='active'?'disabled':'active';closeDialog();render();toast('账号状态已更新（模拟）');},
  'refresh-capacity':()=>{if(route.scene==='default')render();else go('admin-capacity');toast('已刷新示例配置；真实页面每 5 秒读取运行状态');},
  'confirm-capacity':()=>commitCapacity({...dialog}),
  'confirm-learn':()=>{if(model.applied||!model.learning)return;model.applied=true;model.learningCount++;model.dictionaryVersion++;model.terms.push({id:Date.now(),term:'芸',aliases:['云'],source:'auto',version:1,context:'本次命名修正，用户已确认'});render();toast('已确认建议并加入词库（模拟）');},
  'dismiss-suggestion':()=>{model.resultSuggested=true;go('result','inserted');},
  'undo-learn':()=>{if(model.undone)return;const term=model.terms.find(x=>x.id===7);if(!term||term.version!==1){go('result','undo-conflict');return;}model.undone=true;model.terms=model.terms.filter(x=>x.id!==7);model.dictionaryVersion++;render();toast('本次新词学习已撤销；累计学习次数保留');},
  'toggle-term':el=>{const id=Number(el.dataset.term);model.selectedTerms=model.selectedTerms.includes(id)?model.selectedTerms.filter(x=>x!==id):[...model.selectedTerms,id];render();},
  'cancel-selection':()=>{model.selectedTerms=[];render();},
  'select-loaded':()=>{const rows=model.terms.filter(x=>(model.source==='all'||x.source===model.source)&&norm(x.term+' '+x.aliases.join(' ')).includes(norm(model.query)));model.selectedTerms=rows.every(x=>model.selectedTerms.includes(x.id))?[]:rows.map(x=>x.id);render();},
  'bulk-delete':()=>{modal('删除所选词条',`<p>确认删除所选 ${model.selectedTerms.length} 个词？历史记录与累计学习次数不受影响。</p>`,btn('取消','close-modal')+btn('确认删除','confirm-bulk-delete','danger-btn'));},
  'confirm-bulk-delete':()=>{if(route.scene==='conflict'){closeDialog();render();toast('词条版本冲突，整批未删除');return;}model.terms=model.terms.filter(x=>!model.selectedTerms.includes(x.id));model.selectedTerms=[];model.dictionaryVersion++;closeDialog();render();toast('已删除所选词条（原型模拟）');}
};
Object.assign(actions, providerActions);
function historyDetail(text,status) {dialog={type:'history'};modal('口述详情',`<p>${status}</p><h3>原始转写</h3><p>${text}</p><h3>整理结果</h3><p>${text}</p>`,btn('复制原文','copy-raw')+btn('复制结果','copy-result')+btn('关闭','close-modal','primary-btn'));}
document.addEventListener('click',e=>{
  const el=e.target.closest('button, a');if(!el||el.disabled)return;
  if(el.dataset.go){go(el.dataset.go);return;}
  if(el.dataset.action){actions[el.dataset.action]?.(el);return;}
  if(el.dataset.edit){termDialog(Number(el.dataset.edit));return;}
  if(el.dataset.source){model.selectedTerms=[];model.source=el.dataset.source;render();return;}
  if(el.dataset.historyFilter){model.historyFilter=el.dataset.historyFilter;render();return;}
  if(el.dataset.settings){model.settingsTab=el.dataset.settings;render();}
});
document.addEventListener('input',e=>{
  if(e.target.id==='termSearch'||e.target.id==='userSearch'){
    const id=e.target.id,pos=e.target.selectionStart;if(id==='termSearch')model.selectedTerms=[];model[id==='termSearch'?'query':'userQuery']=e.target.value;render();$('#'+id).focus();$('#'+id).setSelectionRange(pos,pos);
  }
  if(e.target.id==='termName')$('[data-action="save-term"]').disabled=!e.target.value.trim();
  if(['userEmail','userPassword'].includes(e.target.id))$('[data-action="save-user"]').disabled=!$('#userEmail').value.trim()||!$('#userPassword').value;
  if(e.target.id==='csvText'){$('[data-action="run-import"]').disabled=false;$('#csvSummary').innerHTML='';}
});
document.addEventListener('change',async e=>{
  if(e.target.id==='sceneSelect')go(route.page,e.target.value);
  if(e.target.id==='localeSelect'){model.locale=e.target.value;render();}
  if(e.target.id==='learningToggle'){model.learning=e.target.checked;toast(model.learning?'自动学习已开启':'自动学习已关闭；当前观察停止');}
  if(e.target.id==='userStatus'){model.userFilter=e.target.value;render();}
  if(e.target.id==='retentionSelect'){
    const next=Number(e.target.value);if(next!==0&&(model.retention===0||next<model.retention)){
      e.target.value=String(model.retention);dialog={type:'retention',next};modal('缩短历史保留期限',`<p>超过 ${next} 天的历史正文将永久清理，之后延长期限也不能恢复。</p><p>累计统计与独立词库保留；旧备份按最长 7 天保留周期清除。</p>`,btn('取消','close-modal')+btn('确认并保存','confirm-retention','primary-btn'));
    }else{model.retention=next;render();toast('保留期限已更新；已清正文不能恢复');}
  }
  if(e.target.id==='csvFile'){
    const f=e.target.files[0];if(!f)return;
    try{if(f.size>1048576)throw Error('文件超过 1 MiB');const data=new TextDecoder('utf-8',{fatal:true}).decode(await f.arrayBuffer());$('#csvText').value=data;$('[data-action="run-import"]').disabled=false;$('#csvSummary').innerHTML='';$('#dialogError').textContent='';}catch(err){$('#dialogError').textContent='无法读取 UTF-8 CSV：'+err.message;}
  }
});
document.addEventListener('submit',e=>{
  e.preventDefault();const form=e.target;
  if(form.id==='authForm'){
    const values=new FormData(form),email=String(values.get('email')).trim().toLowerCase(),password=String(values.get('password'));
    if(count(password)<8||count(password)>128){$('#authError').textContent='密码须为 8–128 个字符，空格也计入长度。';return;}
    const errors={invalid:'邮箱或密码错误',duplicate:'该邮箱已注册，请直接登录',disabled:'账号已禁用，请联系管理员',limited:'请求过于频繁，请稍后重试',forbidden:'当前账号没有管理员权限',offline:'无法连接服务，请稍后重试'};
    if(errors[route.scene]){$('#authError').textContent=errors[route.scene];return;}
    const page=route.page;const submit=form.querySelector('[type=submit]');submit.disabled=true;submit.textContent='正在提交…';
    const hash=location.hash;setTimeout(()=>{if(location.hash!==hash)return;model.email=email;go(page==='register'?'login':page==='admin-login'?'admin-users':'onboarding');if(page==='register')toast('注册完成，请登录（模拟）');},450);
  }
  if(form.id==='capacityForm'){saveCapacity();return;}
  if(form.id==='dateForm'){
    const v=new FormData(form),days=(Date.parse(v.get('to'))-Date.parse(v.get('from')))/86400000+1;
    $('#dateError').textContent=!Number.isFinite(days)||days<1||days>366?'请选择有效日期，含首尾最多 366 天。':'范围格式有效；原型保持固定示例数据，不发起真实查询。';
  }
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if(dialog){actions['close-modal']();return;}
    if(route.page==='result'&&['preparing','recording','finalizing','refining','processing-slow'].includes(route.scene)){actions['cancel-recording']();return;}
  }
  if(e.key==='Tab'&&dialog){
    const nodes=Array.from($('#modalLayer').querySelectorAll('button:not([disabled]),input:not([disabled]),textarea,select,a[href]'));const first=nodes[0],last=nodes.at(-1);
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
  }
});
$('#resetDemo').addEventListener('click',()=>location.reload());
window.addEventListener('hashchange',render);
render();
