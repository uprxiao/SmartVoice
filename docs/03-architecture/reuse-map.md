# 自有实现复用清单

> 核查日期：2026-10-17。本文为方案 B 的实现依据，不扩大 PRD 范围。
> 证据类型：指定本地源码的静态只读核查；没有运行源应用、模型、单测或真实输入流程。已有测试只说明测试代码存在，不表示本次通过。
> 参考产品的观察与推断见研究文档；本文中的代码来自自有项目，不是参考产品私有代码，也不能证明参考产品服务端采用同一实现。

## 1. 来源与版本边界

所有相对路径以 SmartVoiceInput 仓库根目录为基准。下文采用两个来源简称：

| 简称 | 来源目录 | 版本依据 |
|---|---|---|
| D | `../SmartDD 早期原型/` | HEAD 为 `7f75b3bea26ffe3d68a21a13d5a4af80581f6352`，但工作区不干净；语音目录、`native/voice-input.swift`、`shared/voice.ts` 及多数语音测试为未跟踪文件，`shared/ipc/rpc.ts` 和 `package.json` 有未提交修改。必须以本文 SHA-256 锁定当前工作区文件，不能将语音实现标成该 commit 已包含。 |
| S | `../smartdd-speech-recognition/` | 核查时不是 Git 仓库；`pyproject.toml` 声明 `0.1.0`，版本号不能唯一确定内容，以逐文件 SHA-256 为准。 |

开发时只提取适用模块，不整体搬运 SmartDD 的终端、项目管理、数据库连接和发布流程。不读取或复制源目录的 `.env`、`config.local.yaml`、服务令牌、词库数据库、日志或个人数据。来源有变动时先更新哈希和适配结论。

## 2. 桌面端复用映射

“可复用”表示核心逻辑可保留，仍须适配命名、类型和新项目契约，并经过 SmartVoiceInput 验收。

