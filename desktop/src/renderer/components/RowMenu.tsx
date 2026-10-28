import { Fragment, type ReactNode } from 'react';
import { Menu } from '@base-ui/react/menu';
import { Ellipsis } from 'lucide-react';

export function RowMenu({ label, items }: {
  label: string;
  items: { label: string; icon?: ReactNode; danger?: boolean; separatorBefore?: boolean; disabled?: boolean; run: () => void | Promise<void> }[];
}) {
  return (
    <Menu.Root>
      <Menu.Trigger className="row-icon-button" aria-label={label} title={label}>
        <Ellipsis size={16} />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={5} align="end" className="z-50">
          <Menu.Popup className="row-menu-popup">
            {items.map((item) => (
              <Fragment key={item.label}>
              {item.separatorBefore && <Menu.Separator className="my-1 h-px bg-[var(--border-thin)]" />}
              <Menu.Item disabled={item.disabled} className={`row-menu-item ${item.danger ? 'text-[var(--danger)]' : ''}`}
                onClick={() => { void item.run(); }}>
                {item.icon}{item.label}
              </Menu.Item>
              </Fragment>
            ))}
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
