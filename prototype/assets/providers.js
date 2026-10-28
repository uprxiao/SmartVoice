/* V2 · 管理后台“模型配置”页原型：虚构数据、内存状态；不联网、不保存真实密钥。
   依据 docs/02-prd/PRD-v2.md FR-11 与 docs/03-architecture/v2-provider-design-20261027.md §7–§8。
   必须在 app.js 之前加载：这里只定义函数和数据，渲染时才使用 app.js 中的 btn/pill/note/modal 等工具。 */
'use strict';
const providerAllowlist = ['*.maas.aliyuncs.com', 'api.deepseek.com'];
const providerTypes = {
  asr: [['bailian_realtime', '百炼云端识别（流式）'], ['qwen3_mlx', '本机识别（Qwen3-ASR · 仅 macOS Apple Silicon）']],
  llm: [['openai_compatible', 'OpenAI 兼容接口'], ['ollama', 'Ollama（本机/内网）']]
};
const pmodel = {
  seq: 10, cloudConfirmed: false, editing: null,
  items: [
    {id: 1, kind: 'asr', name: '本机识别', type: 'qwen3_mlx', url: '', modelId: 'qwen3-asr-1.7b', secret: '', enabled: true, active: true, maxRecord: 180, maxConc: 1, test: {ok: true, at: '10-27 14:02'}, version: 3},
    {id: 2, kind: 'asr', name: '百炼云端识别', type: 'bailian_realtime', url: 'wss://ws-••••.cn-beijing.maas.aliyuncs.com', modelId: 'qwen3-asr-flash-realtime', secret: '3f9a', enabled: true, active: false, maxRecord: 360, maxConc: 8, test: {ok: true, at: '10-27 14:05'}, version: 2},
    {id: 3, kind: 'llm', name: 'DeepSeek 整理', type: 'openai_compatible', url: 'https://api.deepseek.com', modelId: 'deepseek-flash', secret: 'a71c', enabled: true, active: true, maxConc: 4, test: {ok: true, at: '10-27 13:58'}, version: 5},
    {id: 4, kind: 'llm', name: '内网备用模型', type: 'ollama', url: 'http://10.0.0.8:11434', modelId: 'qwen3-coder:30b', secret: '', enabled: false, active: false, maxConc: 1, test: {ok: false, at: '10-26 18:20', reason: '连接超时'}, version: 1}
  ],
  audit: [
    ['10-27 14:05', 'admin0@example.test', '测试连接', '百炼云端识别', '—', '成功'],
    ['10-27 14:04', 'admin0@example.test', '修改', '百炼云端识别', 'api_key、model_id', '成功'],
    ['10-27 13:58', 'admin1@example.test', '设为当前', 'DeepSeek 整理', '—', '成功'],
    ['10-26 18:20', 'admin0@example.test', '测试连接', '内网备用模型', '—', '失败：连接超时']
  ]
};
const pScene = () => route.scene;
const pBusy = () => pScene() === 'submitting';
const typeLabel = (kind, type) => (providerTypes[kind].find(([v]) => v === type) || [type, type])[1];
const typeShort = {bailian_realtime: '百炼云端 · 流式', qwen3_mlx: '本机 Qwen3-ASR · 仅 Mac', openai_compatible: 'OpenAI 兼容接口', ollama: 'Ollama · 本机/内网'};
const isCloudAsr = p => p.kind === 'asr' && p.type === 'bailian_realtime';
const isLinux = () => pScene() === 'linux-host';

