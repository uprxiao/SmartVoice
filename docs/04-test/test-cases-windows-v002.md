# Windows v0.0.2 专项测试用例

2026-10-29，S3。在 [S0](../01-prototype/windows-v002.md)、[WIN-01～12](../02-prd/windows-v002-requirements.md)及 [S2](../03-architecture/windows-v002-design.md)确定并复核后生成。以下 **40 条**是 Windows 增量，不替代既有 [通用用例](test-cases.md)、真实模型质量、性能、隐私及两平台验收；旧 TC-INSERT-04 仍撤销。用例定义和开发前探针通过均不等于产品通过。

## 数据、层级与记录

- F-WIN：虚构原文 `BEGIN React END`；选区 UTF-16 `[6,11]`；单行插入 `中文🙂`，预期 `BEGIN 中文🙂 END`、光标 10、选区长度 0。光标插入组 `[6,6]` 的预期为 `BEGIN 中文🙂React END`、光标 10；空框组 `[0,0]` 为 `中文🙂`、光标 4。多行文本为 `中文🙂\r\nDocker`（此处以转义表示真实 CRLF），全文按原始单位比对，不统一换行后伪造严格匹配。另组 LF 单独记录目标实际保留/转换及未核验状态。
- F-WIN-TARGET：独立 Notepad 经典 Edit、Chrome input/textarea、打包 Electron 输入框；另有 password/readonly/disabled/button/canvas 不可读、普通/高 IL 窗口。输入夹具自身统计 paste 次数及 Ctrl/Shift/Alt/Meta、Enter 次数，但不能单靠事件数判 inserted。
- F-WIN-CLIP：虚构纯文本、HTML+Unicode、RTF+Unicode、DIB、文件列表（仅隔离临时假文件）、GDI/不可复制格式、超过 32 MiB；每组保存实际格式/字节摘要。不得使用真实用户剪贴板或文件进行自动测试。
- L1：纯策略单测、IPC/RPC Mock 边界；L2：托管 Windows 实际 Medium 进程+真实 Win32/UIA/打包资源；L3：正式安装包+真实桌面、物理键盘、IME、麦克风/服务/模型。某条 L1/L2 通过不能覆盖它的 L3 参数。
- 每次执行冻结源码 SHA、包/夹具摘要、OS/缩放/语言/IL、前置配置；按 `用例 × 参数组` 逐项记录 pass/fail/not_run/blocked，不省略失败分母。执行记录放本地被忽略的验收目录；缺环境不得写通过。截图仅作辅助，必须有状态/正文/光标/副作用或数据库读回。

## 原生输入与一次执行

| ID / 关联 | 层级与前置、操作 | 判定标准 |
|---|---|---|
| TC-WIN-01 / WIN-01～02 | L2/L3；Notepad、Chrome input、textarea；F-WIN 光标、选区、空框三组分别自动插入；多行框另测 CRLF/emoji | 全文、UTF-16 光标、零残余选区逐项精确相等才 inserted/written/observable；各组仅一次 Ctrl+V、无 Enter，外部前后缀不变 |
| TC-WIN-02 / WIN-01 | L1+L2/L3；录音在 A、结果处理切 B、claim 等待中改 B 光标/选区；修饰键等待时再切 C | 自动路径采用最后当前合格位置，A/B 不被迟到写入；一次登记后只有一次实际粘贴，不恢复早先捕获目标 |
| TC-WIN-03 / WIN-01、03 | L1+L2；显式历史插入捕获目标后，分别改原文/选区/控件、销毁窗口、进程退出与句柄复用；另组本应用前台允许恢复同一有效目标 | 不可核验目标 failed/none；不会转写新窗口。恢复组必须核实际前台/同控件/原文/选区一致，仍只一次粘贴；`release` 两次均安全 |
| TC-WIN-04 / WIN-02 | L1+L2；SendInput 已接受但目标不消费/读回少字/光标错/仍有选区/无法读取；含自然首次 Chrome 尚无 pattern 再可读 | 缺失或任一不符均 unverified/unknown；不能用包含子串、接受事件数或“多数一致”通过。延迟变可读仅在预算内严格匹配才成功；真空框不被当永久失败 |
| TC-WIN-05 / WIN-03 | L2/L3；普通应用向高 IL，另组相同/较低 IL；保存剪贴板序号和目标原值 | 高 IL failed/none、目标无事件/无变化、剪贴板不改；说明管理员窗口、保留结果、无自动复制/提权。普通组独立验证，不能只看 TokenElevation |
| TC-WIN-06 / WIN-03 | L2/L3；password/readonly/disabled/button 各组；无前台/桌面任务栏/锁屏/本应用各组 | 明确拒绝且 none，无粘贴/Enter；已知密码不读正文。无法取得进程生命周期/完整性时拒绝，不冒报可输入 |
| TC-WIN-07 / WIN-02～03 | L2/L3；未知 Custom/canvas，安全属性不可知但非已知保护；夹具收一次 paste | 允许一次尝试，固定 unverified/unknown，结果保留、不学习/自动再试；帮助说明未知安全属性限制，不能把它当 inserted |
| TC-WIN-08 / WIN-01、07 | L1+L2；取消/账号退出切换/休眠分别发生在准备、claim、日志后、helper 调用、迟到返回；再开始新会话 | 未派发旧操作不写；已派发不确定如实 unknown，杀旧 helper 不允许稍后复活。旧账号结果不进入新账号/新操作，不自动重放 |
| TC-WIN-09 / WIN-01、11 | L1+L2；同 insertion_id 重复请求、重复自动事件、响应丢失、helper 崩溃/5 秒超时/重启；日志写盘失败 | 跨重启依旧 claim+持久化日志防重；最多一次副作用，不确定禁止重试。日志失败在粘贴前停止；helper 内重复 id 不重写，容量满不逐出身份后重放 |
| TC-WIN-10 / WIN-11 | L1+L2；扁平 JSONL 的 permission/capture/validate/insertCurrent/insert/inputBounds/release；非法 JSON/UTF-8、过大行、空/超 32000 UTF-16 text、错误字段类型/未知 action | 数字 id 对应响应，无 data/ok 包裹；坏请求副作用前拒绝；stdout 不夹日志，不泄漏原正文。未知 probe/observe 不作为生产动作；EOF/父退出清理 |

