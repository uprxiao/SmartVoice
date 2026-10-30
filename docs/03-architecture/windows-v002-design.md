# Windows v0.0.2 技术设计

2026-10-29，S2。依据 [WIN-01～12](../02-prd/windows-v002-requirements.md)、[S0 平台原型](../01-prototype/windows-v002.md)、现有 Main/Preload/Renderer 架构和已通过的阶段 1 探针。本文描述目标实现；独立探针已通过不表示正式产品已接入。S3 在本设计复核后生成，S4 另行登记。

## 当前实现与改动位置

| 层 | 当前 | 本版改动 |
|---|---|---|
| Main 原生桥 | `core/native-input.ts` 仅 darwin，数字 id 的扁平 JSONL | 支持 win32 的 `.exe`、隐藏控制台、父进程身份；保持原超时/断线 effect 与不重放机制 |
| Windows 原生 | `native/windows/` 为独立技术验证 | 复用 Win32/UIA/剪贴板/修饰键模块，增加正式动作与本地目标生命周期；验证入口与产品入口分离 |
| 会话服务 | claim → 持久化 started → 一次 insert/insertCurrent → receipt；成功后可启动观察 | 保留该顺序，区分插入读回能力与修改观察能力；Windows 每会话关闭观察，不改账号偏好或 Mac |
| 窗口 | Windows close 销毁主窗，HUD 常驻，无托盘 | close 隐藏；托盘与显式退出；第二实例打开/重建；自启进托盘；重建窗口继续绑定音频/崩溃处理 |
| Renderer/RPC | 设置权限固定 AX、麦克风预检仅 Mac | 平台能力、麦克风状态、Windows 自启本机 RPC；按平台渲染权限/帮助/学习限制及双语提示 |
| Server/Admin | 现有多租户、回执、历史、统计 | 不新增 API/表/迁移/后台功能；安排真实契约与统计回归 |

低层平台实现和策略集中 `desktop/native/`；Main/Renderer 只使用平台能力及共享类型，不各写一套 Win32 或注册表实现。沿用 MobX、typed RPC、zod、现有 token/服务地址边界，不引入新的 UI 库或模型部署方式。

## 原生进程与协议

正式程序：`resources/voice/smartvoiceinput-input.exe`；开发路径位于 `desktop/resources/voice/`。Main 使用 `spawn`、三条 pipe、`windowsHide:true`，传入当前父 PID；父身份仅用于拒绝本应用窗口与父退出清理，不是用户凭据。helper 初始化 Per-Monitor V2 DPI、COM MTA 与仅八个修饰键的低级 hook/消息线程；stdout 只输出协议，stderr 不记录正文、标题、剪贴板或凭据。

**沿用实际共享桥的扁平格式，不使用旧文档的 data/ok 包裹层。**

```json
{"id":1,"action":"permission","prompt":false}
{"id":1,"supported":true,"trusted":true,"observationSupported":false}
{"id":2,"action":"insertCurrent","insertion_id":"opaque-id","text":"虚构结果"}
{"id":2,"status":"inserted","effect":"written","observable":true,"app":"notepad.exe","target":"opaque-target"}
{"id":3,"error":{"code":"NATIVE_REQUEST_INVALID","message":"输入请求无效","effect":"none"}}
```

Windows 的 `trusted:true` 只兼容现有“可尝试原生输入”调用链，不表示已获得 macOS AX 权限。Main 的 `accessibilitySupported/accessibilityTrusted` 仍仅用于 darwin，Windows 不据该旧字段展示授权按钮。

