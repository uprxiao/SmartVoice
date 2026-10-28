# Qwen 原生流式与 DS-R2 正式接入设计（S2）

2026-10-23。依据已授权的[S1增量](../02-prd/r2-product-change-20261023.md)。当前源码基点 `7b9c57e`，现状见沟通稿§3（内部记录，未公开）。本文定义目标接口，尚不表示实现或实测通过。

## 1. 整体方案

**录音中增量送入独立Qwen会话；停止排空尾帧，释放ASR音频；固定R2一次返回全文；沿用现有结果和插入链路。** 正文输出与学习分类按用途路由，外部HTTP/WS及数据库正文形状不另起版本。旧JSON整理实现只作显式旧配置/诊断兼容，不参与R2内容生成或补救。

1. `asr.provider=qwen3_mlx` 选择Mac原生流式适配器。模型工作进程常驻并完成虚构预热才ready；每个user/session/attempt有独立状态。
2. `llm.refine_protocol=ds_r2` 选择固定提示及text合同。官方连接设置 `thinking=disabled`，SSE收齐后一次提交；不是边生成边写输入框。
3. 冻结词库结构仍由已有 `refine_hints` 选择；只将正确拼写转换为 `confirmed_terms` 发给R2。别名的确定性本机处理见§4。
4. `classification_llm` 可单独配置本地资源。R2使用公网时，未配本地分类则分类明确unavailable；不向正文公网连接发送真实修改片段，不伪称自动学词成功。
5. 原 `RefineResult(text,review_required,review_reason)`、session/result、原稿/整理稿、回执、历史、统计与最终光标规则继续复用。

## 2. 配置与部署身份

| 配置/对象 | 目标合同 |
|---|---|
| `asr.provider` | 新增 `qwen3_mlx`；旧mock/旧适配器保留显式兼容，不作为当前选型 |
| `asr.model_path` | 已有本地Qwen3-ASR 1.7B目录，禁止自动下载 |
| `asr.runtime_path` | 可选运维提供的已安装 `mlx_qwen3_asr` 包根；只在隔离worker内加入路径，不改服务全局PYTHONPATH |
| `asr.model_manifest_path` | 必填已有模型文件相对路径、字节数与SHA256清单；启动逐文件检查，无缺失/路径逃逸，清单摘要作为资源身份 |
| Qwen参数 | 原生chunk2秒、上下文30秒、max_new_tokens4096、固定endpoint；250ms接收，单worker串行执行，多会话状态分开；当前只支持execution_slots=1，不能借此宣称20路模型能力 |
| `llm.refine_protocol` | `ds_r2`为新默认；`legacy_edits`只保留显式旧链路回归 |
| `llm.thinking` | `disabled`或不发送；当前官方R2配置必须disabled，参数不盲发给不支持的服务 |
| `llm.max_output_tokens` | R2部署8192，单次10秒、自动异常重试最多1次、总30秒不变 |
| `classification_llm` | 可选独立LlmConfig，仅mock/回环或明确内网地址；密钥按独立env引用。未配置时本地旧provider可共享；公网R2默认禁止classify复用 |

已验研究环境、权重revision/10文件摘要及运行库v0.4.4见完整交接§5（内部记录，未公开）。研究Python/MLX与现有服务venv的运行库并不完全一致，正式运行必须留存实际版本/摘要，作为新变量验证，不冒称原实验同环境。启动不自动修改旧模型或下载替代权重。官方R2正文prepare另用`svi-r2-text-health-v1`虚构SSE生成，完整stop/DONE且正文为ready才成功；只GET models不能证明纯正文流可用。分类独立健康失败不阻断正文。

Mac先落地；MLX不外推Linux/Windows。冷启动校验/预热时间独立于暖调用，预热只用虚构静音，不计用户用量；实际业务调用和预热分开记录。缺模型、运行库、摘要不符或稳定前缀能力不足明确不ready。部署示例单列，不在未测试前直接替换用户运行中的配置。

## 3. LLM 请求、响应与故障

