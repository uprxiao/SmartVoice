# Windows 版本（v0.0.2）：范围、平台差异与技术验证决策稿

2026-10-29。**状态：S0/S1 草稿，待本人评审。** 来源：本人 2026-10-29 决定——“针对 Windows 的修改专门做一个版本，放在学习版本之前”，并要求页面按 Windows/macOS 区分（如权限）、先做技术验证再开发。本稿只确定范围与验证方案；S1 条款（PRD/业务规则修订）、S2 设计、S3 用例在本稿评审通过后依次编写（[AGENTS](../../AGENTS.md) §2），不提前写用例。

## 1. 版本计划

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.0.1 | 首个公开版本（口述、整理、云端识别、词库、历史、后台、辅助功能引导） | 已发布 |
| **v0.0.2** | **Windows 版**：Windows 文字插入、按平台区分的权限与界面、Windows 原生辅助程序、Windows 技术验证与真机验收 | 本稿 |
| v0.0.3 | 快速纠正学习（[决策](fast-correction-learning-decision-20261028.md)、[设计](../03-architecture/fast-correction-learning-design-20261028.md)；原计划 v0.0.2，顺延） | S0–S3 已起草，待评审 |

Windows 排在学习之前的理由：学习依赖“插入后读回目标内容”，Windows 目前没有插入与读回能力（见 §2），先补齐插入才有可学习的基础。开发分支 `feature/0.0.2`。

## 2. 现状（2026-10-29 对代码的核对）

| 项 | 现状 | 位置 |
|---|---|---|
| 自动插入 | **Windows 不支持**：原生桥在非 macOS 平台直接返回 `NATIVE_UNSUPPORTED`（“外部输入目前仅在 macOS 提供”）；识别、整理、历史、统计可用，结果只能复制 | `desktop/src/main/core/native-input.ts` |
| 原生辅助程序 | 仅 macOS（Swift，719 行，stdin/stdout 按行 JSON，动作 `permission/capture/validate/insert/insertCurrent/inputBounds/observe/keyDown/release`）；构建脚本只在 `darwin` 生成 | `desktop/native/voice-input.swift`、`desktop/scripts/build-voice-helper.mjs` |
| 设置页权限区块 | **未按平台区分**：始终显示“辅助功能”、黄色“未授权”标记以及“请求权限/打开系统设置”按钮；在 Windows 上无意义 | `desktop/src/renderer/views/SettingsView.tsx` |
| 录音胶囊“未开启辅助功能”提示、首次登录授权框 | 已仅限 macOS（`darwin` 判断），Windows 上不弹 | `desktop/src/main/rpc.ts`、`index.ts` |
| 麦克风权限检查 | **只在 `darwin` 分支**；Windows 完全不检查，系统禁用麦克风时没有明确引导 | `desktop/src/main/rpc.ts` |
| 关闭主窗口 | `window-all-closed` 在非 macOS 直接退出；**无托盘**，没有“关闭后继续后台运行并可重新打开”的入口 | `desktop/src/main/index.ts` |
| 全局快捷键 | 默认 `CommandOrControl+Shift+Space`（Windows 即 Ctrl+Shift+Space）；Windows 保留表**不含**它（macOS 含 `Control+Shift+Space`） | `desktop/native/shortcut-policy.ts` |
| 录音胶囊窗口 | 已 `focusable:false`、置顶；仅 macOS 使用 `panel` 类型 | `desktop/src/main/app/window.ts`、`native/hud-window-policy.ts` |
| 安装包 | NSIS 目标已配置，CI 在 `windows-2022` 构建；**未签名**；完整业务验收未做 | `desktop/electron-builder.config.cjs`、`.github/workflows/build.yml` |
| 验收要求 | Windows 记事本、Chrome 普通 input/textarea 必须能自动插入并读回（BR-07；TC-INSERT-02/07/08、TC-NFR-08）；目前均为 `blocked` | `docs/04-test/test-cases.md` |

## 3. 范围

**范围内**：
1. Windows 通用粘贴插入（与 macOS 同语义：一次粘贴、不自动回车、可读回才算已输入、否则“不可核验”，不自动重试/学习）。
2. Windows 原生辅助程序（沿用现有协议，除 `observe/keyDown/release`——属 v0.0.3 学习，本版不做）。
3. 权限与界面按平台区分（§4）。
4. 麦克风权限检查与引导；关闭窗口/托盘行为；Windows 快捷键保留表与默认值复核；多显示器 DPI 下录音胶囊定位。
5. 技术验证（§6）、安装包与真机验收。

