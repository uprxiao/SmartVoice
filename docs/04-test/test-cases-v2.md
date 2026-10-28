# SmartVoiceInput V2 测试用例：云端 ASR 与 Provider 管理（S3）

> 2026-10-27。**状态：S3 本人 2026-10-27 评审通过；全部用例未执行。** 依据：[PRD-v2](../02-prd/PRD-v2.md)（S1 已通过）、[V2 技术设计第 2 版](../03-architecture/v2-provider-design-20261027.md)（S2 本人 2026-10-27 评审通过；§8.4 错误码为编写本文时补充）、模型配置页原型（内部记录，未公开）。
> V1 的 [test-cases.md](test-cases.md) 与执行台账保持不动；本文只覆盖 V2 新增/变更。V1 用例在 V2 部署上的回归范围见 §11。

> 2026-10-28 修订：本人决定 E2E-07 用量口径为“失败调用的音频计入”（与云端计费一致）；E2E-02 未达 p50 ≤2 s 记为已知限制（判据不改）；DEP-04 网络层出站白名单记为已知限制、只保留应用层白名单（判据不改）。本人决定 T12（见 [README](../README.md) 待决问题），RT-10 判据由“重试 ASR ≤25 s”改为“按重送量放宽的截止内完成”，RT-13 补充重试规则。修订原因：120 s 录音第 70 s 起云端不可用时需重送 85 s，按设计 2× 限速至少 42.5 s，原判据不可达。

> 2026-10-28 后续本人验收确认：在得知 E2E-02 的实际时延、真人/实际插入计时覆盖缺项，以及 DEP-04 只保留应用层白名单后，本人明确“这两项没有问题……确认这个目前验收通过了”。两项本轮按 **pass（本人接受已知限制）** 关闭，原判据、失败数据和未测部分保留。该例外由本人决定，不作为重新测得达标；不扩大到其他缺陷或推送/合并/发版授权。见 确认记录（内部记录，未公开）。

## 0. 执行与判定约定

- **编号**：`TC-V2-<组>-<序号>`；组：PROV（云端 provider）、RT（会话运行时与数据）、CFG（配置、加密、白名单）、PAPI（管理 API）、PUI（后台界面）、LLM（整理模型热切换）、DESK（桌面端）、DEP（Linux 部署）、E2E（真实环境端到端与非功能）。
- **环境分层**（不可互相替代）：
  - **M（模拟）**：使用设计 §5 的云端模拟服务 `mock_bailian_server` 与可注入时钟；用于协议、分段、限速、断线、错误注入等可控场景。
  - **I（集成）**：真实 PostgreSQL + 真实 FastAPI/ASGI + 模拟云端；用于数据、迁移、API、并发与租约。
  - **R（真实）**：真实百炼云端 + 官方 DeepSeek + Linux 服务器 + 桌面安装包；只在 S5 执行，凭据为本人提供的账号。模拟通过不代表真实通过。
- **数据**：全部虚构。云端/LLM Key 在 M/I 层使用假值 `sk-test-<随机>`；主密钥每次测试随机生成 32 字节；R 层真实 Key 只从环境注入，不写入用例记录、日志或截图。音频夹具见 §0.1；真人录音只用于 R 层且须本人授权。
- **精确判定**：HTTP 状态、错误码、`details.reason` 以设计 §8.4 和 [API 契约](../03-architecture/api-contract.md) 为准，必须精确匹配；不能用“失败即可”放宽。API 成功须再读回持久数据；“未保存/原配置不变”须读回数据库与 `GET` 结果核对。
- **隐私核查**：凡涉及日志、审计、错误体的用例，都要检索测试期间产生的日志、审计行、HTTP 响应与 WS 事件，确认不含 Key 明文/末 4 位以外部分、完整业务空间 ID、音频、转写正文。
- **记录**：每条记录环境层（M/I/R）、commit、配置摘要、夹具版本、步骤、实际结果、证据路径、pass/fail/blocked；结果写入 执行台账（内部记录，未公开） 的 V2 段。未执行、缺环境均不等于通过（AGENTS §6）。
- **数字常量**（来自设计 §3.1、§4）：分段目标 30 s、窗口 ±5 s、段内最短 10 s、安静阈值 0.15；补送倍速 2.0（块间隔 0.125 s）；内部重连上限 8；云端录音上限 360 s = 5,760,000 样本，本地 180 s = 2,880,000 样本；每会话 PCM 预留 360 × 32,000 = 11,520,000 字节；PCM 预算 1,073,741,824 字节；云端 finish 截止 = `end_accepted_at + min(25 s, max(10 s, drain_estimate_s + 5 s))`。

### 0.1 音频与模拟夹具

