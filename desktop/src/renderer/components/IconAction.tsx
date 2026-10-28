import type { ReactNode } from 'react';
import { Tooltip } from '@base-ui/react/tooltip';

export function IconAction({ label, children, onClick }: {
  label: string; children: ReactNode; onClick: () => void;
}) {
  return <Tooltip.Provider>
    <Tooltip.Root>
      <Tooltip.Trigger className="row-icon-button" aria-label={label} onClick={onClick}>
        {children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner side="top" sideOffset={6} className="z-[60]">
          <Tooltip.Popup className="rounded-lg bg-[var(--tooltip-bg)] px-3 py-2 text-[12px] leading-4 text-white shadow-lg">
            {label}
          </Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  </Tooltip.Provider>;
}