| action | 请求字段 | 返回与约束 |
|---|---|---|
| `permission` | 可选 prompt | supported/trusted/observationSupported；Windows 不弹 AX 授权、不提权 |
| `capture` | 无 | 当前目标的 opaque target、app、observable；正文、选区和 UIA 引用只保留 helper 内存 |
| `validate` | target、可选 restoreFocus | valid、code/message、本机 reason 阶段码；仅显式目标可恢复焦点，且须仍可核验同一窗口/控件/原文/选区 |
| `insertCurrent` | insertion_id、text | 正常自动路径，写前重新取得当前目标；不受较早 capture 约束 |
| `insert` | target、insertion_id、text、可选 restoreFocus | 历史/手动路径，再次严格校验已指定目标后一次粘贴，不退回其他窗口 |
| `inputBounds` | 无 | available、bounds{x,y,width,height}、coordinateSpace；Windows 为 physical，Main 转 DIP；无结果不操作焦点 |
| `release` | target | 幂等释放本地目标及正文；未知目标也可安全释放 |

本版 Windows 不实现 `observe/keyDown/observeStart/observeStop`；未知动作明确错误，不进入验证替身。技术专用动作由独立 probe 二进制使用；产品打包只包含正式 helper，不包含 verify/probe/夹具，也不以环境变量切换成 Mock。

数字 id 为本进程安全整数；每行 UTF-8 上限 1 MiB，写入文字非空且最多 32000 UTF-16 单元，超限/非法 UTF-8/非法 JSON/未知字段类型在副作用前拒绝。Main 继续 5000ms 超时，杀死对应子进程并将已派发写入视为 unknown；不重发。正常进程退出、stdin EOF、父进程退出均清理目标/线程；账号变更、取消和休眠由 Main 原有代际保护及 invalidate 杀掉旧 helper。

同一 helper 内登记 insertion_id：第一次执行前占位；重复 id 不再次写入，曾写/不确定则返回 unverified/unknown，曾明确零写入则 failed/none。最多保留 10000 个仅含身份与 effect 的标记，达到上限拒绝新写入，不通过逐出旧 id 允许重放。跨 helper 重启仍由 Main 的持久化执行日志与服务端 claim 保护。目标缓存上限 64，逐出只使旧显式目标失效；不跨重启恢复句柄，不落盘原文。

S4 Windows 实测发现 POSIX 目录 fd 的 fsync 返回 EPERM。执行日志仍先同步内容；原子 rename 后 Windows 以可写句柄再次同步最终文件并读回标记，POSIX 继续同步目录。任何同步/读回错误继续阻止粘贴，不吞错误。服务端 claim 与已存在/未知标记仍独立禁止再次写入，不以本机文件缺失作为重放资格。依据 [FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers) 的可写文件句柄要求；Windows 不用 POSIX mode 位冒称已验 NTFS ACL，DPAPI/实际安装账号目录权限保留真机验收。

## 一次粘贴流程

1. Main 核当前账号/会话/result/hash/review，再 claim 并持久化 started；这套既有执行日志不能由 Windows 分支绕过。Windows 不向 helper 传 token、账号凭据或输入框原有全文。
2. helper 在有界等待修饰键释放后选取最终目标；不能把独立探针里“固定夹具 PID”的限制复制到正式通用输入。正常路径不恢复焦点；显式路径只在用户明确选择、可核验且当前仍是本应用/原目标时尝试恢复，必须读回实际前台与焦点成功，失败就保留结果。
   显式目标捕获时，另冻结 `GA_ROOT` 顶层 HWND、PID 与进程创建时间；后续必须仍是该同一根窗口，保留原控件引用、原文和选区。UIA 恢复后前台可能返回原 Edit 子控件，须用它实际所属的 `GA_ROOT` 与冻结身份核对，不能只比较 PID，也不能使用会合并拥有者弹窗的 `GA_ROOTOWNER`。原始 HWND 失效、根身份或控件/基线变化仍拒绝；正常自动输入的前台比较不变。依据：[GetAncestor 的根与拥有者区别](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getancestor)。
   真实复用实验发现：经典 Edit 销毁后，Windows 可复用完整数值 HWND，UIA 比较也可能把新控件视为原控件。捕获 UIA/基线前，使用本产品固定私有窗口属性名，为当前窗口及同进程原生焦点 HWND 留下无正文的生命周期标记；同一存活窗口的缓存共享标记，销毁重建后必须失效。校验、紧邻派发与严格读回都核标记，无法建立/读取或被改动即停止，不靠延迟事件回调判断销毁。释放/逐出/正常退出只移除仍属于自己的标记；强制终止后不含内容的旧标记可由下次捕获替换，或随目标窗口销毁消失。不分配独立全局 atom，不占用每次捕获的新名称。适用相同/较低 IL，仍先执行原准入检查。依据：[SetPropW/UIPI](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setpropw)、[只移除自己添加的属性](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-removepropw)。
   显式恢复先激活原顶层窗口；若辅助进程被系统拒绝，仅在当前仍为本应用/原目标且生命周期未变时，向原先缓存的 UIA 控件调用 `SetFocus`。最多 250ms 等待冻结根窗口与原焦点控件实际稳定；激活返回时可能短暂无前台，不能立即把瞬态当作持久失效。不得选择其他控件或注入激活按键；之后仍比较原文/选区/生命周期，超时零写入。此准备包含在原 5000ms RPC 上限内。系统可能拒绝辅助进程激活，API 返回成功也不替代实际前台/控件读回，依据：[Win32 激活限制](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)、[UIA SetFocus](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-setfocus)。
