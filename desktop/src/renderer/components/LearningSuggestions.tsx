import { useEffect, useRef, useState } from 'react';
import { observer } from 'mobx-react-lite';
import { RotateCcw, X } from 'lucide-react';
import { learningLearnedEvent } from '@shared/events';
import type { LearningSuggestion } from '@shared/types';
import { errorMessage } from '../lib/error-message';
import { events, rpc } from '../lib/ipc';
import { stores } from '../stores';

export const LearningSuggestions = observer(function LearningSuggestions() {
  const { auth, dictionary, stats, ui } = stores;
  const [suggestions, setSuggestions] = useState<LearningSuggestion[]>([]);
  const [undoPending, setUndoPending] = useState<Set<string>>(() => new Set());
  const pendingUndoIds = useRef(new Set<string>());
  const lifetime = useRef(0);

  useEffect(() => {
    lifetime.current += 1;
    const generation = lifetime.current;
    const userId = auth.user?.id;
    const current = () => Boolean(userId) && lifetime.current === generation && auth.user?.id === userId;
    setSuggestions([]);
    setUndoPending(new Set());
    pendingUndoIds.current.clear();
    const offLearned = events.on(learningLearnedEvent, (suggestion) => {
      if (!current() || suggestion.status !== 'learned' || suggestion.undone) return;
      setSuggestions((previous) => {
        const remaining = previous.filter((item) => item.event_id !== suggestion.event_id);
        return suggestion.undo_available && !suggestion.undone ? [...remaining, suggestion].slice(-3) : remaining;
      });
      ui.toast(`已自动学习「${suggestion.after}」`);
      void dictionary.load(true);
      void stats.load(196);
    });
    return () => {
      lifetime.current += 1;
      offLearned();
    };
  }, [auth.user?.id, dictionary, stats, ui]);

  const undo = async (suggestion: LearningSuggestion) => {
    if (!suggestion.undo_available || suggestion.undone || pendingUndoIds.current.has(suggestion.event_id)) return;
    const generation = lifetime.current;
    const userId = auth.user?.id;
    const current = () => Boolean(userId) && lifetime.current === generation && auth.user?.id === userId;
    if (!current()) return;
    pendingUndoIds.current.add(suggestion.event_id);
    setUndoPending((previous) => new Set(previous).add(suggestion.event_id));
    try {
      const result = await rpc.learning.undo(suggestion.event_id);
      if (!current()) return;
      if (result.event_id !== suggestion.event_id || result.undone !== true) throw new Error('服务端尚未确认撤销');
      setSuggestions((previous) => previous.filter((item) => item.event_id !== suggestion.event_id));
      ui.toast(`已撤销学习「${suggestion.after}」`);
      void dictionary.load(true);
      void stats.load(196);
    } catch (error) {
      if (!current()) return;
      ui.toast(`${errorMessage(error) || '撤销学习失败'}；请在词库中手动处理`, 'error');
    } finally {
      if (current()) {
        pendingUndoIds.current.delete(suggestion.event_id);
        setUndoPending((previous) => new Set([...previous].filter((id) => id !== suggestion.event_id)));
      }
    }
  };

  const dismiss = (suggestion: LearningSuggestion) => {
    setSuggestions((previous) => previous.filter((item) => item.event_id !== suggestion.event_id));
  };

  if (suggestions.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-40 flex w-[320px] flex-col gap-2">
      {suggestions.map((suggestion) => (
        <div
          key={suggestion.event_id}
          className="rounded-xl bg-[var(--dialog-bg)] p-3 shadow-[0_16px_40px_rgba(0,0,0,0.28)]"
        >
          <div className="text-[12px] text-[var(--text-3)]">已自动学习</div>
          <div className="mt-1 text-[14px] leading-5">
            <span className="text-[var(--text-3)] line-through">{suggestion.before}</span>
            <span className="mx-1.5 text-[var(--text-3)]">→</span>
            <span className="font-medium">{suggestion.after}</span>
          </div>
          <div className="mt-2 text-[12px] text-[var(--text-3)]">
            {`已将「${suggestion.after}」加入词库。撤销仅影响词库，不修改已输入的文字。`}
          </div>
          <div className="mt-2 flex justify-end gap-2">
            <button className="btn-ghost h-7" disabled={undoPending.has(suggestion.event_id)} onClick={() => dismiss(suggestion)}>
              <X size={12} /> 关闭
            </button>
            <button className="btn-primary h-7" disabled={undoPending.has(suggestion.event_id)}
                aria-busy={undoPending.has(suggestion.event_id)} onClick={() => void undo(suggestion)}>
                <RotateCcw size={12} /> 撤销学习
            </button>
          </div>
        </div>
      ))}
    </div>
  );
});
