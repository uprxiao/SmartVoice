# 快速纠正学习：技术方案（S2，2026-10-28）

状态：**S2 起草，待本人评审**。实现 [S1 需求变更](../02-prd/fast-correction-learning-decision-20261028.md) LR-01～LR-09。依据：B 路线可行性实验（内部记录，未公开）；讨论过程见设计讨论稿（内部记录，未公开）与 `docs/review/review-20261028-*.md`。本文只覆盖 macOS；Windows 维持现有行为，不宣称已实现。

## 1. 架构定案

**观察留在现有插入 helper 进程内，与命令共用主线程，以事件驱动方式运行；主动事件走新增的第 4 个管道（fd 3），stdin/stdout 的数字 id 命令协议不变。**

| 候选 | 结论 | 理由 |
|---|---|---|
| 独立观察进程 | 不采用 | 公开 API 无法跨进程传递 AXUIElement，“同一控件”证明无可靠方案（摘要不能区分同值同光标控件） |
| 同进程、单独工作线程 | 不采用 | 需要为 `targets` 字典与 Target 引入并发同步；NSEvent 监听必须在主线程，仍要改主循环 |
| **同进程、主线程事件循环（采用）** | 采用 | 插入核验得到的 AXUIElement 直接交给观察，无身份交接；命令、AX 通知、按键回调全部在主线程串行执行，无需加锁；所有事件按处理顺序写出，顺序天然一致 |

主线程改造：

- `NSApplication.shared.run()` 成为主循环（激活策略 `.prohibited`，不显示图标、不抢焦点）。
- stdin 读取移到后台线程：每读到一行，`DispatchQueue.main.async` 交主线程执行 `command()`，回写 stdout 不变。命令仍逐条串行执行，现有前台识别等主线程要求保持满足。
- 插入后的 0.5 秒等待保留在主线程。期间到达的 AX 通知与按键回调排队，不丢失；资格判定只用事件自带时间，不受处理延迟影响。观察只在插入完成后才开始，新录音开始即停止旧观察，因此插入与观察不会互相等待。
- 已接受的代价：观察与插入共享进程，观察崩溃会连带插入 helper，按现有“helper 退出 → 在途插入结果未知、不自动重试”处理。观察回调只做读取和输出，不做复杂计算。

## 2. 原生观察协议

### 2.1 命令（stdin/stdout，沿用 id 应答）

| 命令 | 参数 | 应答 |
|---|---|---|
| `observeStart` | `target`（已插入且已核验的 target_id）、`observation`（Main 生成的 UUID）、`quietMs`（M） | `{started:true, generation}` 或 `{started:false, reason}`；成功后开始推送事件 |
| `observeStop` | `observation` | `{stopped:true}`；同步移除 AXObserver 与按键监听后才应答 |
| 现有 `observe`（轮询） | 不变 | 保留给兜底与回退开关 |

同一时刻最多一个观察；新的 `observeStart` 先停止旧观察。`release`、`invalidate`、权限刷新时同样先停止观察。

### 2.2 事件（fd 3，每行一个 JSON）

公共字段：`v`（协议版本 1）、`generation`（helper 进程代次）、`observation`、`seq`（单调递增，从 1 开始）、`t`（`ProcessInfo.systemUptime` 秒；与 `NSEvent.timestamp` 同一时间基，实验中回调时间减事件时间稳定为正的 3–51 ms）。

| type | 额外字段 | 何时产生 |
|---|---|---|
| `key` | `ts`（`NSEvent.timestamp`）、`cat`（char/return/backspace/arrow/space/modifier/other） | 观察期间的全局按键按下；只在观察期间安装监听，停止即移除 |
| `sample` | `start`、`end`（读取起止 uptime）、`valid`、`text`（仅本次片段，规则同现有 `observe`）、`reason`（无效原因）、`comp`（composing/committed/unknown）、`compEvidence`、`source`（当前输入源 ID） | AXValueChanged / AXSelectedTextChanged 通知后读取；另每 300 ms 兜底读取一次 |
| `end` | `reason`（focus_changed / element_invalid / outside_changed / permission / stopped） | 焦点通知、读取报控件失效（-25202）、片段外变化、权限丢失 |
| `error` | `code` | 监听或注册失败（记录能力，不终止插入侧） |

