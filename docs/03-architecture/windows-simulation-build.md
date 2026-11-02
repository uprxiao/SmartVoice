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

开发检查使用 `desktop` 中的 `pnpm typecheck`、`pnpm test`。正常构建与 `pnpm build:simulation` 在同一工作目录顺序执行；electron-vite 的配置编译缓存不能在两个构建之间并行共享。Windows x64 用 `pnpm package:simulation` 生成独立 NSIS；其他系统明确拒绝此打包入口。`scripts/simulation/verify-build.mjs` 同时核对构建模块图与实际 ASAR，检查模拟包不含音频设备 API、正常包不含模拟能力。安装包 manifest 带 flavor、源码、身份、Electron 输入和资源摘要；模拟 flavor 无法进入正式发布校验。

CI 由 `build.yml` 的 `simulation_only` 入口调用 `windows-simulation.yml`。构建、ASAR、摘要与实际安装检查通过后，先保存名称明确带 `unverified` 的诊断安装包；它只表示安装阶段通过。名称为已验证的最终 artifact 仍须全部运行门禁成功后上传，不创建 tag 或 Release。实际安装的正常包作为并存对照。匿名证据只包含 `summary.json` 及四份显式列出的 UI 报告，私有工作目录中的凭据、模拟状态、进程清单和浏览器 profile 不上传。

## 安装后的测试方式

安装名称为 SmartVoiceInput Simulation，可与正常版分开卸载。用公开虚构账号 `tester@example.test`、密码 `Simulation123!` 登录本机模拟服务。若正常版已占用默认快捷键，先退出正常版或在设置中选择不冲突的组合。

将光标放进记事本、Chrome 或微信的可编辑输入框。按默认 `Ctrl+Shift+Space` 开始，确认胶囊显示“模拟”；再次按下后，固定句通过正常插入链填入当前前台输入位置。它不自动回车或发送。在已有文字、选区和空输入框分别核对结果；开始后切换目标，再结束，核对写入结束后实际执行时的最新位置。微信需用户实际安装并逐项验证，不由记事本或 Chrome 的结果推断。

登录页与主窗持续显示模拟标识。历史和统计来自本机模拟会话，不同步生产账号。用户完成本轮验证后再移除临时实现；已安装包、用户数据和历史资产的清理需要单独明确范围。

## 临时实现移除

移除清单随实现登记，至少包含模拟产品装配目录、合成源与本机服务、专用 Vite/Builder 配置、模拟构建脚本、模拟 CI 入口及状态标记。不能直接按字面批量删除共享文件。确认本轮验收完成后，逐项核对真实引用再移除临时部分，收回仅为模拟添加的接缝；正常身份、真实采集与正式插入链继续回归。当前不提前删除用户尚未使用的验收版本。

| 范围 | 验后移除或收回的内容 |
|---|---|
| 独立运行模块 | `desktop/src/main/simulation/`、`desktop/src/renderer/simulation/` 及其中专用测试 |
| 专用构建与检查 | `desktop/electron.vite.simulation.config.ts`、`desktop/electron-builder.simulation.config.cjs`、`scripts/simulation/`、`windows-simulation.yml`、`build.yml` 的模拟入口、`package.json` 的模拟命令与 `.gitignore` 对应输出 |
| 专用 Windows 驱动 | `simulation-smoke.ps1`、`simulation-ui.cjs`、`simulation-shortcut.cs` |
| 产品装配 | 先恢复正常引用，再核对 `desktop/build/{products.cjs,product-config.ts,vite-config.ts}`、`src/{main,renderer}/product/`、`src/shared/product.d.ts`、Vite/Vitest/TypeScript 配置；不能直接删除仍被正常版引用的文件 |
| 正式源码接缝 | 逐项复核 ApiClient 端点与 WS 地址、SessionService 服务身份、Main 启停、RPC 采集权限、录音 contract、Renderer 采集源、banner/提示、`buildFlavor`/`not-required` 及相关测试；先迁移调用再收回临时部分 |
| 通用正常行为 | Windows NSIS 使用自身 `APP_ID` 清理启动项、窗口与托盘采用实际产品名可以保留；正式 native helper、claim、journal、回执和插入逻辑本批未另造分支 |
| 记录与资产 | 原需求、用例、失败与结果记录保留；已交付安装包及本机用户数据不随源码移除而自动删除 |
