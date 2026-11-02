import { presentation } from '@product/presentation';

export function ProductBanner() {
  if (!presentation.banner) return null;
  return <div role="note" className="mx-6 mb-3 shrink-0 rounded-lg border border-[var(--border)] bg-[var(--bg-sidebar)] px-3 py-2 text-[11px] leading-5 text-[var(--text-2)]">
    {presentation.banner}
  </div>;
}
