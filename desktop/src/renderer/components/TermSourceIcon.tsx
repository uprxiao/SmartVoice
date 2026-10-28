import { Feather, Sparkles } from 'lucide-react';

/** Dictionary source glyphs from lucide (ISC): auto-learned vs manually added. */
export function TermSourceIcon({ source, size, decorative = false, muted = false }: {
  source: 'auto' | 'manual';
  size: number;
  decorative?: boolean;
  muted?: boolean;
}) {
  const Icon = source === 'auto' ? Sparkles : Feather;
  return <Icon size={size} strokeWidth={2}
    className={`shrink-0 ${source === 'auto' ? 'text-[var(--term-auto)]' : 'text-[var(--text-disabled)]'} ${muted ? 'opacity-50' : ''}`}
    role={decorative ? undefined : 'img'} aria-hidden={decorative || undefined}
    aria-label={decorative ? undefined : source === 'auto' ? '自动添加' : '手动添加'} />;
}
