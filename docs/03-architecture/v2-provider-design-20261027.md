# V2 技术设计：云端 ASR provider 与后台 Provider 管理（S2 增量）

2026-10-27。**状态：S2 第 2 版，待本人评审。** 依据 [PRD-v2](../02-prd/PRD-v2.md)（S1，本人 2026-10-27 确认“需求应该都好了”）与实验批次 01（内部记录，未公开）、02（内部记录，未公开）、03（内部记录，未公开）、04（内部记录，未公开）、05（内部记录，未公开）、07（内部记录，未公开）、08（内部记录，未公开）、09（内部记录，未公开）、10（内部记录，未公开）（06 受污染作废）。

**与 V1 的关系**：只描述 V2 新增/变更；未提及的 V1 设计（[architecture](architecture.md)、[provider-config](provider-config.md)、[api-contract](api-contract.md)、[data-model](data-model.md)、[session-protocol](session-protocol.md)、[deployment](deployment.md)）沿用，V1 文件不回写。S3 测试用例在本稿通过评审、且 provider 管理页原型补齐（§8.3）之后才编写。

### 第 2 版修订记录（2026-10-27）

第 1 版经对照代码复核，发现会导致功能出错的问题 5 处、与现状不符 5 处、漏写 3 处；本人同日答复：

| # | 事项 | 本人决定 / 修订 |
|---|---|---|
| R1 | 断线恢复时当前段文字重新开始，会违反运行时“`stable_text` 只增不减、最终文字须以其开头”的校验（`session_runtime.py:935`、`:1378`），恢复后的会话必然被判失败 | `stable_text` 只含已完成段，当前段一律放 `provisional_text`（§3.3） |
| R2 | 重试截止时间为“开始 + `finish_timeout_ms`(≤10 s)”（`session_runtime.py:703`、`:1350`），按 2× 重送整段录音必然超时 | **本人同意**：内存保存已完成段文字（与失败音频同一 600 秒有效期），重试只重送未完成段（§3.7） |
| R3 | 每会话按 180 秒预留 PCM、总预算 128 MiB（`config.py:11-13`、`:61`），上限加长后并发会骤降 | **本人决定 PCM 预算提高到 1 GiB**（§4.2） |
| R4 | 180 秒还写死在数据库约束、本地 Qwen 检查点、桌面端 | 逐处列出改动；上限按 provider 区分，会话创建时固定（§4.1） |
| R5 | 长文字整理可能超过 DeepSeek 调用 10 秒上限，未测 | **本人决定先不补测**，列为风险（§13 T5） |
| R6 | 单次录音上限 | **本人决定 6 分钟（360 秒）**，取代 PRD-v2 原 10 分钟（§4.1；PRD-v2 同步修订） |
| R7 | 恢复后偶发丢句是否提示用户（原 T1） | **本人决定不提示**；丢句视为缺陷，后续修复（§3.8、§13 T1） |
| R8 | provider 管理页是否先补原型（原 T6） | **本人同意先补原型**，再写 S3（§8.3） |
| R9 | `AGENTS.md`“密钥不入库”与“Key 加密存数据库”字面冲突 | **本人同意**改写为“不进 Git、不写日志；运行期 provider 密钥可加密存数据库” |
| R10 | 管理 API 前缀、出口配置字段、JSON 类型、多进程广播、加密依赖与现状不符 | 已按现状更正（§6、§8、§9） |
| R11 | ASR 词库提示未应用没有落表；整理模型热切换未写；重试时 provider 已切换的处理未写 | 已补（§4.3、§7.3、§7.4） |
| R12 | 编写 S3 时发现新接口未定义精确错误码（原写“502 测试失败”等），无法按“错误码精确匹配”写判据 | 2026-10-27 补 §8.4 错误码表；只新增码与 details.reason，不改既有码含义 |

## 1. 范围与不变量

| 范围 | 内容 |
|---|---|
| A | `bailian_realtime` ASR provider：因果分段、限速补送、断线恢复、错误映射（§3、§5） |
| B | 会话运行时、容量、数据与桌面端的配套改动（§4） |
| C | Provider 配置管理：存储、加密、白名单、校验、热切换、审计、管理 API、后台界面与原型（§6–§8） |
| D | Linux 部署、可观测、容量与成本（§9–§11） |

**不变量（沿用 V1）**：业务层只依赖 `AsrProvider/AsrSession` 接口，新增 provider 不改业务层；音频只在内存，成功/取消/无语音/不完整立即释放，失败重试资格 600 秒（BR-05）；密钥不进 Git、不写日志；`user_id` 租户规则不变（provider 配置是**全局部署配置**，不进租户表）；不静默降级（FR-11-10）；V1 部署的“单 Uvicorn worker、单应用副本、PostgreSQL advisory lock 拒绝第二实例”（[deployment](deployment.md) §1）保持不变。

## 2. 总体结构

```
desktop ──WS PCM──▶ session_runtime ──(lease)──▶ ProviderManager ──▶ AsrProvider (bailian_realtime | qwen3_mlx | mock)
                         │                              │                └─ CloudConnection ⇄ wss://<workspace>.maas.aliyuncs.com
                         │                              ├─ ProviderRegistry（DB: provider_configs / provider_active / provider_audit）
                         │                              └─ SecretBox（AES-256-GCM，主密钥来自环境）
admin web ──REST──▶ /api/admin/providers*  ──▶ ProviderService（校验/测试/CAS/审计）
```

- **ProviderManager**（`app.state.providers`）：分别持有“当前 ASR 实例”和“当前整理 LLM 实例”；会话 `acquire(kind)` 得到租约（引用计数）；切换时先构建并探测新实例，再原子替换指针；旧实例在租约归零或超时后 `close()`（§7.3）。单进程内状态即可（V1 已强制单 worker）。
- 现有 `build_asr/_build_llm`（`providers/__init__.py`）保留为工厂；YAML 仍是启动默认与兜底。

