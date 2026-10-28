2026-10-23 R2增量见[R2正式设计](r2-production-design-20261023.md)：外部事件与结果字段沿用，Qwen稳定partial/尾帧及完整正文结束后才提交；取消/旧attempt/最新光标规则不变。

# 会话、音频与副作用协议（v0.3）

2026-10-17。依据 [业务规则 BR-04～08](../02-prd/business-rules.md)。本文件定义预期实现；尚未开发或通过真实环境验收。

## 1. 所有权、容量与时钟

- V1 一个异步 Uvicorn 协调器、一个服务实例；初始20个录音/处理槽，管理员持久化配置并可按部署资源提高，接纳不排队。槽从创建事务成功持续到本 attempt 进入终态；重试重新申请槽。占满返回 `429 CAPACITY_EXCEEDED`，不创建会话或消耗幂等键。每账号最多一个 `created/streaming/finalizing/refining` 会话，违反返回 `409 ACTIVE_SESSION_EXISTS`。
- 数据库是会话/结果/插入资格的权威；进程内存只持音频、固定词库快照、任务句柄与运行指标；接纳权威为数据库容量配置及活动会话查询，按容量行锁原子申请。启动时用 PostgreSQL 会话级 advisory lock 保证实例唯一，失去数据库连接/该锁即停止接单并取消运行任务；禁止多个 worker/replica。
- `attempt_no` 从 1 开始。任务提交任何结果时必须同时匹配 `user_id/session_id/current_attempt_no/state`；更新影响 0 行代表已取消或已被替代，丢弃回调。不可用内存中的“仍在运行”覆盖数据库状态。
- HTTP/WS 事件时间用 UTC；持续时间/截止检查用服务端单调时钟。`Result.attempt_kind=initial`时，`stop_to_result_ms`从服务端接受有效audio.end到ready事务提交，包含本attempt内收尾/自动重试；attempt_kind=asr/refine时从新attempt.started_at到ready提交，仅是本次重试处理耗时，不计入首次停止到结果的性能分位数；客户端“停止到插入”另以客户端单调时钟测量，不用两台机器时钟相减。

## 2. 状态转换

| 当前状态 | 事件 / 条件 | 下一状态 / 持久化动作 |
|---|---|---|
| 无 | 创建成功、占槽、保存词库快照版本 | created，创建 attempt 1 |
| created | 唯一 WS 鉴权成功并发 ready | streaming |
| created | 创建后 10 秒无有效 WS | failed，`CONNECT_TIMEOUT`，释放槽 |
| streaming | 合法帧 | 累加 received_samples；首次有效样本登记统计身份 |
| streaming | 有效 audio.end 且 total_samples=0 | empty，不调用整理 |
| streaming | 有效非空 audio.end | finalizing，记录 end_accepted_at / audio_complete=true |
| streaming | 10 秒没有合法音频/控制消息；断线；协议错误 | failed，`AUDIO_IDLE_TIMEOUT/AUDIO_INTERRUPTED/AUDIO_PROTOCOL_ERROR`，完整性 false，释放音频 |
| finalizing/refining | end后收到音频或不同的audio.end，且ready尚未提交 | failed，AUDIO_PROTOCOL_ERROR；取消任务并释放缓存/工作副本，禁止迟到ready |
| finalizing | ASR 返回非空 final | refining；保存 attempt.raw_text，立即释放音频 |
| finalizing | ASR 返回无语音 | empty，释放音频 |
| finalizing | ASR 失败/超时且有完整音频 | failed，按第 5 节开放 asr 重试 |
| refining | 整理与程序保护通过 | ready，新结果 review_required=false |
| refining | 整理或程序保护不可用/失败 | ready，refined_text=raw_text、review_required=true，reason 非空 |
| created/streaming/finalizing/refining | cancel 在结果提交前取得行锁 | cancelled，取消 provider、清空音频/正文、释放槽 |
| ready/empty/failed/cancelled | cancel | cancelled 原状态返回 200；其余返回 409 `SESSION_TERMINAL` |
| failed | 显式 asr retry 且缓存、容量有效 | finalizing，新 attempt；不重新接收音频 |
| ready | 显式 refine retry 且允许、容量有效 | refining，新 attempt；复用本会话 raw_text |