**范围外**：快速纠正学习（v0.0.3）；以管理员权限运行或 `uiAccess` 方案；Windows 代码签名（另行决定，§7 Q3）；Windows on ARM；Windows 以外的 Linux 桌面；输入法组合状态的探测（无可靠公开办法，列为已知限制）。

## 4. 平台差异：界面与行为

| 位置 | macOS（现状） | Windows（v0.0.2 目标） |
|---|---|---|
| 设置页“权限”区块 | 辅助功能：已授权/未授权，“请求权限”“打开系统设置” | **不显示“辅助功能”**。改为：①“麦克风”状态（来自系统隐私设置）与“打开麦克风隐私设置”（`ms-settings:privacy-microphone`）；②“输入到其他应用”能力说明：无需授权，但**以管理员身份运行的窗口无法自动输入**，此时结果会复制到剪贴板 |
| 首次登录 | 自动弹一次辅助功能授权框 | 不弹；如检测到麦克风被系统禁用，提示并给出设置入口 |
| 录音时提示 | 未授权辅助功能时胶囊提示“未开启辅助功能，结果不会自动输入”+“去授权” | 不适用；改为：目标是更高权限窗口时，胶囊提示“目标窗口以管理员运行，结果已复制，请手动粘贴” |
| 插入失败卡片 | 复制结果 / 去授权 / 关闭 | 复制结果 / 关闭（无“去授权”）；按失败原因区分文案（管理员窗口、受保护输入框、目标已变化、无法核验） |
| 麦克风 | 请求系统授权（`askForMediaAccess`） | Windows 无请求 API：读取状态（`getMediaAccessStatus`），被禁用则引导到系统设置，不重复弹窗；桌面应用由一个全局开关控制，改动后可能需要重启应用 |
| 快捷键 | 保留表含 `Control+Shift+Space` | 保留表按 Windows 复核（含输入法常用键 Shift+Space、Ctrl+Space、Ctrl+Shift 等）；默认值待实测（§7 Q2） |
| 关闭主窗口 | 窗口隐藏，应用继续运行（Dock） | 隐藏到托盘，应用继续运行；托盘菜单“打开 / 退出”；是否开机自启为设置项，默认关闭 |
| 菜单 | macOS 应用菜单 | Windows 标准菜单（`menu.ts` 已有平台分支，需复核） |
| 文案 | 提到 macOS 的路径与术语 | 全部文案（含 i18n 中英）按平台出对应版本；帮助页“权限”一节按平台显示 |

界面改动需先补 S0 原型（设置页权限区块、胶囊提示/失败卡片的 Windows 状态），评审后再进入 S3；后台页面无平台差异。

## 5. Windows 插入语义（拟定，S1/S2 再细化）

依据 [BR-07 通用粘贴](universal-paste-decision-20261027.md) 与 [终端粘贴决定](terminal-paste-decision-20261027.md)，Windows 实现的行为规则：

1. **目标**：以粘贴前一刻的前台窗口为准，不绑定录音开始/停止时的目标；录音胶囊不得抢占前台。
2. **准入检查**（任一不满足则不写入，复制降级并说明原因）：目标为可见的顶层应用窗口，不是本应用、桌面/任务栏/锁屏/UAC 安全桌面；进程仍存活且与取目标时一致；**目标完整性级别不高于本应用**（`SendInput` 被 UIPI 拦截时不返回错误，必须自行比较；不使用 `TokenElevation` 单独判断）；UI Automation 可读时，`IsPassword`、`IsReadOnly`、`IsEnabled` 判定为密码/只读/禁用则拒绝。
3. **修饰键**：粘贴前等待用户释放快捷键的修饰键（超时约 0.5 秒），避免注入的 Ctrl+V 变成 Ctrl+Shift+V；超时则对仍按下的修饰键补发弹起，仅在其仍被物理按住时恢复。
4. **剪贴板**：保存原内容 → 写入结果文字，并**同时写入排除格式**（`ExcludeClipboardContentFromMonitorProcessing`、`CanIncludeInClipboardHistory=0`、`CanUploadToCloudClipboard=0`），使听写文字不进入 Win+V 历史、不上传云剪贴板 → `SendInput` 发 Ctrl+V → 等待目标处理（约 0.35 秒起，实测定）→ 仅当剪贴板序号未变时恢复原内容。
5. **读回**：经 UI Automation 读取目标内容与光标；全文/光标精确匹配才记 `inserted` 并允许学习；读不到（自绘控件、Chromium 首次读取为空、终端）记 `unverified`，不自动重试、不学习；不自动 Enter。
6. **终端**：一次 Ctrl+V，不回车。Windows Terminal 默认对多行粘贴弹确认框，此时结果记 `unverified`。
7. **不探测输入法组合状态**：Windows 没有跨进程可靠方法；列为已知限制，在真机用中文输入法验收其表现。