## 3. `bailian_realtime` Provider

模块：`server/app/providers/asr/bailian_realtime.py`（+ `bailian_segmenter.py`、`bailian_conn.py`）。

### 3.1 配置字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `type` | – | `bailian_realtime` |
| `base_url` | – | `wss://<host>`，host 必须命中出口白名单（§6.3） |
| `model_id` | `qwen3-asr-flash-realtime` | 请求参数 `model` |
| `api_key` | – | 后台加密存储，或 YAML 的 `api_key_env` 环境变量引用 |
| `language` | 沿用 `asr.language`，`auto` 时不传 | `input_audio_transcription.language` |
| `segment_target_s` | 30 | 实验决定（批次 05/09） |
| `segment_window_s` | 5 | 目标±窗口 |
| `segment_silence_ratio` | 0.15 | 安静块：250 ms RMS < 本会话开头 20 秒内语音中位 RMS × 比例；无语音基准时用固定 RMS 150 |
| `segment_min_s` | 10 | 段内至少这么长才允许切 |
| `catchup_rate` | 2.0 | 落后实时的发送倍速，**校验上限 2.0**（批次 08） |
| `max_reconnects` | 8 | 单会话内部重连上限（实验值，S4 可调） |
| `max_record_seconds` | 360 | 本 provider 的单次录音上限（本人决定 6 分钟，§4.1） |
| `max_concurrent_sessions` | 8 | provider 级并发上限（≤ 全局 `admission_ceiling`；目标环境实测后调整，§11） |
| `use_env_proxy` | false | 默认直连、忽略系统代理（批次 01、07） |
| `connect_timeout_ms` | 15000 | |

`AsrCapabilities` 新增：`provider_id`、`supports_hints: bool`（云端 False）、`replay_rate: float | None`（云端 2.0）、`max_record_seconds: int`、`max_concurrent_sessions: int | None`、`supports_resume: bool`（云端 True，见 §3.7）。`stable_partial=True`。

### 3.2 会话内部组件

| 组件 | 职责 |
|---|---|
| `Segmenter`（因果） | 每到一个 250 ms 块判断：段内 ≥ `segment_min_s` 且自 `target−window` 起遇到第一个安静块 → 在该块之后切；到 `target+window` 仍无 → 硬切。只用已到达音频，**不暂存、不向前看**（批次 09：暂存方案停止延迟 4–5 秒） |
| `SegmentLedger` | 每段 `start_sample/end_sample`、状态 `open→committed→completed`、`text`；未完成段持有 PCM 引用，完成后立即放弃引用 |
| `SendQueue` | 唯一出口。块带“实时到达时刻”，按时到达立即发送，落后块以 `0.25 s / catchup_rate` 的间隔发送（2×）；commit 在队列中保序。**禁止无限速突发**（批次 08：不限速 6 个会话仅 2 个成功，成功者丢约 45% 内容） |
| `CloudConnection` | 建连（白名单与解析结果校验、固定已校验 IP、`proxy=None`）、`session.update`（`pcm`、`16000`、`turn_detection=null`）、读循环（`transcription.text`→当前段文字，`completed`→段结果，`error`→映射）、关闭/abort |
| `Recovery` | 连接异常、读循环异常、错误事件或云端关闭帧（批次 10 观察到 `1001 going away … upstream_reset`）→ 新建连接，从**首个无结果段的起点**按 `SendQueue` 规则重送并追赶；已完成段保留；超过 `max_reconnects` → 对外失败（可重试，§3.5、§3.7） |

### 3.3 `AsrSession` 映射与部分结果约束

| 接口 | 行为 |
|---|---|
| `open(options)` | 申请 provider 级并发名额（满则 `ProviderError('busy', retryable=True)`）；创建 Ledger/Segmenter；首块到达前建连；若 `options.resume` 非空则按 §3.7 恢复已完成段 |
| `feed(pcm, start_sample)` | 沿用 V1 连续性校验；入 Segmenter/SendQueue 后立即返回，不阻塞调用方 |
| `partials()` | **`stable_text` = 已完成段文字按序拼接**，只在某段 `completed` 时增长；**`provisional_text` = 当前段的 `text` + `stash`**（可任意修订，断线重连后可变短）；`revision` 单调递增；`decoded_samples` = 已完成段的 `end_sample`（单调，断线不回退），`received_samples` = 已接收样本数。满足运行时校验 `stable_text` 前缀单调、`decoded ≤ received ≤ samples`（`session_runtime.py:935-937`） |
| `finish(total_samples, deadline)` | Segmenter 收尾→末段 commit→等待全部段 `completed`；返回 `AsrFinal(text=按序拼接, segments=[AsrSegment], no_speech=全部为空, processed_samples)`，保证 `text` 以已发出的 `stable_text` 开头（`:1378`）；超 `deadline` → `ProviderError('timeout', retryable=True, resume=…)`（§3.7） |
| `cancel()` | 幂等；关闭/abort 连接、清空缓冲与已完成段文字、释放并发名额；`D+2 秒` 内确认连接已关闭 |
| `drain_estimate_s()`（新增可选） | 当前积压音频秒数 / `catchup_rate`，供运行时计算 `finish` 截止时间（§4.4） |

### 3.4 段拼接与无语音

按段序拼接 `transcript`，段间不插入字符（中文），英文边界由云端输出决定（批次 04/09/10 未见异常）。全部段为空 → `no_speech=True`（沿用 V1 empty 流程，不调用整理）。

### 3.5 错误映射

| 云端/传输现象 | `ProviderError.code` | retryable | 处理 |
|---|---|---|---|
| 握手 401/403 | `auth_failed` | 否 | 后台提示 Key 无效；会话失败 |
| 握手/事件 429、额度或并发限制 | `rate_limited` | 是 | 映射为现有“繁忙/稍后重试” |
| 握手超时、DNS/TLS 失败、连接重置、关闭帧 1001、`InternalError` | `unavailable` | 是 | 先内部 Recovery，超过次数才对外失败 |
| `invalid_request_error`（如 commit 被拒） | `bad_request` | 否 | 通常是我方违反协议（如补送过快）；告警 |
| `finish` 超时 | `timeout` | 是 | BR-05 显式“重新识别” |
| 白名单/解析校验失败 | `egress_denied` | 否 | 配置错误；保存时应已被拒，运行时再校验作防御 |

