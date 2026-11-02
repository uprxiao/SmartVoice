# Windows 临时模拟验收构建

2026-11-02，S2。依据[需求 SIM-01～12](../02-prd/windows-simulation-test.md)及既有桌面契约，比较两份独立结构方案后选用 Electron Main 内置本机 HTTP/WS 模拟服务和编译期合成输入源。用户希望尽量接近正式产品且验证后移除，故保留现有 ApiClient、AuthService、SessionService、journal、NativeVoiceInput、快捷键与 HUD 为唯一桌面业务实现。

## 装配边界

| 层 | 正常构建 | 临时模拟构建 |
|---|---|---|
| 产品身份 | SmartVoiceInput 及既有 appId/profile | SmartVoiceInput Simulation，独立 appId/profile、自启和 NSIS 身份 |
| 输入源 | 现有 PcmRecorder 与真实麦克风准备 | 合成零 PCM 源与明确的无需麦克风准备，不访问音频硬件 |
| 服务 | 现有固定 HTTPS/WSS 与公开 CA | 当前 Main 拥有的 127.0.0.1 随机端口 HTTP/WS 设施，不向远端转发 |
| 会话与插入 | 正式实现 | 同一正式实现，端点与输入源在构建边界替换 |
| 存储 | 正式账号服务与正式 profile | 独立本机虚构状态，正式 TokenStore 与 journal 使用稳定模拟服务身份 |
| 构建输出 | 原有 out 与正常 manifest | 独立 out-simulation、安装包名称及标明 simulation 的 manifest |

另一完整方案是在进程内新增 DesktopBackend/音频 transport 领域适配器。它需要扩大具体 ApiClient/AuthService/SessionService 的依赖面，并绕开已有 WS/ACK 解析，因此本批不采用。直接向 Renderer 填入结果或直接调用 helper 会跳过资格与一次执行链，也不采用。

## 核心数据形状

产品模式为构建时 normal 或 windows-simulation。身份目录统一派生名称、appId、profile、exe、NSIS 与自启键，不向源码目录写共享生成文件，不从运行期参数选择模式。

输入源契约保持现有 start、acknowledge、stop 的使用形状，取消由 stop 的 discard 语义完成。合成源使用 idle/running/stopping 状态和操作身份管理定时器。第一次开始即产生一个合法虚构帧，后续每 250 ms 最多 4000 样本；正常停止只发送一次合法尾块，取消同步失效旧操作并丢弃尾块，不补发睡眠积压。时间、样本计数和输入电平均明确属于模拟数据。

服务端点同时拥有 HTTP/WS 地址、稳定 storageIdentity 与可选私有请求头。ApiClient 持有唯一端点；refresh、logout、导入和 WS 均从其派生。随机端口不进入 TokenStore 或 journal 身份，否则重启会破坏凭据及防重恢复。正常端点默认保持原配置，模拟端点不传 Renderer。

本机设施启动后生成仅 Main 可见的随机 nonce，拒绝缺少 nonce、错误 Host/Origin、非 loopback 和越界请求，不开放 CORS。它没有调用原生输入的能力，关闭时收口自己的连接和定时器。模拟域只提供桌面页面实际依赖的账号、能力、设置、会话、claim/receipt、历史、统计及词库接口，未知接口明确失败。

## 结果、回执与持久化

固定文本及其 SHA256 通过正式结果协议返回。正式客户端仍检查 session/attempt/revision/hash；插入仍依次完成 claim、本机 fsync 执行标记、一次 insertCurrent、严格正文/光标核验和回执。最新前台光标规则、剪贴板所属版本规则、unknown 不重放及原生保护目标拒绝保持。

模拟设施是其 JSON 状态的唯一写者。持久变更串行提交，原子保存成功后才回包；claim 的 reserved/终态回执和幂等身份跨重启保留，未结束会话启动后取消，不恢复录音或自动插入。不保存 PCM。账号仅使用虚构 example.test 或 example.invalid 数据，密码使用本机盐化摘要，令牌仅存摘要；真实 TokenStore 仍参与本机凭据加密与恢复。统计从实际模拟会话和回执派生，unverified 不计成功。

这些行为用于产生受控测试条件，不作为生产数据库、后端鉴权、模型或后台验收。服务器与 Admin 生产源码不改，既有三端 CI 继续回归。

## 构建和验证

两份静态 Vite 配置选择独立装配。正常 module graph/ASAR 拒绝模拟 host、合成源、固定句和模拟身份；模拟 graph/ASAR 拒绝 PcmRecorder、worklet 和真实音频 API。模式不能由普通运行期参数或环境变量启用。

Windows x64 原生 CI 构建 NSIS、实际安装及运行。使用实际全局快捷键和真实 helper，独立读回外部目标全文、UTF-16 光标、选区和回执；不能以调用业务 toggle 或仅看 SendInput 返回值代替。打包后读回 appId、profile、自启项、helper、manifest、SHA256 和模拟标识；正常包并存和卸载隔离单独验证。

## 临时实现移除

移除清单随实现登记，至少包含模拟产品装配目录、合成源与本机服务、专用 Vite/Builder 配置、模拟构建脚本、模拟 CI 入口及状态标记。不能直接按字面批量删除共享文件。确认本轮验收完成后，逐项核对真实引用再移除临时部分，收回仅为模拟添加的接缝；正常身份、真实采集与正式插入链继续回归。当前不提前删除用户尚未使用的验收版本。
