2026-10-27多屏胶囊补充：本人要求当前输入位置所在屏优先，控件/前台窗口/鼠标逐级兜底，使用workArea全局坐标，不抢焦点、不弹主窗；录音及处理时跟随当前输入屏。验收三屏逐一、输入焦点与鼠标分屏、负坐标/不同缩放、隐藏后不再移动或显示。见docs/02-prd/hud-multiscreen-decision-20261027.md；真实三屏尚待本人复验。

2026-10-27权限刷新补充：本人授权后返回应用应自动复查并更新辅助功能状态，无需退出重开；不能自行授予系统权限。设置可见且缺权限时低频复查，focus/visible时复查；权限探测不打断在途粘贴、不自动重放。实测与修复见r2-permission-refresh-fix-20261027.md（docs/04-test）。

2026-10-27终端规则修订：本人明确终端也允许Command-V、不自动回车，见[决定](../02-prd/terminal-paste-decision-20261027.md)；取代此前“终端仅复制”，其他历史决定保留。

2026-10-23 R2对外契约兼容；新多行正文、回退确认、最新光标一次插入/隐藏主窗口和学习须重验。见[R2设计](r2-production-design-20261023.md)，不重做已认可页面样式。

# 桌面与原生输入契约（v0.2）

2026-10-29 Windows v0.0.2 的平台设计见 [windows-v002-design.md](windows-v002-design.md)。下文 Native 字段已按共享桥/Swift 的实际扁平格式校正，Windows 不新增修改观察；v0.0.3 事件通道仍为后续草稿。

依据：方案 B、[业务规则](../02-prd/business-rules.md)、[API](api-contract.md)、[会话协议](session-protocol.md)。复用来源见 [reuse-map](reuse-map.md)。本文件是待实现契约，不代表现有 SmartDD 代码已经具备所有能力。

## 1. 进程与通信

Main 拥有账号/token、HTTP/WS、session/result 身份和原生桥。Renderer 拥有麦克风采集与页面状态；Preload 只暴露共享 typed RPC 中的白名单方法。启用 contextIsolation，关闭 nodeIntegration；Main 校验发起者窗口和 zod 参数，不提供任意网络请求/文件读写方法。

2026-10-19 用户确认连续口述：终态结果未复制/插入/确认也允许 `session.start`；ready 事件所指结果必须已经由服务端持久化，开始下一段只结束本机展示，不删除历史、不重跑旧结果。Main/Renderer 同时移除“先处理结果”的开始门禁，保留在途互斥；开始时推进代际、释放旧目标并清除本机插入资格，新段在结果返回后独立捕获当时当前目标。无目标继续采集、保存历史且零原生写入；旧SID/attempt/revision事件不得驱动新段。结果页“新的一段”直接开始，与快捷键一致。历史条件重试的显式选择规则另沿用下文。

| RPC 类别 | 输入/输出要点 |
|---|---|
| auth.login/register/logout/status | 凭证输入经短生命周期消息到 Main，Renderer 不接收 token；返回脱敏账号/登录状态 |
| voice.capabilities/start/stop/cancel | 返回客户端会话键与服务端 session_id；所有后续音频/事件必须匹配该键 |
| voice.audioChunk | Uint8Array PCM + seq/sample_count；单次最多 8008 字节，拒绝重复/旧会话块 |
| voice.result/copy/confirm/retarget | result_id；原文/整理稿均可复制，仅整理稿可插入；待确认必须显式确认，retarget 使用新目标快照 |
| dictionary/history/stats/settings | 固定请求结构代理到 Main，禁止直接暴露服务凭证 |
| events.voiceState/authState | 带递增本机 revision；Renderer 丢弃旧 revision，切换账号清空界面缓存 |

历史条件重试由 `history.selectForRetry(sessionId, kind)` 先只读正式会话快照；Main 核账号/操作代际、当前未处理结果、retry_kind、过期及插入资格，失败不重置原结果或目标。成功后清旧目标、冻结会话 learning_enabled、关闭自动插入，Renderer 绑定新代际后调用既有 `session.retry`；重试完成仍需明确重新指定或复制。选择在途与录音、插入、重新指定互斥，不调用模型或操作系统写入。

音频可用既有 typed RPC 的二进制传输方式，避免将 PCM Base64 多次复制；若采用 MessagePort，仍必须由 Main 校验绑定的窗口/会话。客户端音频缓存和 WS bufferedAmount 均计入 2000 ms 背压预算，不能各允许 2 秒叠加。忽略终态后的采集回调并关闭 AudioContext/轨道。

