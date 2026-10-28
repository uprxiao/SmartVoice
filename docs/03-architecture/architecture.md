# 架构设计（v0.3 · 方案 B）

2026-10-17 按用户授权细化。本文定义实现方案，不代表已开发或实测。需求与边界见 [PRD](../02-prd/PRD.md) 和 [业务规则](../02-prd/business-rules.md)。本轮补齐原型与开发前文档，S4 尚未开始。

## 1. 部署与职责

```text
Desktop：Main + Preload + Renderer + Native Helper
   │ HTTPS /api/...；WSS /ws/v1/...（音频）
   ▼
Nginx（公司网络 TLS，保留原路径）
   ├ /api、/ws → FastAPI / Uvicorn 单进程
   └ /admin    → React 管理后台静态文件
FastAPI → PostgreSQL（账号、会话文本/元数据、词库、调用与统计）
        → ASR provider（本机/内网；会话级增量识别）
        → LLM provider（本机/内网/公网；整理、学习分类）
```

| 层 | 职责与边界 |
|---|---|
| Desktop Main | token、安全存储、HTTP/WS、会话编排、快捷键、HUD 窗口、插入执行标记、原生桥 |
| Desktop Preload | typed RPC 薄桥、参数校验；不暴露通用文件/网络/凭证 API |
| Desktop Renderer | React 页面、MobX 展示状态、AudioWorklet 采集/重采样；不直连服务端、不持有 token |
| Native Helper | 平台权限、捕获/复核快照、插入并读回、有限修改观察、剪贴板保护 |
| Server | 认证/租户隔离、会话状态与取消、ASR/整理流水线、词库/学习、正文保留、统计 |
| Provider | 封装模型协议、能力、生命周期、错误和用量；不包含用户账号/历史业务 |
| Admin | 同一账号体系下的管理员入口，仅用户/使用频率/模型用量；不提供用户正文浏览 |

词库与历史由服务端按账号提供，同账号重新请求可见同一份数据；V1 不另做实时跨端同步、离线编辑或第三方云服务。音频不进 PostgreSQL；处理成功立即释放，完整 ASR 失败音频仅内存短暂重试。数据生命周期见 BR-02/05。

## 2. 服务端工程边界

Python 3.11+、FastAPI、Pydantic v2、SQLAlchemy、PostgreSQL 16、Alembic；asyncio 提供网络编排，阻塞推理在 provider 工作线程/子进程中执行，不阻塞 WebSocket 事件循环。数据库会话按请求/事务创建，不把同一 AsyncSession 跨并发任务共享。

```text
server/
  app/
    api/          auth, users, capabilities, sessions, dictionary, learning, stats, admin
    core/         config, db, security, limits, logging, lifecycle
    schemas/      请求/响应/事件类型与错误码
    models/       SQLAlchemy 模型与约束
    repositories/ 按 user_id 访问与行锁
    providers/    asr, llm：协议与独立适配器
    services/     sessions, refiner, dictionary, learning, statistics, retention
    main.py       应用装配、启动/停机
  migrations/     Alembic 唯一迁移入口
  tests/          按 S3 编写，单测/协议/真实集成分层
```

目录为未来落地约定，本轮不生成工程文件。配置加载顺序、启动命令、管理员初始化与迁移核对见 [部署设计](deployment.md)。ORM 与迁移以 [数据模型](data-model.md) 为准，接口以 [API 契约](api-contract.md) 为准。

## 3. 关键流程

### 3.1 登录与权限

桌面安装随机 ID → 登录创建登录会话 sid → access/refresh 仅由 Main 持有 → HTTP/WS 校验 sid、账号状态和租户 → access 过期凭独立 refresh 接口刷新。退出当前 sid；管理员禁用撤销全部 sid。Admin 使用同账号体系，页面内存持有 token，页面刷新后重新登录。首次管理员与密码恢复由运维命令处理，不增加 Web 功能。

### 3.2 口述

```text
快捷键 → 捕获本机目标 → capabilities → 创建 session（容量/账号检查）
→ WS ready → 采集并逐帧上传 → 校验/ACK → 增量 ASR → stable/provisional
→ 用户停止/180秒上限 → 排空尾帧 → audio.end → end ACK
→ ASR final → 释放音频 → 词库提示、整理与程序校验 → result 持久化
→ result.ready 或断线后 GET session → 原目标复核或结果确认
→ insertion claim → 本机执行标记落盘 → native 插入/读回 → receipt
→ 若可观察，有限纠错观察 → 分类/建议/词库写入 → 可撤销
```

会话控制命令、转换、ACK、超时和迟到结果规则见 [会话协议](session-protocol.md)。处理 state、result 版本、insertion 状态分开保存；客户端状态只是这些实体的界面映射。

### 3.3 失败恢复

