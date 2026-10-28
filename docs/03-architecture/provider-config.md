2026-10-26用户采用[真人ASR质量容错口径](../02-prd/asr-quality-decision-20261026.md)：整体原意基本保留时允许少量词语/专名听错；不为当前样本增加纠错、词表、提示或模型切换。“大概90%”不作为实测准确率。provider接口、稳定partial、尾帧、资源预算及DS-R2固定提示/一次整理不变，R2仍以实际转写和授权词表为输入。

2026-10-23当前接入以[R2正式设计](r2-production-design-20261023.md)为准：新增Qwen原生流式、refine text与classify JSON分用途；下文旧edits合同仅为legacy_edits兼容历史，不能用于R2正文成功判定。

# Provider、提示与执行预算（v0.3）

2026-10-17。这是可实现的接口设计，具体模型/硬件组合须经P1/P2实测；示例值不代表已验收。部署位置是配置，业务层依赖统一接口。V1仍受业务边界约束：**ASR只可本机/获准内网，LLM可本地/内网/公网；音频不发送公网**。

## 1. 配置与能力

当前本机 `mlx_whisper` 适配器使用已安装 mlx-whisper 0.4.3 的目录约定：`config.json` 加 `weights.safetensors`（优先）或 `weights.npz`。模型摘要必须绑定加载器实际选择的同一权重文件；目录同时有两者时也不得哈希另一个文件。只有 Hugging Face 名称 `model.safetensors` 而无上述文件时，启动明确报告本地模型不完整；不把它误判为有效加载、不修改原模型目录、不自动下载。其他 provider 保持各自配置约定。

```yaml
asr:
  provider: whisper_cpp          # 预定首个Linux适配器；须实现稳定partial后验收
  model_id: deployment-asr-id    # 运维自定、可追溯标识，不冒充实测模型版本
  model_path: /opt/models/asr/model.bin
  model_sha256: REQUIRED_MANIFEST_VALUE
  language: auto
  stable_partial_required: true
  decode_window_ms: 10000
  decode_interval_ms: 1000
  execution_slots: 1           # 本地起步，实际吞吐须测；非会话上限
  decode_timeout_ms: 3000      # 普通窗口执行超时
  finish_timeout_ms: 10000
  remote_base_url: null          # 本地实现为空；远端仅允许部署批准的内网地址
  api_key_env: null
llm:
  provider: openai_compatible
  model_id: deployment-llm-id
  base_url: https://llm.example.invalid/v1
  api_key_env: SVI_LLM_API_KEY
  structured_output: json_object # json_schema | json_object | prompt_json，启动时选定一种
  temperature: 0
  max_output_tokens: 4096
  max_inflight: 4              # 需匹配上游配额/算力，非20路性能承诺
  classify_queue_size: 20
  foreground_wait_ms: 2000
  classify_wait_ms: 1000
  call_timeout_ms: 10000
  automatic_retries_per_attempt: 1
  fidelity_audit: off   # 2026-10-19用户确认取消第二次LLM审核，不能重新启用
pipeline:
  stop_to_result_budget_ms: 30000
  classification_budget_ms: 10000
```

- 配置由Pydantic校验，未知字段拒绝；所有单位明确。硬上限为协议/预算的一部分，配置不能提高180秒录音、2秒ACK、30秒结果预算等产品边界；性能调优更改必须同步契约。示例placeholder未替换时启动配置校验失败，不能拿示例发请求。
- 本地模型必须在发布前准备，manifest记录来源、版本和SHA-256；校验实际文件摘要，不运行中下载。model_id随权重/服务版本改变，不把“同一个名称”当成相同模型证据。
- 只实现本次验收选定的适配器；接口允许后续加mlx_whisper/faster_whisper/远程内网ASR，不在骨架阶段承诺全部可用。macOS开发期如选mlx适配器须单独验收同一稳定partial契约。整段识别可以作为适配器开发测试，但 **不满足V1验收能力，readiness不通过**。
- structured_output为已验证服务能力的静态配置。启动探测所配模式是否可接受，不对每次用户请求依次盲试三种协议；不支持时明确报告配置错误。provider不在业务层临时切换厂商或模型。
- ASR remote地址须命中配置的精确主机/地址允许列表并由出口网络限制；校验解析结果、重定向目的地，禁止把公网地址伪装成内网域名。LLM仅发送本次转写和受限词库提示/局部纠错，不发送音频和原输入框全文。