HUD 单独非激活窗口，不能改变当前目标焦点。录音所在 Renderer 设置 backgroundThrottling=false；隐藏主窗口不能销毁 AudioWorklet。只有观察到真实 RMS 才驱动波形，处理阶段使用固定处理中状态，不伪造语音输入。

2026-10-20 HUD细化：尺寸/时机以补查（内部记录，未公开）和FR-02-02为准。准备/录音/处理中分别54×12、116×34、92×34；处理动效必须独立于新WS事件持续运行，慢提示从同一SID/attempt进入finalizing/refining起计，二者切换不重置，6000＋ceil(audio_ms/30000)×500ms仅为提示预算。真实ready才做100%完成过渡，350ms保留＋250ms退场；empty/failed/cancelled立即清理，新操作清旧计时。不得由旧SID/attempt/revision使新胶囊复活。Main延后关闭与Renderer过渡使用同一常量，仍showInactive、focusable=false。主窗口在处理中提供取消，不向服务器新增状态、请求或超时。

录音开始显式区分本机准备与捕获：Worklet初始unprepared，原建图连接/resume完成并固定epoch后才prepare；连续两个非空、有限且时间连续的真实量子（静音有效）后ready，Recorder发begin，下一连续量子先回begun再进入原DSP。准备中不累计PCM/level/ACK信用/180秒；ready之后和begun之后的缺量子继续按原保护拒绝，不重置录音时钟。begin前取消不发送PCM或flushed，stop/close/fail与服务端终态解除启动waiter。

服务端WS ready仅表示streaming。本机typed RPC `session.captureReady(sessionId, attemptNo)`须当前窗口授权、账号/操作上下文及精确SID/attempt/state=streaming校验，幂等设置本地`SessionProgress.capture_ready`；不新增服务端状态或WS字段。Renderer仅在真实Recorder.start的begun完成后调用，RPC前后核代际/账号/SID/attempt及仍streaming。未确认时Store/Home/HUD显示准备状态，不先显示聆听/录音波形；取消仍可用。新操作/reset/终态清除本地就绪，迟到ready不得复活旧录音。没有新增本机超时或延长原服务端音频空闲10秒、背压与处理预算。


## 2. 设置与窗口生命周期

已知保留表与等价签名集中实现在 [native/shortcut-policy.ts](../../desktop/native/shortcut-policy.ts)，主进程统一校验；具体支持集合以该产品自有表为准，不从系统实时枚举。Mac新版保存、拒绝、写盘回滚和完整重启已验，见实施证据（内部记录，未公开）。

- 默认快捷键 `CommandOrControl+Shift+Space`；保存前按平台保留表匹配并归一化等价修饰符/顺序/大小写，命中不注册、不写盘；不枚举任意第三方占用，也不以同进程 `isRegistered` 当作全局查询。新键注册及设置持久化均成功后才注销旧键，任一失败保留旧配置并释放本次新键；明确注册失败提示不可用，不断言必然被其他应用占用。支持组合键切换录音，V1 不承诺单 Fn/Option 或按住说话。范围与测试基线见[用户修订](../02-prd/shortcut-conflict-decision-20261019.md)。
- Escape 只由焦点应用处理。主进程拒绝含 `Esc`/`Escape` 键的全局快捷键，包括别名、大小写及带修饰键的形式；拒绝发生在注册及写盘之前。旧持久配置含该键时使用默认组合键，不自动覆盖原配置文件。
- 服务地址集中定义在 shared/config.ts，构建时覆盖，服务根地址不能混入凭据/查询参数；远程必须 HTTPS，WS 从同根地址派生 WSS。显示实际地址和连接状态，不提供运行时地址编辑。
- 麦克风、辅助功能权限分开检查；麦克风拒绝不能进入 recording；无插入权限可继续录音但结果仅复制。权限在运行中被撤销立即停止对应操作并保留已确认结果。

2026-10-20首次授权补正：macOS的`session.start`在创建服务端会话前由Main读取麦克风状态；not-determined时请求系统授权并等待真实决定，再读granted才继续。denied/restricted直接返回本机授权路径，unknown/系统调用失败明确提示无法确认，不创建会话或开始采集。等待系统提示期间仍为原preparing，可取消；迟到决定须核对operationId、账号与contextVersion，不能创建孤儿会话或影响新操作。未新增RPC、服务端状态或超时；授权结束后才创建会话，既有音频空闲10秒、背压和处理预算保持不变。Renderer对Main预检错误与getUserMedia拒绝共用中英文权限提示。
- 系统休眠/退出时停止采集并尽力 cancel；恢复后新建会话，不能复用旧录音目标。隐藏窗口不等于退出。Main 检测 Renderer/native helper 崩溃，结束对应阶段并提示失败。
- 同一安装路径单实例；第二实例只激活现有主窗口。不同账号数据、插入执行标记以 `(server_origin,user_id)` 隔离；退出清除 UI/结果内存/观察任务，执行去重标记保留到其结果不再可获取。
- safeStorage 只在 Main 调用；加密不可用时不明文落盘，本次仅内存登录并提示。操作系统提供的保护能力并不等于所有同机程序均无法读取；不额外宣称绝对隔离，参考 [Electron safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage)。