对外不新增错误码，映射到现有 ASR 失败码与 `retry_kind`（S4 对照 api-contract §1 逐个落表）。

### 3.6 PCM、文字与隐私

- provider 只引用未完成段的 PCM；完整音频仍由会话运行时按 BR-05 持有（失败时 600 秒内可重试）。
- 失败时 provider 把**已完成段的文字与样本边界**交给运行时，作为 `resume` 数据与音频放在同一个租约里，同一 `D` 截止、同一 `D+1 秒` 清空；不写数据库、不写日志（本人同意，R2）。
- 日志只含 session_id、段序号、耗时、错误码、重连次数、秒数；不含音频、转写、Key；host 中的 `ws-…` 业务空间段脱敏。

### 3.7 失败后的“重新识别”（只重送未完成段）

1. provider 对外失败时，`ProviderError` 携带 `resume = {provider_id, provider_version, segments: [(start_sample, end_sample, text), …]}`（仅已完成段）。
2. 运行时把 `resume` 与失败音频一起保存在 `audio_lease` 中（同一 `D`）。
3. 用户发起重试：若当前 ASR provider 与 `resume.provider_id/version` 相同且 `supports_resume`，运行时以 `AsrOpenOptions.resume` 打开新会话，**只 `feed` 从首个未完成段起点开始的音频**；provider 直接把已完成段计入 `stable_text`，从该点继续分段识别。
4. 重送量 = 首个未完成段起点到结尾：停止前不久断线时 ≤ 约 35 秒；录音中途云端不可恢复时为断线后的全部尾部（可达数分钟）。重试截止按重送量放宽（§4.4，T12）。
5. provider 已被切换、版本不同或不支持恢复 → 整段重送给当前 provider；若音频时长超过当前 provider 的 `max_record_seconds`（如 360 秒录音切到本地 180 秒的 Qwen）或当前 provider 不可用 → 返回 `retry_kind=none`、`retry_unavailable_reason=provider_changed`，提示“识别服务已变更，请重新录音”（R11）。
6. 重试截止时间见 §4.4。

### 3.8 已知缺陷：恢复后偶发丢句

批次 09 的 10 次恢复中 1 次丢失整句（34 字）；无断线的正常运行也出现过 1 次 19 字缺块。**本人决定不向用户提示**，记为缺陷后续修复。设计要求：会话 attempt 元数据记录 `asr_reconnects`（次数）与 `asr_resend_s`（重送秒数），便于定位；S4 增加“恢复前后段文字比对”的诊断日志（只记长度与段号，不记文字）。

## 4. 会话运行时、容量、数据与桌面端改动

### 4.1 录音上限：360 秒，按 provider 区分，会话创建时固定

| 位置 | 现状 | 改动 |
|---|---|---|
| `session_runtime.py:42` `MAX_SAMPLES = 2_880_000` | 全局常量 180 秒 | 会话创建时取当前 ASR provider 的 `max_record_seconds × 16000`，写入 runtime；云端 360 秒=5,760,000 样本；本地 `qwen3_mlx` 保持 180 秒 |
| `providers/asr/base.py:23` `AsrOpenOptions.max_samples=2_880_000` | 默认值 | 由运行时按会话上限传入 |
| `models/session.py:79` 约束 `received_samples <= 2880000` | 数据库硬约束 | Alembic 迁移放宽为 `<= 5760000`（幂等：元数据判断 + 动态 SQL；执行后核对约束实际定义） |
| `providers/asr/qwen_checkpoint.py:31/46` 写死 2880000 | 本地 Qwen | 不改（本地保持 180 秒） |
| `config.py:12` `MAX_SESSION_SECONDS = 180` | PCM 预留按 180 秒 | 改为“所有已配置 ASR provider 的 `max_record_seconds` 最大值”（V2 为 360），用于 PCM 预留（§4.2） |
| 会话创建响应 / `GET /api/v1/capabilities` | 无上限字段 | 会话创建响应新增 `max_record_seconds`（本会话固定值）；能力接口新增当前 provider 的 `max_record_seconds` |
| `desktop/src/shared/config.ts:25` `maxDurationSeconds: 180`；`renderer/stores/session-store.ts:100` 到 180 秒自动停 | 桌面端写死 | 改为使用本会话创建响应中的 `max_record_seconds`；缺失时回退 180（兼容旧服务端）；HUD 剩余时间提示按该值。**桌面端因此有功能改动**（第 1 版写“desktop 无功能变更”有误） |
| `pcm-worklet.js:88` 注释“180 s limit” | 注释 | 核对 360 秒下整数安全后更新注释 |

切换 provider 不影响已创建会话的上限。

### 4.2 PCM 内存预算：1 GiB

- `audio.active_pcm_budget_bytes`：V2 部署配置设为 **1,073,741,824（1 GiB）**（本人决定，R3）。
- 每会话预留 = 360 × 32,000 = 11.52 MB；`effective_admission_ceiling = min(sessions.admission_ceiling, 1 GiB // 11.52 MB = 93)`，20 路目标不受影响。
- 失败音频最多保留 20 份，最坏 20 × 11.52 MB ≈ 230 MB；活动 20 路 ≈ 230 MB；合计约 460 MB < 1 GiB。S4 须核对失败音频是否计入同一预算，若不计入则单列上限并在部署清单写明进程内存需求（建议主机预留 ≥ 2 GiB 给应用进程）。

### 4.3 会话、租约与词库提示

