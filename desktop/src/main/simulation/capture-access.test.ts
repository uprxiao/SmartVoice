import { describe, expect, it, vi } from 'vitest';
vi.mock('electron', () => { throw new Error('Simulation must not query hardware'); });
import { microphoneInfo, openMicrophoneSettings, prepareCaptureAccess } from './capture-access';

describe('simulation capture access', () => {
  it('reports not-required and does not request or query system audio access', async () => {
    expect(microphoneInfo()).toEqual({ status: 'not-required', error: false });
    expect(prepareCaptureAccess(() => true)).toBeUndefined();
    await expect(openMicrophoneSettings()).rejects.toThrow('模拟版无需麦克风');
  });
});