## 3. Native helper 契约

macOS 使用 Accessibility/CGEvent；Windows 使用 UI Automation/SendInput。2026-10-28 快速纠正学习（S2 待评审）：macOS helper 新增事件驱动观察（`observeStart/observeStop`）与第 4 管道（fd 3）主动事件，stdin/stdout 数字 id 协议不变，详见 [fast-correction-learning-design-20261028.md](fast-correction-learning-design-20261028.md)。平台实现放 desktop/native，通过 JSONL stdin/stdout 与 Main 连接。stderr 不输出目标文本；stdout 只能是协议响应。不要向日志写 stdin/stdout 原消息。

请求公共字段：`id`（本进程递增）、`action`，动作参数直接在同一对象内（`{...data,id,action}`），没有 `data` 包裹层。响应同样为 `id` 加动作字段，或 `error:{code,message,effect}`，没有 `ok/data` 包裹层。effect=`none|written|unknown`；错误文案不包含目标正文。单行最大 1 MiB，参数超限在副作用前拒绝。Main 超时 5000 ms 杀掉 helper 并拒绝待处理请求；已派发的写入请求视为 unknown，不自动重放。

| action | 必需数据 | 返回/不变量 |
|---|---|---|
| permission | 可选 prompt | supported/trusted；Windows 增加 observationSupported=false，不代表 macOS AX 授权 |
| capture | 无 | target、app、observable；原值/选区/控件引用留 helper，不跨 Renderer IPC |
| validate | target、可选 restoreFocus | valid、code/message；显式目标复核同一应用/窗口/控件、焦点、选区和原值；不可读目标不伪造匹配 |
| insert | target、insertion_id、text、可选 restoreFocus | 显式目标再次校验，一次写入；返回 status/effect/observable/code/message |
| insertCurrent | insertion_id、text | 正常自动路径，同次请求取得最终当前目标，返回写入结果及可用 target/app |
| inputBounds | 无 | available、bounds；Windows 标记 physical，由 Main 转 DIP |
| observe | target | Mac 既有有限观察，返回 valid/text（仅本次片段）/composition/compositionEvidence；Windows 本版不实现。v0.0.3 observeStart/Stop 另见学习设计草稿 |
| release | target | 清除片段、原值与句柄，幂等；不是新增学习能力。dispose 为 Main 原生桥本地关闭方法，不是协议动作 |

应用/窗口/控件标识不得只靠进程名；包含进程生命周期和控件有效性检查。快照仅当前进程有效，不跨重启恢复。原生 API 的 UTF-16/平台坐标由 helper 转换为统一 Unicode 码点；保留在本地的原始快照与定位不能上传给服务端。

2026-10-27当前方案见[通用粘贴决定](../02-prd/universal-paste-decision-20261027.md)。macOS统一一次PID定向Command-V，无按应用名称的正向名单，不调用AX文本setter或事后光标setter。`proc_pidinfo(PROC_PIDTBSDINFO)`/启动时间识别生命周期，当前前台进程及标准窗口捕获/写前复核；仅AX焦点应用缺失/属性不支持可由刷新后的AppKit当前应用补足，其他通信/权限错误不当作可粘贴证据。已知保护/只读/禁用和非输入状态拒绝；终端同样允许一次粘贴、不自动回车，缺少控件/正文/选区时不单独阻止派发。

快照分两级：可读全文/选区用作精确核验及有限观察；不可读只持有同进程/窗口身份，不伪造文本控件。窗口内实际光标/选区由目标应用处理，未知密码/只读状态不能保证识别。可读手动目标仍校验快照、允许从产品窗口显式恢复；不可读手动目标不激活后台旧窗口。发送前一次执行登记，保存/恢复剪贴板沿竞争版本规则；发送后严格读回才inserted，其他情况unverified、无学习/自动重试。

先前Chrome/Code/WPS/微信专用路线仅为历史：Chrome富文本错误AX范围曾使整值重建删后缀，现统一粘贴避免重建；错误AX预期仍不能当作可靠核验。原WPS/微信授权和失败保留，不追认新路线通过。

