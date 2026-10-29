# 文档索引

本仓库按“文档驱动、阶段门禁”推进（见 [AGENTS.md](../AGENTS.md) §2）。调研材料、实验、真实环境验收记录与截图只保存在维护者本地（`.gitignore`），不在公开仓库中；本仓库保留需求、设计、测试用例等可公开的部分。

## 阶段状态

| 范围 | 状态 |
|---|---|
| V1（口述、整理、插入、词库与修改学习、历史统计、管理后台） | 已完成开发与验收 |
| V2（云端 ASR provider、后台模型服务管理） | 已完成开发与验收 |
| 辅助功能未授权提示（TC-INSERT-22） | 已完成，随 v0.0.1 发布 |
| **Windows 版本（v0.0.2）**：Windows 文字插入、权限与界面按平台区分、原生辅助程序 | [范围与技术验证决策稿](02-prd/windows-version-decision-20261029.md)已于 2026-10-29 由本人确认；**技术验证工具已准备并通过本地检查，待 Windows 实际执行，未进入 S4**。Windows 专属 S0 原型、S1 正式条款修订、S2 设计、S3 用例尚未完成；9 项阻断验证通过后依次补齐并评审。接续入口见[验证说明](03-architecture/windows-technical-verification.md) |
| 快速纠正学习（BR-08 2026-10-28 修订） | S0–S3 已起草，**待评审**；开发计划顺延到 v0.0.3（v0.0.2 先做 Windows 版） |

已知限制：Mac 预览包未经 Apple 签名与公证；Windows 安装包未签名，完整业务验收未完成；长期稳定性待上线后跟踪。

## 文档

| 目录 | 内容 |
|---|---|
| [01-prototype](01-prototype/README.md) | 原型说明与页面状态矩阵（原型本体在 `prototype/`） |
| [02-prd](02-prd/PRD.md) | 产品需求（[V2](02-prd/PRD-v2.md)）、[业务规则](02-prd/business-rules.md)及各项需求决策记录（`*-decision-*.md`） |
| [03-architecture](03-architecture/architecture.md) | 技术架构、[API 契约](03-architecture/api-contract.md)、[数据模型](03-architecture/data-model.md)、[会话协议](03-architecture/session-protocol.md)、[桌面契约](03-architecture/desktop-contract.md)、[Provider 配置](03-architecture/provider-config.md)、[部署](03-architecture/deployment.md)、[构建与发布](03-architecture/release.md) |
| [04-test](04-test/test-cases.md) | 测试用例（[V2](04-test/test-cases-v2.md)）、[覆盖映射](04-test/coverage.md)、[执行矩阵](04-test/execution-matrix.md)、[固定样本说明](04-test/fixtures.md)、[实验记录模板](04-test/experiment-record-template.md) |
| [05-branding](05-branding/) | Logo 设计过程 |

## 待决问题

- Windows 版本（v0.0.2）范围与 Q1–Q7 已确认，不重复请求评审；当前待办是[技术验证](03-architecture/windows-technical-verification.md)。独立探针已补齐，通过 Windows 交叉检查和 7 项策略单测；Windows 运行尚未开始，未记录任何阻断项通过。技术验证程序不等于产品开发完成，既有通用 PRD/设计/用例与 v0.0.3 学习草稿不替代 Windows 专属 S0–S3。推送开发分支及执行远端验证仍按 AGENTS §7.3 授权范围进行。
- 快速纠正学习 S1 [需求变更](02-prd/fast-correction-learning-decision-20261028.md)、S2 [技术方案](03-architecture/fast-correction-learning-design-20261028.md)、S3 用例（TC-LEARN-16～31）待本人评审后进入开发。