| 模块与来源 | 已观察到的实现 | 分类及改造边界 |
|---|---|---|
| 工程依赖：D `package.json`；窗口：D `src/main/app/window.ts` | electron-vite、React、TypeScript、Tailwind、Base UI、MobX、zod、vitest、electron-builder 已存在；主窗口 `backgroundThrottling: false` | **复用工程组织，裁剪产品依赖**。沿用三段进程与别名，不照搬 SmartDD 全部依赖、窗口尺寸和发布规则；隐藏窗口录音需要真实环境验证。 |
| Typed RPC：D `src/shared/ipc/rpc.ts` | `createRPCController`、`createRPCRouter`、`registerRPCRouter`、`createRPCClient`；支持来源授权回调；客户端类型映射和错误消息清理 | **可复用**。泛型保证编译期类型，不等于运行时参数验证；每个暴露入口补 zod 校验和可信 sender 校验，preload 只放薄桥。保留该文件注明的 Yoda Apache-2.0 来源声明。 |
| 录音：D `src/renderer/core/voice/{recorder,audio-worklet,pcm}.ts` | `VoiceRecorder.start/finish/dispose`；AudioWorklet 混单声道，按实际 `sampleRate` 做带状态的 windowed-sinc 重采样；输出 PCM S16LE、16 kHz、4000 样本完整块及短尾块；8 字节小端头为 seq/count | **可复用核心算法**。250 ms 指传输块间隔，不是 ASR 出字间隔。最大时长、流控与新 capabilities 对齐；保留 flush 后等待所有 in-flight 发送、断麦和 processor error 处理。 |
| 音量与 HUD：D `src/shared/voice-level.ts`、`src/shared/voice-presentation.ts`、`src/main/core/voice/voice-hud.ts` | `VoiceHud.show/update/levels/close`；不可激活悬浮窗、只传实测音量、取消/停止/查看、成功反馈后收起；IPC 检查 sender | **可复用交互骨架，视觉需适配原型**。移除 SmartDD 文案及固定测试音频默认流程；不把“查看”之外的 HUD 点击变为抢焦点操作。Windows 全屏、多屏、任务栏表现另测。 |
| 会话编排：D `src/main/core/voice/voice-service.ts` | 主进程持服务凭据和 WebSocket；generation/session/epoch 隔离迟到消息；audio.ack 统计、终止后结果恢复、一次插入及回执、修改观察 | **需改造**。旧凭据为 `voice.local-service-token`，不是账号登录；新增 access/refresh 生命周期、设备及用户隔离。旧录音积压阈值直接写 32000 样本/64000 字节，改为统一配置并维持等价的 2 秒边界。 |
| 公共类型与局部差异：D `src/shared/voice.ts` | `VoiceCapabilities`、`VoiceResult`、`VoiceCorrection`；`scopedCorrection` 限定在本次插入片段，以 Unicode codepoint 返回 offset，并扩展完整 ASCII 单词 | **复用差异算法，重整 API 类型**。当前 serviceUrl schema 仅允许回环 HTTP，需按本项目集中地址与 HTTPS/WSS 策略改造；禁止 UI 直接配置或持有 token。UTF-16 原生选区与协议 codepoint 必须显式换算。 |
| 应用内目标：D `src/renderer/core/voice/targets.ts` | DOM 输入框捕获原值、选区、所属表单/root、input type；插入后 read-back；试过一次后禁止自动重插 | **可复用目标校验逻辑**。SmartDD 注册终端等内部对象不属于本项目默认需求；跨应用输入走原生桥，不能用 DOM 成功代替跨应用验收。 |
| macOS 原生桥：D `src/main/core/voice/native-input.ts`、`native/voice-input.swift` | JSONL helper 的 `permission/capture/insert/observe/release/keyDown`；AX 控件、应用 PID、原值、选区检查；AX 写入或向目标 PID 发送粘贴；剪贴板 changeCount 保护；写后读回；超时杀 helper、不重试 | **需改造后复用**。当前仅 darwin；允许的 AX role 是文本域/文本框/组合框，拒绝安全密码框或不可读选区。原值只保存在 helper 内存，observe 返回本次片段。上限为原值 200000 UTF-16 单元、插入 32000 UTF-16 单元，不能误写成 codepoint；需要与服务端文本上限和错误码统一。 |
| Windows 原生输入 | `native-input.ts` 对非 darwin 返回 unsupported，其他操作直接抛错；本次指定来源仅发现 Swift helper | **需新实现**。UI Automation、SendInput、按键状态/钩子、权限与剪贴板、helper 打包、目标快照核验和回读均不能宣称已有。macOS 代码及测试不证明 Windows 支持。 |

### 2.1 插入与观察应保留的约束

- 正常听写在结果返回后捕获当前目标；插入前校验应用、控件、选区、原值；明确确认后才允许恢复原应用焦点。
- 已尝试写入而 read-back 不可靠时返回 `unverified`，保留文本供检查/复制；网络回执失败不能触发再次插入。
- 源实现将观察与成功回执绑定；本项目按桌面契约在本机核验插入、目标仍有效且学习开启后立即有限观察，提交纠错前再确认回执入库，避免网络延迟错过早期编辑；完整输入框正文不上传。
- 当前源实现首次观察在 200 ms 后、随后约 300 ms 轮询，空闲 15 秒或最长 60 秒时提交最终差异并结束；这些是来源默认值，最终以本项目规则和 capabilities 为准。
- 当前 `scopedCorrection` 把多处改动包成一个最小差异区间，不能假设能自动学习多条独立纠正；超长、句子改写和跨片段变化由服务端拒绝学习。

## 3. 服务端复用映射