## 4. 插入顺序与失败

2026-10-20恢复实现的具体顺序：早期目标读取仅可用作页面提示/显式重新指定候选，不作为正常自动插入的准入条件。自动路径先校验结果身份/正文hash并完成claim及本机一次执行标记，再通过单次原生请求在helper内部读取最终焦点并立即按该目标当前选区插入；Main与helper之间不再把早期target句柄跨网络等待冻结使用。原生返回最终target/app用于真实读回和局部学习；缺最终目标明确零写入、回执failed且结果保留。调用已派发后超时/断线仍为不确定，不重发写入。旧候选迟到必须释放，不可覆盖已选最终句柄；取消、账号或新段隔离保留。显式重新指定仍可使用用户选择的目标，不把点击主窗口本身当作自动插入目标。该顺序待源码及回归实证，不能因文档更新记通过。

2026-10-20后继用户澄清：[实际粘贴前的最终光标](../02-prd/final-paste-focus-decision-20261020.md)为当前产品规则。下列1、2、4步中“结果返回时捕获并冻结、后续变化拒绝”的顺序是现有旧实现记录，已不再作为目标行为；新顺序及原生接口方案待恢复开发后细化，当前未改代码或宣称通过。TC-INSERT-04旧拒绝用例已撤销，最终位置跟随归入TC-INSERT-03重验。一次执行、账号/取消隔离及真实读回规则保留。

1. Main 收到 current result，核对会话键；正常现场听写立即启动一次当前目标捕获，目标不取自录音开始/停止快照。结果及时发布、结束原模型等待；插入等候这次捕获，目标元数据更新仅发 progress、不重复发结果。需要 review 时先展示原文/回退原因，不自动写入。
2. 正常 review_required=false 时，Renderer 不以录音开始时是否有目标阻断自动请求；Main按2026-10-20[最终光标决定](../02-prd/final-paste-focus-decision-20261020.md)执行：资格确认与单次写入登记后，在同一原生请求内重新获取当前目标并紧邻写入，使用当前选区，不能因早先捕获目标改变而拒绝或写回旧位置，无需每次弹确认。最终目标密码/只读等限制（终端按2026-10-27决定允许粘贴、不自动回车）、无目标复制降级及BR-07兼容粘贴边界仍有效。捕获超时或取消/账号/代际变化不得迟到串入，迟到句柄释放。历史重试/恢复和手动插入仍显式指定目标并校验其快照，不自动套用正常听写的目标跟随。原文仅展示/复制，插入固定使用refined_text；回退时refined_text=raw_text且须确认。手动复制显示“已复制”，不伪报inserted。旧“捕获后变化即拒绝”的文字已被上述决定及INSERT03取代，INSERT04保留历史但撤销。
3. 对 result.refined_text 原样 UTF-8 SHA-256 申请服务端 claim；文本不可由客户端改写或切换成另一内容后复用 hash。
4. 获准后，本机执行日志先原子写入 `{result_id,insertion_id,phase:started}`（不含正文）；再调用 helper。再次校验失败为零写入 failed，才可用户显式重新指定。
5. 默认路径用所选片段替换选区并读回：原前缀+插入文本+原后缀一致，且焦点/控件仍相同，方可 inserted。兼容粘贴无可靠读回只能 unverified。未写入失败与写入不确定必须区分。
6. 保存本机终态，发送 insertion 回执；断线只重发同一回执，不能重放 native 操作。本机读回确认 inserted 时显示“已输入”并立即开始符合条件的有限观察；unverified 显示“无法确认是否已输入，请检查目标”。纠错上报另须等待服务端确认回执入库。

服务端同结果已有 reserved/inserted/unverified 时禁止新 claim。重复 claim 响应只是查询已有资格状态，不是再次执行命令。本机重启发现 started 无终态时，按 unverified 处理并尝试补回执；不得因 HTTP 请求超时推断“没有写入”。明确 effect=none 的 failed 才允许用户再次操作，禁止自动循环重试。

剪贴板兜底仅在原生适配器明确尚未发生写入、目标仍可验证且已选定单次粘贴方案时使用；不能在一次未知结果后改走粘贴。缓存原剪贴板及版本，粘贴完成后仅当当前版本仍由本轮写入才恢复。用户主动点“复制”则保留结果文本，不恢复旧内容。

## 5. 有限纠错观察

遵循 BR-08：每 300 ms 采样，静止 15 秒/总 60 秒截止，IME 组合期间不分类。只有原有前后文本仍可证明未变时，才能把本次片段的最终局部替换提交服务端；不允许通过剪贴板轮询其他应用内容。

