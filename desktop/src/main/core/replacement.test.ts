import { describe, expect, it } from 'vitest';
import { singleReplacement } from './replacement';

describe('singleReplacement', () => {
  it('keeps a Chinese correction as the whole word rather than its changed suffix', () => {
    expect(singleReplacement('帮我加一个按钮，不要修改数据库。', '帮我加一个按键，不要修改数据库。'))
      .toEqual({ offset: 5, before: '按钮', after: '按键', left: '帮我加一个', right: '，不要修改数据库。' });
    expect(singleReplacement('😀请按按钮。', '😀请按按键。'))
      .toEqual({ offset: 3, before: '按钮', after: '按键', left: '😀请按', right: '。' });
  });

  it('extracts one changed word with code-point offsets', () => {
    expect(singleReplacement('加一个按钮，不要修改数据库。', '加一个按纽，不要修改数据库。')).toEqual({
      offset: 3,
      before: '按钮',
      after: '按纽',
      left: '加一个',
      right: '，不要修改数据库。',
    });
  });

  it('counts astral characters as one code point', () => {
    const result = singleReplacement('😀按钮', '😀按纽');
    expect(result?.offset).toBe(1);
    expect(result?.before).toBe('按钮');
    expect(result?.after).toBe('按纽');
  });

  it('rejects unchanged and empty rewrites', () => {
    expect(singleReplacement('没有变化', '没有变化')).toBeNull();
    expect(singleReplacement('', '新增')).toBeNull();
    expect(singleReplacement('新增', '')).toBeNull();
  });

  it('rejects punctuation, newlines and long replacements', () => {
    expect(singleReplacement('第一句。', '第一句！')).toBeNull();
    expect(singleReplacement('上一行下\n文', '上一行新文')).toBeNull();
    expect(singleReplacement('a'.repeat(33), 'b'.repeat(33))).toBeNull();
  });

  it('rejects two separated edits collapsed into one span', () => {
    expect(singleReplacement('用户修改了按钮和标题', '用户修改了开关和文案')).toBeNull();
  });

  it('keeps the final single contiguous edit', () => {
    expect(singleReplacement('用户修改了按钮', '用户修改了开关')).toMatchObject({
      before: '按钮',
      after: '开关',
    });
  });

  it('bounds the stored left and right context', () => {
    const left = '前'.repeat(120);
    const right = '后'.repeat(120);
    const result = singleReplacement(`${left}词${right}`, `${left}辞${right}`);
    expect(result?.left).toHaveLength(80);
    expect(result?.right).toHaveLength(80);
    expect(result?.left.endsWith('前')).toBe(true);
    expect(result?.right.startsWith('后')).toBe(true);
  });
});

describe('singleReplacement learning guards', () => {
  it('keeps complete SQL lite -> SQLite evidence instead of a pure deletion', () => {
    expect(singleReplacement('这个项目使用 SQL lite 存储配置', '这个项目使用 SQLite 存储配置')).toEqual({
      offset: 7, before: 'SQL lite', after: 'SQLite', left: '这个项目使用 ', right: ' 存储配置',
    });
  });

  it('keeps a lexical Latin correction but rejects edits across separate words', () => {
    expect(singleReplacement('使用 cloud 模型', '使用 Claude 模型')?.before).toBe('cloud');
    expect(singleReplacement('使用 cloud 模型', '使用 Claude 模型')?.after).toBe('Claude');
    expect(singleReplacement('使用 cat 和 dog', '使用 bat 和 dig')).toBeNull();
    expect(singleReplacement('按钮和按钮', '按纽和按纽')).toBeNull();
  });

  it('uses 80 code points of context without splitting an emoji', () => {
    const left = '😀'.repeat(81), right = '🚀'.repeat(81);
    const result = singleReplacement(`${left}钮${right}`, `${left}纽${right}`);
    expect(result?.offset).toBe(81);
    expect([...result!.left]).toHaveLength(80);
    expect([...result!.right]).toHaveLength(80);
    expect(result?.left).toBe('😀'.repeat(80));
    expect(result?.right).toBe('🚀'.repeat(80));
  });

  it('allows 32 code points and excludes 33 regardless of UTF-16 width', () => {
    expect(singleReplacement(`前${'😀'.repeat(32)}后`, `前${'😃'.repeat(32)}后`)?.before).toBe('😀'.repeat(32));
    expect(singleReplacement(`前${'😀'.repeat(33)}后`, `前${'😃'.repeat(33)}后`)).toBeNull();
  });

  it.each([
    ['重试 3 次', '重试 5 次'],
    ['重试三次', '重试五次'],
    ['不要删除文件', '删除文件'],
    ['明天处理', '今天处理'],
    ['读取 /tmp/a.log 文件', '读取 /tmp/b.log 文件'],
    ['读取 C:\\tmp\\a.log 文件', '读取 C:\\tmp\\b.log 文件'],
    ['使用 foo 工具', '使用 foobar 工具'],
    ['使用 foo bar 工具', '使用 foo 工具'],
    ['普通输入', '普通输入新词'],
    ['普通输入', '普通'],
    ['前钮后', '前纽\u0001后'],
  ])('excludes non-term edit %s -> %s', (before, after) => {
    expect(singleReplacement(before, after)).toBeNull();
  });

  it('allows a minimal isolated homophone for server suggestion, not automatic learning', () => {
    expect(singleReplacement('云', '芸')).toEqual({ offset: 0, before: '云', after: '芸', left: '', right: '' });
  });
});