## 6. 技术验证方案（先于开发）

**目的**：确认上述方案在 Windows 上技术可行；结论可能改变设计，所以先验证、再定稿 S2 与开发。验证程序将成为 Windows 辅助程序的雏形，放在 `desktop/native/windows/`，验证原始结果与截图只保留本地。

**阶段 1：GitHub 托管 Windows 机器（我自己完成，无需占用任何人的电脑）**
- 环境：固定 `windows-2025` 托管机（`windows-latest` 会悄悄换映像；ARM 映像不可用于前台操作）。已知经验性事实（非 GitHub 官方保证，每次运行先做预检）：作业运行在交互式控制台会话，`SendInput` 与截图可用；默认 1024×768、96 DPI，可调；只有经典记事本；以管理员运行且 UAC 关闭（完整性 High），**提升权限环境下输入与 `SetValue` 会“返回成功但无效果”，所以被测应用必须以中等完整性启动**。
- 触发：新增仅手动触发的验证工作流（本仓库推送不会自动触发工作流，需手动 `workflow_dispatch`）；产物（日志 JSON、截图）下载到本地留证。
- **推送分支需要本人授权**（AGENTS §7.3）。
- 局限：不含真实中文输入法、真实硬件、混合 DPI 多屏、家庭版/专业版差异、杀毒软件、Win11 新版记事本。

**阶段 2：真实 Windows**（开发进行中并行，S5 前完成）：中文输入法、安装包、多显示器 DPI、Windows Terminal、Win+V 历史排除等托管机验证不了的项。借用的那台电脑的使用方式见 §7 Q5。

**验证项与通过判据**（“阻断”＝全部通过后才进入 S2 定稿与开发；失败则回到本稿修订方案）：

| 编号 | 验证内容 | 通过判据 | 阶段 | 阻断 |
|---|---|---|---|---|
| V-01 | 录音胶囊显示/更新/隐藏期间的前台窗口 | `GetForegroundWindow` 在胶囊显示前后一致（不是只看 `isFocused()`） | 1 + 真机 | 是 |
| V-02 | 记事本粘贴 + UIA 读回 | 多段文本（含中文、emoji、换行）插入于光标/选区；读回全文与光标精确匹配 | 1 + 真机 | 是 |
| V-03 | Chrome 普通 input/textarea | 同 V-02 | 1（需安装 Chrome）+ 真机 | 是 |
| V-04 | 修饰键残留 | 用户按住 Ctrl+Shift 时触发粘贴，目标收到的是 Ctrl+V 而非 Ctrl+Shift+V；无修饰键卡住 | 1 | 是 |
| V-05 | 剪贴板排除格式 | 听写文字不出现在 Win+V 历史；原剪贴板恢复 | 真机（托管机可行性另评估） | 否（S5 前必过） |
| V-06 | 剪贴板保存/恢复保真度 | 文本、HTML、RTF、图片、文件列表恢复一致；无法安全复制的格式有明确处理且不丢原内容 | 1 + 真机 | 否 |
| V-07 | 完整性级别检测 | 中等完整性目标可插入；高完整性目标被**检测并拒绝**（不静默丢弃，复制降级） | 1 | 是 |
| V-08 | UIA 目标识别 | 密码框、只读框、禁用控件识别；读不到时降级为 `unverified` | 1 | 是 |
| V-09 | Chromium 首次读取为空 | 预热/重试后能读回；读不到时降级而不是误判成功 | 1 | 是 |
| V-10 | 光标位置回退链 | 经典控件、Chrome、VS Code 都能得到非空位置；回退链按序生效 | 1 + 真机（多屏 DPI） | 是 |
| V-11 | 终端 | cmd/PowerShell/Windows Terminal 一次粘贴不回车；多行确认框情形记为 `unverified` | 真机 | 否 |
| V-12 | 麦克风权限状态 | `getMediaAccessStatus('microphone')` 在 Windows 的各取值与“打开麦克风隐私设置”可用 | 真机 | 否 |
| V-13 | 全局快捷键 | `register` 成功；与微软拼音、搜狗的输入法热键无冲突；确定 Windows 默认值 | 1（注册）+ 真机（输入法） | 否 |
| V-14 | 原生辅助程序 | 长驻进程冷启动耗时、协议往返、崩溃重启、随安装包的路径（`extraResources`） | 1 | 是 |
| V-15 | 托盘与生命周期 | 关闭主窗口后继续运行并可从托盘重新打开；单实例；安装/卸载 | 1 + 真机 | 否 |