| 位置 | 改动 |
|---|---|
| `self.asr_provider` | 改为会话创建时 `ProviderManager.acquire('asr')`，会话结束/取消/失败/重试前释放；attempt 记录 `asr_provider_id` |
| `AsrOpenOptions` | 新增 `resume`（§3.7）；`hint_terms` 照常传入，由 provider 按 `supports_hints` 决定 |
| `sessions.asr_hints`（`models/session.py:60`） | 继续记录候选词，新增列 `asr_hints_applied bool not null default true`；云端 provider 写 `false`（FR-02-11），避免“存了词表=用了词表”的误读 |
| `session_attempts` | 新增 `asr_provider_id`、`asr_reconnects`、`asr_resend_s`（§3.8） |
| 容量 | `effective_admission_ceiling` 再与当前 ASR provider 的 `max_concurrent_sessions` 取小；切换后新上限仅约束新会话，降容不终止在途任务（BR-12） |

### 4.4 截止时间（finish 与重试）

现状：`_asr_operation_deadline = min(runtime.deadline, end_accepted_at + finish_timeout_ms)`，`runtime.deadline = 开始 + stop_to_result_budget_ms(≤30 s)`，`finish_timeout_ms ≤ 10 s`（`config.py:104`）。

改动：
- `asr.finish_timeout_ms` 校验上限从 10,000 放宽到 25,000，仅对 `replay_rate` 非空的 provider 生效；本地 provider 仍 ≤10,000。
- 云端会话的 ASR 截止时间 = `end_accepted_at + min(25 s, max(10 s, drain_estimate_s + 5 s))`，仍受 `runtime.deadline` 与音频租约 `D` 约束。
- 正常停止：停止→出字实测 0.24–0.5 秒（批次 09、10），不受影响。
- 恢复追赶期间停止：积压 ≤ 约 35 秒 → 2× 约 17.5 秒，落在 25 秒内（**推算，未实测**，§13 T4）。
- 重试（只重送未完成段，§3.7）：截止 = `end_accepted_at + 重送秒数/补送倍速 + 10 s`，不受 25 秒上限约束；本次尝试的整体预算（`stop_to_result_budget_ms`）顺延同样的重送时长；仍受失败音频截止 `D` 约束（2026-10-28 本人决定 T12）。
- `stop_to_result_budget_ms`（30 s）不变；ASR 用掉较多时间时，整理按现有规则超预算回退原文并提示。

### 4.5 整理模型（LLM）热切换

- `RefinerService`（`services/refiner.py:82`）不再在启动时固定持有 `llm`；每次整理调用前 `ProviderManager.acquire('llm')`，调用结束释放。
- 分类 LLM（学习分类）首版仍由 YAML 配置（PRD-v2 §1），不进 ProviderManager。
- `PurposeRoutedLlmProvider` 的路由规则（公网 R2 不发送纠错派生数据，`providers/__init__.py`）对后台配置的 LLM 同样生效。
- 切换后：进行中的整理调用用原实例完成；新调用用新实例。

## 5. 云端模拟服务（测试夹具）

`server/tests/fixtures/mock_bailian_server.py`（`websockets` 实现）：支持 `session.update/append/commit/finish`，按 commit 产生 `text`/`completed`；可注入握手 401/429、连接重置、关闭帧 1001、错误事件（`InternalError`/`invalid_request_error`）、延迟；**当音频到达速度快于 3× 实时时丢弃部分音频或拒绝 commit**（复现批次 08）。S3 的 provider 单元/集成用例全部基于它；真实云端只用于 S5 联调。

## 6. Provider 配置：数据与安全

### 6.1 数据模型（全局配置，不带 `user_id`；Alembic 迁移幂等）

JSON 列沿用项目现有 SQLAlchemy `JSON` 类型（`models/session.py:60`、`models/dictionary.py:45`）。

`provider_configs`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | uuid pk | |
| `kind` | varchar(8) | `asr` / `llm` |
| `name` | varchar(64) | unique(kind, name) |
| `type` | varchar(32) | asr：`bailian_realtime` / `qwen3_mlx` / `mock`；llm：`openai_compatible` / `ollama` / `mock` |
| `base_url` | varchar(512) null | 本地类型为空 |
| `model_id` | varchar(128) | |
| `params` | JSON | 该类型白名单字段（§6.2），未知字段拒绝 |
| `secret_ciphertext` | bytea null | AES-256-GCM：nonce(12B)‖密文‖tag(16B)；附加数据 = `provider_id‖kind` |
| `secret_key_id` | varchar(32) null | 主密钥标识 |
| `secret_last4` | varchar(4) null | 仅显示 |
| `enabled` | bool | 停用不可被选为当前 |
| `last_test_at` / `last_test_ok` / `last_test_reason` | | 最近一次连通测试（原因已脱敏） |
| `version` | int | 乐观锁 |
| `created_at/updated_at/updated_by` | | |

`provider_active`：`kind` pk、`provider_id` fk、`confirmed_cloud_at/by`（云端 ASR 隐私确认）、`version`、`updated_at/updated_by`。
`provider_audit`：`id`、`at`、`actor_user_id`、`action`（create/update/delete/test/activate/confirm_cloud）、`kind`、`provider_id`、`changed_fields`（JSON，仅字段名）、`result`（ok/rejected/failed + 脱敏原因）。**不存任何字段值**。

`model_calls` 增列：`provider_config_id uuid null`、`segments int null`、`reconnects int null`；ASR 行 `purpose='asr'` 每 attempt 一行，沿用 `processed_audio_samples`、`elapsed_ms`、`status`、`error_code`。用量统计按 provider 汇总 ASR 音频秒数，沿用 `aggregated_at` 与 BR-10 口径。

**加密依赖**：项目当前无 `cryptography`，V2 新增该依赖（`server/pyproject.toml` + `uv.lock`，按 AGENTS 锁文件规则单独说明依据，不升级锁文件格式）。

### 6.2 参数白名单

