# 文档索引

本仓库按“文档驱动、阶段门禁”推进（见 [AGENTS.md](../AGENTS.md) §2）。调研材料、实验、真实环境验收记录与截图只保存在维护者本地（`.gitignore`），不在公开仓库中；本仓库保留需求、设计、测试用例等可公开的部分。

## 阶段状态

| 范围 | 状态 |
|---|---|
| V1（口述、整理、插入、词库与修改学习、历史统计、管理后台） | 已完成开发与验收 |
| V2（云端 ASR provider、后台模型服务管理） | 已完成开发与验收 |
| 辅助功能未授权提示（TC-INSERT-22） | 已完成，随 v0.0.1 发布 |
| **Windows 版本（v0.0.2）**：Windows 文字插入、权限与界面按平台区分、原生辅助程序 | [范围决策](02-prd/windows-version-decision-20261029.md)已确认；**S0/S1/S2/S3 已补齐复核，S4 主体实现，剩余参数补测及缺陷回归中**。见 [S0 原型](01-prototype/windows-v002.md)与 [WIN-01～12](02-prd/windows-v002-requirements.md)。三端回归、Windows 56项原生参数/15组实际界面联调和开发安装包验证通过；真实音频/模型、物理按键、输入法、多屏及系统登录等参数仍须执行，不宣称40条或S5全部通过 |
| 快速纠正学习（BR-08 2026-10-28 修订） | S0–S3 已起草，**待评审**；开发计划顺延到 v0.0.3（v0.0.2 先做 Windows 版） |

已知限制：Mac 预览包未经 Apple 签名与公证；Windows 安装包未签名，完整业务验收未完成；长期稳定性待上线后跟踪。

### Windows v0.0.2 阶段登记