阻断项全部通过前，不进入开发；非阻断项在开发中并行验证，S5 前必须全部有结论。

## 7. 待本人决定的问题

| # | 问题 | 建议 |
|---|---|---|
| Q1 | Windows 辅助程序语言：Rust（`windows` crate，单 exe）/ C#（需 self-contained 或 NativeAOT）/ C++（COM） | 建议 Rust：无运行时依赖，Windows API 覆盖完整；没有找到可靠的体积/冷启动对比数据，由 V-14 实测确认 |
| Q2 | Windows 默认快捷键是否沿用 `Ctrl+Shift+Space` | 待 V-13 实测（Shift+Space 是输入法全角/半角键，Ctrl+Shift 是切换输入语言，无来源直接讨论该组合）；冲突则 Windows 另选默认并加入保留表 |
| Q3 | Windows 代码签名 | v0.0.2 仍发未签名预览包；2024 年起 EV 也不能绕过 SmartScreen，信誉靠累积；候选：SignPath Foundation（开源免费但需公开知名度并手动审批）、Azure Artifact Signing（个人仅美/加）、Certum 开源证书（私钥需硬件令牌）。需你决定发布者身份后另议 |
| Q4 | 关闭窗口行为与自启 | 关闭到托盘；开机自启做成设置项，默认关闭 |
| Q5 | 借用的那台 Windows 的使用方式 | 先向机主问清：Windows 10 还是 11、物理机还是虚拟机、x64 还是 ARM、是否愿意安装软件、能否用测试用本地账号、使用哪种中文输入法。建议方式 C（机主按步骤运行验证脚本并回传结果文件）；SSH 直连因会话隔离做不了界面验证，只有“机主保持登录且不锁屏 + 交互式计划任务”才可行，须机主信任 |
| Q6 | 支持的 Windows 版本 | 沿用现有约定 Windows 10/11 x64；Windows 10 已于 2025-10-14 结束主流支持，是否仍要求验收 Windows 10 请确认 |
| Q7 | 管理员窗口 | 首版不支持（不要求管理员运行、不用 `uiAccess`）：明确提示并复制降级 |

## 8. 流程与产物

| 阶段 | 产物 | 说明 |
|---|---|---|
| 技术验证 | `desktop/native/windows/`（验证程序）、手动触发的验证工作流；本地证据 | 阻断项通过后进入下一步 |
| S0 | 原型：设置页权限区块、胶囊提示/失败卡片的 Windows 状态；更新页面状态矩阵 | 沿用既有原型规范，数据为虚构 |
| S1 | 业务规则与 PRD 修订：BR-07（Windows 实现与已知限制）、FR-04-05、FR-09、权限相关条款 | 评审后生效 |
| S2 | 桌面契约与设计：Windows 辅助程序协议与实现、平台差异、生命周期、构建与打包 | 含 Q1 的结论 |
| S3 | Windows 专属用例（并保持 V1 通用用例） | 在 S1/S2 通过后才写 |
| S4/S5 | 实现、CI（Windows 构建与验证）、真机验收 | 安装包验收对应 TC-NFR-08 |

## 9. 公开来源

- [Clipboard Formats（Microsoft Learn）](https://learn.microsoft.com/en-us/windows/win32/dataxchg/clipboard-formats)
- [SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)、[AllowSetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-allowsetforegroundwindow)
- [UI Automation TextPattern Overview](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-textpattern-overview)、[IsPasswordProperty](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.automationelement.ispasswordproperty)
- [UIPI 问题（Power Automate 文档）](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/desktop-flows/ui-automation/uipi-issues)
- [Turn on app permissions for your microphone in Windows](https://support.microsoft.com/en-us/windows/privacy/turn-on-app-permissions-for-your-microphone-in-windows)
- [Electron systemPreferences](https://www.electronjs.org/docs/latest/api/system-preferences)、[globalShortcut](https://www.electronjs.org/docs/latest/api/global-shortcut)、[Tray](https://www.electronjs.org/docs/latest/tutorial/tray)、[Accessibility](https://www.electronjs.org/docs/latest/tutorial/accessibility)
- [Chromium UI Automation 文档](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/accessibility/browser/uiautomation.md)
- [Code signing options for Windows app developers](https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options)
- [Rust for Windows and the windows crate](https://learn.microsoft.com/en-us/windows/dev-environment/rust/rust-for-windows)