`ready/empty/failed/cancelled` 为本次处理终态。ready 可因用户明确重试进入新 attempt，不代表已有结果可在重试中插入。插入状态独立存储，后端不存在 `inserted` 会话态。

第一次进入任一终态时写 `first_terminal_at`，后续重试不改；正文保留时限以它为准。cancel 与 ready 使用同一 session 行锁/CAS：先成功提交的操作生效；若取消先提交则不产生 result.ready，若 ready 先提交则取消返回 409。客户端已关闭显示时，迟到 ready 可保存历史，不自动插入。

## 3. WebSocket 和帧

路径 `/ws/v1/sessions/{session_id}/audio`。仅 Desktop Main 使用支持自定义头的 WS 客户端，传 `Authorization: Bearer …`，不把 token 放 URL、子协议或 Renderer。握手校验 active 用户、登录 sid、会话归属、desktop 类型和 device_id 与创建时一致。一个会话仅一条连接；未成功握手前可重连，进入 streaming 后不得接续/补传。跨租户一律 404；第二连接 409。

- Desktop 无 Origin 可接受；若提供 Origin，必须命中配置的精确允许列表，否则握手403 FORBIDDEN。Admin Web 不连接音频 WS。每 5 秒至少检查一次当前登录会话及账号，撤销/禁用则取消未完成任务并关闭；活动 WS 的 access 到期也关闭，Main 开始录音前必须确保 access 剩余有效期大于 240 秒，否则先 refresh。
- `ready`：`{type,session_id,attempt_no,audio:{encoding:"pcm_s16le",sample_rate_hz:16000,channels:1,chunk_samples:4000,max_samples:2880000}}`。
- 二进制：`uint32 seq LE + uint32 sample_count LE + int16 PCM LE`，seq 从 0 严格连续。正常帧 sample_count=4000；尾帧允许 1–4000，短帧后只能 audio.end/cancel。最大帧 8008 bytes，不允许 0 样本二进制帧。
- 每个合法帧立即累计确认：`{type:"audio.ack",seq,total_samples}`。ACK 仅表示完整校验并接收入内存，不能等待模型解码才发，也不代表磁盘持久化或 ASR 成功。
- Main 维护最老未 ACK 帧时间与未 ACK 样本数。超过 2000 ms，或继续发送将令未 ACK 样本超过 32000 时立即停止采集并终止本轮；不丢帧后继续。ACK 允许累计覆盖，seq/total_samples 不得倒退或超出已发送值。
- 文本消息限 4096 UTF-8 bytes，只接受 audio.end/cancel；JSON 未知字段、未知类型、非法序号、字节长度不符、超过总样本上限均为协议错误。
- 到 2880000 样本自动停止，排空尾帧后发送 `{"type":"audio.end","last_seq":N,"total_samples":M}`；零帧时 last_seq=-1、M=0。服务端必须验证 last_seq 与已收帧一致、样本总数一致，不能凭 audio.end 宣称补齐缺帧。
- 接受后回 `{"type":"audio.end_ack","last_seq":N,"total_samples":M,"attempt_no":1}`。相同end重传仅重发ACK；不同end或end后音频为AUDIO_PROTOCOL_ERROR。若仍finalizing/refining，行锁下取消尚未提交的处理、置failed、清音频/本轮正文并关闭4400，迟到输出CAS失败；若ready已先提交则仅拒绝/关闭，不回滚结果。不启动第二次识别。
- audio.end 已在服务端接受后即不受音频闲置计时影响，断线继续 finalizing/refining；客户端即使未收到 end_ack，也可 GET 查询 `audio_complete`。未接受 end 则断线失败，绝不生成不完整结果。

事件顺序：ready → audio.ack/asr.partial（交错）→ audio.end_ack → asr.final（有语音时）→ result.ready 或 error；空音频/无语音发 session.empty；取消发 cancelled。`asr.partial` 有 `revision≥1, stable_text, provisional_text, decoded_samples, received_samples`；revision 严格递增、stable_text 是上一版本前缀扩展，decoded_samples≤received_samples；只用于展示，不写最终历史。`asr.final` 为 `{type,text,attempt_no}`，text 必须以最新 stable_text 开头；违反时按 provider 失败处理，不悄悄改写已经稳定的文本。