## 修饰键与剪贴板

| ID / 关联 | 层级与前置、操作 | 判定标准 |
|---|---|---|
| TC-WIN-11 / WIN-04 | L1+L2；物理来源状态机单测；托管机注入并实际读回 Ctrl+Shift 按住，调用粘贴 | 注入 key-up 不覆盖真实来源，提前物理释放不会被恢复，启动来源未知不假装物理。实测目标只收一次普通 Ctrl+V，之后无卡键；记录 physical_keyboard_verified=false，不能覆盖下一条 |
| TC-WIN-12 / WIN-04 | **L3 必须**；真实物理按住 Ctrl/Shift/Alt/Win 左右键：在 500ms 内释放、一直按住、补发 key-up 后恢复前松开；分别执行 | 不产生 Ctrl+Shift+V/Alt/Menu 等额外动作；只恢复仍真实按住的键，物理松开后无卡键；无重复 paste/Enter。保留每组实测，托管注入不能代替 |
| TC-WIN-13 / WIN-04 | L2/L3；F-WIN-CLIP 各安全格式分别快照→粘贴→恢复 | 原格式及内容逐项保真。不能复制、GDI 或超限在写前拒绝且原内容不变；明确复制入口；拒绝不能登记为格式恢复成功 |
| TC-WIN-14 / WIN-04 | L2/L3；helper 写后、恢复前另一个进程复制新内容；另组快照与发布间变更、部分写入失败 | 发布版本 Close 后核 owner，恢复锁内比较版本；外部新内容不覆盖，快照过期不开始粘贴；异常不得再次粘贴。不能用同进程版本变化替代全部跨进程组 |
| TC-WIN-15 / WIN-04 | L2/L3；helper 持有剪贴板后空闲等 stdin，外部进程再次复制；另组 UIA 读回被拖慢时外部复制 | 外部复制能完成，所属窗口消息持续处理；不死锁、不靠杀进程才完成。同时核最终版本属于外部进程；计时及超时如实记录 |
| TC-WIN-16 / WIN-04 | L2 验格式；**L3 验 Win+V/云**；打开历史/云设置前先保存原值，自动插入虚构唯一标记，用户点击复制另组 | 自动临时文本带三种排除格式，真实历史/云均不出现；主动复制按用户动作允许。系统历史/云未可用则该参数 not_run，不以格式存在宣布实际云验证通过 |

## 权限、界面与学习范围