每个 `type` 一份 pydantic `Strict` 模型，仅允许 §3.1 字段及范围：`catchup_rate ∈ (0, 2.0]`、`segment_target_s ∈ [15, 60]`、`max_record_seconds ∈ [10, 360]`（云端）/`[10, 180]`（本地）、`max_reconnects ∈ [0, 20]` 等，越界 422。LLM 的 `refine_protocol`、`temperature` 等沿用 V1 固定值，后台不可改。

### 6.3 出口白名单（严格）

扩展现有字段 `egress_policy`（`config.py:52`，默认 `deny-public`），不另起配置段：

```yaml
egress_policy: allowlist          # deny-public（V1 默认）| allowlist
egress_allowlist:
  - "*.maas.aliyuncs.com"         # 百炼（含业务空间 ID 子域）
  - "api.deepseek.com"
```

两种模式下，本机/内网地址都按 V1 规则（部署声明的回环/内网地址）允许；`deny-public` 不允许任何公网地址；`allowlist` 允许公网地址，但必须命中清单。（2026-10-27 原型复核时澄清。）校验在保存、测试、切换、建连时各做一次：
1. ASR 仅 `wss`、LLM 仅 `https`（本机/内网 LLM 沿用 V1 规则允许 `http`），默认端口 443；禁止 URL 用户信息；禁止 IP 字面量（除非清单显式列出该 IP）。
2. `*.x.y` 只匹配 `x.y` 的子域（按点边界，不匹配 `evilx.y`）；其余精确匹配。
3. 公网条目的解析结果必须是公网地址（拒绝回环、私网、链路本地、CGNAT 等）；**建连使用已校验的 IP 并固定**（防 DNS 重绑定），SNI 与证书校验用原主机名。
4. 不跟随重定向。
5. 清单只能改部署配置并重启生效，后台无入口。

## 7. ProviderService / ProviderManager

### 7.1 管理操作

| 操作 | 规则 |
|---|---|
| 创建/修改 | 参数校验（§6.2）→ 地址校验（§6.3）→ 连通性测试 → 通过才落库（Key 加密，只写不读）；`version` CAS，冲突 409 |
| 测试 | ASR：建连→`session.update`→发 1 秒合成静音→commit→等 `completed` 或 `error`，限时 15 秒；LLM：沿用 `prepare()`（`/models` + 文本探测）；结果脱敏。云端测试产生少量费用，界面提示 |
| 切换当前 | 目标须 `enabled` 且测试通过（或现场再测）→ 构建新实例并 `prepare()` → 事务内 CAS 更新 `provider_active` → 原子替换指针；任一步失败，原当前项不变 |
| 删除/停用 | 当前使用项拒绝（409）；删除时先清密文；审计保留 |
| 启用云端 ASR | 首次把云端 ASR 设为当前，请求须带 `confirm_cloud=true`（界面勾选“音频将发送到第三方云端识别服务”），记录 `confirmed_cloud_at/by` 与审计（FR-11-07） |

### 7.2 当前来源与兜底（FR-11-10）

启动：读 `provider_active` → 取配置并解密 → 构建；缺失、解密失败（主密钥缺失/不匹配）、构建失败 → 回落 YAML，并在后台与健康检查中显示“来源=yaml（原因）”；YAML 也不可用 → `acquire()` 抛 `ProviderError('unavailable')`，创建会话返回明确错误，**不静默改用 mock**（mock 仅 `environment=development` 且显式配置时允许）。

### 7.3 热切换与租约

- `acquire(kind)` 返回 `Lease(instance, provider_config_id, version)`；会话（ASR）或单次调用（LLM）结束后 `release()`。
- 切换后：新会话/新调用用新实例；旧实例进入 `draining`，租约归零或超过 `drain_timeout = 最大 max_record_seconds + 120 s` 后 `close()`。
- 并发名额按新 provider 的 `max_concurrent_sessions` 计算；已超出的在途会话不终止。

### 7.4 重试时 provider 已切换

见 §3.7 第 5 条：能整段重送则重送；超出新 provider 上限或新 provider 不可用 → `retry_kind=none`、`retry_unavailable_reason=provider_changed`，提示重新录音；每个 attempt 记录实际 provider。

## 8. 管理 API、后台界面与原型

### 8.1 管理 API

前缀沿用现有 `/api/admin`（`api/admin.py:33`），均需 `admin`；错误体沿用 `ApiError`；请求与响应不含 Key 明文。

| 方法 路径 | 说明 | 关键错误 |
|---|---|---|
| `GET /api/admin/providers?kind=asr\|llm` | 列表：id、name、type、base_url（业务空间段脱敏）、model_id、params、enabled、`is_active`、`secret_set`、`secret_last4`、最近测试、`version` | – |
| `POST /api/admin/providers` | 创建（`api_key` 只写） | 422（含 `egress_denied`）、502（测试失败）、409 |
| `PATCH /api/admin/providers/{id}` | 修改（`version` 必填；`api_key` 缺省=不变，空串=清除） | 409、422、502 |
| `DELETE /api/admin/providers/{id}` | 删除 | 409（当前使用中） |
| `POST /api/admin/providers/{id}/test` | 连通性测试 | 502（脱敏原因） |
| `PUT /api/admin/providers/active` | `kind`、`provider_id`、`version`、`confirm_cloud` | 409、422、502 |
| `GET /api/admin/providers/active` | 各 kind 当前项、来源（db/yaml + 原因）、并发上限、录音上限、最近测试 | – |
| `GET /api/admin/providers/audit?limit&cursor` | 审计（字段名级） | – |

用户侧：`GET /api/v1/capabilities` 增加当前 ASR 的 `max_record_seconds`、`stable_partial`、`supports_hints`；会话创建响应增加本会话 `max_record_seconds`（§4.1）。

### 8.2 后台界面