`result.ready` 为 `{type,session_id,attempt_no,result}`，result 与 HTTP 完全同一 schema。终态事件发送后服务端以 1000 关闭；客户端依靠 GET 获取权威状态，不以关闭码推断是否存在结果。

## 4. 错误与关闭码

握手尚未升级时返回 HTTP 错误；升级后发 `{type:"error",error:{code,message,retryable,details},session_id,attempt_no}`，不发送伪 HTTP 响应。details 不含音频、正文或 token。

| 情况 | HTTP（握手/业务 API） | 已升级 WS code / 关闭码 |
|---|---|---|
| 无效/过期/撤销凭证 | 401 AUTH_INVALID | AUTH_INVALID / 4401 |
| 禁用/无权限 | 403 ACCOUNT_DISABLED/FORBIDDEN | ACCOUNT_DISABLED/FORBIDDEN / 4403 |
| 非本账号资源 | 404 NOT_FOUND | 不应已建立连接 |
| 第二连接/状态不允许 | 409 SESSION_STATE_CONFLICT | SESSION_STATE_CONFLICT / 4409 |
| 序号/长度/控制协议错误 | 不适用 | AUDIO_PROTOCOL_ERROR / 4400 |
| WS 消息过大 | 不适用 | MESSAGE_TOO_LARGE / 1009 |
| 最大总时长超发 | 不适用 | AUDIO_LIMIT_EXCEEDED / 4400 |
| 未完成上传闲置/中断 | 不适用 | AUDIO_IDLE_TIMEOUT/AUDIO_INTERRUPTED / 4408（无法发送则直接记库） |
| ASR 失败/超时 | 查询仍返回 200 状态 | ASR_FAILED/ASR_TIMEOUT / 1011 |
| 服务停止/内部故障 | 503 SERVICE_UNAVAILABLE | SERVER_SHUTDOWN/INTERNAL_ERROR / 1011 |

终态之后的非法额外消息只拒绝/关闭连接，不回滚已经提交的结果。错误事件的 retryable 只表示允许查询当前重试能力，不能直接触发客户端自动重录/插入。

## 5. 重试、释放与重启

- 完整音频 ASR 首次失败时设唯一 `retry_expires_at=first_asr_failed_at+600s`，按 session 缓存，不因尝试次数或读取延期；默认最多20份且PCM总bytes≤134217728（128MiB），任一超限淘汰最早到期项（同期限按session_id）。20份最长原始PCM为115.2MB；活动池与worker工作缓冲另计。ASR retry把音频所有权从失败池转入活动池，失败时再转回，不复制一整份长期缓存，不改变原截止时间。
- 没有完整end、无语音、取消、ASR成功、淘汰仍立即释放音频并清理临时工作副本。TTL统一为 `D=first_asr_failed_at+600s`：到D立即拒绝新ASR重试、禁止过期音频继续处理及迟到ASR输出提交；第599秒开始的重试同样失效。入口/派发/提交使用原绝对期限校验，不等待扫描或DB标志；`ASR retry deadline=min(本轮10秒收尾预算,D)`，不延长原30秒处理预算。
- 到D开始取消与回收：本地服务进程的runtime/provider/排队参数及其他PCM引用最迟D+1秒清空；独立worker最迟D+2秒完成terminate/kill并确认退出，两期限并行起算、不相加。执行许可在退出确认前持续占用，不重建同槽worker冒充释放；超期未确认继续隔离并报故障。到期访问guard与定时清理均必需，1秒扫描只是兜底，不是资格延长；回收不等待DB checkout/事务，晚到元数据更新按准确attempt CAS。D前已合法完成ASR并释放音频的持久化raw按原整理规则继续，清理收尾不能产生新ASR结果。
- retry 响应始终返回 `retry_kind: asr|refine|none`、`retry_expires_at`（仅 asr）、`retry_unavailable_reason`。none 原因枚举：`not_needed,incomplete_audio,audio_expired,audio_evicted,server_restarted,cancelled,empty,content_expired,insertion_reserved,already_inserted,insertion_unverified,in_progress`。
- 再次整理只针对模型/保护故障的原文回退结果；不对正常结果提供“多抽一次”。ready→refining 时旧结果保留在数据库供追溯，但会话 state 非 ready，不能 claim；新 ready 原子更新 current_result_id、统计贡献。重试使用创建时词库快照，避免本轮规则漂移。
- `refine` 可由同账号任一有效 desktop 登录发起；新 runtime 的鉴权上下文绑定实际调用者，提交前再次校验该 sid。原 session.device_id/created_login_sid 保留来源；撤销原来源登录不撤销其他有效登录发起的整理。`asr` 重试与音频 WS 仍要求创建设备；插入沿第6节的 claim 设备规则。重放不更换已接纳尝试的授权上下文。
- retry、claim、cancel 都锁定同一 session 行并验证 current_attempt_no/current_result_id；任一 result 有 reserved/inserted/unverified 时拒绝重跑模型，即使该 result 非当前结果。
- 进程重启时所有缓存失效；将遗留 created/streaming/finalizing 标 failed `SERVER_RESTARTED`；refining 若已有持久化 raw_text 则生成待确认原文回退 ready，reason=`server_restarted`，否则 failed。所有占槽清零，失败的 asr 能力为 none/server_restarted。ready 历史保持，不恢复自动插入或旧 WS。首次终态/统计在恢复事务内更新。
- 保留设置到期优先于重试/读取；超期原文不可重整、不可 claim、不可学习或撤销。reserved 尚未收到回执时保留资格，不以超时回收。