| ID | 内容 | 用途 |
|---|---|---|
| F-V2-SIL | 1 s 全零 PCM | 连通测试、无语音 |
| F-V2-TONE-N | N 秒合成语音段 + 0.6 s 静音交替（脚本生成，固定种子），用于制造已知切点 | 分段位置精确断言 |
| F-V2-FLOW-N | N 秒无静音的连续合成信号（RMS 始终高于阈值） | 硬切兜底 |
| F-V2-TTS-A/B | 实验批次 05/10 的合成语音：`experiments/bailian_seg_20261027/tts_script.txt`，`say -v Tingting -r 200`（A，有停顿）与 `-r 260` 去标点（B，几乎无停顿）；参考文字即脚本 | R 层质量；M 层长时序 |
| F-V2-MOCK | 模拟服务可配置：每次 commit 返回 `completed`（文字 = `seg<i>:<样本区间>`，便于核对拼接与重送）；`text`/`stash` 中间事件；握手 401/429；第 t 秒重置连接；发送关闭帧 1001；错误事件 `InternalError`/`invalid_request_error`；**收到的音频速度在任意 2 s 窗口超过 3× 实时则丢弃超额部分并拒绝下一次 commit**（复现批次 08）；每连接记录收到的样本区间与到达时间 | M/I 层 |
| F-V2-KEYS | `SVI_SECRET_KEYS={"k1":<32B>,"k2":<32B>}`、`SVI_SECRET_ACTIVE_KEY_ID=k1`；另组缺失/不匹配 | 加密与回落 |
| F-V2-ALLOW | `egress_policy=allowlist`，`egress_allowlist=["*.maas.aliyuncs.com","api.deepseek.com"]`；M/I 层用测试 DNS 解析器把这些主机解析到模拟服务（公网段测试地址），另配一组解析到 10.0.0.0/8 与 127.0.0.1 | 白名单 |

## 1. 云端 ASR provider（PROV）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-PROV-01 | 设计 §3.2 Segmenter；FR-02-10 | M；F-V2-TONE-75（静音起点位于 27.0 s、33.0 s、58.0 s 等已知位置） | 按真实速度 feed 后 finish | 第 1 次 commit 的切点落在 25 s 后**第一个**安静块（27.0–27.25 s）的结束处，即 27.25 s ±0.25 s，不等待到 35 s；第 2 段同理；每段 ≥10 s；commit 数与 completed 数相等；`AsrFinal.segments` 边界与 commit 位置一致 |
| TC-V2-PROV-02 | 设计 §3.2 硬切兜底 | M；F-V2-FLOW-100 | 同上 | 每段在目标+5 s（35 s）硬切；段长 35 s±0.25 s；不暂存音频（每块在其实时到达时刻 ±50 ms 内已发出） |
| TC-V2-PROV-03 | 设计 §3.2 段内最短 | M；静音位于 3 s、8 s、26 s | 同上 | 3 s、8 s 处不切；首个切点在 26 s 处之后 |
| TC-V2-PROV-04 | 设计 §3.3 partials；运行时校验 `session_runtime.py:935` | M；模拟服务对当前段先发 `text` 增长、再发变短的 `text`（修订）与 `stash` | 收集全部 `AsrPartial` | `stable_text` 只在某段 `completed` 时增长、前缀单调；当前段文字只出现在 `provisional_text`，可变短；`revision` 严格递增；`decoded_samples` 单调且 ≤ `received_samples` ≤ 已 feed 样本 |
| TC-V2-PROV-05 | 设计 §3.3 finish；`session_runtime.py:1378` | M | finish | `AsrFinal.text` = 各段 `completed` 文字按序拼接，以最后一次 `stable_text` 为前缀；`no_speech=false`；`processed_samples` = feed 总样本 |
| TC-V2-PROV-06 | 设计 §3.4 | M；F-V2-SIL ×12 s，模拟服务对每段返回空 transcript | finish | `no_speech=true`；运行时进入 empty，不调用整理（核对 LLM 模拟调用次数=0） |
| TC-V2-PROV-07 | 设计 §3.2 SendQueue；BR-13.3 补送限速 | M；在第 20 s 让模拟服务断开，第 20.4 s 可重连 | 运行至结束 | 重连后落后块发送间隔 ≥0.125 s（容差 −10 ms），任意 2 s 窗口平均速度 ≤2.05× 实时；追上实时后恢复按到达即发；模拟服务未触发“超速丢弃/拒绝 commit” |
| TC-V2-PROV-08 | 批次 08 对照（负控） | M；把 `catchup_rate` 临时设为不限速（仅测试注入，非配置值） | 同 PROV-07 | 模拟服务触发超速丢弃与 commit 拒绝；provider 把 `invalid_request_error` 映射为 `bad_request` 并记录告警——证明 PROV-07 的限速确有作用 |
| TC-V2-PROV-09 | 设计 §3.2 Recovery；BR-13.3 | M；第 8 段 commit 后 0.05 s 断开（已提交未返回） | 运行至结束 | 新连接从第 8 段起点重送（模拟服务记录的首个样本 = 第 8 段 `start_sample`）；第 1–7 段文字保留且不重复；最终 20/20 段；`stable_text` 全程不回退；会话不被运行时判失败 |
| TC-V2-PROV-10 | 设计 §3.2；批次 10 | M；第 400 s 与第 900 s 各发送一次关闭帧 1001（`going away`），音频 1200 s（仅测 provider，不经运行时上限） | 运行至结束 | 两次均识别为 `unavailable` 并内部恢复；全部段完成；`reconnects=2` |
| TC-V2-PROV-11 | 设计 §3.2 重连上限 | M；模拟服务在第 10 s 后拒绝所有新连接 | 运行 | 重连尝试 8 次后对外抛 `ProviderError(code='unavailable', retryable=True)`，携带 `resume`，其中只含已完成段（边界与文字）；不再继续尝试 |
| TC-V2-PROV-12 | 设计 §3.5 错误映射 | M；分别注入握手 401、握手 429、`InternalError` 事件、`invalid_request_error` 事件、finish 超时 | 逐组运行 | 依次得到 `auth_failed`（不可重试，不内部重连）、`rate_limited`（可重试）、`unavailable`（先内部恢复）、`bad_request`（不可重试，告警）、`timeout`（可重试）；错误对象与日志不含 Key、完整 URL、云端原始报文 |
| TC-V2-PROV-13 | 设计 §3.3 cancel；BR-05 类比 | M；会话进行中 | cancel 两次 | 幂等；连接关闭（模拟服务记录关闭）在 2 s 内确认；缓冲与已完成段文字清空；并发名额释放（随后可立即新开一个占满上限的会话） |
| TC-V2-PROV-14 | 设计 §3.3 open；FR-02-11；BR-13.2 | M；`hint_terms=["React","Docker"]` | 打开会话并运行 | 模拟服务收到的 `session.update` 不含 `corpus` 或任何词表字段；`capabilities.supports_hints=false` |
| TC-V2-PROV-15 | 设计 §3.1 并发名额；§8.4 | M；`max_concurrent_sessions=2` | 同时开 3 个会话 | 第 3 个 `open` 抛 `ProviderError('busy', retryable=True)`；释放 1 个后可再开 |
| TC-V2-PROV-16 | 设计 §3.1 `use_env_proxy=false` | M；环境设置 `HTTPS_PROXY`/`ALL_PROXY` 指向一个记录连接的假代理 | 打开会话 | 假代理收到 0 个连接；直连模拟服务成功 |
| TC-V2-PROV-17 | 设计 §6.3 建连再校验与 IP 固定 | M；F-V2-ALLOW；校验后把同一主机的解析结果改为 10.0.0.5 | 建连 | 使用校验时的 IP 建连（不重新解析），SNI/证书校验用原主机名；另组若首次解析即为私网地址，建连前拒绝为 `egress_denied` |
| TC-V2-PROV-18 | 设计 §3.6 日志隐私 | M；运行 PROV-07/09/12 | 检索日志 | 只含 session_id、段序号、耗时、错误码、重连次数、秒数；业务空间段脱敏为 `ws-…`；无音频、无转写、无 Key |