| 模块与来源 | 已观察到的实现 | 分类及改造边界 |
|---|---|---|
| 音频会话：S `src/smartdd_voice/session.py` | `Session.accept/end/cancel/run/watch_timeout`；严格 seq 和样本数；audio.end 核对累计数；持续接收与推理分离；末尾补识别；成功/取消清音频；cancelled 检查阻断迟到结果 | **复用状态机算法，改造持久化与归属**。源 Session 在内存，无 user_id/device_id，服务重启后结果消失；需要按当前 API/数据模型补会话元数据、幂等、终态竞争、取消生效点和恢复记录。不能直接套入多 worker。 |
| 协议/清理：S `src/smartdd_voice/api.py`、`config.py` | 创建/查询/取消/重试/插入回执/纠错/词库；全服务只能有一条活动会话；失败音频默认 600 秒、结果内存 1800 秒；清理每 5 秒；重试要求 failed 且完整音频已结束 | **协议结构可复用，鉴权与调度需重写**。单个本地 Bearer token、拒绝所有带 Origin 的请求，不适用账号管理后台。新增用户/设备鉴权、租户条件、每用户与全局容量、队列/拒绝策略；明确 TTL 的上限及清理粒度。 |
| ASR：S `src/smartdd_voice/asr.py`、`vendor/hypothesis.py` | `AsrEngine.prepare/decode/close`；MLX whisper 本地权重、单 worker ProcessPool；decode 返回 words/segments/seconds；`StreamingText` 用 LocalAgreement 合并稳定前缀、保留临时文本、按确认词边界裁窗 | **抽取为 MLX provider 与独立流式适配器**。不是通用 ASR 接口实现；MLX 依赖在 pyproject 中限 macOS arm64，不能作为 Linux 默认可运行依据。其他 provider 与 Linux 部署需独立实现/接入。 |
| 整理：S `src/smartdd_voice/refiner.py` | `refine/apply_terms/context_kind/classify`；结构化 JSON；明确删减片段、确定性保护和独立语义审核；异常回退已完成词库纠正的完整转写，同时保留原始 ASR raw_text | **规则算法可复用，LLM 调用需拆 adapter**。当前直接调用 Ollama `/api/tags/show/generate/chat`，预热且拒绝云模型，不是 OpenAI-compatible 客户端；应抽离 `generate_structured` 等调用和模型健康检查，保留程序保护和回退；独立LLM复审按2026-10-19用户决定不再保留。 |
| 词库：S `src/smartdd_voice/dictionary.py` | SQLite WAL，normalized(term)+scope 唯一；乐观 version；手工来源优先；alias 上限；学习前后快照和撤销；英文词边界；限定学习上下文 | **需 PostgreSQL/租户改造**。没有 user_id；全局 dictionary version、correction_id 唯一、scope 默认 default 都不能直接复制。迁移唯一约束、事务、租户内版本/幂等键，按最终数据归属保留正确范围；检索不能沿用静默 2000 条截断。 |
| 鉴权/历史/统计/管理 | 指定来源有本地令牌、内存结果和会话 timeline；没有本项目邮箱账号、refresh/device 撤销、租户隔离、持久历史、聚合统计和 admin 角色体系 | **需新实现**。不能把“本地单用户服务跑通”视为 server 骨架已具备这些模块。 |

### 3.1 partial 能力不能无条件继承

源服务在 capabilities 固定声明 `partial_asr: true`，默认每累计约 1 秒新音频触发窗口重识别；这依赖 words 的时间戳和 LocalAgreement，而非每个 250 ms 音频包都发一次文字。

新适配器内部须核验底层原生增量、词时间戳和窗口适配能力，对外按 API 契约声明 stable_partial；批量 provider 即便可只发 final，也不满足 [业务规则 BR-04](../02-prd/business-rules.md) 的 V1 稳定 partial 验收要求，不得作为 V1 验收配置。客户端按 capabilities 展示状态，禁止把未知内容伪装成 partial。`stable_text`、`provisional_text`、`revision`、`decoded_samples`、`received_samples`、`confirmed_samples` 是可复用的源事件字段，命名以新 API 契约统一。

### 3.2 忠实性与学习算法的可复用规则