`text` 只含本次插入片段（沿用 `observedScalarFragment` 与“插入长度＋256”上限），不输出整框正文。按键事件不含字符与键码。

### 2.3 Node 侧接收

- `NativeVoiceInput.spawnChild` 改为 `stdio: ['pipe','pipe','pipe','pipe']`，读取 `child.stdio[3]`；新增独立的行解析器，限长 64 KB/行、最多缓存 256 行，超限或解析失败只对当前观察发出 `channel_error` 并停止观察，**不调用会杀掉 helper 的 `fail()`**。
- 校验 `v`、`generation`（与当前子进程一致）、`observation`（与当前观察一致）、`seq` 连续；任何缺口、乱序或旧代次事件使当前观察“失去资格”（不冻结，只结束）。
- stdout 命令应答解析器保持不变，仍拒绝无数字 id 的行。

## 3. 资格判定（Main，纯函数）

在 `correction-eligibility.ts` 实现，输入为本次观察按 `seq` 排序的事件，输出 `{freeze: Replacement} | {skip: reason}`，单元测试覆盖。

**触发点**：`key.cat==='return'`；或 `sample` 显示片段被清空/无效；或 `end`；或空闲兜底（最近一次有效 `sample` 后 15 s 无新事件）；或 60 s 截止。触发时间 `T`：按键用其 `ts`，其余用对应 `sample.start` 或 `end.t`。

**候选**：`T` 之前最近一次满足以下条件的 `sample`：`valid`、`comp !== 'composing'`、片段与插入文本不同、且 `start − 最近一次早于 start 的 key.ts ≥ M`。

**冻结条件（全部满足）**：

1. 存在候选，且 `(候选.start, T)` 区间内没有任何 `key`（触发本身那次回车除外）。
2. 候选之后到 `T` 之间没有其他 `valid` 且内容不同的 `sample`（内容又变了说明不是最终稿）。
3. `singleReplacement(插入文本, 候选.text)` 返回有效替换（现有规则，含保护内容）。
4. `comp==='committed'`，或 `comp==='unknown'` 且条件 1 成立（LR-04：此时选词/上屏都会产生按键，已被条件 1 排除）。
5. 本次观察未失去资格（无通道错误、无序号缺口、身份未变）。

不满足任一条件 → 跳过，原因映射为首页状态（见 §6）。每个观察最多判定冻结一次，判定后立即 `observeStop`。

**M**：初值 150 ms，作为常量集中定义；S3 用 50/100/150/300 ms 对照“补字后多快发送仍能学”与“反例不误学”，按结果定稿，不为补绿调整。

## 4. 冻结任务（FrozenCorrectionJob）

- 由 `SessionService` 在冻结时创建，持有不可变 body（session_id、insertion_id、新的 correction_id、offset/before/after/left/right）、账号上下文（userId、accountEpoch）和**从冻结时刻起 30 s 的绝对期限**；同时最多 3 个，超出时新冻结直接跳过并提示。
- 流程：等待原插入回执确认（复用现有 `receipt()` 共享任务与 journal 状态，不另发请求）→ 提交 correction（失败按现有规则至多重试一次，同一 body 与 correction_id）→ 轮询学习事件至期限。全部等待都按剩余期限截断。
- 与录音操作解耦：不依赖 `operationEpoch`、`progress.result`、`insertionId`；`nextOperation()` 只停止观察，不取消冻结任务。
- 取消：账号切换/退出/失效、关闭学习、应用退出、期限到。辅助功能权限撤销（本人 2026-10-28 确认）：立即停止观察、不再读取目标；已冻结的任务在自身期限内继续提交，不取消。
- 只有服务端 `learned` 才发 `learningLearnedEvent` 并刷新词库与统计；跨账号的迟到结果丢弃。

## 5. Electron / Chromium 输入框可读（LR-07）