## 2. 会话运行时与数据（RT）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-RT-01 | FR-02-06′；设计 §4.1 | I；当前 ASR=云端（360 s） | 创建会话 | 创建响应 `max_record_seconds=360`；`GET /api/v1/capabilities` 同为 360 |
| TC-V2-RT-02 | FR-02-06′；设计 §4.1 | I；当前 ASR=本地 `qwen3_mlx`（macOS 主机，180 s） | 同上 | 均为 180 |
| TC-V2-RT-03 | 设计 §4.1 会话固定 | I；创建会话（云端 360）后管理员把当前 ASR 切到本地 | 继续录音到 300 s | 本会话仍按 360 s 上限，不被截断；之后新建会话为 180 |
| TC-V2-RT-04 | FR-02-06′；BR-04 上限 | I；云端会话 | 发送恰好 5,760,000 样本；另组再多发 1 帧 | 恰好上限自动收尾进入 finalizing；超发帧按既有协议拒绝（错误事件与关闭码同 V1 超限规则） |
| TC-V2-RT-05 | 设计 §4.1 迁移 | I；空库与已有 V1 数据库各一 | 执行迁移两次 | 约束变为 `received_samples <= 5760000`；第二次执行无变化；读回 `pg_constraint` 定义核对；已有行不变；写入 5,760,001 被数据库拒绝 |
| TC-V2-RT-06 | 设计 §4.2；R3 | I；`active_pcm_budget_bytes=1073741824`，`admission_ceiling=100` | 读 `effective_admission_ceiling` | = 93（1,073,741,824 // 11,520,000）；`admission_ceiling=20` 时为 20 |
| TC-V2-RT-07 | 设计 §4.3 容量与 provider 上限 | I；全局上限 20、云端 `max_concurrent_sessions=8` | 9 个不同用户并发创建 | 8 个 201；第 9 个 429 `CAPACITY_EXCEEDED`、`details.reason=provider_limit`、`Retry-After: 1`；不消耗幂等键 |
| TC-V2-RT-08 | FR-02-11；设计 §4.3 | I；用户词库有 3 个词，当前 ASR=云端 | 完成一次口述 | `sessions.asr_hints` 记录候选词，`asr_hints_applied=false`；整理请求的 `confirmed_terms` 仍包含词库词（核对 LLM 模拟收到的请求） |
| TC-V2-RT-09 | 设计 §4.3、§3.8 | I；PROV-09 场景经运行时 | 完成 | attempt 记录 `asr_provider_id`、`asr_reconnects=1`、`asr_resend_s` > 0；`model_calls` ASR 行含 `provider_config_id`、`segments`、`reconnects` |
| TC-V2-RT-10 | 设计 §3.7 重试只重送未完成段；R2；BR-13.5 | I；120 s 音频，第 70 s 后模拟服务拒绝所有连接 → 会话失败 | 读会话；恢复模拟服务；在 D 前发起 ASR 重试 | 失败后 `error_code=ASR_FAILED`、`retry_kind=asr`；重试时模拟服务收到的首个样本 = 首个未完成段起点，收到样本总数 = 该点到结尾；结果文字 = 原已完成段 + 新段；重试 ASR 截止 = 重送秒数/2 + 10 s（本例 85 s → 52.5 s，不受 25 s 上限约束）、本次尝试整体预算顺延同样时长，并在该截止内完成（2026-10-28 T12 本人决定修订，原“≤25 s”物理上不可达） |
| TC-V2-RT-11 | 设计 §3.6 resume 隐私与 TTL | I；RT-10 失败后不重试，受控时钟推进到 D 与 D+1 s | 检查内存与接口 | 到 D：重试返回 409 `RETRY_UNAVAILABLE`、`details.reason=audio_expired`；≤D+1 s：运行时内存中的 PCM 与 resume 文字均已清空（测试钩子读回）；resume 文字从未写入数据库或日志 |
| TC-V2-RT-12 | 设计 §3.7(5)、§7.4；§8.4 | I；360 s 云端会话失败后，管理员把当前 ASR 切到本地（180 s） | 发起 ASR 重试 | 409 `RETRY_UNAVAILABLE`、`details.reason=provider_changed`；会话 `retry_kind=none`、`retry_unavailable_reason=provider_changed`；另组 120 s 会话切到本地后可整段重送给本地并完成 |
| TC-V2-RT-13 | 设计 §4.4 截止时间 | I；受控时钟；模拟服务在停止前 30 s 断开并延迟恢复，使停止时积压约 30 s | 停止并等待 | 运行时使用的 ASR 截止 = `end_accepted_at + min(25, max(10, 15+5)) = +20 s`；在 20 s 内追完则 ready，否则 `ASR_TIMEOUT`、`retry_kind=asr`；本地 provider 仍为 `end_accepted_at + 10 s`；重试按 RT-10 的放宽截止（T12） |
| TC-V2-RT-14 | 设计 §4.4 配置校验 | I | `finish_timeout_ms=25000` 分别配给云端与本地 provider | 云端接受；本地启动校验拒绝（>10000） |
| TC-V2-RT-15 | V1 回归 | I；当前 ASR=本地 `qwen3_mlx` | 运行 V1 的 VOICE/ASR/HIST 自动化回归 | 与 V1 基线一致；V2 改动不改变本地 provider 行为（180 s、截止时间、partials） |