网络断开时 audio.end 的接收状态未知：不补传，先 GET 查询 audio_complete；确认为 false 才按不完整音频失败并提示重新录音，true 则允许查询处理结果。连接恢复后得到的结果仅展示/复制，或由用户重新指定目标，不自动插入。ASR 失败且完整内存音频尚在：显式重识别；LLM 失败且原文尚在：显式重整理；插入失败：复用已有结果，不重新识别。服务端重启不承诺音频恢复，DB 中可用原文仍可用于重新整理。

cancel 与最终结果提交在 session 行锁/版本条件下竞争；cancel 先提交就禁止写入迟到结果。每个结果最多一个有效插入资格；插入不确定禁止自动重做。不得用 HTTP 回执成功替代目标控件读回验证。

### 3.4 整理与学习

以验证过的改口/删减证据生成有效原文，再保护仍有效的数字、路径、专名、否定、条件和理由；程序检测到越界或模型故障时回退原文并要求确认；取消第二次LLM审核。2026-10-19 用户修订词库口径：唯一有效别名可在同句多处纠正，不再强制专名/普通词义消歧；词条身份、完整词边界及其他内容保护保留，见 BR-08。当前实现需按新口径回归后才能宣称达标。修改学习只读本次插入片段，Unicode 码点坐标；整段改写/其他控件/超过观察窗口不学习。

模型具体协议与返回类型见 [Provider 设计](provider-config.md)，规则见 BR-06/08/09。保守回退优先于生成看似流畅却改意的文字；需要真实固定样本验证，不能据提示词文本宣称质量达标。

## 4. 数据一致性

- 所有用户从属资源读写带 user_id；客户端传入的 ID 只用于定位，归属以认证主体为准。管理员聚合是显式授权路径，不复用无租户的普通查询。
- session/request 幂等、结果替换、插入资格、词库乐观锁、学习确认/撤销均有数据库约束和事务，不仅靠内存变量。
- 会话统计按稳定 session 身份计算贡献，重试更新贡献而不新增录音次数；插入和学习迟到回执更新相应维度，重复事件不累计。
- 每次真实 provider 调用单独记录用量，包括分类/重试及历史真实审核用量；unknown token 保留未知状态。按日/模型聚合可回算。
- 正文清理不删必要统计事实。默认永久或 7/30/90 天；到期不可读，后台 1 小时内物理清除正文；词库独立保存。

## 5. 并发与运行

当前目标20个活动会话，后台可调且允许未来服务器配置高于20；每账号最多一个活动会话。一个异步Uvicorn协调器负责API/WS与事务，阻塞ASR放在独立执行池/获准内网provider，LLM采用独立有界队列。接纳槽、模型执行槽、内存预算分别控制，详见[并发与容量设计](concurrency-target.md)。无接纳槽立即429；模型内部有限等待计入总deadline。配置CAS、原子接纳、3:1公平调度、取消清理和验证容量均有明确契约。多API副本须先完成owner/租约/栅栏/路由演进设计，当前仍拒绝多worker/replica；模型池可独立扩展。

DB 连接/认证服务健康与模型能力分开暴露；ASR 未就绪不可开始录音，LLM 不可用时允许完成 ASR 并回退确认。启动不自动下载模型，校验模型路径/摘要与配置。外部模型错误、预算耗尽和取消都有明确终态；临时 provider 文件必须在 finally 与启动清理中释放。

## 6. 桌面工程

沿用 SmartDD：electron-vite、React 19、TypeScript、Tailwind CSS 4、Base UI、lucide-react、MobX、zod、typed RPC、vitest、electron-builder；别名 @shared/@main/@renderer。平台代码只在 desktop/native，Main 适配一致的 helper 契约。

音频 Renderer 保持 `backgroundThrottling: false`，HUD 非激活；Main 控制录音会话 ID 和背压。权限拒绝、设备丢失、休眠、退出都必须停止相关会话，不把下一轮音频/结果绑定到上一轮。具体契约见 [桌面设计](desktop-contract.md)。

开发服务根地址 `http://127.0.0.1:17863` 集中在 `desktop/src/shared/config.ts`；部署构建替换根地址，接口路径相同，远程使用 HTTPS/WSS。设置页只显示服务状态和地址，不提供运行时编辑器。

## 7. 复用与验证

[复用核验表](reuse-map.md) 记录来源、文件哈希、已有测试与差异。已有工程架构可以沿用，不能据“存在源文件”认定其跨平台、多租户或模型质量已通过。特别是自有源后端 SQLite/本地模型约束需要改造，Windows 原生输入需要实现与实测。

验收以 [测试用例](../04-test/test-cases.md) 为准；部署硬件/模型和真实录音/Windows 环境作为执行前置留在文档索引 P1/P2。不得因此降低性能目标或把未执行项记为通过。


## 8. 三端开发交接

开发包含 Desktop + Server + Admin；按同一契约先完成前端 Mock，再逐模块接真实服务，最终进行完整本地联调和两平台验收。具体工程组织、Mock 注入边界、数据夹具和测试顺序见 [三端交接](fullstack-handoff.md)。服务骨架通过只代表局部完成。