1. **先验证删减再比较保护项。** `verified_omissions` 验证原文中的连续锚点、不重叠、删减类型为 filler/repeat/self_correction。明确改口可以去掉已撤回的数字或专名，保留最终说法；不能要求所有原文数字和专名永远全部保留。
2. **保护项相等不等于语义相同。** `fidelity_problem` 比较路径、数字、英文词、否定词计数；动作对象、否定范围、单位对应关系等属于模型实际效果，必须用真实语料验收；2026-10-19用户决定取消独立LLM复审，不将程序计数升格为完整语义证明。计数比较是风险闸门，不能证明百分之百忠实。对确定性检查中英文 casefold、路径/数字正则的覆盖限制要用数据集验收，不能直接升格为所有语言保证。
3. **不使用固定压缩比例判错。** 重复内容可能合理大幅缩短；源实现用锚点及审核判断。源实现失败时可回退词库纠正后的完整转写，本项目按 BR-06 明确回退 `raw_text` 并要求确认，不能不加区分地照搬源 fallback；历史审核失败结果仍要求确认，新流程不派发第二次LLM审核。
4. **学习的是词语纠正。** `correction_guard` 拒绝空值/纯增删、过长、句子标点改写、数字、否定、需求及时间变化；`classify` 区分相同对象的误识别纠正与换成另一个对象。分类是 term/suggest/ignore，不包含已经校准的置信度分数；业务规则若引入数值门槛，必须另行定义和验证，不能称为已复用能力。
5. **按发生位置判断语境。** alias 只在对应位置且词边界匹配时形成候选；遮蔽目标词后判断当前和学习语境，同一 alias 对应多个合格词条时不猜测；已有语境每词条最多保留 3 条、两侧最多 80 codepoint 是来源限制。
6. **撤销不得覆盖后续修改。** `Dictionary.undo` 只在当前词条版本等于学习后的版本时恢复旧值/删除新增词；后来编辑或删除应返回冲突。确认学习必须引用服务端此前返回的 suggested，不接受客户端随意 `confirm=true`。

### 3.3 复用前必须修正的契约差异

- `client_request_id`、`correction_id`、`insertion_id` 的幂等应以用户归属为前提，并校验相同键是否携带相同规范化请求。源创建/回执只按已有 ID 返回的行为不能直接代表新契约满足冲突检测。
- 每次 ASR/LLM 调用记录模型配置快照、耗时、结果与用量；源 refiner 只取 JSON content，没有汇总 token/成本明细，新管理统计需补。
- 词库版本应在处理使用时冻结；源结果在结束时读取全局最新 version，不足以证明它就是本次处理所用版本。`used_term_ids` 源实现记录相关候选列表，不等同于实际替换命中的词条，命名/统计必须澄清。
- 源运行时为全局单活跃会话、LLM 调用锁和全局纠错锁。多用户环境需明确资源容量与任务优先级；不把添加 user_id 当成已解决并发和隔离。
- 重试完整音频、重新整理现有 raw_text、重报插入回执是不同操作。源只有保留音频条件下的新会话重试，不能据此宣称历史记录可永久重新识别。
- 协议中的错误码、单位、大小上限、服务重启/TTL 行为以新文档为准，避免继续依赖中文错误字符串分支。

## 4. 已有测试可迁移范围

以下只核查了相关测试的存在、名称及必要断言；**本次全部未运行**。部分测试使用 mock/fake，不能代替音频、模型、OS 输入和打包后 smoke。