## 3. 配置、加密与白名单（CFG）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-CFG-01 | FR-11-03；设计 §6.1 | I；F-V2-KEYS | 创建带 Key 的配置后直接查库 | `secret_ciphertext` 不含明文（逐字节检索）；`secret_key_id=k1`；`secret_last4` = 末 4 位；用 k1 解密得原值；把密文换到另一行（附加数据不同）解密失败 |
| TC-V2-CFG-02 | FR-11-03 | I | 遍历全部管理接口响应、审计行、日志 | 只出现 `secret_set` 与末 4 位；无明文 |
| TC-V2-CFG-03 | 设计 §13 T7 轮换 | I；k1 加密的 3 条配置 | 执行 `svi secrets rotate` 切到 k2，再移除 k1 | 3 条均为 `secret_key_id=k2` 且可解密；中途失败（注入第 2 条写失败）时已改的回滚、全部仍可用 k1 解密 |
| TC-V2-CFG-04 | FR-11-10；设计 §7.2 | I；库内当前 ASR 为云端，启动时 `SVI_SECRET_KEYS` 缺失 | 启动服务 | 回落 YAML 的 ASR；`GET /api/admin/providers/active` 显示来源 `yaml` 与原因（解密失败）；健康检查同样显示；不使用 mock |
| TC-V2-CFG-05 | FR-11-10 | I；库内配置无效且 YAML 也无可用 ASR，`environment=production` | 启动并创建会话 | 创建会话 503 `SERVICE_UNAVAILABLE`、`details.reason=asr_unavailable`；不静默使用 mock；`environment=development` 且显式配置 mock 时才允许 mock |
| TC-V2-CFG-06 | FR-11-04；设计 §6.3 | I；F-V2-ALLOW | 分别保存：`wss://ws-x.cn-beijing.maas.aliyuncs.com`、`wss://maas.aliyuncs.com`、`wss://evilmaas.aliyuncs.com`、`wss://api.deepseek.com.attacker.io`、`https://api.deepseek.com`、`https://user:pw@api.deepseek.com`、`wss://1.2.3.4`、`ws://ws-x.cn-beijing.maas.aliyuncs.com`（ASR） | 第 1、5 条通过地址校验；`maas.aliyuncs.com` 本身不匹配 `*.`（拒绝）；其余拒绝，均为 422 `VALIDATION_ERROR`、`details.reason=egress_denied`、`fields=["base_url"]`；数据库无新增行 |
| TC-V2-CFG-07 | 设计 §6.3(3) 私网解析 | I；`*.maas.aliyuncs.com` 的某主机被测试 DNS 解析为 10.0.0.5 | 保存该地址 | 422 `egress_denied`；另组解析为公网测试地址则通过 |
| TC-V2-CFG-08 | 设计 §6.3 内网规则 | I；`egress_policy` 分别为 `deny-public` 与 `allowlist`；LLM 地址 `http://10.0.0.8:11434`（部署声明的内网）、`https://api.deepseek.com` | 保存 | 两种模式下内网地址都允许；`deny-public` 下公网地址 422 `egress_denied`；`allowlist` 下命中清单的公网地址允许 |
| TC-V2-CFG-09 | 设计 §6.3(4) | I；模拟服务对测试请求返回 302 到白名单外 | 连通测试 | 不跟随重定向；502 `PROVIDER_TEST_FAILED`、`reason=unavailable` |
| TC-V2-CFG-10 | 设计 §6.2 参数白名单 | I | 分别提交 `catchup_rate=2.5`、`segment_target_s=10`、`max_record_seconds=361`（云端）、`max_record_seconds=181`（本地）、`max_reconnects=21`、未知字段 `foo`、修改 `temperature` | 全部 422 `VALIDATION_ERROR`，`fields` 精确列出该字段；数据库不变 |
| TC-V2-CFG-11 | FR-11-06；§8.4 | I；在 Linux 主机 | 创建 `type=qwen3_mlx` | 422 `VALIDATION_ERROR`、`details.reason=host_unsupported`、`fields=["type"]` |
| TC-V2-CFG-12 | 设计 §6.1 全局配置 | I | 检查三张新表结构 | 无 `user_id` 列；`provider_configs` 有 unique(kind,name)；`provider_active` 每个 kind 恰一行；迁移重复执行无变化 |
| TC-V2-CFG-13 | AGENTS §5.1 | I | 检索仓库与发布产物 | 无主密钥、无真实 Key；`.env`/配置样例只含变量名 |

