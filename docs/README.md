# 文档索引

本仓库按“文档驱动、阶段门禁”推进（见 [AGENTS.md](../AGENTS.md) §2）。调研材料、实验、真实环境验收记录与截图只保存在维护者本地（`.gitignore`），不在公开仓库中；本仓库保留需求、设计、测试用例等可公开的部分。

## 阶段状态

| 范围 | 状态 |
|---|---|
| V1（口述、整理、插入、词库与修改学习、历史统计、管理后台） | 已完成开发与验收 |
| V2（云端 ASR provider、后台模型服务管理） | 已完成开发与验收 |
| 辅助功能未授权提示（TC-INSERT-22） | 已完成，随 v0.0.1 发布 |
| **Windows 版本（v0.0.2）**：Windows 文字插入、权限与界面按平台区分、原生辅助程序 | [范围决策](02-prd/windows-version-decision-20261029.md)已确认；**9 项开发前阻断技术验证通过，S0/S1/S2/S3 已补齐复核，S4 主体实现已落盘并同步开发分支**。见 [S0 原型](01-prototype/windows-v002.md)与 [WIN-01～12](02-prd/windows-v002-requirements.md)。三端自动回归与 Windows 开发安装包构建/启动验证通过；剩余联调、真实物理按键、输入法、多屏等验收仍须执行 |
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
| S4 正式开发 | 主体已实现，分层验证中 | 正式 helper、平台 UI/麦克风/RPC、托盘/单实例/后台自启、坐标与打包已实现；源码 `48628d1` 的[完整 CI](https://github.com/uprxiao/SmartVoice/actions/runs/40881145082)通过，含 Desktop Mac/Windows 各694项、Rust13项、Admin93项和 Server/自然时钟回归；真实 PostgreSQL/API 21项专项另已通过。`8bc42f6` 的[Windows 开发包验证](https://github.com/uprxiao/SmartVoice/actions/runs/40883701708)通过：正式原生17/17参数、9/9技术阻断、NSIS x64、普通完整性进程后台隐藏/不抢焦点、真实RPC自启off/on/off读回、第二实例和helper清理。Mac arm64开发包构建通过。失败与未执行参数全部保留，不以部分参数冒称40条或S5全通过。见 [专项用例](04-test/test-cases-windows-v002.md)与 [三端责任](03-architecture/windows-v002-design.md#构建部署与三端责任) |
| S5 真机验收 | 未开始 | Windows x64 自测；V-04 物理按键及 V-05/06/11/12/13/15 和目标安装包均须留证 |

S4 接续复核：`f4a4520` 后续 CI 中 Admin 测试夹具在源码编译阶段触发 250ms VM 超时，其他任务通过。`87fd620` 将编译准备移到受限调用之前，保留原限制与93项分母，[完整 CI 复验通过](https://github.com/uprxiao/SmartVoice/actions/runs/40900959347)。Admin 又完成9组真实浏览器/隔离 PostgreSQL/HTTP/WS 联调，三种回执与重复请求的计数、筛选、错误恢复及登录退出均核对通过；见[复跑说明](../admin/tests/browser-live-README.md)。模型与 PCM 明确为虚构夹具，剩余 Windows 参数与 S5 真机要求保留。

Windows 参数继续展开：新增 Notepad/Chrome 光标与空框后，[23 参数运行](https://github.com/uprxiao/SmartVoice/actions/runs/40908758987)为21通过、2个 Chrome 空框失败，另有夹具清理未确认；原失败保留，未放宽全文/光标或清理判据。后续[30 参数诊断](https://github.com/uprxiao/SmartVoice/actions/runs/40910927523)为27通过、Chrome空框2组及DIB 1组失败，9/9技术阻断与清理通过。HTML/RTF/文件列表等各格式保真和不安全格式拒绝已有实测；空框标签名归一化修复仍要求独立 ValuePattern为空、无嵌入子项、零光标/选区，DIB关联格式继续处理；正在展开异常读回、协议字节与跨进程版本回归。

`cc93f22` 的[41 参数回归](https://github.com/uprxiao/SmartVoice/actions/runs/40924485576)为39通过、2失败：Chrome两种空框已精确通过；四种异常读回、非法字节/EOF/超大行、生产修饰键中和、跨进程新复制与三排除 DWORD 均通过。剩余 DIB 和父进程退出分别修复中；后者改用进程对象存活状态结合原始创建时间，并在实际粘贴前核父身份。所有修复仍须 Windows 独立回归，不提前记通过。

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