| 阶段 | 状态 | 依据 |
|---|---|---|
| 开发前技术验证 | 阶段 1 已通过 | 源码 `ece6669`，[Windows 运行 40857579332](https://github.com/uprxiao/SmartVoice/actions/runs/40857579332)：10 项策略单测通过、15 项完整分母、9 项阻断通过；V-04 使用本人同日确认的分层判据。非阻断与真机未验项保留 |
| S0 平台补充 | 已补齐并复核 | [4 页面/28 场景](01-prototype/windows-v002.md)，继承现有视觉 token 与方案 B；实际浏览器复核权限差异、失败卡与托盘操作。原型不替代 OS 验收 |
| S1 正式需求修订 | 已整理并复核 | [WIN-01～12](02-prd/windows-v002-requirements.md)与 PRD/BR-07/11 对齐；需求用户评审依据为本人已确认的范围与 Q1–Q7，本次不追加 Windows 修改观察 |
| S2 Windows 技术设计 | 已整理并复核 | [技术设计](03-architecture/windows-v002-design.md)对齐实际扁平 JSONL、一次执行、平台能力、生命周期、坐标与三端责任；S0 首次关闭确认时序同步复核 |
| S3 Windows 测试用例 | 已生成并复核 | [40 条专项用例](04-test/test-cases-windows-v002.md)覆盖 12/12 WIN，数据、独立参数与 L1/L2/L3 判据完整；物理键盘/安装包/真实闭环要求保留 |
| S4 正式开发 | 主体实现，剩余参数补测中 | 正式 helper、平台权限 UI、托盘/单实例/后台自启、坐标、打包及首次凭据持久化已实现。[完整 CI](https://github.com/uprxiao/SmartVoice/actions/runs/40967667984)通过：Desktop Mac/Windows 各716项、Rust15项、Admin93项和 Server/自然时钟回归；真实 PostgreSQL/API 21项及 Admin 浏览器9组另已通过。[本轮 Windows 完整验证](https://github.com/uprxiao/SmartVoice/actions/runs/40967683045)通过：56/56正式原生参数、9/9技术阻断、15/15实际界面组、NSIS两轮安装/卸载/重装、后台恢复与真实 Main/preload RPC。其余物理/真实业务参数保留，未放行完整 S5。见下方证据及[三端责任](03-architecture/windows-v002-design.md#构建部署与三端责任) |
| S5 真机验收 | 未开始 | Windows x64 自测；V-04 物理按键及 V-05/06/11/12/13/15 和目标安装包均须留证 |

接续审计（2026-10-30）：剩余38条中仍有可自动补测参数。前轮完整CI、56项原生参数和15组界面联调通过，代表已执行批次通过；协议边界、真实桥故障、托盘提示持久化与生产HUD等正在补测。按原S3层级逐项复核，不把全部partial归为需要用户手测，也不额外挂真机条件。

补测复现了 Windows Renderer 崩溃后被重新打开请求复用的问题；已修正窗口重建，并追加实际 Main 的账号恢复、快捷键冲突/保存/重启验证。当前处于缺陷回归，旧批次通过不代表该新增参数已通过。

当前验证依据（分母分别登记，不合并为40条全部通过）：

- 正式 helper：[56参数实测及整轮验证](https://github.com/uprxiao/SmartVoice/actions/runs/40967683045)全部通过，覆盖严格全文/UTF-16光标、空框、DIB关联位图、父进程死亡、迟到 UIA 读回、跨进程复制与显式目标恢复；同 PID/owner 链不同根窗口拒绝。L2、开发资源和 NSIS 的静态 CRT helper 摘要一致；9项技术阻断均通过，Chrome 首次空读后恢复已有实际轨迹。
- 首次凭据：[五组对照](https://github.com/uprxiao/SmartVoice/actions/runs/40959839233)后修复初始化时序，父进程独立解密 canary 才允许持久化。[实际客户端复验](https://github.com/uprxiao/SmartVoice/actions/runs/40961476396)已通过首次注册后立即强退、后台恢复、第二实例和退出清理，原10组 Desktop/Admin UI 全部通过；系统真实重新登录另验。
- 三端联调：真实 PostgreSQL/API 21项和 Admin 浏览器9组通过，含三种回执、重复请求、租户、统计、403及错误恢复，见[复跑说明](../admin/tests/browser-live-README.md)。Windows [15组实际界面联调](https://github.com/uprxiao/SmartVoice/actions/runs/40967683045)全部通过：历史重新整理、显式选择 Notepad 目标、恢复选区插入、重复点击不重写、退出，以及同库 Admin 1会话/1插入/14字/0学习。独立全文/光标、剪贴板、SQL回执与清理均核对。PCM 与模型均为虚构夹具，不代替真实录音或质量验收。
- 安装与产物：两轮实际 NSIS 安装/卸载/重装已核对仅清自有启动项、保留其他虚构项和用户文件、重装默认 off。[生产源码三架构构建及完整 CI](https://github.com/uprxiao/SmartVoice/actions/runs/40961484374)全部通过，Mac arm64/x64 和 Windows x64 的下载摘要、大小、版本与来源均独立核对。Mac 另核结构、架构、CA、ad-hoc 签名和实际 Electron 42.11.15；本地 x64 经 Rosetta，不能替代物理 Intel 设备。最新 Windows 开发安装器另由上述完整门禁保留，本地静态解包已核主程序/helper的AMD64架构、ASAR版本/固定服务/CA；四个关键资源摘要与两轮实际安装一致。实际 Windows 安装行为由 Windows 环境验证，静态解包不替代。尚未创建 Release。

失败与修复保持可追溯：[Chrome空框/DIB初始失败](https://github.com/uprxiao/SmartVoice/actions/runs/40910927523)、[父退出等边界](https://github.com/uprxiao/SmartVoice/actions/runs/40924485576)、[显式恢复失败](https://github.com/uprxiao/SmartVoice/actions/runs/40935021016)均保留原分母，修复后再独立回归；Admin 250ms 夹具超时移除编译准备影响后，原限制和93项分母不变，[复验通过](https://github.com/uprxiao/SmartVoice/actions/runs/40900959347)。[上一轮失败](https://github.com/uprxiao/SmartVoice/actions/runs/40964235724)的 Chrome 准备和历史按钮点击另保留证据：现以只读 DOM 确认 Chrome 就绪，历史按钮等真实命中后点击，且核对精确 Main 窗口；没有改 UIA、插入预算或判定标准。本轮复验通过仍不覆盖真实麦克风、模型、物理按键、IME、多屏和系统登录等 S5 参数。

## 文档

| 目录 | 内容 |
|---|---|
| [01-prototype](01-prototype/README.md) | 原型说明与页面状态矩阵（原型本体在 `prototype/`） |
| [02-prd](02-prd/PRD.md) | 产品需求（[V2](02-prd/PRD-v2.md)）、[业务规则](02-prd/business-rules.md)及各项需求决策记录（`*-decision-*.md`） |
| [03-architecture](03-architecture/architecture.md) | 技术架构、[API 契约](03-architecture/api-contract.md)、[数据模型](03-architecture/data-model.md)、[会话协议](03-architecture/session-protocol.md)、[桌面契约](03-architecture/desktop-contract.md)、[Provider 配置](03-architecture/provider-config.md)、[部署](03-architecture/deployment.md)、[构建与发布](03-architecture/release.md) |
| [04-test](04-test/test-cases.md) | 测试用例（[V2](04-test/test-cases-v2.md)）、[覆盖映射](04-test/coverage.md)、[执行矩阵](04-test/execution-matrix.md)、[固定样本说明](04-test/fixtures.md)、[实验记录模板](04-test/experiment-record-template.md) |
| [05-branding](05-branding/) | Logo 设计过程 |

## 待决问题

- Windows WIN-12：已提出是否追加旧版修改学习的澄清，当前按本人原先确认的“observe 后移”范围继续，不追加 Windows 修改观察，不把未回复当成新的批准。若本人选择扩围，先回到 S1/S2/S3 补齐再实现；`release` 则按实码保留为既有目标生命周期清理。见 [S1 补充](02-prd/windows-v002-requirements.md)。

- Windows 技术验证 V-04 层级已于 2026-10-29 由本人确认：阶段 1 使用托管机按住键状态、目标一次 Ctrl+V/无卡键实测及物理事件来源/恢复策略单测；物理键盘按住、提前释放等继续是 S5 真机必验。按该判据复验，未据确认本身登记通过；见[决策稿 §6](02-prd/windows-version-decision-20261029.md#6-技术验证方案先于开发)。

- Windows 版本（v0.0.2）范围与 Q1–Q7 已确认，不重复请求评审；本人于 2026-10-29 明确授权推送并按既定门禁继续 S0–S4，同范围工作沿用该授权。阶段 1 技术门禁及 Windows 专属 S0/S1/S2/S3 已依次复核，S4主体实现与三端自动回归已执行；既有通用文档与 v0.0.3 学习草稿不替代本版产物。源码只同步 `feature/0.0.2`，尚无本版 main/tag/Release；发生新需求决策仍先登记本节。
- 快速纠正学习 S1 [需求变更](02-prd/fast-correction-learning-decision-20261028.md)、S2 [技术方案](03-architecture/fast-correction-learning-design-20261028.md)、S3 用例（TC-LEARN-16～31）待本人评审后进入开发。