## 4. 管理 API（PAPI）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-PAPI-01 | FR-11-11；§8.4 | I；未登录、普通用户 U1、管理员 A0 | 逐个请求 §8.1 的 8 个接口 | 未登录 401 `AUTH_INVALID`；U1 403 `FORBIDDEN`；A0 正常；每请求逐次校验当前角色（A0 被降级后下一请求即 403） |
| TC-V2-PAPI-02 | FR-11-01、FR-11-05 | I；模拟服务正常 | `POST /api/admin/providers` 创建云端 ASR | 201；读回字段正确、`enabled=true`、`is_active=false`、`version=1`、最近测试成功；模拟服务收到一次 1 s 静音测试会话；审计 1 行 `create`，`changed_fields` 只含字段名 |
| TC-V2-PAPI-03 | FR-11-05；§8.4 | I；模拟服务返回握手 401 | 创建 | 502 `PROVIDER_TEST_FAILED`、`reason=auth_failed`；数据库无新行；审计 1 行 `result=failed`（脱敏原因） |
| TC-V2-PAPI-04 | FR-11-01；§8.4 | I；已有同 kind 同名配置 | 创建同名 | 409 `PROVIDER_NAME_EXISTS`、`fields=["name"]`；不同 kind 同名允许 |
| TC-V2-PAPI-05 | FR-11-11 | I；两个管理员读到 `version=2` | 先后 PATCH | 一个成功（version=3），另一个 409 `VERSION_CONFLICT`、`current_version=3`；后者内容未写入 |
| TC-V2-PAPI-06 | FR-11-03 | I | PATCH 省略 `api_key`；PATCH `api_key=""`；PATCH 新 Key | 省略：密文不变；空串：密钥清除且对需要 Key 的类型返回 422 `VALIDATION_ERROR`（`fields=["api_key"]`）而不保存；新 Key：重新测试通过后替换，`secret_last4` 更新 |
| TC-V2-PAPI-07 | FR-11-02、FR-11-07；§8.4 | I；当前 ASR=本地；目标为云端，从未确认过 | `PUT /providers/active` 不带 `confirm_cloud`；再带 `confirm_cloud=true` | 前者 422 `VALIDATION_ERROR`、`reason=cloud_confirmation_required`、当前项不变；后者 200，`provider_active` 更新并记录 `confirmed_cloud_at/by`，审计 `confirm_cloud` 与 `activate` 各 1 行；之后切回再切到云端不再要求确认 |
| TC-V2-PAPI-08 | FR-11-02、FR-11-05 | I；目标配置测试失败（模拟 429） | 设为当前 | 502 `PROVIDER_TEST_FAILED`、`reason=rate_limited`；当前项与 `version` 不变；新会话仍用原 provider |
| TC-V2-PAPI-09 | FR-11-02；§8.4 | I | 删除当前项；停用当前项；把停用项设为当前；删除不存在 id | 409 `PROVIDER_ACTIVE`；409 `PROVIDER_ACTIVE`；409 `PROVIDER_DISABLED`；404 `NOT_FOUND`；均无数据变化 |
| TC-V2-PAPI-10 | FR-11-08 | I | 删除一个带 Key 的非当前配置 | 200；行与密文删除（查库确认）；审计保留 `delete` 行且不含任何字段值 |
| TC-V2-PAPI-11 | FR-11-09 | I | `GET /providers/active`；`GET /providers?kind=asr` | 返回当前项、来源（db/yaml+原因）、并发上限、录音上限、最近测试；`base_url` 的业务空间段脱敏；`secret_set`/`secret_last4` |
| TC-V2-PAPI-12 | FR-11-08 | I；产生 >60 条审计 | `GET /providers/audit?limit=50` 翻页 | 按时间倒序；cursor 规则同 V1（非法 cursor 422 `CURSOR_INVALID`）；只含字段名 |
| TC-V2-PAPI-13 | 设计 §8.1 测试接口 | I | `POST /providers/{id}/test` 成功与失败各一次 | 成功更新 `last_test_ok=true`；失败 502 `PROVIDER_TEST_FAILED` 且更新 `last_test_ok=false`、脱敏原因；不改配置其他字段与 `version` |

