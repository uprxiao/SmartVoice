# 三端开发与联调交接（v0.2）

2026-10-29 Windows v0.0.2 增量任务与责任见 [Windows 设计“构建、部署与三端责任”](windows-v002-design.md#构建部署与三端责任)。本版重点 Desktop；Server/Admin 核查和回归不省略，不新增后台页面、API 或迁移。以下为既有通用三端基线，Windows S3 完成后再登记正式开发。

2026-10-17。依照用户最新要求，S4 包含 `desktop/`、`server/`、`admin/` 三个交付物；开发前先完成原型、需求、技术与测试设计。本文是开工交接，不表示已经编写业务代码或通过应用测试。

## 1. 开工依据与边界

- 界面：[可离线打开的原型](../../prototype/index.html)、[页面状态矩阵](../01-prototype/page-state-matrix.md)。评审工具栏、示例数据和演示状态选择器不进入产品。
- 需求：[PRD](../02-prd/PRD.md)的 49 项 FR、[BR-01～12](../02-prd/business-rules.md)。仍为方案 B，含用户本轮明确授权的容量管理。
- 协议：[API](api-contract.md)、[会话协议](session-protocol.md)、[桌面 RPC / Native](desktop-contract.md)。三端共用状态、错误码、版本与身份规则，不能各写一套猜测。
- 服务：[数据模型](data-model.md)、[Provider](provider-config.md)、[部署](deployment.md)。账号、租户、历史、词库、管理统计需本项目实现；旧服务不是直接可用的多用户后端。
- 复用：[来源与文件哈希](reuse-map.md)。仅迁移自有实现，避开源项目无关和未提交改动，参考产品证据仅指导行为。
- 验收：[219 条用例](../04-test/test-cases.md)、[固定夹具](../04-test/fixtures.md)、执行台账（内部记录，未公开）。Mock 通过不改变真实环境用例的未执行状态。

## 2. 同期推进的工程工作

| 工作包 | Desktop | Server | Admin | 可交付检查点 |
|---|---|---|---|---|
| A：工程与契约 | 迁移 electron-vite 三段结构、typed RPC、页面路由、MobX 与 token 边界；原型样式转 React | 配置、日志脱敏、PostgreSQL/Alembic、认证/租户、能力探针与 provider 接口 | React/Vite 工程、管理员登录、路由守卫、类型化 API client | 三端可构建；同一契约对应的 Mock 场景可独立演示 |
| B：账号与个人数据 | 登录/注册/权限、词库/历史/设置/洞察 | 账号/设备/保留、词库版本与 CSV、统计 | 用户列表/创建/启停、频率与模型页 | UI Mock 测试 + 真 PostgreSQL 的 API/权限/迁移测试 |
| C：口述闭环 | AudioWorklet、Main HTTP/WS、HUD、结果状态、平台 helper 与读回 | 音频 ACK/取消/重试、真实 ASR/LLM、结果/claim/receipt、学习 | 接入相同会话与模型调用统计 | 真实服务联调：录音→处理→插入→学习→历史→后台统计 |
| D：故障与发布验证 | 原生目标/权限/休眠/崩溃、两平台安装包 | 故障恢复/保留清理/备份/性能与容量 | 401/403/409/429、未知用量、刷新重登 | 完整用例台账、缺陷复现与修复回归、真实环境验收记录 |

A/B 中前端不等待整个后端完成；先对契约 Mock 开发，API 就绪后逐项切换真实实现。各工作包均包含三端任务；后端骨架通过只是局部检查点，不能当成 S4 交付。依赖关系是“契约先定、模块逐项联调”，不是先做完整后端再开始前端。

## 3. 前端与管理后台落地边界

Desktop 页面、状态、事件由 `desktop-contract.md` 规定。Renderer 仅通过白名单 RPC 访问业务，Main 才持有服务地址和 token；原型直接修改 JS 对象的方式不得迁入正式业务。Native 返回 effect=unknown 时不能通过新的 Mock 成功值掩盖不确定写入。

Admin 采用 React + Vite + TypeScript；布局、字号和颜色沿用本项目原型，基础控件与图标沿用桌面约定，避免再引入一套 UI 风格。建议目录：`src/app/`（入口/路由/会话）、`src/api/`（类型化 client/错误映射）、`src/pages/`（login/users/stats/models）、`src/components/`、`src/test/`。这是文件组织约定，不新增产品功能。

后台部署根为 `/admin/`，采用 hash 路由（`/admin/#/users` 等），静态 base 固定 `/admin/`，避免刷新深层路由依赖服务器重写。API 使用同源 `/api`；开发代理到 `http://127.0.0.1:17863`。后台 token 仅页面内存，刷新重新登录；不使用 localStorage/URL/token cookie。401 尝试既定 refresh 流程，失效清空会话；403 拒绝访问并展示原因，不能只靠隐藏按钮保护接口。未授权不能先显示旧列表或用户正文。

列表遵循游标分页，切换筛选重置 cursor，网络错误保留可见上下文并明确失败；Mutation 等待真实响应再读回，不显示乐观成功。关闭弹窗或离开页面后迟到响应不回填到另一账号；请求取消和账号 epoch 同时校验。所有用户可见文案使用 zh-CN/en 资源键；原型只展示主要英文布局样例，完整译文与截断检查在 S4 执行 TC-SET-02。

## 4. Mock 设计与切换

1. **同一边界替换 transport**：Desktop 在 Main 的 HTTP/WS adapter 后注入 Mock，Renderer 仍走原 RPC；native helper 另有测试替身。Admin 注入相同 DTO 的 API client。组件中不写 `if(mock)` 业务分支。
2. **仅开发/测试显式启用**：测试入口或开发构建选择 Mock；发行构建不能启用 Mock transport/native，也不打包演示凭据。开发窗口明确显示 Mock 标记。服务连接失败不能自动回退 Mock 并伪报成功。
3. **契约统一**：按 API/WS 字段创建固定 JSON 和事件序列，运行 schema 校验；实现后生成 OpenAPI/共享 TS 类型并对照文档，不能以生成物反向覆盖已定规则。未给出 schema 的“方便字段”不能由某一端私增。
4. **状态显式控制**：使用测试时钟与场景参数覆盖加载/空/错误、401/403/409/429、ACK 丢失、取消竞争、结果过期、用量 unknown。身份/result_id/attempt_no/version 必须真实变化；同一测试重复事件不加统计、不重复写入。
5. **固定数据**：账号 F-ACCOUNT、音频 A10/A30/A120、整理 T01～T15、学习 F-LEARN、CSV F-CSV、插入 F-TARGET、统计 F-STAT、调用 F-USAGE 均来自测试夹具。原型的 8 词初始库与混合 CSV 只用于页面演示；应用测试必须重置为 F-CSV 的规定基线。
6. **切换验收**：只换 adapter，页面和 store 不重写；逐 API 对照请求、响应、错误码、DB 读回及跨端显示。录音/权限/插入最终必须接真实 OS 与模型；原型截图、Mock 波形、Mock “已输入”都不能用于证明原生成功。

永久保留在 API 为 `null`，原型下拉内部的 `0` 仅为 HTML 值；正式 mapper 必须转成 `null`。服务端负责完整 Unicode NFKC+casefold；前端即时校验不替代最终唯一性约束。前端展示率为百分比、API 为 0–1；时间 ms 与秒/分钟之间显式转换。

## 5. 本地到真实环境的验证顺序

1. 三端编译/类型检查 → 单测；先检查各工程测试脚本与锁文件，记录实际运行命令，不提前声称不存在的 npm/pytest 命令成功。
2. Desktop/Admin 的 Mock 页面交互：所有原型主路径、错误恢复、键盘焦点与禁用态；断言状态与副作用，不只快照截图。
3. 真 PostgreSQL 迁移与 API 集成：空库/再次升级/约束、账号隔离、数据读回；provider stub 仅控制异常和竞态。
4. 同时启动真实 Server/Desktop/Admin，完成端到端账号、数据、录音与用量联调，确认后台统计与实际会话一致。独立数据库/凭据/临时目录；先核查本机 17863 占用，不能终止无关旧服务。
5. 真实模型、麦克风、macOS/Windows 基线输入框与打包安装测试；采集 A10/A30/A120 音频 SHA，填硬件/模型/网络清单，执行性能、容量、隐私清理和备份恢复。
6. 对照 219 个 ID 填实际结果与证据；失败先复现、定位、修复，再回归相关路径；全部必需用例和平台矩阵通过才登记 S5 完成。

Windows、Linux 部署资源、模型凭据或真实音频缺失只可标 `blocked/not_run`，不能减测试数、用复制降级放宽基线控件或用 Mac 代替 Windows。当前这些是执行资源前置，设计与交接已明确；运行参数在首次环境验收记录中冻结。

## v0.3 容量开发增补

- 本轮目标20个不同账号同时处理，后台运行值可调；[并发设计](concurrency-target.md)是接纳/调度/内存/扩展的统一入口。不能复用旧单用户代码的全局音频缓冲、单一current_session或全局prompt。
- Server在首个骨架里实现容量表/CAS、活动部分索引、短事务接纳与PCM预留、provider有界调度接口；实际模型可先Mock，必须能制造多用户交错/取消/同时end。
- Admin增加`#/admin-capacity`对应容量页面：GET/PATCH、输入校验、降容确认、冲突/保存结果待确认、5秒轮询及过期指标、未验证报告。生产管理Web的具体hash可沿用该标识；原型路由同名。只有会话运行上限可改，不改模型/密钥。
- Desktop读取capabilities最新运行值，429保留明确繁忙提示，不轮询自动创建、不产生多个录音；管理员降容不终止已经开始的录音。
- Mock必须覆盖20个账号隔离、版本CAS、负载指标null/0区别；真实阶段按F-CAPACITY从1到20逐档执行，再迁服务器扩容。Mock20连接不算20路真实推理通过。
