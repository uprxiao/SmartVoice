// Native reasons stay local. The existing HTTP receipt enum is unchanged.
const EN: Record<string, string> = {
  '不支持的输入动作': 'This input action is unavailable.',
  '输入请求无效': 'Invalid input request.',
  '写入前剪贴板或按键已变化，本次没有输入': 'The clipboard or keys changed. No text was inserted.',
  '写入前目标已变化，本次没有输入': 'The target changed before input. No text was inserted.',
  '剪贴板已变化或不可用，本次没有输入': 'The clipboard changed or is unavailable. No text was inserted.',
  '剪贴板恢复失败，请检查剪贴板；本次不会再次输入': 'Could not restore the clipboard. Check it manually; input will not be repeated.',
  '剪贴板格式无法安全保留，请复制结果后手动粘贴': 'The clipboard format cannot be preserved safely. Copy the result and paste manually.',
  '当前窗口不可确认': 'The current window could not be confirmed.',
  '当前窗口无法安全输入，结果已保留': 'This window cannot receive input safely. Your result is saved.',
  '当前输入框受保护、只读或已禁用，结果已保留': 'This field is protected, read-only or disabled. Your result is saved.',
  '快捷键状态尚未确认，请松开按键后重试': 'The key state could not be confirmed. Release the shortcut keys and try again.',
  '指定的输入位置已变化，本次没有输入': 'The specified target changed. No text was inserted.',
  '指定的输入位置已变化，请重新指定或复制结果': 'The specified target changed. Select a target again or copy the result.',
  '无法安全保存剪贴板，请复制结果后手动粘贴': 'The clipboard cannot be saved safely. Copy the result and paste manually.',
  '无法确认是否已输入，请先检查目标；不会自动重试': 'Input could not be verified. Check the target first; there is no automatic retry.',
  '本次输入已登记或执行容量已满，请重新开始': 'This input is already registered or the execution limit was reached. Start a new dictation.',
  '目标窗口以管理员运行，无法自动输入。请复制结果后手动粘贴。': 'Administrator window. Copy the result and paste manually.',
  '请先切换到需要输入的应用': 'Switch to the app where you want to insert text.',
  '请松开快捷键后重试': 'Release the shortcut keys and try again.',
  '原生输入组件不可用，请复制结果后手动粘贴': 'Input is unavailable. Copy the result and paste manually.',
};
export function windowsInputMessage(message: string, locale: string = 'zh-CN'): string {
  return locale === 'en' ? EN[message] ?? message : message;
}