3. 准入核当前可见顶层应用窗口、非本应用/桌面/任务栏、可访问交互桌面、PID+进程创建时间、完整性级别。普通应用拒绝更高 IL；读取失败不靠 TokenElevation 或 SendInput 成功值推定可输入。
4. UIA 可读时先核密码/保护/禁用/只读；明确按钮、菜单等非输入控件拒绝。Custom/Pane 等未知属性不能仅因不可读而一律拒绝。每次正文读回前再次检查保护属性，不读已知密码内容；未知安全属性仍有已明示限制。
5. 本地读取最终控件正文与选区，用于严格核验。优先 TextPattern；经典 `Edit` 只有 ValuePattern 时，向同 PID、当前 hwndFocus 的原生 Edit 发只读 `EM_GETSEL`，使用 32 位返回位置和 150ms 消息超时，不猜光标。空正文+合法光标是正常空输入框，不可一律当失败。S4 对照实测补充：Chromium Edit 的 TextPattern 可能以单个 U+FFFC 表示空控件，仅在 ValuePattern 独立明确为空、零光标/选区、无嵌入子对象时规范化；真实 U+FFFC 与其他差异仍原样比较（[Chromium 源码依据](https://chromium.googlesource.com/chromium/src/+/main/ui/accessibility/ax_position.h)）。Chromium 首次尚无 pattern/选区可有界重试，正文与选区无法一致则保持不可核验。正文/选区缺失仍可一次粘贴。全文最大 200000 UTF-16 单元，越界不记录可核验成功。
6. 剪贴板可安全快照后写入文字与排除格式，最终再紧邻核当前目标、保护状态、控件身份及剪贴板版本；变化期间正常路径重新采用最新合格位置，不能写回旧位置。最终核查后发生变化且无法安全重取则明确零写入。
7. `SendInput` 一次投递普通 Ctrl+V；不发 Enter、不调用 UIA SetValue/全文 setter，不作第二次输入。调用返回的事件数只表明系统接受事件，不证明目标收到。
8. 等待目标处理，350ms 后开始有界读回，最多再等 1000ms。全文必须逐字符等于“当前前缀+原样结果+当前后缀”，光标位于结果末尾且选区长度为零，目标仍为同控件，才 inserted/written/observable=true。否则 unverified/unknown；UTF-16 边界不能拆代理对，跨业务层偏移仍按码点换算。
   每次读取前和返回后都核同一个绝对截止时间；单次 UIA 阻塞后才返回的精确匹配也属于迟到，必须 unverified/unknown。平台调用未返回时仍由原 5000ms 子进程超时保护，不将迟到输出当作核验成功。
9. 仅剪贴板仍为本轮发布版本时恢复；读回结果不依赖恢复成功伪造。原生异常、超时、取消后副作用不明不能改报 failed 或自动再粘贴；Main 回执按实际 none/written/unknown 登记，历史继续保留结果。

   休眠/Renderer 中断先取消任何 busy 操作，包括 state 已为 ready 但 claim 或插入仍在途的情况，再 invalidate 原生进程；不能只按音频状态判断。已经派发并终止 helper 的情况仍为 unknown；实测强制终止可能发生在剪贴板恢复前，此时不能宣称已恢复原剪贴板，需保留实际观察。该故障参数与正常粘贴/恢复参数分开，不新增粘贴或自动复制补偿。

### 修饰键与剪贴板

等待释放 500ms；仍按住则补发 key-up，并只恢复 hook 证实仍被物理按住的键。hook 忽略注入事件，仅保存 Ctrl/Shift/Alt/Win 左右共八位来源状态，不采集正文按键或时间序列。启动时已按住但来源未知的键不冒充物理按住，超时拒绝。策略单测与托管机实测是阶段 1 证据，真实物理按住/提前释放必须 S5 另验。

剪贴板所属的 message-only window 由独立消息线程持有和 pump，不能让 JSONL stdin 等待或 UIA 阻塞占住所属窗口的消息处理。主业务线程只在有界重试后打开/持锁复制，所有已发布格式均立即提供数据，不引入延迟渲染。需要用另一个夹具进程在 helper 空闲等待 stdin 时复制，验证其不会被所属窗口消息卡住。

快照只复制可安全拥有的内存格式，复制总量上限 32 MiB；Unicode、HTML/RTF、DIB/文件列表按真实格式留存。GDI 句柄、不可复制/未知/超大格式在写之前拒绝，原内容不动并提示复制结果；本版不以丢弃格式换取自动粘贴。

Windows 会在 DIB 后枚举合成的 CF_BITMAP。仅对原格式顺序以 DIB 开始、无调色板、受界检查的 BI_RGB32 内存图片，额外用 GetObject/GetDIBits 核实关联位图尺寸及每个 RGB 像素一致后，才将该关联项视为可由保留的 DIB 重建；DIB/DIBV5 原字节仍全部保存、恢复并比对。CF_BITMAP 为源、像素冲突、未知布局/颜色配置或无法读取时继续写前拒绝，不泛化跳过 GDI。依据：[系统合成格式与枚举顺序](https://learn.microsoft.com/en-us/windows/win32/dataxchg/clipboard-formats)、[GetDIBits](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-getdibits)。

写入 `CF_UNICODETEXT`，同时写入 `ExcludeClipboardContentFromMonitorProcessing`、`CanIncludeInClipboardHistory=0`、`CanUploadToCloudClipboard=0`。原始快照、比较与写入在锁内，CloseClipboard 后重新锁定并确认所属窗口才登记发布版本；不能把关闭前的旧序号当发布版本。恢复时在同一个锁内比较版本再 Empty/Set，保留用户中途新内容。部分写入失败也仅恢复可证属本轮的版本；恢复异常明确反馈，不能用再次粘贴补救。Win+V/云行为和多格式保真仍须 S5 验证。

## 错误与业务契约

| 情况 | 原生 status/effect | 既有回执码 / UI |
|---|---|---|
| 高 IL | failed/none | PERMISSION_DENIED；Windows 专属管理员窗口说明，无“去授权” |
| 密码/只读/禁用/非输入或无法安全保存剪贴板 | failed/none | TARGET_UNSUPPORTED；说明限制并保留结果 |
| 显式目标失效或最终目标变化无法重取 | failed/none | TARGET_CHANGED |
| 已投递、读回不匹配/缺失 | unverified/unknown | VERIFY_FAILED；不自动重试 |
| helper 断开/超时/执行记录不确定 | 按共享桥派发证据判定 | CLIENT_INTERRUPTED；不重放；明确未派发才 none |

API、数据库和 Admin 不增加 Windows 原生错误枚举。细分原因/平台文案只在本机展示，服务端沿用现有状态与允许的 error_code；正文、剪贴板快照与窗口原文不上传。HUD 的“去授权”条件必须是 darwin，而非所有 PERMISSION_DENIED；unverified 不触发失败卡。语言资源提供短 HUD 文案与完整解释，不能把长段落塞进两行卡片。

## 本机 RPC 与权限

保留原有 AppInfo 字段，新增以下有类型的本机能力，不进入 HTTP API：

```ts
microphone: { status: 'granted'|'denied'|'restricted'|'not-determined'|'unknown'; error: boolean };
nativeInputSupported: boolean;
correctionObservationSupported: boolean;
autoStart: { supported: boolean; configurable: boolean; registered: boolean; enabled: boolean; systemDisabled: boolean; error: boolean };
```

`app.info` 从 Main 读系统状态，异常返回 unknown/error，不造 granted。新增 `app.openMicrophoneSettings()`、`app.openStartupSettings()`、`app.updateAutoStart(enabled:boolean)`，仅白名单主窗口可调用；参数做运行时 boolean 校验，Renderer 不能传任意 URI、注册表路径或可执行文件。Mac 既有 AX 方法保留并加平台边界。

Windows 麦克风使用 `getMediaAccessStatus('microphone')`；granted 才通过 Main 启动预检，denied/restricted 明确拒绝，unknown/读取失败明确无法确认，不创建旧操作的服务会话。Windows 不调用 `askForMediaAccess`。真实 `getUserMedia` 仍是采集的最终检查，失败由现有取消链停止本次会话；系统状态不是音频已采集的证据。系统入口固定 `ms-settings:privacy-microphone`，从系统设置返回后沿用 Settings watcher 的 focus/visible/低频复查。

首次交互登录若检测到麦克风禁用，在当前可见主窗口引导到设置权限区，提供系统设置入口；后台自启/恢复账号不因此激活主窗。后续录音失败同样保留可操作引导，不弹不存在的 Windows AX 请求框。

Windows `correctionObservationSupported=false`；Main 每会话传 `learning_enabled=false` 并不启动 CorrectionObserver。插入的 observable=true 仍表示读回成功，不能为了关观察而把已确认输入改为未核验。账号 `learning_enabled`、其他设备与 Mac 路径不变；Windows 首页/设置显示本版暂不支持自动纠正学习，已有历史计数与词库不清零。

## Windows 首次凭据持久化

首次加密成功只证明取得密文，不能证明加密配置已持久化。独立对照确认同步/异步 safeStorage 及公开 DOMStorage flush 后立即强退均无法恢复，独立正常退出预热后同组可恢复；产品强退恢复仍须另验。

Windows 持有单实例锁的 Main 在初始脚本中同步启动本应用的固定内部初始化模式，必须早于 `app.ready`。子进程使用相同 userData/sessionData，仅在 ready 后调用公开 safeStorage 加密固定非账号 canary，再正常退出，让 Electron 提交自己的配置。父 Main 等待真实退出0，核对唯一应答的 schema、两目录身份摘要及规范编码的非账号密文 challenge；自身 ready 后再独立解密为固定 canary，确认实际使用同一份已持久化的密钥，才允许 TokenStore 持久化。不能仅以子进程退出0推断配置写盘成功。challenge 只在内部管道/内存传递，不记录或落文件，验证后清空其 Buffer。父 Main 保持原 PID 与单实例锁；内部子进程不申请锁，不创建窗口/托盘/RPC/native helper，不加载账号、不发 HTTP，也不继承调试端口或模型/服务凭据。开发壳显式传入应用目录；安装版直接使用本应用 exe，所有参数均为数组，不经过 shell。

子进程隐藏启动、输出上限8KiB、准备期限8000ms；失败、超时、被终止、应答缺失/重复、目录不符、父进程无法解密或明文不符均使用既有“仅本次内存登录”分支，不将当前密文冒称可恢复，不采用明文或新密钥 provider。Mac 不执行此初始化，也不触碰其 Keychain。实现集中 [Windows 存储初始化](../../desktop/native/windows/credential-bootstrap.ts)，不读写 Chromium 私有密钥格式，不通过延迟强退测试来放宽判据。

时序依据为锁定 [Electron 主脚本加载后才初始化全局配置](https://github.com/electron/electron/blob/v42.11.15/shell/browser/electron_browser_main_parts.cc#L290-L311)和[正常退出提交配置](https://github.com/electron/electron/blob/v42.11.15/shell/browser/browser_process_impl.cc#L182-L187)。必须保留初始脚本中的同步等待；移入 ready 回调会让父进程先取得未持久化的不同密钥。实际首次注册后立即强退、后台恢复、第二实例、包启动和全部失败分支按 TC-WIN-27/33 回归；候选实验不替代正式产品通过。

## 托盘、单实例与自启

Main 保持强引用 Tray，使用自有图标，菜单“打开/退出”。首次关闭提示沿用 S0 文案，使用主窗口所属的原生信息对话框，用户确认后隐藏；确认状态保存在本机小型配置，重复关闭不重复提示。对话框等待期间防重复关闭，不阻塞音频渲染线程。关闭事件 preventDefault 后隐藏窗口，保留 Renderer/AudioWorklet、`backgroundThrottling:false`；明确退出先设置 quitting，再取消会话、invalidate native、释放快捷键/托盘，允许窗口退出。托盘创建失败不得留下无入口的隐藏进程，应保留可见窗口及退出入口并说明原因。

统一 `ensureMainWindow/showMainWindow` 处理托盘打开、第二实例、activate；最小化先 restore，已销毁则只重建一个，早到 second-instance 在初始化后执行。新窗口必须重新绑定渲染崩溃中断处理；鉴权与音频 sender 校验仍使用当前窗口。快捷键和结果不调用 showMainWindow。

自启只在 Windows 安装版可配置；开发环境不把 Electron 开发壳注册进系统。Main 启动时将 AppUserModelID 明确设置为与安装器 appId 一致的 `com.smartvoiceinput.desktop`，启动项 name 使用同一固定 ID；`setLoginItemSettings` 使用当前安装 exe、`args:['--background']`。读回必须同时满足：同 exe/args 的 openAtLogin 完整命令行匹配，以及名称、user scope、路径匹配的 launchItems.enabled；据此区分已注册与被系统禁用。锁定的 [Electron 42.11.15 实现](https://github.com/electron/electron/blob/v42.11.15/shell/browser/browser_win.cc) 将 getter 的 path 按命令行解析，因此查询参数中的 exe 路径须加双引号，支持含空格及中文的安装目录；setter 仍传原始路径，由 Electron 统一转义。launchItems.args 的 Chromium GetArgs 会省略 `--background` 这类 switch，不能用它验证启动参数；openAtLogin 仅对 AppUserModelID 对应项比较完整命令行，故固定身份与同名项是必要前提。任一证据不符均报错误，不忽略参数，也不单用 executableWillLaunchAtLogin。开关成功读回后再确认 UI；失败回滚并复核原配置，若无法确认回滚明确报状态不确定并重新读取，不假报成功。入口固定 `ms-settings:startupapps`。

启动参数仅在 Windows 使用：`--background` 创建并保留隐藏主窗口与 Tray，不弹主窗；用户主动再次启动总是打开主窗。自启不自动口述、不登录新账号、不改 token 安全存储。卸载钩子只清理本应用名称的 Run/StartupApproved 值，不删除其他启动项或用户账号文件。

## 多屏与坐标

helper 返回 UIA/Win32 物理像素；Main 对 `coordinateSpace:'physical'` 使用 `screen.screenToDipRect(null,bounds)` 转换，再走现有 HudPosition 的 displayMatching/workArea。Mac 既有 DIP 路径保持，不能把 Windows 物理坐标直接送入 Electron setBounds。回退顺序为 Win32 caret → UIA range → focused element → foreground window → mouse；保留每层失败语义，不以非空鼠标坐标冒充光标定位已验。

HudPosition 继续按当前代际/可见性检查迟到响应；显示、更新、隐藏不激活窗口。真实负坐标、100/150/200% 等缩放及输入/鼠标分屏留到实际资源可用时按 S3 矩阵执行。

## 构建、部署与三端责任

`build-voice-helper.mjs` 在 Windows 用 Cargo `--locked --release --target x86_64-pc-windows-msvc --bin smartvoiceinput-input`，显式使用 `crt-static`（[Rust 依据](https://doc.rust-lang.org/reference/linkage.html#static-and-dynamic-c-runtimes)），检查真实退出码，再将唯一正式 exe 复制到 resources/voice。Mac Swift 构建保持。Windows 安装包只装对应 `.exe`，Mac 只装其 helper；不得顺手打包 verify、probe、源码、夹具或其他平台残留文件。

正式 L2 使用同一目标与静态 CRT 参数构建的 helper，记录执行文件摘要；接真实 Main 前再独立核对 L2、开发资源和 NSIS 资源的 SHA-256 一致。独立技术 probe/verify 仍与产品程序分开，不把不同 CRT 构建直接当成同一个二进制。

沿用 NSIS、现有公开内网 CA/服务地址注入与 HTTPS/WSS；Rust 无独立运行时安装步骤，产品 smoke 检查实际 DLL 导入不依赖外部 C runtime，不加管理员 manifest/uiAccess。版本元数据按项目发布约定同步；分支 push 不创建 tag、不发布。Windows 普通 CI 构建纳入 Rust 检查/单测；技术验证与正式产品 smoke 分开，源 SHA、环境和失败分母留证。

| 交付物 | S4 工作 | 验证 |
|---|---|---|
| Desktop | 正式 helper/桥、窗口托盘自启、平台 UI/RPC、词库学习能力边界、坐标、打包 | 先类型/构建再单测，Mock 边界/故障对照；Windows 实际 helper/打包产品联调、Mac 回归；真机安装/音频在 S5 |
| Server | 核已有 per-session learning、claim/receipt、租户隔离与统计，无需新增表/API/provider | 现有编译与受影响单测，隔离 PostgreSQL 的真实 claim/回执/重复与跨账号集成，保留数据读回证据 |
| Admin | 核真实 failed/unverified/inserted 与账号统计展示，无需新增管理页 | 构建/类型检查与统计/错误态测试，接相同服务读回，不用虚构成功值替代联调 |

源代码已实现与仅设计目标必须分别记录。独立壳、Mock、CI 编译不能替代正式安装包+真实服务/模型/音频验收。

## 核查依据

- [原生桥实码](../../desktop/src/main/core/native-input.ts)、[会话执行/回执](../../desktop/src/main/core/session-service.ts)、[typed RPC](../../desktop/src/shared/ipc/rpc.ts)；本版契约以实际兼容格式为准。
- [Electron App](https://www.electronjs.org/docs/latest/api/app)：单实例、自启读取使用相同参数及 Windows launchItems；[麦克风状态](https://www.electronjs.org/docs/latest/api/system-preferences)：Windows 全局隐私设置及 Mac 请求 API；[Screen](https://www.electronjs.org/docs/latest/api/screen)：物理像素与 DIP 转换。
- [Windows 设置 URI](https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings-app)、[EM_GETSEL](https://learn.microsoft.com/en-us/windows/win32/controls/em-getsel)、[SendMessageTimeout](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw)；剪贴板、完整性及技术验证原始依据见 [Windows 验证说明](windows-technical-verification.md)。