## 5. 后台界面（PUI）

以 原型（内部记录，未公开） 的 12 个状态为界面验收基准，先用 Mock 数据开发，再接真实 API 逐项联调（AGENTS §2）。

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-PUI-01 | FR-11-01、FR-11-09 | Admin 应用 + Mock API | 打开“模型配置” | 两类列表、当前项摘要、配置来源、白名单说明、变更日志与原型 default 状态一致；导航高亮 |
| TC-V2-PUI-02 | FR-11-03、FR-11-04、FR-11-05 | Mock API 依次返回 422 egress_denied、409 PROVIDER_NAME_EXISTS、502 PROVIDER_TEST_FAILED(auth_failed)、201 | 新增配置并提交 | 前三次在弹窗内显示对应中文原因、输入保留、列表不变；第四次关闭弹窗并刷新列表；Key 输入框提交后清空，列表只显示 `••••末4位` |
| TC-V2-PUI-03 | FR-11-07 | Mock API；目标云端未确认 | 设为当前 | 弹出隐私确认，未勾选时确认按钮禁用；勾选后提交带 `confirm_cloud=true`（抓取请求体核对） |
| TC-V2-PUI-04 | FR-11-02 | Mock API | 当前项行 | 删除、停用按钮禁用并有说明；停用项“设为当前”禁用 |
| TC-V2-PUI-05 | FR-11-10、FR-11-11 | Mock API 分别返回 loading、读取失败、403、409 VERSION_CONFLICT、来源=yaml、无可用服务 | 逐一查看 | 与原型 loading/error/forbidden/conflict/fallback/no-provider 状态一致；读取失败不显示为空列表；冲突提示刷新且不覆盖 |
| TC-V2-PUI-06 | FR-11-06 | Mock API 声明主机为 Linux | 查看与新增 | 本机识别不可测试/设为当前，新增时类型选项禁用并说明 |
| TC-V2-PUI-07 | AGENTS §6.6 | 真实 API + 真实 PG + 模拟云端 | 在真实 Chrome 中完成新增→测试→设为当前→编辑 Key→停用→删除 | 每步界面结果与 `GET` 读回、数据库、审计一致；无控制台错误；页面与网络请求中无 Key 明文 |
| TC-V2-PUI-08 | 原型既有问题 | 真实 Admin 应用，深色外观 | 查看主按钮 | 记录主按钮对比度（原型中深色主按钮浅底白字问题，不得原样带入正式实现；判定标准：文字与背景对比度 ≥4.5:1） |

## 6. 整理模型热切换（LLM）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-LLM-01 | 设计 §4.5 | I；两个 LLM 模拟服务 A（当前）、B | 切到 B 后发起新口述 | 新口述的整理请求只到达 B；`model_calls` 记录 B 的 `provider_config_id` |
| TC-V2-LLM-02 | 设计 §4.5、§7.3 | I；A 的响应延迟 3 s | A 整理进行中切到 B | 进行中的调用在 A 完成并写入结果；之后的调用到 B；A 在租约归零后关闭 |
| TC-V2-LLM-03 | 设计 §4.5 路由规则 | I；后台把 LLM 设为公网 `api.deepseek.com`（模拟） | 触发一次学习分类 | 分类请求不发往公网整理 provider（沿用 `PurposeRoutedLlmProvider` 规则），走 YAML 分类配置或按 V1 规则跳过 |
| TC-V2-LLM-04 | 设计 §4.5 | I；切换到测试失败的 LLM | 设为当前 | 502 `PROVIDER_TEST_FAILED`；整理仍用原 LLM |

## 7. 桌面端（DESK）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-DESK-01 | FR-02-06′；设计 §4.1 | 桌面单元/组件测试；会话创建响应 `max_record_seconds=360` | 模拟录音进度到 359.75 s、360 s | 360 s 时自动停止（替代原写死 180 s，`session-store.ts:100`）；HUD 剩余时间按 360 计算 |
| TC-V2-DESK-02 | 设计 §4.1 兼容 | 同上，响应缺少该字段（旧服务端） | 录音 | 回退 180 s 自动停止 |
| TC-V2-DESK-03 | 设计 §4.1 | 两个连续会话，响应分别为 360 与 180 | 依次录音 | 各自按本会话值停止，不沿用上一会话的值；`desktop/src/shared/config.ts` 不再作为上限来源 |
| TC-V2-DESK-04 | 设计 §4.1 `pcm-worklet.js` | worklet 单元测试 | 连续生成 360 s 帧 | 样本计数与帧序号为安全整数，无溢出/丢帧，最后一帧长度规则同 V1 |

