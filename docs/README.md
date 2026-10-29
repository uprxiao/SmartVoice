# 文档索引

本仓库按“文档驱动、阶段门禁”推进（见 [AGENTS.md](../AGENTS.md) §2）。调研材料、实验、真实环境验收记录与截图只保存在维护者本地（`.gitignore`），不在公开仓库中；本仓库保留需求、设计、测试用例等可公开的部分。

## 阶段状态

| 范围 | 状态 |
|---|---|
| V1（口述、整理、插入、词库与修改学习、历史统计、管理后台） | 已完成开发与验收 |
| V2（云端 ASR provider、后台模型服务管理） | 已完成开发与验收 |
| 辅助功能未授权提示（TC-INSERT-22） | 已完成，随 v0.0.1 发布 |
| **Windows 版本（v0.0.2）**：Windows 文字插入、权限与界面按平台区分、原生辅助程序 | [范围决策](02-prd/windows-version-decision-20261029.md)已确认；**9 项开发前阻断技术验证通过，S0/S1/S2 已补齐复核，S3 进行中，尚未进入 S4**。见 [S0 原型](01-prototype/windows-v002.md)与 [WIN-01～12](02-prd/windows-v002-requirements.md)。真实物理按键、输入法、多屏等 S5 验收仍须执行 |
| 快速纠正学习（BR-08 2026-10-28 修订） | S0–S3 已起草，**待评审**；开发计划顺延到 v0.0.3（v0.0.2 先做 Windows 版） |

已知限制：Mac 预览包未经 Apple 签名与公证；Windows 安装包未签名，完整业务验收未完成；长期稳定性待上线后跟踪。

### Windows v0.0.2 阶段登记

| 阶段 | 状态 | 依据 |
|---|---|---|
| 开发前技术验证 | 阶段 1 已通过 | 源码 `ece6669`，[Windows 运行 40857579332](https://github.com/uprxiao/SmartVoice/actions/runs/40857579332)：10 项策略单测通过、15 项完整分母、9 项阻断通过；V-04 使用本人同日确认的分层判据。非阻断与真机未验项保留 |
| S0 平台补充 | 已补齐并复核 | [4 页面/28 场景](01-prototype/windows-v002.md)，继承现有视觉 token 与方案 B；实际浏览器复核权限差异、失败卡与托盘操作。原型不替代 OS 验收 |
| S1 正式需求修订 | 已整理并复核 | [WIN-01～12](02-prd/windows-v002-requirements.md)与 PRD/BR-07/11 对齐；需求用户评审依据为本人已确认的范围与 Q1–Q7，本次不追加 Windows 修改观察 |
| S2 Windows 技术设计 | 已整理并复核 | [技术设计](03-architecture/windows-v002-design.md)对齐实际扁平 JSONL、一次执行、平台能力、生命周期、坐标与三端责任；S0 首次关闭确认时序同步复核 |
| S3 Windows 测试用例 | 进行中 | S0/S1/S2 确定并复核后登记；逐项覆盖 WIN-01～12，保留既有通用验收及 S5 真机标准 |
| S4 正式开发 | 未开始 | S3 评审及登记后按用例实施 |
| S5 真机验收 | 未开始 | Windows x64 自测；V-04 物理按键及 V-05/06/11/12/13/15 和目标安装包均须留证 |

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

- Windows 版本（v0.0.2）范围与 Q1–Q7 已确认，不重复请求评审；本人于 2026-10-29 明确授权推送并按既定门禁继续 S0–S4，同范围工作沿用该授权。阶段 1 技术门禁已通过，当前依次整理并复核 Windows 专属 S1/S2/S3；既有通用文档与 v0.0.3 学习草稿不替代它们。发生新需求决策仍先登记本节。
- 快速纠正学习 S1 [需求变更](02-prd/fast-correction-learning-decision-20261028.md)、S2 [技术方案](03-architecture/fast-correction-learning-design-20261028.md)、S3 用例（TC-LEARN-16～31）待本人评审后进入开发。
