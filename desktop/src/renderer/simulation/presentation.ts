export const presentation = Object.freeze({
  banner: 'Windows 模拟测试版 · 无需麦克风 · 本机模拟服务 · 固定文本：这是一段 Windows 模拟输入测试。React、Docker 和 TypeScript。',
  hudLabel: '模拟',
  authDescription: '本版仅用本机虚构账号测试桌面输入。快捷键开始模拟，再次按下后插入固定测试文字；不录音，不调用模型。',
  accountHint: '公开测试账号 tester@example.test，密码 Simulation123!。也可注册 @example.test 或 .invalid 虚构邮箱。账号与盐化密码摘要仅存本机模拟目录，客户端加密保存登录令牌。',
  accountPlaceholder: 'tester@example.test',
  serviceLabel: '本机模拟服务 · 固定测试结果',
  serviceAddress: '仅当前应用拥有的本机模拟服务',
  serviceHint: '模拟服务状态只说明本机测试设施可用，不代表真实 ASR、LLM 或生产服务通过验收。',
  microphoneHint: '本版使用合成输入，无需麦克风，也不查询系统麦克风权限。',
});