## 2. ASR 生命周期和数据类型

接口示意（不是已开发代码）：

```python
class AsrProvider(Protocol):
    model_id: str
    capabilities: AsrCapabilities
    async def prepare(self) -> None: ...
    async def open(self, options: AsrOpenOptions) -> AsrSession: ...
    async def health(self) -> ProviderHealth: ...
    async def close(self) -> None: ...

class AsrSession(Protocol):
    async def feed(self, pcm_s16le: bytes, start_sample: int) -> None: ...
    def partials(self) -> AsyncIterator[AsrPartial]: ...
    async def finish(self, total_samples: int, deadline: float) -> AsrFinal: ...
    async def cancel(self) -> None: ...
```

| 类型 | 必需字段与不变量 |
|---|---|
| AsrCapabilities | `encoding=pcm_s16le,sample_rate_hz=16000,channels=1,stable_partial=true,backend_version` |
| AsrOpenOptions | `session_id,user_id,attempt_no,language="auto",hint_terms:list[str],max_samples=2880000`；内部可选 `outcome_deadline,eligibility_deadline` 为同一事件循环绝对秒；hint_terms≤100/2000码点，无原输入框正文 |
| AsrPartial | `revision,stable_text,provisional_text,decoded_samples,received_samples`；revision单调，stable前缀只增长，样本位置基于本会话0点，decoded≤received |
| AsrFinal | `text,segments:[{start_sample,end_sample,text}],no_speech:boolean,model_id,processed_samples`；segment位于[0,total_samples]，非重叠顺序；no_speech时text空，其他非空；text≤32768码点且保持stable前缀 |
| ProviderError | `code,retryable,stage,provider,model_id`；不含请求正文；code为unavailable/timeout/invalid_input/invalid_output/refused/cancelled/transport_error/resource_exhausted |

provider实例只共享只读模型/连接池，不共享用户prompt、音频、稳定前缀。每次open创建独立session；feed仅按序追加新PCM，不传“全量还是增量不明”的bytes。外部音频ACK由session服务校验/接收后立即发送，不能依赖feed背后的模型完成。

本地适配器把模型批量decode包装成流式：维护每轮缓冲、滑动窗口和LocalAgreement-2（连续两次解码一致的前缀提交），stable不回退；窗口移动基于样本绝对位置，保留未稳定尾部与必要重叠，避免尾句重复或丢失。finish强制处理所有尚未稳定样本/非整帧尾音再合成最终结果。decode_window/interval只影响内部策略，不改变外部250ms帧。若尾部无法满足稳定前缀契约，应抛invalid_output，而非静默覆写已经稳定内容。

CPU/GPU阻塞解码必须在专用工作线程/子进程执行，不能阻塞ASGI事件循环；V1使用独立模型worker执行池，每worker同一时刻一个decode，便于隔离和强制取消（这不是第二个Uvicorn worker）。cancel幂等，停止消费/清理缓冲、终止或丢弃未完成解码；无法立即中断底层模型时隔离任务，过期输出不得回到已取消session。每次decode/finish实际处理样本数单独计入model_calls，重复窗口解码不得当作新增用户音频。

ASR重试收尾保留原操作预算 `B=min(attempt.started_at+10秒,attempt总deadline)`，音频资格为 `E=min(B,首次失败的D)`；物理执行截止为 `min(E,B−1秒账本预留)`。不能先把B裁成D再从D扣1秒，否则最后1秒合法重试永远不能派发。取消/到期先同步清理可控PCM，原账本收尾沿B，不给识别或结果续时；未提供可选字段的既有provider调用仍沿原单deadline语义。两个字段只在内部适配器传递，不新增HTTP/WS业务字段，也不承诺线程序列化副本已被bytearray清理覆盖。

## 3. LLM 接口及统一响应

```python
class LlmProvider(Protocol):
    model_id: str
    async def prepare(self) -> None: ...
    async def generate(self, request: LlmRequest, deadline: float) -> LlmResponse: ...
    async def health(self) -> ProviderHealth: ...
    async def close(self) -> None: ...
```