| 来源测试 | 可作为迁移起点的覆盖 |
|---|---|
| D `src/shared/ipc/rpc.test.ts` | RPC 注册、参数、Promise 返回、代理非 thenable、错误消息 |
| D `src/shared/voice.test.ts` | Unicode/emoji 的局部差异、限制在插入片段、整词纠正、地址限制 |
| D `src/shared/voice-level.test.ts`；`src/renderer/core/voice/pcm.test.ts` | 音量限频与静音；16/44.1/48 kHz、短尾、帧顺序、块划分不影响结果、抗混叠 |
| D `src/renderer/core/voice/targets.test.ts` | 显式确认后恢复原输入框、值/选区/type/form/root 变化拒绝插入 |
| D `src/renderer/core/voice/voice-store.test.ts`；`src/main/core/voice/voice-service.test.ts` | 旧录音 flush/错误不影响新录音、取消后迟到消息、一次插入、回执 ID 复用、失败回执不重插、结果供手动恢复 |
| D `src/main/core/voice/voice-hud.test.ts`；`src/renderer/core/voice/voice-controls.test.ts` | 不抢焦点、sender 校验、成功收起、旧 timer 不隐藏新错误、音量/HUD 状态展示 |
| S `tests/test_service.py` | 本地鉴权、增量/尾音、插入回执及学习、缺帧拒绝、取消、词库版本与撤销 |
| S `tests/test_edges.py` | 无音频、短尾、重复 end、坏帧、停止前/后断线、保留完整音频才重试、迟到推理、TTL、重启、事务回滚、上下文及撤销冲突 |
| S `tests/test_cleanup.py` | 改口、重复、引号字面量、保护项、语义审查失败、伪造锚点、提示注入、显式列表 |
| S `tests/test_refiner_context.py` | 同词不同语境、普通词不被专名替换、alias 边界、多个目标冲突、候选改写审核 |

迁移后新增账号/设备/租户/管理员、持久化、并发、provider 能力、用量聚合、Windows 和跨应用兼容测试，追踪到本项目 [测试用例](../04-test/test-cases.md)。源中没有对应模块的测试，不能沿用旧项目“测试通过”结论。

## 5. 来源声明与后续验证

- D 的 RPC 和窗口源码注明 Yoda Apache-2.0；对应来源中存在 `NOTICE`、`licenses/Yoda-Apache-2.0.txt`。实施复制时保留相关声明与许可，不因来源是自有项目而丢失上游归属。
- S 的 `vendor/hypothesis.py` 注明 ufal/whisper_streaming、MIT、上游 commit `6da90b44b7e50d79695e68166d2a2c7609c75abb`；来源中存在 `licenses/whisper-streaming.txt`。这里只记录本地声明，未联网验证上游仓库内容。
- 完成本项目适配后按工程要求先编译/类型检查，再单测，再打包 smoke 与真实模型、麦克风和目标应用验收；当前文档核查不满足 S4/S5 门禁。

## 6. 核查文件 SHA-256

下列哈希对应本次实际读取/计算的文件内容。D 的 HEAD 不能替代这些工作区哈希；S 没有可引用的 commit。完整相对路径为上文来源目录加以下文件路径。

