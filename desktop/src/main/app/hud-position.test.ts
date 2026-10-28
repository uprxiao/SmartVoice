import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Display, Rectangle } from 'electron';
import { HudPosition, hudBounds, inputRectangle } from './hud-position';
const displays = [
  { id: 1, scaleFactor: 2, workArea: { x: 0, y: 25, width: 1512, height: 900 } },
  { id: 2, scaleFactor: 1, workArea: { x: -2560, y: 0, width: 2560, height: 1400 } },
  { id: 3, scaleFactor: 1.5, workArea: { x: 0, y: -1440, width: 2560, height: 1415 } },
] as Display[];
let mouse = { x: 100, y: 100 };
const containing = (x: number, y: number) => displays.find(d => x >= d.workArea.x && x < d.workArea.x + d.workArea.width && y >= d.workArea.y && y < d.workArea.y + d.workArea.height) ?? displays[0];
const screen = {
  getCursorScreenPoint: () => mouse,
  getDisplayNearestPoint: (point: { x: number; y: number }) => containing(point.x, point.y),
  getDisplayMatching: (r: Rectangle) => containing(r.x, r.y),
};
const geometry = (x: number, y: number) => ({ available: true, source: 'caret', bounds: { x, y, width: 0, height: 18 } });
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
let controller: HudPosition | undefined;
beforeEach(() => { vi.useFakeTimers(); mouse = { x: 100, y: 100 }; });
afterEach(() => { controller?.stop(); controller = undefined; vi.useRealTimers(); });

describe('input-screen recording capsule', () => {
  it.each([[100, 300, 0], [-1800, 300, 1], [300, -1000, 2]])('chooses input (%s,%s) in a three-screen arrangement, independent of mouse/scale', async (x, y, index) => {
    mouse = { x: -2200, y: 100 }; const move = vi.fn(), show = vi.fn();
    controller = new HudPosition(screen, async () => geometry(x, y), move, show); controller.start(); await settle();
    expect(move).toHaveBeenCalledExactlyOnceWith(hudBounds(displays[index].workArea));
    expect(show).toHaveBeenCalledOnce();
  });
  it('follows a new input screen while remaining non-activating and stops polling when hidden', async () => {
    const read = vi.fn().mockResolvedValue(geometry(100, 300)); const move = vi.fn(), show = vi.fn();
    controller = new HudPosition(screen, read, move, show); controller.start(); await settle();
    read.mockResolvedValue(geometry(-1800, 300)); await vi.advanceTimersByTimeAsync(250);
    expect(move).toHaveBeenLastCalledWith(hudBounds(displays[1].workArea)); expect(show).toHaveBeenCalledOnce();
    controller.stop(); const calls = read.mock.calls.length; await vi.advanceTimersByTimeAsync(1000);
    expect(read).toHaveBeenCalledTimes(calls); expect(vi.getTimerCount()).toBe(0);
  });
  it('falls back to mouse screen when AX geometry is unavailable or rejected', async () => {
    mouse = { x: 500, y: -1200 }; const move = vi.fn();
    const read = vi.fn().mockRejectedValueOnce(new Error('AX unavailable')).mockResolvedValue({ available: false });
    controller = new HudPosition(screen, read, move, vi.fn()); controller.start(); await settle();
    expect(move).toHaveBeenCalledExactlyOnceWith(hudBounds(displays[2].workArea));
    await vi.advanceTimersByTimeAsync(250); expect(move).toHaveBeenCalledOnce();
  });
  it('shows a mouse-screen fallback within 150ms if a location read is slow and later corrects to input screen', async () => {
    let resolve!: (value: unknown) => void; const read = vi.fn().mockReturnValue(new Promise(done => { resolve = done; }));
    const move = vi.fn(), show = vi.fn(); controller = new HudPosition(screen, read, move, show); controller.start();
    await vi.advanceTimersByTimeAsync(149); expect(show).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1); expect(move).toHaveBeenLastCalledWith(hudBounds(displays[0].workArea));
    resolve(geometry(-1800, 300)); await settle(); expect(move).toHaveBeenLastCalledWith(hudBounds(displays[1].workArea));
    expect(show).toHaveBeenCalledOnce();
  });
  it('old pending geometry never shows or moves a hidden/new recording HUD', async () => {
    let resolve!: (value: unknown) => void;
    const read = vi.fn().mockReturnValueOnce(new Promise(done => { resolve = done; })).mockResolvedValue(geometry(300, -1000));
    const move = vi.fn(), show = vi.fn(); controller = new HudPosition(screen, read, move, show);
    controller.start(); controller.stop(); controller.start(); await settle();
    expect(move).toHaveBeenCalledExactlyOnceWith(hudBounds(displays[2].workArea));
    resolve(geometry(-1800, 300)); await settle(); expect(move).toHaveBeenCalledOnce();
    controller.stop(); await vi.advanceTimersByTimeAsync(1000); expect(show).toHaveBeenCalledOnce();
  });
  it('does not restart polling on repeated recording/processing progress and does not overlap reads', async () => {
    const read = vi.fn().mockReturnValue(new Promise(() => undefined));
    controller = new HudPosition(screen, read, vi.fn(), vi.fn()); controller.start(); controller.start();
    await vi.advanceTimersByTimeAsync(1000); expect(read).toHaveBeenCalledOnce();
    controller.stop(); expect(vi.getTimerCount()).toBe(0);
  });
  it('repositions for display work-area changes without re-showing', async () => {
    const display = { ...displays[0], workArea: { ...displays[0].workArea } };
    const local = { ...screen, getDisplayMatching: () => display };
    const move = vi.fn(), show = vi.fn(); controller = new HudPosition(local, async () => geometry(100, 300), move, show);
    controller.start(); await settle(); display.workArea = { ...display.workArea, y: 70, height: 855 };
    await vi.advanceTimersByTimeAsync(250); expect(move).toHaveBeenLastCalledWith(hudBounds(display.workArea));
    expect(show).toHaveBeenCalledOnce();
  });
});

describe('screen-coordinate boundaries', () => {
  it('keeps negative global origins and Dock/menu work-area insets in logical points', () => {
    expect(hudBounds(displays[1].workArea)).toEqual({ x: -1430, y: 1224, width: 300, height: 104 });
    expect(hudBounds(displays[2].workArea)).toEqual({ x: 1130, y: -201, width: 300, height: 104 });
    expect(hudBounds({ x: -40, y: -30, width: 100, height: 80 })).toEqual({ x: -40, y: -30, width: 100, height: 80 });
  });
  it('accepts zero-width carets, rounds outward, and rejects invalid geometry', () => {
    expect(inputRectangle(geometry(-100.5, -900.2))).toEqual({ x: -101, y: -901, width: 1, height: 18 });
    for (const value of [null, {}, { available: false }, { available: true, bounds: { x: NaN, y: 0, width: 1, height: 18 } }, { available: true, bounds: { x: 0, y: 0, width: -1, height: 18 } }]) expect(inputRectangle(value)).toBeNull();
  });
});