| 类型 | 字段 |
|---|---|
| LlmRequest | `purpose:refine\|audit\|classify,system_prompt_version,input:typed_object,output_schema_id,max_output_tokens,temperature=0,call_id` |
| LlmResponse | `parsed:typed_object\|null,provider,model_id,finish_reason:stop\|length\|refusal\|error,prompt_tokens:int\|null,completion_tokens:int\|null,elapsed_ms:int,error:ProviderError\|null` |

adapter负责API形状、结构化模式、响应解析、用量/结束原因映射；不能只返回dict丢失截断/拒绝/未知用量信息。响应即使可解析JSON但finish_reason=length也不可当成功。取消通过协程取消+底层连接中断传播；未完成外部调用记unknown，不宣称没有费用。

### 3.1 业务输出

- refine 的模型输出协议为 `refine-v5-dictionary-no-audit`：顶层必须有 `edits`（0–64项），可选 `candidate_text`（1–32768 Unicode码点），不得有其他字段。每项由严格 `oneOf` 选择下列五种形状，字段不能跨 kind 混用：
  - `filler` / `repetition` / `format`：仅 `kind,before,after`。before 为原文唯一、非空、逐字匹配的连续引文；after 为该引文改后片段。两者最多32768码点。filler 仅删除一个连续口头禅片段，repetition 仅压缩无意重复，format 仅改不影响保护内容的标点/空白。
  - `self_correction`：必须且仅有 `kind,before,after,old,trigger,final`。后三个引文分别在 before 中唯一、逐字匹配且按旧值→明确改口词→完整最终说法排列；old 至 trigger 间仅标点空白，trigger 必须是原有明确改口词，不能任意取“是”。after 只能把 old 至 final 的完整范围替为原 final，保留外围语境。完整数字单位、否定、至少等约束不能截短。
  - `term_correction`：必须且仅有 `kind,before,after,dictionary_term_id,dictionary_alias`。身份来自本会话冻结快照；after 只把 before 中一个完整 alias 位置变成该词条原样规范词，外围语境不变。兼容同一条证据中同一 ID/alias 的多处纠正：仅当 before 内至少两处原样 alias 一次性替为规范词能逐字重建 after 时，拆成逐位置 canonical 证据（合计最多64条）；不递归替换，不补改未声明位置，不允许夹带其他变化。每处继续执行第7节原有保护；不能仅因同一条证据包含两处映射而拒绝。
- 所有 before 引用同一份原 raw，不引用前项 after；范围不得重叠。服务端只在精确且唯一匹配后计算原 raw 的 Unicode 半开区间，转换成既有 canonical edits（`kind,source_start,source_end,source_text,replacement_text`，改口另带 `trigger` 和 `replacement_source_start/end`，词库另带三项冻结身份）。不修补错误引文、猜测重复位置或补写最终说法；定位/证据错误不获得额外重试。
- 模型应省略无意义编辑；严格字段/唯一原文定位已通过的 `before==after` 仅视为无操作元数据，不产生 canonical edit、改口或词库豁免。它仍参与范围冲突检查，也不能掩盖非法字段、候选中的保护内容变更或现有未整理检查。
- 未给 candidate_text 时，将全部有效编辑一次性应用原 raw 得到稿件；给出时使用该完整稿进行原有 `validate_edits`、保护检查和未整理检查，不调用第二次LLM审核。不能用派生稿覆盖、兜底或修正错误候选，也不能用只适用于派生稿的位移去证明自由改写后的词条位置。保留忠实自然语序改写能力，不要求候选等于机械替换稿。声明口头禅在唯一相邻语境仍保留时，沿用现有确定性拒绝；这不是全部语义整理已被确定性证明。
- 历史audit协议（仅兼容读取与历史实验，当前不派发）：`{decision:"pass"|"reject",violations:[{category:"fact"|"number"|"negation"|"condition"|"reason"|"proper_term"|"language",source_start,source_end,reason_code}]}`。旧输入为raw/candidate/已验证改口和词库证据；2026-10-19已取消自动复审调用，不能把历史协议解释为当前必需步骤。
- 历史复审输入曾包含 `verified_repetitions`，当前校验辅助结构保留、但不发送复审：每项仅 `kind,source_start,source_end,source_text,replacement_text`，偏移仍为原raw码点；精确片段、重复、重叠及其他原校验全部成功后才能取得。空证据不添加该键，保留原审核输入。此证据只证明局部词法与位置，不证明强调或其他语义可删除；原程序保护照常执行，不派发独立审核，拒绝仍回退，不增加调用、重试或预算。
- classify：`{decision:"term"|"suggest"|"ignore",ambiguous:boolean,category:"proper_term"|"spelling"|"other",evidence:{before_start,before_end,before,after},reason_code}`。只收到本次插入片段局部before/after及左右各≤80上下文。验证证据与请求完全匹配；decision=term、局部guard通过、无歧义/别名冲突且类别正确才自动learned；其他可能词语修正suggested，非词语ignored。**不使用未校准的模型置信数值门槛**。

