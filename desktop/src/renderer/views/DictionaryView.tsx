import { FormEvent, useEffect, useState } from 'react';
import { observer } from 'mobx-react-lite';
import { Loader2, Pencil, Search, SquareCheck, SquareMinus, Trash2, Upload, X } from 'lucide-react';
import type { Term } from '@shared/types';
import { stores } from '../stores';
import { errorMessage } from '../lib/error-message';
import { tr } from '../lib/i18n';
import { RowMenu } from '../components/RowMenu';
import { TermSourceIcon } from '../components/TermSourceIcon';

const SOURCE_LABEL: Record<'all' | 'manual' | 'auto', string> = {
  all: '所有',
  manual: '手动添加',
  auto: '自动添加',
};

export const DictionaryView = observer(function DictionaryView() {
  const { dictionary, ui, auth } = stores;
  const locale = auth.settings?.locale ?? 'zh-CN';
  const [search, setSearch] = useState(dictionary.query);
  const [searchOpen, setSearchOpen] = useState(Boolean(dictionary.query));
  const [editing, setEditing] = useState<Term | null>(null);
  const [creating, setCreating] = useState(false);
  const [termValue, setTermValue] = useState('');
  const [aliasValue, setAliasValue] = useState('');

  useEffect(() => {
    if (dictionary.selected.size === 0 || creating) return;
    const cancel = (event: KeyboardEvent) => {
      if (event.key === 'Escape') dictionary.clearSelection();
    };
    window.addEventListener('keydown', cancel);
    return () => window.removeEventListener('keydown', cancel);
  }, [dictionary, dictionary.selected.size, creating]);

  const removeSelected = async () => {
    const userId = auth.user?.id, count = dictionary.selected.size;
    if (!userId || !count || dictionary.busy || dictionary.loading) return;
    if (!window.confirm(`确认删除所选 ${count} 个词？历史记录和累计学习次数不受影响。`)) return;
    if (auth.user?.id !== userId) return;
    try {
      if (await dictionary.removeSelected() && auth.user?.id === userId) ui.toast(`已删除 ${count} 个词`);
    } catch (error) {
      if (auth.user?.id === userId) ui.toast(errorMessage(error) || '删除失败，选择已保留', 'error');
    }
  };

  const openCreate = () => {
    setEditing(null);
    setTermValue('');
    setAliasValue('');
    setCreating(true);
  };

  const openEdit = (term: Term) => {
    setEditing(term);
    setTermValue(term.term);
    setAliasValue(term.aliases.join('，'));
    setCreating(true);
  };

  const close = () => {
    setCreating(false);
    setEditing(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const aliases = aliasValue
      .split(/[，,]/)
      .map((value) => value.trim())
      .filter(Boolean);
    try {
      if (editing) {
        await dictionary.update(editing, { term: termValue.trim(), aliases });
        ui.toast('词条已更新');
      } else {
        await dictionary.create(termValue.trim(), aliases);
        ui.toast('词条已添加');
      }
      close();
    } catch (error) {
      ui.toast(errorMessage(error) || '保存失败', 'error');
    }
  };

  const remove = async (term: Term) => {
    if (!window.confirm(`删除词条「${term.term}」？历史记录不受影响。`)) return;
    try {
      await dictionary.remove(term);
      ui.toast('词条已删除');
    } catch (error) {
      ui.toast(errorMessage(error) || '删除失败', 'error');
    }
  };

  const importCsv = async () => {
    try {
      const summary = await dictionary.importCsv();
      if (summary) ui.toast(`导入完成：${summary}`);
    } catch (error) {
      ui.toast(errorMessage(error) || '导入失败', 'error');
    }
  };

  return (
    <div className="dictionary-view mx-auto w-full max-w-[822px] pb-8">
      <div className="flex h-10 items-center justify-between">
        <h1 className="page-title">词典</h1>
        <div className="flex items-center gap-2">
          <button className="btn-secondary" onClick={() => void importCsv()} disabled={dictionary.busy}>
            <Upload size={14} /> 导入
          </button>
          <button className="btn-primary" onClick={openCreate} disabled={dictionary.busy}>
            新词
          </button>
        </div>
      </div>

      {dictionary.selected.size > 0 ? <div className="mt-4 flex h-10 items-center gap-2">
        <button className="row-icon-button" role="checkbox" aria-checked={dictionary.allLoadedSelected ? true : 'mixed'}
          aria-label="全选或取消已加载词条" title={`仅选择当前筛选下已加载的 ${dictionary.items.length} 个词条`}
          disabled={dictionary.busy || dictionary.loading} onClick={() => dictionary.toggleAllLoaded()}>
          {dictionary.allLoadedSelected ? <SquareCheck size={16} /> : <SquareMinus size={16} />}
        </button>
        <span className="text-[14px] leading-4">已选择 {dictionary.selected.size} 个词</span>
        <button className="btn-ghost rounded-full border border-[var(--border)] text-[var(--danger)]"
          disabled={dictionary.busy || dictionary.loading} onClick={() => void removeSelected()}>
          {dictionary.busy ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />} 删除
        </button>
        <button className="btn-ghost" disabled={dictionary.busy} onClick={() => dictionary.clearSelection()}>取消选择</button>
      </div> : <div className="mt-4 flex items-center justify-between gap-4">
        <div className="segmented dictionary-source-filter">
          {(['all', 'auto', 'manual'] as const).map((value) => (
            <button
              key={value}
              disabled={dictionary.loading || dictionary.busy}
              aria-pressed={dictionary.source === value}
              className={`segmented-item ${dictionary.source === value ? 'active' : ''}`}
              onClick={() => {
                dictionary.setSource(value);
                void dictionary.load(true);
              }}
            >
              {value !== 'all' && <TermSourceIcon source={value} size={14} decorative muted={value === 'auto' && dictionary.source !== 'auto'} />}
              {SOURCE_LABEL[value]}
            </button>
          ))}
        </div>
        <div className="flex min-w-0 items-center gap-2">
          {(searchOpen || dictionary.query) && <div className="relative w-[min(240px,25vw)]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-3)]" />
          <input
            className="input h-[36px] pl-9"
            placeholder="搜索词条或别名"
            aria-label="搜索词条或别名"
            autoFocus
            disabled={dictionary.busy}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape' && !dictionary.query) setSearchOpen(false);
              if (event.key === 'Enter' && !dictionary.loading && !dictionary.busy) {
                dictionary.setQuery(search);
                void dictionary.load(true);
              }
            }}
          />
          </div>}
          <button className="row-icon-button h-8 w-8 rounded-full bg-[var(--surface)]" aria-label="搜索词条" title="搜索词条"
            aria-expanded={searchOpen || Boolean(dictionary.query)} onClick={() => setSearchOpen(!searchOpen)}><Search size={16} /></button>
        </div>
      </div>}

      {dictionary.loading && (
        <div role="status" className="mt-3 flex items-center gap-1.5 text-[13px] text-[var(--text-3)]">
          <Loader2 size={13} className="animate-spin" /> {tr(locale, 'list.loadingDictionary')}
        </div>
      )}

      {dictionary.error && (
        <div role="alert" className="mt-3 flex items-center justify-between gap-3 rounded-lg bg-[rgba(217,45,32,0.08)] px-3 py-2 text-[13px] text-[var(--danger)]">
          <span>{dictionary.error}</span>
          <button
            className="btn-ghost shrink-0 text-[var(--danger)]"
            onClick={() => void dictionary.load(true)}
            disabled={dictionary.loading}
          >
            {tr(locale, 'list.retry')}
          </button>
        </div>
      )}

      <div className="dictionary-grid mt-4 grid grid-cols-3 gap-x-1.5 gap-y-3">
        {dictionary.items.map((term) => (
          <div
            key={term.id}
            className={`dictionary-term group relative flex min-h-9 min-w-0 items-center gap-1 rounded-[10px] border p-2 hover:bg-[var(--fill)] focus-within:bg-[var(--fill)] ${dictionary.selected.has(term.id) ? 'border-[var(--text-2)] bg-[var(--fill)]' : 'border-[var(--border-thin)]'}`}
            title={[
              term.aliases.length > 0 ? `别名：${term.aliases.join('、')}` : '',
              term.source === 'auto' ? '自动学习' : '手动添加',
              `v${term.version}`,
            ]
              .filter(Boolean)
              .join(' · ')}
          >
            <TermSourceIcon source={term.source} size={12} />
            <span className={`min-w-0 flex-1 truncate py-0.5 text-[14px] leading-4 text-[var(--text)] ${dictionary.selected.size ? 'pr-8' : 'group-hover:pr-16 group-focus-within:pr-16'}`}>{term.term}</span>
            <div className={`row-hover-actions absolute right-px top-1/2 -translate-y-1/2 ${dictionary.selected.size ? 'opacity-100' : ''}`}>
              {dictionary.selected.size === 0 && <RowMenu label={`更多词条操作：${term.term}`} items={[
                { label: '编辑', icon: <Pencil size={14} />, run: () => openEdit(term) },
                { label: '删除', icon: <Trash2 size={14} />, danger: true, separatorBefore: true, run: () => remove(term) },
              ]} />}
              <label className="row-icon-button">
                <input type="checkbox" className="h-3.5 w-3.5 accent-[var(--text-2)]" aria-label={`选择词条 ${term.term}`}
                  checked={dictionary.selected.has(term.id)} disabled={dictionary.busy || dictionary.loading}
                  onChange={() => dictionary.toggleSelection(term)} />
              </label>
            </div>
          </div>
        ))}
      </div>

      {dictionary.loaded && !dictionary.loading && !dictionary.error && dictionary.items.length === 0 && (
        <div className="py-20 text-center">
          <div className="text-[14px] font-medium text-[var(--text-2)]">还没有词条</div>
          <div className="mt-1 text-[13px] text-[var(--text-3)]">
            添加产品名、项目名或专业术语，识别时会优先匹配。
          </div>
        </div>
      )}

      <div className="mt-4 flex items-center justify-between text-[12px] text-[var(--text-3)]">
        <span>
          {dictionary.loaded && !dictionary.loading && !dictionary.error
            ? tr(locale, 'list.dictionaryCount', { count: dictionary.items.length, version: dictionary.version })
            : tr(locale, 'list.dictionaryCountUnknown')}
        </span>
        {dictionary.hasMore && (
          <button
            className="btn-secondary"
            onClick={() => void dictionary.load(false)}
            disabled={dictionary.loading}
          >
            {dictionary.loading && <Loader2 size={13} className="animate-spin" />} 加载更多
          </button>
        )}
      </div>

      {creating && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-[rgba(17,17,17,0.35)]">
          <div className="w-[420px] rounded-xl bg-[var(--bg)] p-5 shadow-[var(--shadow-window)]">
            <div className="flex items-center justify-between">
              <div className="text-[16px] font-medium">{editing ? '编辑词条' : '添加词条'}</div>
              <button className="btn-ghost h-7 w-7 px-0" onClick={close}>
                <X size={14} />
              </button>
            </div>
            <form className="mt-4 flex flex-col gap-3" onSubmit={submit}>
              <label className="flex flex-col gap-1.5">
                <span className="text-[13px] text-[var(--text-2)]">词条</span>
                <input
                  className="input"
                  value={termValue}
                  onChange={(event) => setTermValue(event.target.value)}
                  placeholder="例如：Kubernetes"
                  maxLength={64}
                  required
                />
              </label>
              <label className="flex flex-col gap-1.5">
                <span className="text-[13px] text-[var(--text-2)]">别名（用逗号分隔，可选）</span>
                <input
                  className="input"
                  value={aliasValue}
                  onChange={(event) => setAliasValue(event.target.value)}
                  placeholder="例如：k8s，kube"
                />
              </label>
              {editing && (
                <div className="text-[12px] text-[var(--text-3)]">
                  当前版本 v{editing.version}；保存后版本会递增。
                </div>
              )}
              <div className="mt-1 flex justify-end gap-2">
                <button type="button" className="btn-secondary" onClick={close}>
                  取消
                </button>
                <button className="btn-primary" type="submit" disabled={dictionary.busy || !termValue.trim()}>
                  {dictionary.busy && <Loader2 size={13} className="animate-spin" />} 保存
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
});