`admin/src/views/ProvidersView.tsx`，沿用现有 Vite/React、样式与 `http.ts`/`api.ts`：两栏（语音识别 / 文字整理）各一张列表，列为名称、类型、模型、状态（当前使用/可用/停用/测试失败）、最近测试；操作：新增、编辑（Key 显示“已设置 ••••末 4 位”，可替换/清除）、测试连接、设为当前（云端 ASR 弹隐私确认）、停用、删除；底部审计抽屉。保存/切换失败显示后端脱敏原因，原当前项保持高亮。覆盖默认、加载、空、错误、版本冲突（提示重新加载）、禁用（当前项不可删）状态。

### 8.3 原型（S0 补充，S3 之前完成）

本人同意先补原型（R8）。**2026-10-27 已完成**：`#admin-providers`，12 个状态、58 项浏览器核验通过，见 原型核验（内部记录，未公开）。按 AGENTS §2/§4：在 `prototype/` 新增 provider 管理页（纯 HTML/CSS/JS，沿用现有后台原型样式与虚构数据，例如“百炼云端识别”“本机识别”“DeepSeek 整理”），覆盖 §8.2 全部状态；`docs/01-prototype/` 更新页面清单与状态矩阵；再复核 PRD-v2 与本设计是否与原型一致，然后才进入 S3。后台页面无参考产品对照，不虚构参考产品截图。

### 8.4 错误码（R12，S3 编写时补充）

沿用 [api-contract](api-contract.md) §1 统一错误体与既有码；V2 只新增下列码或 `details.reason` 取值。

| 场景 | HTTP / code | details |
|---|---|---|
| 未登录 / 非管理员 | 401 `AUTH_INVALID` / 403 `FORBIDDEN` | – |
| 配置不存在 | 404 `NOT_FOUND` | – |
| 参数非法、未知字段、越界 | 422 `VALIDATION_ERROR` | `fields:[…]` |
| 地址不在白名单 / 非法协议 / IP 字面量 / 解析为私网 | 422 `VALIDATION_ERROR` | `reason:"egress_denied"`, `fields:["base_url"]` |
| 本机识别在非 macOS Apple Silicon 主机 | 422 `VALIDATION_ERROR` | `reason:"host_unsupported"`, `fields:["type"]` |
| 首次启用云端 ASR 未带 `confirm_cloud=true` | 422 `VALIDATION_ERROR` | `reason:"cloud_confirmation_required"`, `fields:["confirm_cloud"]` |
| 版本冲突 | 409 `VERSION_CONFLICT` | `current_version` |
| 同类重名 | 409 `PROVIDER_NAME_EXISTS`（新增） | `fields:["name"]` |
| 删除/停用当前使用项 | 409 `PROVIDER_ACTIVE`（新增） | – |
| 把停用项设为当前 | 409 `PROVIDER_DISABLED`（新增） | – |
| 保存/测试/切换时连通测试失败 | 502 `PROVIDER_TEST_FAILED`（新增） | `reason ∈ auth_failed, rate_limited, unavailable, timeout, bad_request`；不含 Key、完整 URL 或云端原始报文 |
| 创建会话时无可用 ASR 或 LLM | 503 `SERVICE_UNAVAILABLE` | `reason:"asr_unavailable"` 或 `"llm_unavailable"` |
| ASR provider 并发名额已满 | 429 `CAPACITY_EXCEEDED`，`Retry-After: 1` | `reason:"provider_limit"`（与既有 `active_limit`、`audio_budget` 并列） |
| 重试时 provider 已变更且新 provider 无法承接 | 409 `RETRY_UNAVAILABLE`；Session `retry_kind=none`、`retry_unavailable_reason=provider_changed`（新增枚举值） | `reason:"provider_changed"` |
| 云端识别失败 / 超时 | 会话 `error_code` 沿用 `ASR_FAILED` / `ASR_TIMEOUT`，WS 1011 | 内部原因（`auth_failed`、`rate_limited`、`unavailable`、`bad_request`、`timeout`、`egress_denied`）只写 `model_calls.error_code` 与日志 |

## 9. Linux 部署（要点，细节随 S4 补入 `deployment.md`）

- Python 3.11–3.13；`uv sync` 不装 `mac` 可选依赖（无 MLX）；Linux 只配置云端 ASR + OpenAI 兼容 LLM；`qwen3_mlx` 在非 macOS arm64 主机构建与测试时返回“当前主机不支持”。
- 沿用 V1：单 Uvicorn worker、单应用副本、advisory lock（第 1 版的“多进程广播”待决项删除）。
- 环境注入（不进 Git）：`SVI_DATABASE_URL`、`SVI_SECRET_KEYS`（JSON：`{key_id: base64(32B)}`）、`SVI_SECRET_ACTIVE_KEY_ID`，以及 YAML 兜底用的 `api_key_env` 变量。
- 进程内存：PCM 预算 1 GiB（§4.2），主机为应用进程预留 ≥ 2 GiB。
- 前置 Nginx/Caddy 终止 TLS，仅暴露 HTTPS/WSS，`proxy_read_timeout` ≥ 最长会话 360 s + 120 s；客户端地址改 `desktop/src/shared/config.ts`。
- 出口：服务器防火墙/代理只放行白名单域名的 443 出站，与应用层白名单双重约束；时钟同步。
- 健康检查新增“provider 来源/当前项/最近测试”。

## 10. 可观测与告警

记录（无音频/转写/Key）：会话 attempt 的 `asr_provider_id`、段数、`asr_reconnects`、`asr_resend_s`、停止时积压秒数、阶段耗时；provider 级活跃会话数、连接失败率、`rate_limited` 次数、关闭帧 1001 次数；后台“模型与用量”增加按 provider 的 ASR 秒数与调用数。告警阈值（建议，S5 定值）：10 分钟内 `unavailable`/`bad_request` 占比、`asr_reconnects>0` 的会话占比、并发接近上限。

## 11. 容量与成本