## 8. Linux 部署（DEP）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-DEP-01 | 设计 §9 | R；全新 Linux（记录发行版、CPU、内存、Python、PostgreSQL 版本） | 按部署文档安装（不装 `mac` 可选依赖） | 安装成功；无 MLX 依赖；`qwen3_mlx` 在该主机不可选（CFG-11） |
| TC-V2-DEP-02 | V1 部署约束 | R | 配置 workers=2；同库启动第二实例 | 前者启动拒绝；后者 readiness 失败并退出（advisory lock） |
| TC-V2-DEP-03 | AGENTS §5.3 远程 HTTPS/WSS | R；Nginx/Caddy | 客户端连接 | 只暴露 HTTPS/WSS；明文 HTTP/WS 不可用；360 s 会话不被代理超时断开（`proxy_read_timeout` ≥480 s） |
| TC-V2-DEP-04 | 设计 §6.3、§9 出口双重约束 | R | 从服务器访问白名单外 443 地址；访问白名单域名 | 防火墙/代理层拒绝前者；后者可达 |
| TC-V2-DEP-05 | 设计 §9 环境注入 | R | 检查进程环境、配置文件、日志 | 主密钥与 Key 只在环境/Secret 中；日志无值；配置文件只含变量名 |
| TC-V2-DEP-06 | 设计 §4.2 内存 | R；20 路 360 s 会话 + 20 份失败音频保留 | 观测进程 RSS | 未超过部署清单声明的内存；PCM 预算拒绝超额创建为 429 `audio_budget`；记录实测峰值（同时补齐 T11：失败音频是否计入预算） |

## 9. 真实环境端到端与非功能（E2E，S5）

| 编号 | 关联 | 环境 / 前置 | 操作 | 可观察预期 |
|---|---|---|---|---|
| TC-V2-E2E-01 | PRD-v2 §8 | R；Linux + 真实百炼 + 官方 DeepSeek + macOS 安装包；后台配置云端 ASR 与 DeepSeek 为当前 | 一次 10–30 s 真人口述并插入 | 中间结果可见；整理结果插入并读回；会话记录 provider；用 V1 插入用例的读回判据 |
| TC-V2-E2E-02 | NFR 时延 | R；F-V2-TTS 与本人真人录音各 ≥20 次 10–30 s | 测“停止→插入” | 记录 p50/p95 与样本数；对照 V1 目标 p50 ≤2 s、p95 ≤5 s；未达标如实记录，不降低目标 |
| TC-V2-E2E-03 | FR-02-10；FR-02-06′ | R；本人 5–6 分钟连续真人讲话（本人开发后自测，2026-10-27 决定）与 F-V2-TTS-B 6 分钟 | 完整口述 | 自动在 360 s 收尾；分段数、断线恢复次数记录；文本无固定硬切式整句缺失（人工对照原音逐段检查）；记录整理是否超时回退（T5） |
| TC-V2-E2E-04 | BR-13.3 恢复 | R；会话进行中在服务器侧临时阻断到云端的出站 3 s | 完成会话 | 会话完成；`asr_reconnects ≥1`；记录是否丢句（已知缺陷 T1，若丢句记录为缺陷证据，不判本用例失败；会话失败才判失败） |
| TC-V2-E2E-05 | FR-11-02 在途不受影响 | R；会话进行中在后台切换 ASR | 完成会话并开始新会话 | 旧会话按原 provider 完成；新会话用新 provider |
| TC-V2-E2E-06 | BR-13.6；设计 §11 | R；逐步提高并发 4、8、12（不超过云端账号配额） | 并发口述 | 记录各档成功率、断线率、1001 次数、时延；据此确定生产 `max_concurrent_sessions`；不以本机实验结论替代 |
| TC-V2-E2E-07 | BR-13.7 用量 | R；E2E 执行后 | 查看后台“模型与用量” | 按 provider 汇总的 ASR 音频秒数 = 全部调用（含失败、已发往云端计费）的音频之和，且其中成功调用部分 = 成功会话音频时长之和（手算核对）；调用次数一致（2026-10-28 本人决定口径：失败调用的音频计入用量） |
| TC-V2-E2E-08 | BR-13.1 隐私 | R；E2E 全部执行后 | 检索服务器日志、数据库、临时目录 | 无音频文件与音频字节；无 Key；转写只在 V1 允许的会话表中 |
| TC-V2-E2E-09 | FR-11-07 用户文案 | R | 查看 V2 隐私说明 | 明确写有“使用云端识别时，语音会发送到第三方云端识别服务” |

## 10. 需求覆盖