## 6. 插入资格与回执

claim/receipt/correction仅desktop；device_id来自当前JWT sid。另一桌面可在同账号下显式retarget后claim，但回执和纠错只允许claim所属设备；同账号设备不匹配403 FORBIDDEN，跨租户404 NOT_FOUND，幂等重放也先校验设备。

1. 正常review_required=false且原目标有效时自动进入插入；仅review_required=true需要用户明确确认后继续。Main 先校验目标，再 POST `.../insertions/claim`，携带 current result_id、text_hash、insertion_id、review_confirmed。服务端行锁下检查状态、结果、正文未过期及资格，写 reserved。创建资格 201；同一 insertion_id 相同参数重放 200 `replayed=true`，客户端只能查询/补交回执，**不能将重放理解为再次调用 native 的许可**。
2. Main 将 session/result/insertion 的执行标记写入本机持久化日志（无正文）并确认落盘，之后最多一次调用 native。只对本机尚未尝试且首次 claim 成功的操作执行；重启遇 reserved/执行标记一律未知，不重新执行。未领取成功的请求也不能先写目标。
3. POST `.../insertions/{insertion_id}/receipt`：inserted 要本机读回相符；failed 要明确零写入；其他 unverified。reserved→三种终态只一次，重复同值返回现状 200，终态换值 409。响应丢失重发原回执，不再次写目标。
4. 同 result 最多一个 reserved/inserted/unverified（数据库部分唯一约束）；failed 释放资格，但只能用户显式指定目标，使用新的 insertion_id 才允许新 claim。`observable=true` 只允许 inserted。复制不是 inserted，也不产生学习观察。
5. 对外永不发送目标原值、选区快照、应用输入框全文。hash 校验对象为 result.refined_text 原样 UTF-8 SHA-256，不 trim、不换行归一化；用户不能用 claim 悄悄改文本。原文可手动复制。

## 7. 多会话资源协调（v0.3）

接纳/create/retry与后台调容量共用数据库容量行锁，原子性、锁序、PCM预留、释放见[并发设计第3节](concurrency-target.md)。create幂等重放不重复占额；降容只挡新任务，不终止在途。CAPACITY_EXCEEDED details.reason为active_limit或audio_budget，Retry-After=1，仅提示用户稍后重试，客户端不自动循环新建。

收到end后继续使用原有30秒预算，finish等待也计入10秒。多会话ASR调度与LLM等待见provider；ASR超时仍ASR_TIMEOUT，资源不可用仍ASR_FAILED；模型队列等待不得改变WS ACK的2秒边界。成功ASR立即释放音频，再等待LLM。取消或TTL到期不仅丢弃回调，还须终止对应任务并确认本地/内网工作副本释放；TTL的即时资格门禁与1秒/2秒回收上限见第5节，其他会话不被误杀。
