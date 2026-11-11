import { describe, expect, it } from 'vitest';
import { errorMessage, shortcutErrorMessage } from './error-message';

describe('errorMessage', () => {
  it('unwraps Electron IPC and Error prefixes', () => {
    expect(errorMessage(new Error("Error invoking remote method 'history.list': TypeError: fetch failed")))
      .toBe('无法连接服务，请检查网络后重试');
    expect(errorMessage(new Error('Error: 语音服务尚未就绪'))).toBe('语音服务尚未就绪');
  });

  it('keeps actionable server messages unchanged', () => {
    expect(errorMessage(new Error('该邮箱已注册'))).toBe('该邮箱已注册');
  });

  it('localizes shortcut policy errors only for English', () => {
    expect(shortcutErrorMessage('快捷键不能为空', 'zh-CN')).toBe('快捷键不能为空');
    expect(shortcutErrorMessage('快捷键不能为空', 'en')).toBe('Shortcut cannot be empty.');
    expect(shortcutErrorMessage('Escape 用于取消操作，不能设为全局快捷键', 'en'))
      .toBe('Escape is used to cancel. Choose another shortcut.');
    expect(shortcutErrorMessage('此快捷键为系统或常用操作保留，请选择其他组合', 'en'))
      .toBe('This shortcut is reserved by the system or a common action. Choose another combination.');
    expect(shortcutErrorMessage('快捷键 Shift+Space 不可用，仍保留 Super+Control+Z', 'en'))
      .toBe('Shortcut Shift+Space is unavailable; keeping Super+Control+Z.');
  });
});
