import type { Display, Rectangle } from 'electron';

interface ScreenGeometry {
  getCursorScreenPoint(): { x: number; y: number };
  getDisplayNearestPoint(point: { x: number; y: number }): Display;
  getDisplayMatching(bounds: Rectangle): Display;
}
export function inputRectangle(value: unknown): Rectangle | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as { available?: unknown; bounds?: unknown };
  if (record.available !== true || !record.bounds || typeof record.bounds !== 'object') return null;
  const r = record.bounds as Record<string, unknown>;
  if (!['x', 'y', 'width', 'height'].every(key => typeof r[key] === 'number' && Number.isFinite(r[key]))) return null;
  const { x, y, width, height } = r as unknown as Rectangle;
  if (width < 0 || height <= 0) return null;
  return { x: Math.floor(x), y: Math.floor(y), width: Math.max(1, Math.ceil(width)), height: Math.max(1, Math.ceil(height)) };
}
export function hudBounds(area: Rectangle): Rectangle {
  const width = Math.min(300, Math.max(1, area.width));
  const height = Math.min(104, Math.max(1, area.height));
  const bottom = Math.min(72, Math.max(0, area.height - height));
  return { x: Math.round(area.x + (area.width - width) / 2),
    y: Math.round(area.y + area.height - height - bottom), width, height };
}

/** Follows input geometry while visible; never focuses a window or restores a target. */
export class HudPosition {
  private timer: ReturnType<typeof setInterval> | null = null;
  private fallback: ReturnType<typeof setTimeout> | null = null;
  private active = false;
  private visible = false;
  private generation = 0;
  private reading: object | null = null;
  private displayKey: string | null = null;
  constructor(private readonly screen: ScreenGeometry,
    private readonly read: () => Promise<unknown>,
    private readonly move: (bounds: Rectangle) => void,
    private readonly show: () => void) {}

  start(): void {
    if (this.active) return;
    this.active = true;
    this.visible = false;
    this.displayKey = null;
    const generation = ++this.generation;
    this.fallback = setTimeout(() => {
      this.fallback = null;
      if (this.active && generation === this.generation && !this.visible) this.place(null);
    }, 150);
    void this.refresh(generation);
    this.timer = setInterval(() => { void this.refresh(generation); }, 250);
  }

  private place(rect: Rectangle | null): void {
    const display = rect ? this.screen.getDisplayMatching(rect)
      : this.screen.getDisplayNearestPoint(this.screen.getCursorScreenPoint());
    const bounds = hudBounds(display.workArea);
    const key = `${display.id}:${bounds.x}:${bounds.y}:${bounds.width}:${bounds.height}`;
    if (key !== this.displayKey) { this.move(bounds); this.displayKey = key; }
    if (!this.visible) { this.visible = true; this.show(); }
    if (this.fallback) { clearTimeout(this.fallback); this.fallback = null; }
  }

  private async refresh(generation: number): Promise<void> {
    if (!this.active || generation !== this.generation || this.reading) return;
    const reading = {};
    this.reading = reading;
    try {
      const value = await this.read();
      if (this.active && generation === this.generation) this.place(inputRectangle(value));
    } catch {
      if (this.active && generation === this.generation) this.place(null);
    } finally {
      if (this.reading === reading) this.reading = null;
    }
  }

  stop(): void {
    this.active = false;
    this.visible = false;
    this.generation++;
    if (this.timer) clearInterval(this.timer);
    if (this.fallback) clearTimeout(this.fallback);
    this.timer = this.fallback = null;
    this.reading = null;
    this.displayKey = null;
  }
}