function providerHostAllowed(url) {
  let u; try { u = new URL(url); } catch { return false; }
  if (u.username || u.password) return false;
  const host = u.hostname.toLowerCase();
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || host.includes(':')) return host === '10.0.0.8' || host === '127.0.0.1';
  if (host === 'localhost' || host.endsWith('.local')) return true;
  return providerAllowlist.some(rule => rule.startsWith('*.') ? host.endsWith(rule.slice(1)) && host.length > rule.length - 1 : host === rule);
}
function providerStatus(p) {
  if (p.active) return pill('当前使用', 'good');
  if (!p.enabled) return pill('已停用');
  if (p.test && !p.test.ok) return pill('测试失败', 'warn');
  return pill('可用');
}
function providerRows(kind) {
  const rows = pScene() === 'empty' ? [] : pmodel.items.filter(p => p.kind === kind);
  if (!rows.length) return `<tr><td colspan='5'>后台尚未添加${kind === 'asr' ? '语音识别' : '文字整理'}配置，正在使用部署 YAML 默认项。${btn('添加', 'provider-new', 'small-btn', `data-kind='${kind}' ${pBusy() ? 'disabled' : ''}`)}</td></tr>`;
  return rows.map(p => {
    const testing = pBusy() && p.id === 2;
    const testText = testing ? '测试中…' : p.test ? `${p.test.ok ? '成功' : '失败：' + esc(p.test.reason || '')}<small>${p.test.at}</small>` : '未测试';
    const linuxBlocked = isLinux() && p.type === 'qwen3_mlx';
    const actionsHtml = [
      btn('编辑', 'provider-edit', 'small-btn', `data-id='${p.id}' ${pBusy() ? 'disabled' : ''}`),
      btn('测试', 'provider-test', 'small-btn', `data-id='${p.id}' ${pBusy() || linuxBlocked ? 'disabled' : ''}`),
      p.active ? '' : btn('设为当前', 'provider-activate', 'small-btn', `data-id='${p.id}' ${pBusy() || !p.enabled || linuxBlocked ? `disabled title='${linuxBlocked ? '当前主机不支持本机识别' : '停用的配置不能设为当前'}'` : ''}`),
      btn(p.enabled ? '停用' : '启用', 'provider-toggle', 'small-btn', `data-id='${p.id}' ${pBusy() || p.active ? "disabled title='当前使用中的配置不能停用'" : ''}`),
      btn('删除', 'provider-delete', 'small-btn', `data-id='${p.id}' ${pBusy() || p.active ? "disabled title='当前使用中的配置不能删除'" : ''}`)
    ].join(' ');
    const where = p.url ? esc(p.url) : '本机进程（无地址）';
    const keyText = p.type === 'qwen3_mlx' || p.type === 'ollama' ? '无需密钥' : p.secret ? `密钥 ••••${esc(p.secret)}` : '未设置密钥';
    const extra = p.kind === 'asr' ? ` · 单次 ≤${p.maxRecord} 秒 · 并发 ≤${p.maxConc}` : ` · 并发 ≤${p.maxConc}`;
    return `<tr><td>${esc(p.name)}<small>${esc(typeShort[p.type] || p.type)}${linuxBlocked ? ' · 当前主机不支持' : ''}</small></td><td>${where}<small>${esc(p.modelId)} · ${keyText}${extra}</small></td><td>${providerStatus(p)}</td><td>${testText}</td><td class='provider-actions'>${actionsHtml}</td></tr>`;
  }).join('');
}
function providerSection(kind) {
  const title = kind === 'asr' ? '语音识别' : '文字整理';
  const desc = kind === 'asr' ? '录音转文字。切换后只影响新开始的口述；进行中的口述按原配置完成。' : '把识别结果整理成可发送的文字。词库在这一步参与纠错。';
  return `<section class='provider-section'><div class='provider-section-head'><div><h2>${title}</h2><p>${desc}</p></div>${btn('添加配置', 'provider-new', 'secondary-btn', `data-kind='${kind}' ${pBusy() ? 'disabled' : ''}`)}</div><div class='table-wrap'><table class='admin-table provider-table'><thead><tr><th>名称 / 类型</th><th>地址 / 模型</th><th>状态</th><th>最近测试</th><th>操作</th></tr></thead><tbody>${providerRows(kind)}</tbody></table></div></section>`;
}
function providersPage() {
  const top = head('模型配置', '语音识别与文字整理服务。每类可保存多个配置，选择 1 个作为当前使用。', btn('刷新', 'provider-refresh', 'secondary-btn', pBusy() ? 'disabled' : ''));
  if (['loading', 'error', 'forbidden'].includes(pScene())) return top + stateView(pScene(), pScene() === 'error' ? {title: '暂时无法读取配置', desc: '读取失败不代表没有配置；当前服务仍按上次生效的配置运行。请稍后重试。'} : {});
  const asrActive = pmodel.items.find(p => p.kind === 'asr' && p.active), llmActive = pmodel.items.find(p => p.kind === 'llm' && p.active);
  let banner;
  if (pScene() === 'fallback') banner = note('后台保存的密钥无法解密（部署主密钥缺失或不匹配），已回落部署 YAML 默认配置：本机识别 / DeepSeek 整理（环境变量密钥）。请检查部署密钥后，重新录入并测试后台配置。', 'error');
  else if (pScene() === 'no-provider') banner = note('当前没有可用的语音识别服务：后台配置测试失败且部署 YAML 未配置可用项。用户开始口述会收到“识别服务暂不可用”，不会改用其他服务。', 'error');
  else if (pScene() === 'empty') banner = note('当前来源：部署 YAML（后台尚无配置）。添加并测试通过后，可设为当前使用。', 'info');
  else banner = `<div class='provider-summary'><div><span>当前语音识别</span><strong>${esc(asrActive?.name || '—')}</strong><small>${asrActive ? esc(typeShort[asrActive.type]) + ` · 单次 ≤${asrActive.maxRecord} 秒` : ''}</small></div><div><span>当前文字整理</span><strong>${esc(llmActive?.name || '—')}</strong><small>${llmActive ? esc(llmActive.modelId) : ''}</small></div><div><span>配置来源</span><strong>后台配置</strong><small>部署 YAML 作为兜底</small></div></div>`;
  const conflict = pScene() === 'conflict' ? note('配置已被其他管理员修改（版本已变化）。你的修改没有保存，请刷新后重新编辑。', 'error') : '';
  const linux = isLinux() ? note('当前服务器：Linux x86_64。本机识别（Qwen3-ASR）只能在 macOS Apple Silicon 主机运行，此处不可测试或设为当前。', 'info') : '';
  const allow = `<p class='provider-allow'>允许的云端地址（部署配置，后台不可修改）：${providerAllowlist.map(x => `<code>${x}</code>`).join(' ')}</p>`;
  const audit = `<details class='provider-audit' ${pScene() === 'default' ? '' : ''}><summary>最近变更（仅记录字段名，不含密钥与地址内容）</summary><div class='table-wrap'><table class='admin-table'><thead><tr><th>时间</th><th>管理员</th><th>操作</th><th>配置</th><th>变更字段</th><th>结果</th></tr></thead><tbody>${pmodel.audit.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>`;
  return top + banner + conflict + linux + providerSection('asr') + providerSection('llm') + allow + audit;
}
function providerDialog(kind, id) {
  const p = pmodel.items.find(x => x.id === id);
  pmodel.editing = {kind, id, version: p?.version};
  const types = providerTypes[kind].map(([v, l]) => `<option value='${v}' ${(p ? p.type : providerTypes[kind][0][0]) === v ? 'selected' : ''} ${isLinux() && v === 'qwen3_mlx' ? 'disabled' : ''}>${l}${isLinux() && v === 'qwen3_mlx' ? '（当前主机不支持）' : ''}</option>`).join('');
  const type = p ? p.type : providerTypes[kind][0][0];
  const needsKey = type === 'bailian_realtime' || type === 'openai_compatible';
  const keyField = `<label class='field' id='providerKeyField' ${needsKey ? '' : 'hidden'}><span>API Key</span><input class='text-input' id='providerKey' type='password' autocomplete='off' placeholder='${p?.secret ? `已设置 ••••${esc(p.secret)}，留空则保持不变` : '粘贴密钥（仅保存，之后不再显示）'}'><small>加密后保存，页面和接口都不会再显示原文。${p?.secret ? `<label class='provider-inline'><input type='checkbox' id='providerKeyClear'> 清除已保存的密钥</label>` : ''}</small></label>`;
  const urlField = `<label class='field' id='providerUrlField' ${type === 'qwen3_mlx' ? 'hidden' : ''}><span>地址</span><input class='text-input' id='providerUrl' value='${esc(p?.url || '')}' placeholder='${kind === 'asr' ? 'wss://<业务空间>.cn-beijing.maas.aliyuncs.com' : 'https://api.deepseek.com'}'><small>必须在部署允许的地址内：${providerAllowlist.join('、')}；本机/内网地址按部署规则另行允许。</small></label>`;
  const asrExtra = kind === 'asr' ? `<label class='field'><span>单次录音上限（秒）</span><input class='text-input' id='providerMaxRecord' type='number' min='10' max='${type === 'qwen3_mlx' ? 180 : 360}' value='${p?.maxRecord || 360}'><small>云端最长 360 秒（6 分钟），本机识别最长 180 秒；新上限只用于之后开始的口述。</small></label>` : '';
  modal(p ? '编辑配置' : (kind === 'asr' ? '添加语音识别配置' : '添加文字整理配置'),
    `<label class='field'><span>名称</span><input class='text-input' id='providerName' value='${esc(p?.name || '')}' placeholder='例如 百炼云端识别'><small>1–64 个字符，同类中不能重名。</small></label>
     <label class='field'><span>类型</span><select class='select' id='providerType'>${types}</select></label>
     ${urlField}
     <label class='field'><span>模型名称</span><input class='text-input' id='providerModel' value='${esc(p?.modelId || (kind === 'asr' ? 'qwen3-asr-flash-realtime' : ''))}'></label>
     ${keyField}${asrExtra}
     <label class='field'><span>最大并发</span><input class='text-input' id='providerConc' type='number' min='1' max='20' value='${p?.maxConc || (kind === 'asr' ? 8 : 4)}'><small>不能超过后台“并发容量”中的全局上限。</small></label>
     ${note('保存前会先做一次连通测试（云端识别会发送 1 秒静音，产生少量费用）。测试不通过则不保存，原配置不变。', 'info')}`,
    btn('取消', 'close-modal') + btn('测试并保存', 'provider-save', 'primary-btn', pBusy() ? 'disabled' : ''), true);
  dialog = {type: 'provider'};
}
function providerConfirmActivate(p) {
  pmodel.editing = {activate: p.id};
  if (isCloudAsr(p) && !pmodel.cloudConfirmed) {
    modal('启用云端语音识别', `<p>设为当前后，用户之后开始的口述，<b>音频会实时发送到第三方云端识别服务</b>（${esc(p.url)}）进行识别。服务端不长期保存音频；进行中的口述仍按原配置完成。</p><p>单次录音上限将变为 ${p.maxRecord} 秒。启用记录会写入变更日志。</p><label class='provider-inline'><input type='checkbox' id='cloudConfirm'> 我已了解音频会发送到第三方云端识别服务</label>`,
      btn('取消', 'close-modal') + btn('确认并设为当前', 'provider-activate-confirm', 'primary-btn', 'disabled'));
  } else {
    modal('设为当前使用', `<p>将“${esc(p.name)}”设为当前${p.kind === 'asr' ? '语音识别' : '文字整理'}。切换前会再测一次连接；失败则保持原配置。</p><p>只影响之后开始的${p.kind === 'asr' ? '口述' : '整理'}，进行中的不受影响。</p>`,
      btn('取消', 'close-modal') + btn('确认切换', 'provider-activate-confirm', 'primary-btn'));
  }
  dialog = {type: 'provider'};
}
function providerAudit(action, p, fields, result) {
  pmodel.audit.unshift(['刚刚', 'admin0@example.test', action, p.name, fields || '—', result]);
}
const providerActions = {
  'provider-refresh': () => { if (pScene() === 'default') render(); else go('admin-providers'); toast('已重新读取配置（原型模拟）'); },
  'provider-new': el => providerDialog(el.dataset.kind),
  'provider-edit': el => { const p = pmodel.items.find(x => x.id === Number(el.dataset.id)); providerDialog(p.kind, p.id); },
  'provider-test': el => {
    const p = pmodel.items.find(x => x.id === Number(el.dataset.id));
    const reason = pScene() === 'test-failed' ? '鉴权失败（401）：请检查 API Key' : !p.enabled ? '连接超时' : '';
    p.test = {ok: !reason, at: '刚刚', reason}; providerAudit('测试连接', p, '', reason ? '失败：' + reason : '成功'); render();
    toast(reason ? `“${p.name}”测试失败：${reason}` : `“${p.name}”连接正常（原型模拟）`);
  },
  'provider-save': () => {
    const e = pmodel.editing, kind = e.kind, name = $('#providerName').value.trim(), type = $('#providerType').value;
    const url = $('#providerUrl') ? $('#providerUrl').value.trim() : '', modelId = $('#providerModel').value.trim();
    const maxRecord = Number($('#providerMaxRecord')?.value || 0), maxConc = Number($('#providerConc').value);
    const old = pmodel.items.find(x => x.id === e.id), key = $('#providerKey')?.value || '', clear = $('#providerKeyClear')?.checked;
    const needsKey = type === 'bailian_realtime' || type === 'openai_compatible';
    let error = '';
    if (!name || count(name) > 64) error = '名称须为 1–64 个字符。';
    else if (pmodel.items.some(x => x.kind === kind && x.name === name && x.id !== e.id)) error = '同类中已有同名配置。';
    else if (type !== 'qwen3_mlx' && !url) error = '请填写地址。';
    else if (type === 'bailian_realtime' && !url.startsWith('wss://')) error = '云端识别地址必须以 wss:// 开头。';
    else if (type === 'openai_compatible' && !url.startsWith('https://')) error = '公网整理模型地址必须以 https:// 开头。';
    else if (type !== 'qwen3_mlx' && (pScene() === 'egress-denied' || !providerHostAllowed(url))) error = '该地址不在部署允许的范围内（' + providerAllowlist.join('、') + '），未保存。如需接入新服务，请联系部署人员修改服务器配置。';
    else if (!modelId) error = '请填写模型名称。';
    else if (needsKey && !key && (!old?.secret || clear)) error = '该类型需要 API Key。';
    else if (kind === 'asr' && (!(maxRecord >= 10) || maxRecord > (type === 'qwen3_mlx' ? 180 : 360))) error = `单次录音上限须为 10–${type === 'qwen3_mlx' ? 180 : 360} 秒。`;
    else if (!(maxConc >= 1 && maxConc <= 20)) error = '最大并发须为 1–20。';
    else if (pScene() === 'conflict' && old) error = '配置已被其他管理员修改，你的修改没有保存。请关闭后刷新再编辑。';
    else if (pScene() === 'test-failed') error = '连通测试失败：鉴权失败（401）。配置未保存，原配置不变。';
    if (error) { $('#dialogError').textContent = error; return; }
    const changed = [];
    const next = {kind, name, type, url: type === 'qwen3_mlx' ? '' : url, modelId, maxConc, maxRecord: kind === 'asr' ? maxRecord : undefined, test: {ok: true, at: '刚刚'}};
    if (old) {
      ['name', 'type', 'url', 'modelId', 'maxConc', 'maxRecord'].forEach(k => { if (old[k] !== next[k]) changed.push({modelId: 'model_id', maxConc: 'max_concurrent', maxRecord: 'max_record_seconds', url: 'base_url'}[k] || k); });
      if (key || clear) changed.push('api_key');
      Object.assign(old, next, {secret: clear ? '' : key ? key.slice(-4) : old.secret, version: old.version + 1});
      providerAudit('修改', old, changed.join('、') || '—', '成功');
    } else {
      const item = {...next, id: ++pmodel.seq, enabled: true, active: false, secret: key ? key.slice(-4) : '', version: 1};
      pmodel.items.push(item); providerAudit('新增', item, 'name、type、base_url、model_id' + (key ? '、api_key' : ''), '成功');
    }
    closeDialog(); render(); toast('连通测试通过，配置已保存（原型模拟；密钥只保存末 4 位用于显示）');
  },
  'provider-activate': el => providerConfirmActivate(pmodel.items.find(x => x.id === Number(el.dataset.id))),
  'provider-activate-confirm': () => {
    const p = pmodel.items.find(x => x.id === pmodel.editing.activate);
    if (pScene() === 'conflict') { $('#dialogError').textContent = '当前配置已被其他管理员切换，本次未生效。请刷新后再试。'; return; }
    if (pScene() === 'test-failed') { $('#dialogError').textContent = '切换前的连通测试失败：鉴权失败（401）。当前配置保持不变。'; return; }
    if (isCloudAsr(p) && !pmodel.cloudConfirmed) { pmodel.cloudConfirmed = true; providerAudit('确认云端识别', p, '', '成功'); }
    pmodel.items.filter(x => x.kind === p.kind).forEach(x => { x.active = x.id === p.id; });
    providerAudit('设为当前', p, '', '成功'); closeDialog(); render();
    toast(`已切换为“${p.name}”，之后开始的${p.kind === 'asr' ? '口述' : '整理'}使用新配置（原型模拟）`);
  },
  'provider-toggle': el => { const p = pmodel.items.find(x => x.id === Number(el.dataset.id)); if (p.active) return; p.enabled = !p.enabled; p.version++; providerAudit(p.enabled ? '启用' : '停用', p, 'enabled', '成功'); render(); toast(`“${p.name}”已${p.enabled ? '启用' : '停用'}（原型模拟）`); },
  'provider-delete': el => { const p = pmodel.items.find(x => x.id === Number(el.dataset.id)); if (p.active) return; pmodel.editing = {remove: p.id}; modal('删除配置', `<p>删除“${esc(p.name)}”？保存的密钥会一起删除，无法恢复；变更日志保留。</p>`, btn('取消', 'close-modal') + btn('确认删除', 'provider-delete-confirm', 'danger-btn')); dialog = {type: 'provider'}; },
  'provider-delete-confirm': () => { if (pScene() === 'conflict') { $('#dialogError').textContent = '配置已被修改，本次未删除。请刷新后重试。'; return; } const p = pmodel.items.find(x => x.id === pmodel.editing.remove); pmodel.items = pmodel.items.filter(x => x.id !== p.id); providerAudit('删除', p, '', '成功'); closeDialog(); render(); toast('配置已删除（原型模拟）'); }
};
document.addEventListener('change', e => {
  if (e.target.id === 'cloudConfirm') $('[data-action="provider-activate-confirm"]').disabled = !e.target.checked;
  if (e.target.id === 'providerType') {
    const type = e.target.value, local = type === 'qwen3_mlx', needsKey = type === 'bailian_realtime' || type === 'openai_compatible';
    $('#providerUrlField').hidden = local; $('#providerKeyField').hidden = !needsKey;
    const mr = $('#providerMaxRecord'); if (mr) { mr.max = local ? 180 : 360; if (Number(mr.value) > Number(mr.max)) mr.value = mr.max; }
  }
});