| ID / 关联 | 层级与前置、操作 | 判定标准 |
|---|---|---|
| TC-WIN-17 / WIN-05 | L1+L3；mic granted/denied/restricted/not-determined/unknown/异常；登录引导、快捷键、返回系统设置复查 | 仅 granted 可继续创建会话；Win 不调用 askForMediaAccess/AX。固定打开 privacy-microphone，禁用总开关有说明，未知/异常不造允许；后台恢复不激活主窗 |
| TC-WIN-18 / WIN-05、07 | L1+L3；权限预检期间取消/切账号；granted 但真实 getUserMedia 无设备/拒绝；关闭主窗继续真实采集 | 迟到预检不启动旧会话；采集失败明确反馈并取消服务会话，停止音轨/上传；隐藏窗口仍有真实 16k 单声道 PCM/250ms 分块，不以假波形通过 |
| TC-WIN-19 / WIN-06 | L1+L3；管理员/保护/目标变更/helper 不可用/unknown，简中与英文逐组 | 失败卡基准 284px、两行短提示，复制仅点击，关闭/8 秒/新录音让位。Windows 无“去授权”；unknown 不弹零写入失败卡。完整原因与结果仍可查看，所有新增文案有双语资源 |
| TC-WIN-20 / WIN-06、12 | L1+L3；Win 设置/首页/帮助/首次登录，账号全局学习偏好 true；成功输入，修改目标内容；Mac 对照 | Windows 能 strict inserted+observable，但每会话 learning_enabled=false、不启动 observe、不自动入词库；说明暂不支持并保留已有计数/词条。账号偏好和 Mac 学习不改变；手动词库/冻结识别仍正常 |
| TC-WIN-21 / WIN-06、11 | L1+L3；非法 Renderer sender、非 boolean 自启、任意 URI/path、Renderer 查询 token | RPC 白名单/类型校验；仅 Main 持有服务地址和 token，不增加任意系统命令入口；系统入口失败明确提示 |

## 窗口、托盘、自启与快捷键

| ID / 关联 | 层级与前置、操作 | 判定标准 |
|---|---|---|
| TC-WIN-22 / WIN-07 | L1+L3；第一次关闭确认前/后、第二次关闭；重启应用后再次关闭 | 首次信息确认后才隐藏，提示仅一次持久化；重复关闭直接隐藏，Renderer 不销毁/不节流；菜单只有打开/退出，UI 沿用自有资源 |
| TC-WIN-23 / WIN-07 | L1+L3；托盘打开最小化窗口、意外销毁后打开、早到第二实例、同时打开请求、Renderer 崩溃后重建 | 只保留一个主窗/主进程；重建后账号/录音 sender/崩溃中断绑定有效；托盘创建失败不留下无入口的隐藏进程 |
| TC-WIN-24 / WIN-07 | L1+L3；隐藏时录音/处理，快捷键停止、结果插入；托盘退出时存在录音/待返回 helper | 不激活主窗/HUD不抢焦点；隐藏录音与可见组音频一致；退出停止音轨/WS/快捷键/helper，不迟到输入，不用关闭按钮假装已退出 |
| TC-WIN-25 / WIN-08 | L1+L3；全新安装默认、自启开/关、连续点击、真实 API 失败/读回不符/回滚失败；启动项同路径但缺少或错误后台参数另组 | 默认 off；保存中禁重复；固定 AppUserModelID/name/user/path 与完整命令行（含 --background）读回，另核系统 enabled，不能乐观报成功；launchItems.args 省略 switch 不能作为不核参数的依据。失败原值恢复并复核，不确定状态明示。开发壳不可配置，Mac无新增设置 |
| TC-WIN-26 / WIN-08 | L1+L3；系统启动应用禁用自己的注册项、返回应用；系统重新启用 | 区分 registered 与 enabled/systemDisabled；保留应用偏好且提供 startupapps 入口；不能因 Run 项存在造生效，不能写其他应用设置 |
| TC-WIN-27 / WIN-07～08 | L1+L3；实际登录 Windows 启动（--background）、保存/未保存账号、再次手动启动；独立组使用全新profile，首次注册成功后立即强退再后台恢复 | 首次后台不弹主窗，托盘可用；不自动录音/新登录；首次密文必须在立即强退后仍能恢复，不能以增加强退前等待换取通过；手动再启动显示现有主窗；退出清理。实际系统登录参数不能只以手工 argv 代替全部通过 |
| TC-WIN-28 / WIN-08、11 | L3；隔离机器开启自启后卸载再登录、重装；保留另一个假启动项作对照 | 只清本应用 Run/StartupApproved 项，无残留启动；其他项与用户账号文件不删，重装默认 off；记录安装器与卸载器摘要 |
| TC-WIN-29 / WIN-09 | L1+L3；Shift+Space、Control/Ctrl+Space 等价写法、Escape及旧系统键；注册/保存失败、完整重启 | 保留组合明确拒绝；失败保留原键并读回，成功重启一致。不能仅保存字符串无原生注册。中文/英文报错一致 |
| TC-WIN-30 / WIN-09 | **L3**；真实中文/英文 IME；候选 Ctrl+Shift+Space 与 Ctrl+Alt+Space 分别测试录音开/停、输入法切换/候选菜单 | 无意外 IME/系统动作且录音开停一次，记录两候选各组。按 Q2 选择默认；两个不通过保留冲突未解决，注册成功不替代真实输入法 |

## 坐标、产品联调与平台验收