- 云端无 `execution_slots=1` 限制，但**云端账号并发配额与长连接稳定性未知**：批次 07 在 24 路并发下约一半长连接被成批断开；批次 10 在 6 路并发、绕过本机代理时仍出现 2 次云端主动断开（1001）。因此 `max_concurrent_sessions` 默认 8，目标环境实测后再放开；V1 的 20 路目标在云端 provider 下需重测。
- 成本按音频秒计费（以官方价目页为准），用量统计按 provider、按日提供音频秒数。

## 12. 与实验结论的对应

| 设计点 | 依据 |
|---|---|
| 手动 commit；`text` 单调、`stash` 可修订 | 批次 01（A30 101 条 `text` 0 次违反单调） |
| 不传 ASR 词表 | 批次 02 |
| 端到端预期 ≈1.1 s（ASR 0.25 + 整理 0.8） | 批次 03 |
| 长录音分段、30 秒、遇首个安静块切、不暂存 | 批次 04、05、09（Q30c 平均 CER 0.98%，停止 0.34 s；暂存 4.26 s） |
| 补送限速 2×，禁止突发 | 批次 08（2×：6/6 成功、≤4 字差；不限速：2/6） |
| 断线恢复为必需；重连 0.4–0.55 s；从首个无结果段重送 | 批次 09 阶段 C（10/10 会话完整；1/10 丢句）；批次 10（云端两次 1001 主动断开，无恢复的流整段失败、有恢复的流无损） |
| 直连、忽略系统代理；并发默认保守 | 批次 01、07、10 |
| 360 秒上限有余量 | 批次 10：15 分钟、20 分钟单会话均可完成，未发现时长上限 |

## 13. 待决与风险

| # | 事项 | 当前处理 |
|---|---|---|
| T1 | 恢复后偶发丢句 | **本人决定不提示用户，作为缺陷后续修复**；元数据与诊断日志见 §3.8 |
| T2 | 云端单会话时长上限 | 批次 10 未发现；会话轮换仅作后备，首版不实现 |
| T3 | 重试只重送未完成段 | **已采用**（§3.7），不再待决 |
| T4 | 恢复追赶期间停止、重试的 25 秒截止 | 推算，未实测；S5 目标环境验证 |
| T5 | 360 秒录音的整理（约 2700 字）可能超过 DeepSeek 调用 10 秒上限（`config.py:123`），超时回退原文 | **本人决定先不补测**；S5 用真实长录音观察，若超时再调整调用上限或分段整理 |
| T6 | provider 管理页原型 | **本人同意先补**（§8.3），S3 之前完成 |
| T7 | 主密钥丢失/轮换 | `svi secrets rotate` CLI：新增 key_id、重加密、验证、停用旧 key；主密钥丢失则云端 Key 需重新录入，期间回落 YAML |
| T8 | 白名单默认清单与百炼国际站域名 | 部署时按账号地域填写；国际站域名拿到账号后核实 |
| T9 | provider 并发默认值与云端账号配额 | 默认 8；目标环境实测 |
| T10 | 3× 补送是否可用 | 未测；设计上限固定 2.0 |
| T11 | 失败音频是否计入 PCM 预算 | S4 核对代码后在部署清单写明（§4.2） |

## 14. S4 实现分解

1. **provider 层**：`bailian_realtime`（Segmenter/Ledger/SendQueue/Connection/Recovery/resume）+ 云端模拟服务 + 单元与集成测试（最先做）。
2. **运行时与数据**：录音上限按 provider/会话固定、迁移（约束 5,760,000、`asr_hints_applied`、attempt 新列、`model_calls` 新列、provider 三表）、PCM 预算、截止时间、租约、重试 resume、能力与会话响应字段。
3. **配置与安全**：SecretBox（新增 `cryptography`）、ProviderRegistry、白名单校验器、ProviderService、ProviderManager（含 LLM 热切换）、`svi secrets rotate`。
4. **管理 API**：§8.1 全部接口与契约测试。
5. **原型 → 后台界面**：先 §8.3 原型并复核，S3 通过后实现 ProvidersView（先 Mock，后联调）。
6. **桌面端**：录音上限改为使用会话下发值（§4.1）。
7. **Linux 部署**：依赖拆分、systemd/反向代理样例、出口白名单、部署清单；目标环境联调（真实云端 + DeepSeek）、并发与延迟实测。
8. **验收**：S5 在 Linux 实机 + 真实云端；真人 5–6 分钟连续讲话由本人在开发完成后自测（本人 2026-10-27 决定）。

## 15. S4 实现澄清（2026-10-27）

以下是实现时按代码现状做的澄清，不改变本人已定的需求与判据；若与上文冲突，以本节为准并已同步测试记录。