| 需求 | 用例 |
|---|---|
| FR-02-06′ 录音上限按 provider、会话固定 | RT-01、RT-02、RT-03、RT-04、RT-05、DESK-01–04、E2E-03 |
| FR-02-10 服务端静音点分段 | PROV-01、PROV-02、PROV-03、PROV-05、E2E-03 |
| FR-02-11 记录 provider；不应用 ASR 词表、保留整理词库 | PROV-14、RT-08、RT-09 |
| FR-11-01 多配置维护（含参数白名单、全局配置表结构） | PAPI-02、PAPI-04、PUI-01、PUI-07、CFG-10、CFG-12 |
| FR-11-02 每类恰 1 个当前、切换只影响新会话 | PAPI-07、PAPI-08、PAPI-09、PUI-04、LLM-01、LLM-02、E2E-05 |
| FR-11-03 Key 加密、只写不读 | CFG-01、CFG-02、PAPI-06、PUI-02 |
| FR-11-04 严格白名单 | CFG-06、CFG-07、CFG-08、CFG-09、PROV-17、DEP-04 |
| FR-11-05 保存/切换前连通测试 | PAPI-02、PAPI-03、PAPI-08、PAPI-13、LLM-04、PUI-02 |
| FR-11-06 本机识别仅 macOS Apple Silicon | CFG-11、PUI-06、DEP-01 |
| FR-11-07 云端启用确认 | PAPI-07、PUI-03、E2E-09 |
| FR-11-08 审计 | PAPI-02、PAPI-10、PAPI-12 |
| FR-11-09 展示当前项与来源 | PAPI-11、PUI-01 |
| FR-11-10 回落 YAML、不静默降级 | CFG-04、CFG-05、PUI-05 |
| FR-11-11 仅 admin、版本防覆盖 | PAPI-01、PAPI-05、PUI-05 |
| BR-13.1 隐私 | PROV-18、CFG-02、DEP-05、E2E-08、E2E-09 |
| BR-13.2 词库提示 | PROV-14、RT-08 |
| BR-13.3 分段、补送限速、断线恢复 | PROV-01–03、PROV-07–11、E2E-03、E2E-04 |
| BR-13.4 在途与切换 | RT-03、LLM-02、E2E-05 |
| BR-13.5 失败与重试（只重送未完成段、provider 变更） | PROV-11、PROV-12、RT-10、RT-11、RT-12、RT-13 |
| BR-13.6 并发 | PROV-15、RT-06、RT-07、E2E-06 |
| BR-13.7 用量 | RT-09、E2E-07 |
| BR-13.8 安全 | PAPI-01、CFG-03、CFG-13、DEP-05 |
| PRD-v2 §6/§8 端到端、时延、兼容、可用性 | E2E-01、E2E-02、DEP-01–03、PROV-12 |
| 设计 §4.2 PCM 预算 / §4.4 截止时间 | RT-06、RT-13、RT-14、DEP-06 |
| 设计 §4.5 LLM 热切换 | LLM-01–04 |
| 设计 §3.3/§3.4/§3.1 provider 内部契约（部分结果约束、无语音、取消清理、忽略系统代理） | PROV-04、PROV-06、PROV-13、PROV-16 |
| §11 V1 回归（本地 provider 行为不变） | RT-15 |
| 原型既有问题（深色主按钮对比度）不带入正式实现 | PUI-08 |

90 条用例均被上表引用；全部 PRD-v2 新增/变更条款（FR-02-06′、FR-02-10、FR-02-11、FR-11-01–11、BR-13.1–8）均有对应用例；设计中的已知缺陷与风险（T1 丢句、T4 截止时间、T5 长文字整理、T9 并发、T11 内存）分别由 E2E-04、RT-13/E2E-03、E2E-03、E2E-06、DEP-06 收集证据。

## 11. V1 回归范围（在 V2 部署上）

- **必跑**：V1 的 AUTH、DICT、HIST、STAT、SET、ADMIN、API、INSERT 中与服务端/客户端协议相关的自动化用例（Linux + 当前 ASR=云端、模拟或真实）；结果单独登记，不覆盖 V1 台账中的历史结论。
- **按 provider 区分**：V1 VOICE/ASR 中依赖本地 Qwen 或 180 s 的用例，在“当前 ASR=本地”的 macOS 环境回归（RT-15）；云端环境下由本文对应用例替代，不重复判定。
- **变更后作废的旧判据**：V1 中“音频不发送公网”的判定在 V2 部署启用云端 ASR 时不适用（PRD-v2 §3），改由 BR-13.1 与 E2E-08/09 判定；未启用云端 provider 时 V1 判据继续有效。

## 12. 门禁

1. M/I 层用例全部通过后，才进入 R 层；R 层不能以 M/I 结果代替。
2. S4 自测完成条件：PROV、RT、CFG、PAPI、LLM、DESK 全部通过；PUI-01–06 Mock 通过、PUI-07 真实联调通过；编译、类型检查、单测通过。
3. S5 完成条件：DEP、E2E 全部通过（E2E-04 中的丢句按已知缺陷记录，不单独阻断）；时延等指标按原目标判定，未达标不降低标准；已知限制写入文档。

4. 本文件已于 2026-10-27 评审通过，V2 的 S4 编码在 `feature/0.0.4` 分支进行。

2026-10-28 本人最终决定对本轮 E2E-02、DEP-04 的现状作验收例外并确认通过，当前台账区分按原记录通过与本人接受限制通过，原第3条中的指标目标继续保留；适用范围见 本人验收确认（内部记录，未公开）。独立未修缺陷与发布前工作继续跟踪。