| 内部类型 | 变更 |
|---|---|
| `LlmRequest` | 增加`output_mode:json\|text`，默认json兼容旧classify；text仅用于refine。schema仍是canonical约束，text为string/minLength1/maxLength32768 |
| `LlmResponse` | 增加`text:str\|null`，text成功时parsed=null；json成功时text=null。二者混淆或形状不符为invalid_output |
| 调度/校验 | 两层校验均按请求类型；每次物理调用独立记账。不能由provider篡改schema、把正文包装成任意dict绕过 |
| 路由 | 正文、分类可不同资源池，分别使用实际provider/model记账；相同本地资源复用原有有界调度。分类不可用不阻断ASR/正文/历史，但学习返回明确失败 |

R2 system逐字复制归档规则及三组虚构示例，不在生产运行时导入experiments；SHA `a1efa73302779e50d7d80396e3020470c4f0b9c522f794e64cf78ef3fee386f8`。仅两条messages，user使用默认JSON空格、ensure_ascii=false序列化 `transcript` 和 `confirmed_terms`，不发送词条ID、上下文或参考产品保存稿。model=`deepseek-flash`、temperature0、thinking disabled、stream=true、stream_options.include_usage=true、max_tokens8192；不发送JSON response_format。

SSE必须有完整事件、唯一index0正文、finish_reason=stop及`[DONE]`；usage可缺，缺失记unknown而不是0。保留完整换行正文；空/纯空白、超过32768码点、拒绝、length/其他结束原因、正文后的非法增量、非文本、缺终止或异常断流都不返回成功正文。思考已关闭仍返回reasoning_content时作为协议异常，不把思考内容合入结果。解析、传输及收尾都受同一个绝对deadline约束，单个心跳不能续期。

可配置的本地Ollama兼容同一text/JSON判别合同：text不发送`format`，只接收`done=true`、`done_reason=stop`的完整非空正文；JSON分类仍带原schema。正文中的thinking/reasoning/tool_calls不能混入结果。其非流式HTTP读取仍受绝对调用期限限制；此兼容控制不表示本轮选择本地LLM替代已批准的官方Flash，也不表示本地模型质量已验收。

协议/可重试传输最多一次重试，重新发送同一原始请求；拒绝/截断/超时/内容缺陷不另发修复。重试不是第二阶段润色。失败沿用`llm_invalid_output/llm_timeout/llm_refused/llm_truncated/llm_unavailable/budget_exceeded`并保存原文、待确认，绝不发布半篇。HTTP异常只记录安全错误码，不把请求或密钥写日志。

## 4. 词库输入与本机别名

既有选择算法维持50词/结构化4000码点上限、全快照歧义判断和同句多位置。正确拼写按选择顺序去重后传`confirmed_terms`；不将别名直接作为正确词、不为提高成绩临时改原稿。

R2返回后，对原转写确实命中的冻结别名采用一次本机标准化（版本`r2-alias-v1`）：仅唯一身份映射、ASCII整词匹配、最长重叠优先，不递归替换；已有正确词跨度不再当另一个别名。不得改变引用、URL、路径、数字/单位、否定或借映射增加原话未命中的词。对候选中别名的保留也应用同一保护。原raw不变；后处理不触发新模型调用。歧义、不安全、身份不全时跳过，不猜改。

这与研究仅correct_terms输入有明确差异，必须单列别名正反例；R2原提示不变。正文自由整理后的语言质量不能由这个小范围替换器证明。

## 5. Qwen会话、资源与partial

`prepare/open/feed/partials/finish/cancel/health/close`沿用现有接口及`AsrOpenOptions`。独立worker内只共享只读模型，状态键包含user/session/attempt；每段重建state，2秒原生解码、30秒窗口滚动，尾帧finish，不用整段事后识别伪装流式。

feed验证连续样本、s16le偶数长度及上限，追加可清理的待处理PCM后立即返回，模型不阻塞WS接收ACK。父进程待解码backlog上限为2秒chunk加decode_timeout对应样本（默认共5秒）；实时超限明确繁忙失败，内部重试缓存回放可在原E/B期限内背压，不能无限增加驻留PCM。每会话至多一个待执行/在途解码；父进程保留尚未确认交付的PCM和计数，派发后清理副本；worker不保存完整180秒录音。finish等待在途结果，再把剩余尾音及原生finish作为同一顺序请求，不重复解码已确认chunk。

