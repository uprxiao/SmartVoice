import { describe, expect, it } from 'vitest';
import { errorMessage } from './error-message';

describe('errorMessage', () => {
  it('unwraps Electron IPC and Error prefixes', () => {
    expect(errorMessage(new Error("Error invoking remote method 'history.list': TypeError: fetch failed")))
      .toBe('无法连接服务，请检查网络后重试');
    expect(errorMessage(new Error('Error: 语音服务尚未就绪'))).toBe('语音服务尚未就绪');
  });

  it('keeps actionable server messages unchanged', () => {
    expect(errorMessage(new Error('该邮箱已注册'))).toBe('该邮箱已注册');
  });
});