本次片段最终状态稳定后一次提交 correction_id，offset 采用码点计数；before 与初始插入文本必须匹配。一次观察中发生多个分离替换，V1 放弃自动学习而非拼成一个大替换。清空、纯追加/删除、整段改写、控件切换、账号退出、不可观察等情况结束，不补交原框全文。

观察与 insertion 回执传输可同时进行，但 correction 只能在服务端已确认同一 insertion 为 inserted/observable 后提交。回执响应丢失时重报原回执或 GET 确认状态，不再执行 native；观察窗口结束仍无法确认则清除片段，提示词库未更新，不把正文转存本地队列等待无限重试。

learned 显示词条和撤销；suggested 显示本次替换供确认/忽略；ignored 静默结束（可记录无正文原因码）。服务端不可用提示“修改已保留，词库未更新”，不会回改用户输入。撤销冲突时说明词条已有新修改并引导词库手动处理。

## 6. 页面状态与文案

| 页面/状态 | 展示与操作 |
|---|---|
| 首次使用 | 注册/登录 → 权限检查 → 快捷键说明/试说；授权未完成可再次检查 |
| 登录错误 | “邮箱或密码错误”；禁用则“账号已禁用，请联系管理员”；限流显示稍后重试 |
| 无目标/无辅助功能权限 | 仍可录音；完成后“结果已生成，请复制到目标输入框” |
| 准备/录音/收尾/整理 | 文案与动作对应状态；录音显示真实波形，处理阶段允许取消 |
| 断线/过载 | “连接中断，请重新录音”或“服务繁忙，请稍后重试”；不静默排队 |
| 结果待确认 | 展示原因、原文与可用结果；确认、复制、重新指定；不自动插入 |
| 历史 | 加载/空/错误；按能力显示重新识别/重新整理，过期不显示不可用重试按钮 |
| 词库 | 手动/自动筛选、归一化搜索、空状态、版本冲突、CSV逐行结果；表单提交中禁重复 |
| 设置 | 组合快捷键、简中/英文、学习开关、账号/设备列表、只读服务地址/能力状态 |
| 后台 | 登录、用户列表/创建/启用禁用、频率和模型用量；普通用户无后台权限 |

所有文案有 zh-CN/en 资源键，错误码不直接作为面向用户的唯一说明。当前方案 B 原型已移除移动、订阅、团队、推广、翻译、问答和独立同步控件；正式 V1 沿用同一范围。页面/状态/用例映射见 [原型矩阵](../01-prototype/page-state-matrix.md)，Mock 到真实接口的切换见 [三端交接](fullstack-handoff.md)。

## 7. 验收边界

基线控件必须真机完成自动插入、读回、焦点保护、非空选区和有限观察验证。扩展控件即使不能自动插入，也必须正确降级且保留结果。终端按2026-10-27本人决定同样一次粘贴，不合成Enter/发送，覆盖结果含换行的情况且不承诺目标程序如何处理正文换行。平台目标矩阵、测试数据与未执行项见 [测试计划](../04-test/test-cases.md)。

### 历史/词典展示补充（2026-10-20）

布局调整只在Renderer；不变更API/RPC、store请求、重试/复制/删除词条的账号代际校验。详情折叠不丢弃元数据；原文和整理稿必须继续受content_expired约束。更多菜单使用现有Base UI键盘/焦点管理，不用假按钮。搜索展开/关闭是本地展示状态；关闭不能静默清除已应用条件。菜单不引入音频导出或历史删除接口。

首页日历以服务端date字符串为业务日期，使用UTC日历运算确定星期，避免宿主时区和DST使星期偏移；只给既有daily按星期补范围外空白，不创造贡献。月份与格子共享滚动容器，左右操作仅滚动已加载范围，不改变统计API/口径。


### 2026-10-27 微信无AX焦点的单次兼容路线

按[用户决定](../02-prd/wechat-single-paste-decision-20261027.md)单独增加wechatPaste，不扩大documentPaste/WPS角色白名单。系统AX正常时维持可靠路径；仅noValue时允许在主线程prime NSWorkspace后有界处理default run-loop，再取仍active/非hidden/非terminated的准确微信PID，禁止用缓存值或未知应用直接兜底。每次Wechat目标复核都重新查询前台；进程OS代际/启动时间、AXFocusedWindow身份/归属/标准角色均保持。Wechat app级FocusedUIElement必须仍为noValue，窗口明确保护/只读/禁用则拒绝。

历史微信专用实现由2026-10-27通用粘贴方案取代；不可读目标不持有正文/选区快照，不调用AX正文/光标setter或观察，统一pid-paste回执规则。