外部stable必须确实是先前已稳定文本的前缀扩展，且最终全文以稳定前缀开头；违反时明确ASR失败，不能把会改写的文字标stable。provisional是该稳定前缀后的候选。revision单调增加；decoded≤received≤会话总样本。库不提供逐词时间戳时仅发布整个片段样本范围，不伪造词级精度。token cap触发不可假装完整结果。

2026-10-23同音实测已证实v0.4.4的heuristic `stable_text`会内容回退，见13报告复现（内部记录，未公开）。当前待验证映射`qwen-committed-stable-v2`：feed使用不再重解码的`committed_text`为stable，活窗口全部作为provisional；finish才以完整text作为最终stable。不改识别正文、原生解码及尾音，只改变稳定标签的来源；父层单调/样本/前缀检查保留，首个invalid_output不能被随后generation-lost覆盖。短于首次窗口提交的录音可能没有非空中途stable，但完整候选仍逐次可见，最终照原规则完整提交。

执行池有界串行、finish优先且保留既有公平等待策略。2026-10-24红测发现已停止整块重新低优先级feed的实现偏差，按既有本段合同窄修：排队live任务移除后，将全部未确认PCM同finish作为高优先级请求，模型内部2秒chunk及已确认音频不变，见红绿验证（内部记录，未公开）。worker超时/取消必须确认terminate/kill后才复用；一个worker异常丢失全部原生状态时，关联会话明确失效，不静默用空状态续录。取消先清父PCM，排队请求去除，运行中请求强停；下一段实际预热后恢复。每次实际native feed/finish记账，内部窗口提交可能有额外decode，不冒称250ms一包就是一次推理。原600秒重试资格、E/B两个deadline、D+1秒/D+2秒清理边界继续执行。

## 6. 外部接口、表和状态

2026-10-24取消隔离窄修：上节“丢失全部原生状态时关联会话失效”只是原实现的失败兜底，不能作为原CAP11健康流误杀的许可。原20owner两个取消负控已证实失败；按[S2有界恢复](r2-qwen-recovery-20261024.md)保留健康owner最后确认窗口，在原deadline内恢复。无合法快照的流仍明确失败，不得空状态接尾。S3控制/实际OS/模型/TTL与全量计划（内部记录，未公开）先冻结后实现；不是更改原验收标准或提前通过。

| 对象 | 处理 |
|---|---|
| HTTP/WS Result及history | 复用raw_text/refined_text/review_required/reason/retry_kind；无新业务字段 |
| `model_calls` | 沿用provider/model_id/purpose/耗时/token/status；真实每次调用记账、缺usage为null。当前无prompt列，本轮先以部署配置/代码提交/提示SHA留档绑定，不谎称数据库已存prompt版本 |
| sessions/attempts/results | 状态和外键不变，ASR最终稿与refine输出独立持久化；本轮不需新业务表/DDL |
| recording→asr_finishing→refining→ready | 只有正常完整正文可正常ready；故障ready为review_required；取消沿旧终态不迟到提交 |
| desktop | 类型和API兼容；必须重验分点换行、隐藏主窗口、最新焦点一次插入、无目标历史和后续学习 |
| admin | 调用按真实路由身份展示；旧容量报告不能证明新Qwen/R2容量，缺受信运行身份显示unverified，不伪造valid |

## 7. 验证与门禁

S2由上述已查明的旧代码合同和S1范围推导，实施选择不新增业务范围。S3冻结类型/SSE故障、别名、原生Qwen隔离/尾段/回收、真实DB/WS/三端及20×3原速回归；原13项逐项追踪，既有延期不变。数据外发仍仅原转写/授权词表，真实派生分类若无本地路径保持不可用，不擅自云测。具体新版本通过数以新登记为准。
# 2026-10-24分类协议不变量补充

本机分类实模控制复现suggest但ambiguous=false；该组合与既有classify-v3含义矛盾。ClassifierService在schema及原样evidence核验后拒绝此组合，返回默认ignore与INVALID_CLASSIFICATION；学习服务保留failed审计终态，无词条或累计学习写入，不进行语义二次模型请求。合法不确定suggest仍由原流程处理且默认桌面不显示建议卡，term条件/整词不改。固定DS-R2正文提示、thinking disabled及独立本机分类路由不变。红绿和实际范围见专批（内部记录，未公开）。