提示及schema随代码版本管理，测试用固定正反文本核对；不能为了通过某个模型把非法输出当空对象。模型请求使用独立 schema 副本，返回后按 canonical schema 重新校验，不能由 provider 修改请求 schema 放松验证。数字/单位/路径/英文专名/否定/条件/理由的确定性检查先运行；明确改口仅豁免有有效位置证据的被撤回旧值，保留新值及其他数字。“先3次再2次”不能被“改口”豁免。

正式服务工厂显式选用上述精确引文协议，各 provider 使用相同业务链路；`RefinerService` 直接构造仍保留旧 `refine-v2-terms` 数值证据协议用于既有单测和对照。Mock 按实际请求的 schema ID 输出对应形状，仍经过真实转换/程序保护，不以 provider 类型绕过验证。2026-10-19用户确认：不再自动派发audit；旧schema及历史用量仅作兼容读取/历史诊断，classify不变。本次取消复审保留 HTTP Result 形状；S3 按用户明确决定修订，旧审核判据不再适用，真实模型仍需逐项验收。

### 3.2 提示选择

会话创建时固定dictionary_version及快照。ASR取最近updated_at的100个正确词且总≤2000码点，同更新时间按norm/id排序；整理在该快照匹配raw的term/alias取≤50条及最多3份语境、总≤4000码点，优先更长匹配再最近更新时间，平局norm/id。容量上限按实际提交字符串码点（包括alias/context）计算，逐条纳入，不截断半个词条；记录截断条数，不记录内容。

2026-10-19 用户修订词库纠正要求：唯一有效 `claw←cloud` 映射可以纠正同句多处 cloud，包括普通云服务语境；不再要求只改专名位置。共享 alias 的归属歧义仍明确传给模型、不自动任选目标；无映射词不能凭空替换，完整词边界及数字/单位/路径/URL/否定/引号内字面保护仍保留。所有用户文字视为数据，不能执行“忽略规则”等指令、回答问题或翻译。生产提示中旧的逐处语义消歧要求尚待同步与真实回归，不把文档修订视为已实现。

## 4. 时间预算、重试与回退

| 范围 | 约束 |
|---|---|
| 停止至结果总预算 | initial从audio.end接受起30,000ms，人工asr/refine重试从新attempt.started_at起30,000ms；两类耗时用attempt_kind区分，重试不计首次性能样本。含ASR收尾、整理、自动重试；包含有界队列等待；通过单一绝对deadline向下传递 |
| ASR收尾 | 等待与执行合计最多10,000ms且不越过总deadline；超时failed，可按完整音频缓存规则显式asr retry；没有后台自动ASR重试 |
| 每次实际LLM请求 | 最多10,000ms与剩余总预算的较小值 |
| 自动重试 | 一个session attempt的refine合计最多一次，仅传输/临时5xx或输出JSON/schema失败；不为拒绝、截断、保护失败自动重试；重试不延长总deadline |
| 修改学习分类 | 单event总预算10,000ms含最多一次同类重试，独立于已完成口述，不占录音槽但占LLM并发预算 |
| 人工模型重试 | 新attempt，重新计算该attempt30秒预算；ASR重试deadline同时受首次失败+600秒的D限制，到D立即禁止过期音频处理/迟到ASR输出，本地PCM≤D+1秒清空、独立worker≤D+2秒退出确认，确认前执行资源不复用；清理不增加10秒ASR/30秒结果预算，固定词库快照不变 |

取消第二次LLM审核，所有整理结果统一经过程序校验；无论标点或实质改动均不派发audit。对齐比较不是简单删所有标点后就放行，例如数字小数点、路径分隔符、否定保护先行；保护识别为语义标点的变动不可豁免。