| # | 项 | 实现 |
|---|---|---|
| C1 | PCM 预留与单会话上限（§4.1/§4.2） | 不另设“所有 provider 最大值”的动态计算：沿用 `audio.max_duration_seconds` 作为部署上限（V1 固定 180，改为可配 10–360），每会话按它预留 PCM；会话上限 = min(当前 ASR provider 的 `max_record_seconds`, 该部署上限)，创建时固定并写入 `sessions.max_record_seconds`。V2 部署配置 `max_duration_seconds: 360`、`active_pcm_budget_bytes: 1073741824`。这样后台把云端设为当前时，若部署仍是 180 秒预留，会话上限自动为 180，不会超出已预留内存 |
| C2 | 失败音频是否计入 PCM 预算（T11） | 不计入：失败音频另受 `retry.audio_budget_bytes`（默认 128 MiB）与 `retry.max_cached_sessions`（≤20）约束，超出按到期先后淘汰。V2 部署应把 `retry.audio_budget_bytes` 设为 ≥ 20 × 11.52 MB ≈ 230 MB，否则 360 秒失败音频最多保留约 11 份；进程内存需求 = 活动预算 + 失败预算 |
| C3 | finish 截止（§4.4、TC-V2-RT-14） | `asr.finish_timeout_ms` 上限放宽到 25000，但只有 YAML 当前 ASR 为云端类型时允许 >10000，本地 provider 仍拒绝；云端会话截止固定按 `end_accepted_at + min(25 s, max(10 s, 积压秒数/补送倍速 + 5 s))` 计算，不受 `finish_timeout_ms` 影响 |
| C4 | 录音中途云端失败（§3.3） | provider 内部恢复失败后不立即中断录音：`feed` 继续接收（运行时保留完整音频），失败在 `finish` 时一次性抛出，会话 `error_code=ASR_FAILED`（`timeout` → `ASR_TIMEOUT`）、按 BR-05 可重试；内部原因只写 `model_calls.error_code` 与日志。由此产生的长尾重送问题见 README 待决 T12 |
| C5 | attempt 重送量字段（§3.8） | 列名用 `session_attempts.asr_resend_ms`（BigInteger 毫秒），而非 `asr_resend_s` 浮点秒，便于聚合（AGENTS §5.1 单位明确、可聚合） |
| C6 | ASR 的 `model_calls.purpose`（§6.1） | 沿用既有约束值 `asr_finish`（每 attempt 一行，开始于 `open`，结束时补写 `processed_audio_samples`、`segments`、`reconnects`），不新增 `asr` 取值、不改既有约束 |
| C7 | websockets 日志（§3.6） | websockets 在 DEBUG 级记录含 `Authorization` 的握手头；云端连接使用专用 logger `svi.asr.bailian.transport` 并固定 WARNING 级，任何部署日志级别都不写出 Key |
| C8 | 错误事件格式（§3.5） | 实验只保存过截断的 `invalid_request_error` 文本，映射按 OpenAI 兼容 realtime 格式（`error.type/code/message`）实现。2026-10-28 真实云端核对：正常事件字段与解析一致（记录（内部记录，未公开））；错误 Key 在握手阶段即 401；运行中的 `error` 事件仍未在真实云端观察到 |
| C9 | 热切换租约（§7.3） | 不新建 Lease 对象：ASR 以 provider 自身登记的进行中会话为租约（会话结束后从中移除），旧实例在无会话或超过 `max_duration_seconds + 120 s` 后关闭；LLM 切换时把 `state.llm` 与整理/分类服务的引用指向新实例，已开始的调用持有旧实例引用并在旧实例完成，旧实例在其调度器无运行/排队调用（或 120 s）后关闭。这样保持 V1 的 `state.llm` 类型与测试约定不变 |
| C10 | 连通测试细节（§7.1） | 云端 ASR 测试禁用内部重连（报告第一次失败，避免 8 次重连拖长测试）；本机 `qwen3_mlx` 的“测试”只校验主机支持，模型在设为当前时加载；mock 类型仅 `environment=development` 可建 |
| C11 | 未配置主密钥（§6.1） | 保存或读取带 Key 的配置时返回 503 `SERVICE_UNAVAILABLE`、`details.reason=secret_unavailable`（§8.4 未列，新增 reason，不改既有码含义） |
| C12 | LLM 出口 IP 固定（§6.3(3)） | ASR 建连固定已校验 IP；LLM 的 HTTP 客户端在保存/测试/切换时校验白名单与解析结果，但实际请求未固定 IP，依赖网络层出口规则兜底（deployment §10.4 已列为已知限制） |
| C13 | 重定向（§6.3(4)） | websockets 默认最多跟随 10 次重定向；云端连接改为一律不跟随，3xx 视为握手失败（`unavailable`） |
| C14 | 静音语气词（§3.4，2026-10-28 真实云端发现） | 云端对纯静音返回“嗯。”。仅当某段没有任何有声块（250 ms RMS ≤ 200）且文字去标点后只含语气词时，该段文字视为空；有声段原样保留。避免无语音时进入整理/插入 |
| C15 | fake-IP DNS（§6.3） | 服务器 DNS 若经 fake-IP 代理（解析到 198.18.0.0/15 等非公网段），白名单解析校验会按设计拒绝；部署须使用返回真实地址的解析器 |
| C16 | 重试截止（T12，2026-10-28 本人决定） | 云端重试的 ASR 截止 = 重送时长（积压/补送倍速）+ 10 s，不封顶 25 s；本次重试尝试的整体预算顺延同样时长；均受失败音频截止 `D` 约束。原设计“重送 ≤ 一段”只在停止前不久断线时成立 |

## 16. 已知限制（S5，2026-10-28 本人决定记录）

2026-10-28 后续验收决定：本人在了解 L1 时延/测试覆盖和 L2 仅保留应用层白名单的现状后，明确接受两项限制并确认本轮验收通过。对应 E2E-02、DEP-04 记为 pass（本人接受已知限制），本节技术事实继续保留；见 本人验收确认（内部记录，未公开）。该决定不涵盖其他独立缺陷或推送、合并、发版。

| # | 限制 | 说明 |
|---|---|---|
| L1 | 停止→出字 p50 约 2.7 s | Linux + 百炼 + DeepSeek 实测：ASR 收尾 p50 1.31 s、整理 p50 1.14 s；未达 V1 目标 p50 ≤2 s（p95 4.25 s 达 ≤5 s）。暂不优化 |
| L2 | 网络层出站白名单 | 只启用应用层白名单（§6.3）；静态 IP 的 nftables 白名单会被 CDN（DeepSeek）轮换打破，需按域名/SNI 的出口代理才能做网络层双重约束 |
| L3 | 云端无响应停顿 | 实测 1/36 会话在 finish 截止内云端无任何结果且连接未断，以 `ASR_TIMEOUT` 结束、可重试（BR-05）；在 10 s 截止内无法检测并重送补救 |
| L4 | 恢复后偶发丢句 | 原 T1，不提示用户，作为缺陷后续修复 |
| L5 | 中断后的恢复时延 | 指数退避（0.25→4 s 封顶）使短时中断后出字变慢（3 s 中断时停止→结果约 14 s），换取约 20 s 的中断容忍 |
| L6 | 用量口径 | 后台 ASR 用量含失败调用已发送的音频（与云端计费一致） |

