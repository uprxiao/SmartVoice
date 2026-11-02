import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { join } from 'node:path';

const f = vi.hoisted(() => ({
  product: { flavor: 'normal', appId: 'com.smartvoiceinput.desktop', productName: 'SmartVoiceInput', profileDirectoryName: 'SmartVoiceInput' },
  app: { setName: vi.fn(), setPath: vi.fn(), getPath: vi.fn(), setAppUserModelId: vi.fn() },
}));
vi.mock('electron', () => ({ app: f.app, shell: {}, systemPreferences: {} }));
vi.mock('@product/identity', () => ({ product: f.product }));
import { configureProductProfile } from './profile';

beforeEach(() => { vi.clearAllMocks(); f.app.getPath.mockReturnValue('/fictional/app-data'); });
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe('product profile isolation', () => {
  it('preserves the normal profile override', () => {
    Object.assign(f.product, { flavor: 'normal', productName: 'SmartVoiceInput', profileDirectoryName: 'SmartVoiceInput' });
    vi.stubEnv('SVI_USER_DATA', '/fictional/normal-override');
    configureProductProfile();
    expect(f.app.setName).toHaveBeenCalledWith('SmartVoiceInput');
    expect(f.app.setPath).toHaveBeenCalledExactlyOnceWith('userData', '/fictional/normal-override');
  });

  it('ignores a production-profile override for both simulation userData and sessionData', () => {
    Object.assign(f.product, { flavor: 'windows-simulation', productName: 'SmartVoiceInput Simulation', profileDirectoryName: 'SmartVoiceInput Simulation' });
    vi.stubEnv('SVI_USER_DATA', '/fictional/normal-profile');
    configureProductProfile();
    expect(f.app.setName).toHaveBeenCalledWith('SmartVoiceInput Simulation');
    expect(f.app.setPath.mock.calls).toEqual([
      ['userData', join('/fictional/app-data', 'SmartVoiceInput Simulation')],
      ['sessionData', join('/fictional/app-data', 'SmartVoiceInput Simulation')],
    ]);
  });
});