ASR成功即释放音频并持久化raw+选择的词库提示；LLM不可用、超时、拒绝、截断、非法输出、保护失败或总预算耗尽均提交ready，refined_text=raw_text、review_required=true及准确reason。绝不能为等待LLM一直持有音频，也不能把程序校验失败的候选直接返回为可自动插入结果。普通UI不展示模型内部推理或输入prompt。

## 5. 启动、探活与容量

配置错误、未知provider、缺secret引用、模型缺失/摘要错误、ASR不支持stable_partial为readiness失败；进程可保留liveness供诊断但拒绝新会话。LLM配置结构错误同样失败；仅运行时暂时不可达允许degraded，ASR可用时仍接单并返回待确认原文。

健康探测不发送用户数据。provider需要数据探测时只用版本化虚构固定短样本，并单独标synthetic，不计用户用量。Ollama首次prepare用`svi-llm-health-v1`固定JSON健康生成代替空prompt仅加载：要求实际done/stop、生成严格`{"ready":true}`，元数据HTTP200、load-only、截断或非法输出不能标prepared；仅支持thinking时关闭思考，最大32输出token。用量字段缺失仍按未知，不新增用量必填门槛；保留原prepare外层10秒、重探测/LLM降级和业务10/30秒预算。该检查证明能生成，不证明业务整理质量。初始20个会话接纳槽，ASR execution_slots与LLM max_inflight分别配置；接纳不排队，模型采用有界调度。分类等待超过1000ms或队列满时event=failed/reason=MODEL_BUSY；前台整理等待超过2000ms或队列满时回退原文并标llm_unavailable，日志记录MODEL_BUSY，不延长30秒预算。资源扩容必须先实测硬件、模型和并发，不据配置示例宣称延迟达标。

## 6. 多会话调度契约（v0.3）

[并发设计第4–6节](concurrency-target.md)规定了ASR每会话最新窗口合并、finish/partial 3:1分派、独立上下文与取消资源确认；LLM前台/分类3:1分派、有限等待、每用户分类限额与429冷却。业务层只面向provider，不按本机/远端重写状态机。每个队列项必须携带租户/session/attempt/generation/deadline；提交前丢弃取消/过期/旧代输出。内网adapter的节点容量、会话亲和、取消与远端工作副本释放必须验收，未完成不得冒充可用provider。

工作进程数不按会话数自动增长；批处理只在适配器证明隔离、取消与稳定partial契约后启用。本地ASR工作线程/进程及内网ASR工作副本取消后未退出仍占执行资源。失败音频TTL的worker收尾上限自原D起算2秒，退出确认含实际进程/远端任务已停止，不能只清Python句柄或取消协程；超限继续隔离并明确故障，不以收尾期接纳同资源的新任务。本地100ms SIGTERM宽限及kill后join的既定更短策略保持，见[并发设计](concurrency-target.md)。公网LLM取消无法证明上游停止，max_inflight是本服务请求许可上限；许可保守持有至原请求deadline或明确终止回执，费用/远端执行状态可为unknown，详见并发设计第5节。


## 7. 逐位置词库纠正（S4内部契约复核）

2026-10-18 内部契约，2026-10-19 按用户确认修订语义要求。位置证据仍用于验证词条身份及实际改动，不再要求专名/普通词义逐处消歧或只改第一处。它不增加翻译、外部接口或模型调用；实现仍须按新业务口径回归，不能代表真实模型或音频验收通过。

