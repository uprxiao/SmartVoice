const fixedText = '这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。';
const element = id => document.getElementById(id);
let phase = 'idle';
let operation = 0;
let completion = null;

function render(message) {
  element('hud').hidden = !['recording', 'processing'].includes(phase);
  element('emptyHud').hidden = !element('hud').hidden;
  element('hud').dataset.phase = phase === 'processing' ? 'processing' : 'recording';
  for (const id of ['cancel', 'signal', 'finish']) element(id).hidden = phase !== 'recording';
  element('hudProcessing').hidden = phase !== 'processing';
  element('cancel').disabled = phase !== 'recording';
  element('finish').disabled = phase !== 'recording';
  element('toggle').disabled = phase === 'processing';
  element('toggle').textContent = phase === 'recording' ? '结束模拟输入' : '开始模拟输入';
  element('status').textContent = message;
}

function cancel() {
  if (!['recording', 'processing'].includes(phase)) return;
  operation++;
  clearTimeout(completion);
  phase = 'cancelled';
  element('result').hidden = true;
  render('已取消，页面内文字未插入。');
}

function toggle() {
  if (phase === 'processing') return;
  if (phase !== 'recording') {
    operation++;
    phase = 'recording';
    element('result').hidden = true;
    render('模拟输入中，再次按快捷键结束；未访问麦克风。');
    return;
  }
  phase = 'processing';
  const current = operation;
  render('正在产生固定文字…');
  completion = setTimeout(() => {
    if (current !== operation) return;
    phase = element('outcome').value;
    element('result').hidden = false;
    element('result').textContent = fixedText;
    if (phase === 'inserted') {
      const target = element('target');
      target.setRangeText(fixedText, target.selectionStart, target.selectionEnd, 'end');
    }
    render(phase === 'inserted' ? '页面内已插入一次。实际外部输入仍需原生核验。'
      : phase === 'unverified' ? '无法核验，保留结果；不会自动重试。' : '目标拒绝，结果已保留。');
  }, 400);
}

element('toggle').addEventListener('click', toggle);
element('finish').addEventListener('click', toggle);
element('cancel').addEventListener('click', cancel);
element('reset').addEventListener('click', () => {
  cancel();
  phase = 'idle';
  element('target').value = 'BEGIN React END';
  element('result').hidden = true;
  render('未开始，本页未请求麦克风。');
});
document.addEventListener('keydown', event => {
  if (event.code === 'Space' && event.ctrlKey && event.shiftKey && !event.repeat) {
    event.preventDefault();
    toggle();
  } else if (event.code === 'Escape') cancel();
});