```text
D/src/shared/ipc/rpc.ts d779928881306f67c5261333b7d5ea64a7ca68fa9dd66ca622a801ec388ec980
D/src/main/app/window.ts 93e65a50c2ad474feeffb6132a59a15fb138fb818ed64b1d43ccd50eb758f6da
D/src/shared/voice.ts 0c04be47adcacc6e0da6620f1f12bd0e59ab8a9a38299dd67ceba9d8e72d3f2b
D/src/shared/voice-level.ts c5eac35e95bc899bcbee726e315a70abc77624d69ec79122f5a5b136f6b940c4
D/src/shared/voice-presentation.ts d887ecf3d633ced5cf901fa744e77d67e22b52fba151146b3320c5c5e9ac4103
D/src/renderer/core/voice/recorder.ts 5f6c261ed4f04a5fc3fece647cbdaece56fa6f58ea12d01d4bc152e19d081445
D/src/renderer/core/voice/audio-worklet.ts f744415713050c7f34852e2dbb8f3336e6758aaaf4a2a51ad0546d66dbdd1496
D/src/renderer/core/voice/pcm.ts ff6a4ed480bf84ab4480c2fd60c66fbe3b0d67bc96aa21b3e1b660aab257b952
D/src/renderer/core/voice/voice-store.ts 20aa7a444abfccb506681739a017ec93f58446d13cfa9938f6f5099bdac24789
D/src/renderer/core/voice/targets.ts f09ed828a88472719ce86cbeafecd77b27b55e453a3c9456412446183c27993d
D/src/main/core/voice/voice-service.ts 2232d382a2cc7446b58e4fe7b6f883a43a66d6861b82f8fb0ee5247f27bce121
D/src/main/core/voice/voice-hud.ts d3014fbe3f06c1f850262604c23c6e2dafc1c4bc85b183af27384d8beb62a067
D/src/main/core/voice/native-input.ts 0fb1e1d467fcafe4c8a6691159915fdc68b20ab065ccfcc2da24785a5966a5e3
D/native/voice-input.swift c4a061948442f840fba07f78bdfa921fc7709a871d24478aaaf74831864bc060
D/package.json 9df02c0b9cff827183e9dd87cf5e06fee8a7ab76089744c71202c9465dc6b62e
D/src/shared/ipc/rpc.test.ts dc3e029fbe1f12fa5e307881924afbaa5fd5c7eafe173ade20b8791df2aa47d1
D/src/shared/voice.test.ts 3d42d9a96d783501d53847fa8b2cc780a8d5f2dc4cd6f37037aa84f3110b1ed4
D/src/shared/voice-level.test.ts 106e0eef893f77b1deec09062a5358644638dad356a6a8645bae146bd2f5ea8a
D/src/renderer/core/voice/pcm.test.ts d9424cb77b2860f4ee933bcb25af10bf5d9dc603a1d410240089c4127890132a
D/src/renderer/core/voice/targets.test.ts 05be89621888cb0630d7a4e8c3d68ceb3bbc30e809e20ce9fcc585798ef6882a
D/src/renderer/core/voice/voice-store.test.ts 88c71e7daae666016e6767f7b7d192afca4e593989033f0d5e5a0d831acc4b5e
D/src/renderer/core/voice/voice-controls.test.ts 01467b0a9db8a1f207b05afafbb830e87c51b8b34f52685c06db0c723be4b64e
D/src/main/core/voice/voice-service.test.ts 46c08d642f2c92fe0c4342b0e359ca9696488538d010b0deb47224ba80ff6f90
D/src/main/core/voice/voice-hud.test.ts 218e4aa27f6e77777544cb3781a6ee8c7e077e97d44b50d1bdbb503c6b88b043
S/src/smartdd_voice/api.py eb41ce4be7351c027fbcbfd2baa500026250d0630ad923d65f5dda20c4ff4e6b
S/src/smartdd_voice/session.py cf3e6ac94c08721571450b86eae732ffb0ef2c9ece10c38d42b7ce55116f5982
S/src/smartdd_voice/asr.py 95e79f114ac810f7edc4442797ed4d756b297fe0feb3bc74e728bf4865825058
S/src/smartdd_voice/refiner.py 004abee891fb4e70e4c4688770210a9aedbdaf5920642b82f70cc29a7f0cc89e
S/src/smartdd_voice/dictionary.py 0ca4ea38e8a8388009a16caa1393cd996277d3ff040b3394552347983661b222
S/src/smartdd_voice/config.py 30c23e2d418d6d8fecc09599402545f1b42111ec8557c1b5f37a185671fcd314
S/src/smartdd_voice/vendor/hypothesis.py 5ef1b89b880b7b20c906975c28a5f2056c63a1262d14f08da1217c988d315c9b
S/pyproject.toml 89207007cc6561f2a5877dfba8785fd03b2a0f6fd0c1983054af2d87cb2f5fef
S/tests/test_service.py 0ade49094cc102b8f5d9091071c7cfb740ea6053a6dd9e1e8fcc53614de197f1
S/tests/test_edges.py dbee678ba869163b6dec86e499515757d19f9b90f2a82eaf6c2b74fdfcbf4d87
S/tests/test_cleanup.py f36615dcc63af3cd34a9b17afafc8b1275d56b979686ac5c48c281e7cebdc03c
S/tests/test_refiner_context.py 82b2e94e3e75f0d3c3c9456d3cbc319e610bb02bf41f6769a307cb3e2cbae902
```
