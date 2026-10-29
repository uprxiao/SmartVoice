import type { Rectangle } from 'electron';

/** UIA/Win32 return physical pixels. Electron display matching and window placement use DIP. */
export function inputGeometryToDip(value: Record<string, unknown>, rect: Rectangle | null,
  platform: NodeJS.Platform, convert: (rect: Rectangle) => Rectangle): Record<string, unknown> {
  if (platform !== 'win32' || value.available !== true) return value;
  if (!rect || !['physical', 'dip'].includes(String(value.coordinateSpace))) return { available: false };
  return value.coordinateSpace === 'physical'
    ? { ...value, bounds: convert(rect), coordinateSpace: 'dip' } : value;
}