- 在 `capture`／`insertCurrent` 取焦点控件前：若焦点读取返回无值（-25212），且前台应用为 Chromium 内核（包内 `Contents/Frameworks` 下存在含 `Helper (Renderer).app` 的框架，Electron 与 Chrome/Edge 等均满足），依次对应用元素设置 `AXManualAccessibility=true`（Electron）与 `AXEnhancedUserInterface=true`（Chromium 浏览器），以 0.2 s 间隔、最多 1.5 s 重试取焦点。实验中 Chrome 对两项设置分别返回“不支持/未实现”但实际已生效，故**以能否取到焦点判断成败，不看返回码**。非 Chromium 应用不设置。
- 每个“进程 ID＋启动时间”只设置一次，记录在 helper 内存；不在退出时恢复为 false。设置失败或超时按现有“未核验”路径处理。
- 首次设置会给该应用的那次插入增加最多 1.5 s 等待（实验实测约 1.2 s），后续无此开销；S3 实测记录。
- 诊断：`capture` 应答新增无正文字段 `readability`（`ok`／`no_focus`／`no_value`／`electron_activated`／`electron_activation_failed`），写入现有本地 trace，不上传正文。

## 6. 页面状态（LR-08）

Main 新增事件 `learningStatusEvent`：`{kind, term?}`，`kind` 取值与原型一致：

| kind | 产生时机 |
|---|---|
| `observing` | `observeStart` 成功 |
| `learned` | 冻结任务收到服务端 learned（附 before→after） |
| `final_unconfirmed` | 触发时不满足冻结条件 1/2/4（改完立即发送或仍在组字） |
| `unreadable` | 插入为未核验，或 `observeStart` 失败 |
| `off` | 学习开关关闭 |

其他跳过（无修改、修改不是词语替换、提交失败等）不改变卡片，沿用现有提示。Renderer 首页学习卡片显示最近一次状态文案（见原型 learn-* 状态），只保存在内存，重启后回到默认文案。

## 7. 不变与兼容

- 插入一次粘贴、读回核验、inserted/observable 判定、回执与 journal、服务端 correction API 与 `correction_guard` 均不变；不新增服务端字段与迁移。
- 回退开关：常量 `FAST_LEARNING_ENABLED`。关闭时走现有 300 ms 轮询＋15 s 静止规则，便于对照与回滚。
- 隐私：按键只在观察窗口内记录时间与类别；片段文本只在 Main 内存，提交仍限现有局部字段；`text` 不写日志。

## 8. 影响文件

| 位置 | 变更 |
|---|---|
| `desktop/native/voice-input.swift` | 主循环改为 NSApplication；stdin 后台线程；`observeStart/observeStop`；AXObserver 与按键监听；fd 3 事件输出；Electron 激活；`readability` |
| `desktop/src/main/core/native-input.ts` | 第 4 管道、事件解析与校验、观察事件订阅接口 |
| `desktop/src/main/core/correction-eligibility.ts`（新） | §3 纯函数 |
| `desktop/src/main/core/correction-observer.ts` | 改为消费事件流、触发判定，保留轮询回退 |
| `desktop/src/main/core/frozen-correction-job.ts`（新） | §4 |
| `desktop/src/main/core/session-service.ts` | 观察启动/停止、冻结任务管理、学习状态事件 |
| `desktop/src/shared/events.ts`、`desktop/src/main/index.ts` | `learningStatusEvent` |
| `desktop/src/renderer/views/HomeView.tsx`、`lib/i18n.ts` | 学习卡片状态文案 |

## 9. 风险与验证要点（交 S3）

- M 的取舍（漏学边界与误学）、Electron 首次激活延迟、主线程排队对插入耗时的影响、fd 3 背压与序号缺口、新录音紧跟回车时的冻结顺序、权限撤销、长时间运行资源释放。
- 实验未覆盖的目标（Chrome 补字场景、微信、搜狗/微信输入法、正式安装包子进程身份下的按键监听）在 S3 列为必测或标明缺环境。