| ID / 关联 | 层级与前置、操作 | 判定标准 |
|---|---|---|
| TC-WIN-31 / WIN-10 | L1+L2/L3；100/150/200%、负坐标、多屏输入在屏 A 鼠标在 B；经典caret/UIA range/element/window/mouse 逐层降级 | physical→DIP 一次转换，HUD 在输入所在屏 workArea；有输入位置不优先鼠标，逐层依据可追踪；缺屏/缩放参数 not_run，不以单屏替代 |
| TC-WIN-32 / WIN-10 | L1+L2/L3；显示/移动/隐藏15次采前台PID/焦点；定位迟到、取消、开启下一次录音 | HUD showInactive/非激活，不改变当前目标；停止/取消后迟到 bounds 不复活，新代际不混入旧坐标 |
| TC-WIN-33 / WIN-11 | L1+L2/L3；真实 Windows 构建/NSIS、带空格/非ASCII安装路径、冷暖启动/崩溃重启；检查包文件；首次凭据子进程异常退出/超时/应答缺失或其他profile参数 | 只带正式 .exe，无 probe/verify/夹具/其他平台 helper；x64 Medium 无控制台，四轮 ping/真实输入读回，路径不依赖 cwd；初始化耗时按真实边界记录。初始化子进程须正常退出且目录身份相符才允许持久化；异常仅内存登录，无明文fallback/递归启动/额外主窗；Mac不走此流程 |
| TC-WIN-34 / WIN-01、11 | L1+真 PostgreSQL HTTP；同结果重复 claim/receipt、inserted/failed/unverified、跨账号访问；源码冻结 | API/数据库读回状态和 effect 一致；tenant 拒绝；重复不重复统计/执行，unknown 不计 inserted；不新增 Windows 服务端枚举/表 |
| TC-WIN-35 / WIN-06、11 | Admin 构建/类型+Mock错误/空态→接 TC-WIN-34 同服务数据库 | 后台实际状态/计数等于手算与数据库；普通用户无管理员访问；不把 failed/unverified 美化为成功，HTTP失败不显示假数据 |
| TC-WIN-36 / WIN-05、11 | **L3**；正式安装包→HTTPS/WSS登录→真实麦克风/ASR/LLM→Notepad及Chrome输入；真实取消/网络失败 | 完整音频/模型/整理/插入读回与历史回执闭环；CA链/IP/有效期校验保留、safeStorage token 不暴露；遵守既有质量/性能/隐私，不以 Mock 或探针结果替代 |
| TC-WIN-37 / WIN-01、03、11 | **L3**；真实终端单/多行虚构无害文字、编辑器、WPS、微信；真实选择与输入 | 一次通用 Ctrl+V、不自动 Enter/发送；终端/应用自身多行确认不绕过。可读时严格核验，不可读 unverified；应用兼容参数逐项留证 |
| TC-WIN-38 / WIN-11 | **L3**；实际取得 Win10/11 x64 按 Q6、未签名下载/安装/启动/卸载 | 实际桌面版本/硬件/包摘要/SmartScreen记录；另一个版本未验明确标注，不把 Server CI 当两个桌面版验收；不发布缺项草稿/不冒称签名 |
| TC-WIN-39 / WIN-05～12 | Mac 编译/类型/单测+正式包回归：AX/麦克风、普通输入读回、学习、关窗/Dock、HUD | Swift路径/原权限/学习可用，无 Windows 自启或管理员文案；Mac arm64/x64 参数分开，不以本机一种架构替代全部 |
| TC-WIN-40 / WIN-01～12 | L1→L2→L3 全量台账核对，重启/休眠/至少2小时稳定性及既有通用必需用例回归 | 40条及全部参数分母完整，缺陷复现/对照/修复/回归可追溯；未执行明确 not_run。必需真机/平台组不全不得宣称 S5 完整完成；不擅自发版 |

## 覆盖与阶段门禁

| 需求 | 用例 |
|---|---|
| WIN-01 | 01、02、03、08、09、34、37、40 |
| WIN-02 | 01、04、07 |
| WIN-03 | 03、05、06、07、37 |
| WIN-04 | 11～16 |
| WIN-05 | 17、18、36、39 |
| WIN-06 | 19～21、35、39 |
| WIN-07 | 08、18、22～24、27、39 |
| WIN-08 | 25～28、39 |
| WIN-09 | 29、30、39 |
| WIN-10 | 31、32、39 |
| WIN-11 | 09、10、21、28、33～40 |
| WIN-12 | 20、39、40 |

S3 复核检查 12/12 WIN、固定预期、真实副作用证据与平台参数。S4 进入前在文档索引登记三端任务。L1/L2 可以持续开发验证；S5 必须另外执行上述 L3 与既有完整验收，缺环境如实保留，尤其 TC-WIN-12 的真实物理按键不能被 TC-WIN-11 代替。