- 模型输出与 canonical 证据分别严格按 §3.1 实现；`uncertain`、旧 `faithful` 和缺失字段均非法。转换后的 self_correction 最终说法必须有 `replacement_source_start/end`；旧数值协议的 source 区间允许仅覆盖触发词前旧值，或覆盖旧值至最终说法末尾，精确引文协议转换为后一种范围。其他部分重叠区间拒绝。原 raw 永不改写；保护比较仅豁免有精确证据的旧值/触发词，其余数字、路径、否定及英文大小写继续保护。
- 转换后的 canonical `term_correction` 必须同时携带 `source_start,source_end,source_text,replacement_text` 以及 `dictionary_term_id,dictionary_term,dictionary_alias`。后三项来自本会话冻结快照中的唯一词条 ID、原样规范词、原样 alias，不能从当前可变词库补齐。source 必须逐字匹配原 raw，对 alias 的身份比较使用既定 NFKC/casefold；replacement 必须原样等于冻结规范词。
- 确定性校验每个原文位置：词条 ID 唯一、alias 在完整冻结快照中无其他归属、ASCII 边界完整、无数字/路径/否定改写，且该位置在候选稿对应位置确实变成该规范词。允许多处有效 alias 均按映射纠正，不以“同句改了两处”判失败；每处实际变化仍须能从原文和冻结映射证明，相同词频或别处已出现规范词不能替代这种证明。没有有效证据就返回待确认原文。
- 历史 audit 输入 `verified_term_corrections` 每项含上述证据、该位置左右各至多 80 码点和冻结词条最多 3 份语境；保留原 raw/candidate 和 `verified_self_corrections`。有效唯一别名的多处纠正属于已允许的词库修改，不能只因普通含义或历史语境不同而拒绝；用户另已确认取消第二次LLM审核；这些实际文字效果仍需真实模型回归，不再通过额外模型请求验证。
- 创建时冻结完整词库身份/语境只存在运行内存；ASR 正确词选择仍最多 100 条/2000 码点，实际传入 decoder 的 initial_prompt。ASR 完成后按原 raw 匹配冻结 term/alias，再选最多 50 条/4000 码点并持久化受限提示；预算包含序列化的 ID、alias、语境和歧义标记。共享 alias 的歧义按完整快照判定，不因提示裁剪而消失。成功选择、取消、清理/到期时释放完整内存快照。
- 已存在的旧结果重试若冻结提示缺少 ID/alias 证据，不从当前词库猜补；该次词库改写保守回退。重启不保留失败音频或全量内存快照。
- classify 的 `before_start/end` 明确为请求局部 `before` 字符串的 Unicode 半开区间；该请求已经是一处修正，因此要求 `0,len(before)`，并与 before/after 原样匹配，不相对整篇口述或原输入框计数。
- 单次 attempt 只有 refine 一个整理逻辑请求，最多一次许可自动重试，且共享原 30 秒 deadline；分类保留独立 10 秒预算。证据错误不能触发额外调用，且不存在第二次LLM审核。

正式模型 schema 标识为 `refine-v5-dictionary-no-audit`；audit-v2-terms仅为历史协议；旧 `refine-v2-terms` 仅保留上述直接服务对照路径。必须以真实正反录音/文本验证，不得用旧 Refiner 单测证明生产链路正确。format 与 filler 不能借删除小数点改变数值，term 不能触碰原文完整路径/URL/数值/否定保护区间；这些拒绝分支已由 Codex 做受控前后对照，真实模型质量仍待验收。


2026-10-19程序无修改诊断边界：模型返回无需修改的完整原文时，不以“那个方案/那个人/额外”等正常词前缀认定未整理。句首或标点后的填充音/那个需具有空白、标点或文本结束边界，才进入这类确定性未整理检查；明确未清理的口头禅/重复仍按原判据处理。该检查不是完整语义证明，也不添加模型复审。


2026-10-19补齐程序边界：实际Mlx worker串行处理消息，进度条同步只在该worker内使用线程RLock，不创建跨进程进度锁，以免强停后信号量留至服务父进程退出；音频、队列、terminate/kill和2秒执行资源收尾时限不变。引用字面量必须在完整候选稿上保护，不因模型省略edits而失效；无修改诊断不扫描引用内的口头禅/重复。支持既有双引号/反引号及中文单引号，引用内容/出现次数保持，不能用自由自然改写绕过。

口头禅删除的原文边界：filler白名单只能证明片段形态，不能授权删除正常词的一部分。`那个/就是/就是说/然后/额`等有词汇含义的片段，与原文两侧文字直接相连时保守拒绝filler豁免；有空格/标点隔开时仍可按原规则处理，明确语气词`嗯/呃`的既有前缀处理保留。英文um/uh等必须按完整原文ASCII词边界检查，不能因在单独source中fullmatch就删除summer等词的一部分。numeric与正式before/after共用校验，不新增模型审核或猜测语义。该检查只校验声明的filler，不声称能证明任意自由改写的完整语义等价。
