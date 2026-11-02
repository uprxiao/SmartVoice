# 文档索引

本仓库按“文档驱动、阶段门禁”推进（见 [AGENTS.md](../AGENTS.md) §2）。调研材料、实验、真实环境验收记录与截图只保存在维护者本地（`.gitignore`），不在公开仓库中；本仓库保留需求、设计、测试用例等可公开的部分。

## 阶段状态

| 范围 | 状态 |
|---|---|
| V1（口述、整理、插入、词库与修改学习、历史统计、管理后台） | 已完成开发与验收 |
| V2（云端 ASR provider、后台模型服务管理） | 已完成开发与验收 |
| 辅助功能未授权提示（TC-INSERT-22） | 已完成，随 v0.0.1 发布 |
| **Windows 版本（v0.0.2）**：Windows 文字插入、权限与界面按平台区分、原生辅助程序 | [范围决策](02-prd/windows-version-decision-20261029.md)已确认；**S0/S1/S2/S3 已补齐复核，S4 主体实现与当前环境自动回归通过，S5待真机验收**。见 [S0 原型](01-prototype/windows-v002.md)与 [WIN-01～12](02-prd/windows-v002-requirements.md)。完整三端检查、三平台构建、56项原生参数、23组实际Main/PG/Admin联调和补充边界通过；当前40条台账为10条pass、25条partial、5条not_run（TC-WIN-19、TC-WIN-26、TC-WIN-30 新增完整通过；WIN-30 保留一次未复现 c 异常，WIN-19 保留测试工具剪贴板失误）。真实音频/模型、物理按键、输入法、多屏及系统登录等参数保留，不宣称40条或S5全部通过 |
| 快速纠正学习（BR-08 2026-10-28 修订） | S0–S3 已起草，**待评审**；开发计划顺延到 v0.0.3（v0.0.2 先做 Windows 版） |
| Windows 无麦克风模拟验收版 | 2026-11-02 用户明确要求并进一步确认临时、贴近正式产品、验后移除。[S0](01-prototype/windows-simulation.md)已实际浏览器复核；[S1](02-prd/windows-simulation-test.md)按用户已确认行为登记；[S2](03-architecture/windows-simulation-build.md)两份独立方案对比后确定；[S3](04-test/test-cases-windows-simulation.md)随后生成并按 SIM-01～12 复核。S4 临时实现及 Desktop 765 项、构建工具 33 项与正常/模拟模块边界检查通过。已提供源码 `6ce79b8` 的手工测试安装包，Windows NSIS 安装、来源/SHA256、AMD64 主程序/helper、实际 ASAR 与无麦克风模块隔离均通过独立复核。该包[自动运行门禁](https://github.com/uprxiao/SmartVoice/actions/runs/41381696249)为 0 pass、1 fail、40 not_run，停于测试准备阶段的进程身份检查；快捷键、胶囊、外部插入尚无本批通过记录。后续仅修验证工具并复验，不将手工测试包等同 S5 或真实音频通过 |

已知限制：Mac 预览包未经 Apple 签名与公证；Windows 安装包未签名，完整业务验收未完成。helper已派发后被强制终止，临时剪贴板可能尚未恢复，须检查目标与剪贴板；系统不自动重试或补偿粘贴。至少2小时实际使用稳定性与其余必需真机参数仍待执行。

### Windows v0.0.2 阶段登记

| 阶段 | 状态 | 依据 |
|---|---|---|
| 开发前技术验证 | 阶段 1 已通过 | 源码 `ece6669`，[Windows 运行 40857579332](https://github.com/uprxiao/SmartVoice/actions/runs/40857579332)：10 项策略单测通过、15 项完整分母、9 项阻断通过；V-04 使用本人同日确认的分层判据。非阻断与真机未验项保留 |
| S0 平台补充 | 已补齐并复核 | [4 页面/28 场景](01-prototype/windows-v002.md)，继承现有视觉 token 与方案 B；实际浏览器复核权限差异、失败卡与托盘操作。原型不替代 OS 验收 |
| S1 正式需求修订 | 已整理并复核 | [WIN-01～12](02-prd/windows-v002-requirements.md)与 PRD/BR-07/11 对齐；需求用户评审依据为本人已确认的范围与 Q1–Q7，本次不追加 Windows 修改观察 |
| S2 Windows 技术设计 | 已整理并复核 | [技术设计](03-architecture/windows-v002-design.md)对齐实际扁平 JSONL、一次执行、平台能力、生命周期、坐标与三端责任；S0 首次关闭确认时序同步复核 |
| S3 Windows 测试用例 | 已生成并复核 | [40 条专项用例](04-test/test-cases-windows-v002.md)覆盖 12/12 WIN，数据、独立参数与 L1/L2/L3 判据完整；物理键盘/安装包/真实闭环要求保留 |
| S4 正式开发 | 主体实现与当前环境自动回归通过 | 正式helper、权限UI、托盘/单实例/后台自启、坐标、打包及首次凭据持久化已实现。生产源码f35b1ae的[完整检查与三平台构建](https://github.com/uprxiao/SmartVoice/actions/runs/41023324779)全部17任务成功：Desktop Mac/Windows各718项、Rust15、Admin93、Server/8个自然时钟及工具检查，另完成3目标安装包。真实PostgreSQL/API21项及Admin浏览器9组已过。[最终Windows完整验证](https://github.com/uprxiao/SmartVoice/actions/runs/41030811488)通过56原生参数、9技术阻断、23组实际Main/PG/Admin联调、NSIS两轮及真实Main/preload RPC；21张虚构界面PNG辅助，实际读回与SQL决定通过。该源码cc74187相对f35仅测试驱动变化。各批分母独立，S5未完整放行。见[三端责任](03-architecture/windows-v002-design.md#构建部署与三端责任) |
| S5 真机验收 | 未开始 | Windows x64 自测；V-04 物理按键及 V-05/06/11/12/13/15 和目标安装包均须留证 |

接续审计（2026-10-30）按剩余38条的原S3参数逐项复核，不把partial全部归为用户手测，也不为纯L1+L2用例额外挂L3。补测修复Renderer崩溃后复用死窗口、同数值HWND复用误认旧控件、ready状态在途插入未被休眠回调取消、修饰键等待期间取消仍迟到写入，以及严格成功沿用未核验提示五处缺陷。原失败保留，真实模块/原生/会话与完整Main硬退出回归均通过。原40条完整通过03/04/09/10/11/34/35，其余按原L3/环境参数保留partial或not_run。后续 TC-WIN-26 通过系统“启动应用”实际开关、应用内入口与恢复流程，新增完整通过；TC-WIN-30 在中/英布局分别通过 `Control+Shift+Space` 与 `Control+Alt+Space`，新增完整通过，首次英文测试的一次未复现 c 异常保留；TC-WIN-19 通过 284×72 失败卡、复制、关闭/新录音、8 秒、双语和 unknown 不弹失败卡，新增完整通过，测试工具未保存原剪贴板的问题单独记录。

用例完成率按整条全部必需参数通过计算：10/40＝25%；部分通过25/40＝62.5%，未执行5/40＝12.5%，尚未完整验收30条。已有执行记录35/40＝87.5%是测试开展率，不作为完整完成率。设备、软件、网络条件与物理操作按参数分别登记，不能将30条全部归为人工手测。本次新增 TC-WIN-19 HUD 失败卡/unknown、TC-WIN-26 系统启动应用入口和 TC-WIN-30 中/英布局快捷键结果。

当前验证依据（分母分别登记，不合并为40条全部通过）：

- 正式helper：[当前源码56参数](https://github.com/uprxiao/SmartVoice/actions/runs/41023296757)通过，L2、开发资源和该轮NSIS的静态CRT helper摘要一致；9项技术阻断通过，15项技术全分母中的非阻断partial/not_run保留。[19组补测整轮](https://github.com/uprxiao/SmartVoice/actions/runs/41029478928)通过协议/字段/32000 UTF16、10000身份容量、生产桥实际崩溃/5秒超时、托盘确认持久化及真实写盘失败、窗口重建和生产HUD15轮。[5组边界](https://github.com/uprxiao/SmartVoice/actions/runs/41023313801)通过完整数值HWND真实复用拒绝、进程查询拒绝、无前台、私有不可访问desktop与自然冷Chrome；不把私有desktop冒充Winlogon锁屏。
- 首次凭据：[五组对照](https://github.com/uprxiao/SmartVoice/actions/runs/40959839233)后修复初始化时序，父进程独立解密 canary 才允许持久化。[实际客户端复验](https://github.com/uprxiao/SmartVoice/actions/runs/40961476396)已通过首次注册后立即强退、后台恢复、第二实例和退出清理，原10组 Desktop/Admin UI 全部通过；系统真实重新登录另验。
- 会话与剪贴板：[19组会话边界](https://github.com/uprxiao/SmartVoice/actions/runs/41023305786)通过准备/claim/日志/helper/迟到返回与取消/账号/软件休眠回调组合、实际日志I/O失败、修饰等待取消及中英文严格成功；软件回调不替代真实OS睡眠。[3组剪贴板故障](https://github.com/uprxiao/SmartVoice/actions/runs/41020961117)通过跨进程发布竞争、实际部分写与回滚失败，相关实现及锁定依赖后续未变；故障控制仅在测试exe，正式helper不含钩子。
- 三端联调：真实PostgreSQL/API21项和Admin浏览器9组通过，含三种回执、重复请求、租户、统计、403及错误恢复，见[复跑说明](../admin/tests/browser-live-README.md)。Windows[23组实际Main/PG/Admin联调](https://github.com/uprxiao/SmartVoice/actions/runs/41030811488)通过注册/立即强退凭据恢复、真实历史重新整理、显式Notepad选区恢复/插入/防重/退出、同库Admin统计、实际快捷键冲突/持久化和Renderer崩溃恢复；新增真实插入后立即杀Main/helper，已取得本机started+SQL reserved，再后台恢复为同回执unverified/CLIENT_INTERRUPTED，正文/光标不重复改变，结果保留且实际退出成功。Admin独立显示2会话、1确认插入、成功率50.0%、0学习，不能把未知写入统计成成功。21张PNG只辅助；零PCM与Mock模型不替代真实音频/质量。
- 安装与产物：当前源码两轮NSIS安装/卸载/重装确认仅清自有启动项、保留其他虚构项和用户文件、重装默认off。[三目标构建](https://github.com/uprxiao/SmartVoice/actions/runs/41023324779)成功，三包来源、大小与SHA256独立核对。Mac另核MachO/CA/ad-hoc签名与实际Electron42.11.15；x64首次本机Rosetta只读检查15秒超时，后续独立观察17.102秒取得runtime并正常卸载挂载，不作为产品启动性能或物理Intel业务通过。Windows本地静态检查NSIS/7z、AMD64/PE32+、正式helper唯一资源、ASAR版本/固定HTTPS/CA通过；matrix包与专用Windows门禁各自构建，不冒称摘要相同。最终Windows门禁安装器已独立下载核验，其主程序/helper/ASAR/CA四资源摘要与同轮两次实际安装完全一致。未合main/tag/Release。

失败与修复保持可追溯：[Chrome空框/DIB初始失败](https://github.com/uprxiao/SmartVoice/actions/runs/40910927523)、[父退出等边界](https://github.com/uprxiao/SmartVoice/actions/runs/40924485576)、[显式恢复失败](https://github.com/uprxiao/SmartVoice/actions/runs/40935021016)均保留原分母，修复后再独立回归；Admin 250ms 夹具超时移除编译准备影响后，原限制和93项分母不变，[复验通过](https://github.com/uprxiao/SmartVoice/actions/runs/40900959347)。[上一轮失败](https://github.com/uprxiao/SmartVoice/actions/runs/40964235724)的 Chrome 准备和历史按钮点击另保留证据：现以只读 DOM 确认 Chrome 就绪，历史按钮等真实命中后点击，且核对精确 Main 窗口；没有改 UIA、插入预算或判定标准。本轮复验通过仍不覆盖真实麦克风、模型、物理按键、IME、多屏和系统登录等 S5 参数。

## 文档

| 目录 | 内容 |
|---|---|
| [01-prototype](01-prototype/README.md) | 原型说明与页面状态矩阵（原型本体在 `prototype/`） |
| [02-prd](02-prd/PRD.md) | 产品需求（[V2](02-prd/PRD-v2.md)）、[业务规则](02-prd/business-rules.md)及各项需求决策记录（`*-decision-*.md`） |
| [03-architecture](03-architecture/architecture.md) | 技术架构、[API 契约](03-architecture/api-contract.md)、[数据模型](03-architecture/data-model.md)、[会话协议](03-architecture/session-protocol.md)、[桌面契约](03-architecture/desktop-contract.md)、[Provider 配置](03-architecture/provider-config.md)、[部署](03-architecture/deployment.md)、[构建与发布](03-architecture/release.md) |
| [04-test](04-test/test-cases.md) | 测试用例（[V2](04-test/test-cases-v2.md)）、[覆盖映射](04-test/coverage.md)、[执行矩阵](04-test/execution-matrix.md)、[固定样本说明](04-test/fixtures.md)、[实验记录模板](04-test/experiment-record-template.md) |
| [05-branding](05-branding/) | Logo 设计过程 |

## 待决问题

- Windows 40 条本机仿真与正式包复核（2026-11-02）：仿真版已执行 TC-WIN-01/02/06/07/08/09/20/21/22/23/24/25/26/28/29/33。正式 MSVC 包 0.0.2（commit `8139d6d`，SHA256 `FE469F…E9C8`）已安装并登录真实账号，复核 21/22/23/25/26/28/29/33 主路径和 19 HUD 失败卡。英文 UI/HUD 失败卡通过；英文快捷键错误仍为中文。非 ASCII 安装路径 `SVI-äPath` 安装、启动、登录通过并已卸回默认。HUD 取消真实路径通过；HUD 完成首测 5 秒超时 `unverified`、目标未变，复测 `inserted`；真实快捷键再按一次结束 `inserted`；模拟/正式 helper 直接插入各一次通过（1.159s/1.091s）；失败卡新录音让位通过。中文/英文布局下 `Control+Shift+Space` 与 `Control+Alt+Space` 均完成开/停/插入，布局保持不变；最初 `Control+Shift+Space` 注册失败是正式包实例占用默认键，分离实例后恢复，非 IME 缺陷。首次英文 `Control+Alt+Space` 曾出现一个 `c`，同条件重复两次未复现，异常保留；临时 en-US 已移除。Notepad 对 CRLF+emoji 多行插入归一化为 CR，helper 返回 `unverified/VERIFY_FAILED`，未伪造精确匹配。系统“启动应用”实际入口已验，开启时开关为 On，系统关闭后 `registered=true/enabled=false/systemDisabled=true`，重新开启后恢复；已还原应用自启 off。Notepad 编辑器选区实机插入通过，`inserted`、光标 52、选区 0、未自动 Enter。独立 cmd 单行插入停在提示符、历史 `unverified/VERIFY_FAILED`、未回车；多行触发 Windows Terminal 自身确认，helper 未绕过，取消后未执行。`clipboardRestored=true` 已用 `Get-Clipboard` 文本前后哈希核对，插入前后长度和 SHA256 一致；图片/HTML/跨进程并发和 Win+V 云历史仍未验。受控挂起 helper 复现 5 秒超时，历史 `unverified/CLIENT_INTERRUPTED`、无写入，旧 helper 终止；下一次会话自动重启并 `inserted`。TC-WIN-25 已验证同路径但缺少 `--background`：应用返回 `registered=true/enabled=false/error=true`，未误报生效；连点自启开关只产生一次状态变化，随后恢复关闭。真实 API 失败/回滚的临时 ACL 注入被权限策略拒绝，未执行且未修改 ACL；该参数保持 not_run。HUD 失败卡实测 284×72px，“复制结果”点击成功，关闭/新录音/8 秒/中英文均核对；Chrome canvas 归类为受保护目标并显示失败卡，未知窗口夹具返回 `unverified/VERIFY_FAILED` 且 HUD 不弹失败卡。TC-WIN-19 产品行为通过；测试工具点击复制前未保存原剪贴板，原值未恢复，属测试失误并单独保留。托盘菜单在 Windows 11 溢出区用 UIA/合成点击未唤出，实际菜单点击仍待用户右键确认。TC-WIN-01 的 9 组插入、02 目标切换、06 保护拒绝、07 未知控件、08 取消、09 重复 id、20 学习关闭、24 隐藏主窗均通过核心组。TC-WIN-26 已完整通过：系统“启动应用”实际开关、`systemDisabled` 识别、应用内“打开启动应用设置”入口和系统恢复均实测，随后恢复应用自启 off。TC-WIN-30 已完整通过：中文/英文布局下 `Control+Shift+Space` 与 `Control+Alt+Space` 均完成开始/停止/插入，布局保持不变；最初 `Control+Shift+Space` 注册失败是正式包实例占用默认键，分离实例后恢复；首次英文测试出现过一次 `c`，同条件重复两次未复现，异常保留。40 条台账为 10 条完整通过、25 条 partial、5 条 not_run。需要用户参与的 05/12/13-15/16/17-18/27/31-32/36/38-40 留到最后；本机执行细节在本地验收记录，不提交。

- Windows 插入生命周期见证（2026-11-02，已修复待正式包复核）：S5 真机发现跨进程 `SetPropW` 在桌面堆紧张或 Chromium/XAML 窗口属性池耗尽时返回 `ERROR_NOT_ENOUGH_MEMORY (8)`，Chrome/新版记事本被误拒为 `TARGET_CHANGED`。`target_lifetime` 已改为 out-of-context `EVENT_OBJECT_DESTROY` WinEvent hook，S2 设计同步更新。本机 GNU 验证构建通过 Notepad/Chrome 完整应用会话（光标插入、选区替换、`inserted` 回执）及密码框拒绝回归；正式 MSVC 包、CI 三平台与 S5 其余参数仍待复核。

- Windows 无麦克风模拟版实施决策（2026-11-02，已采用）：用户确认“快捷键开始胶囊、不访问麦克风、再次快捷键结束并插入固定文字”，并确认尽量复用产品逻辑、验后移除。比较本机模拟服务与进程内会话适配器后采用编译期替换采集源、Main 内置 loopback HTTP/WS；正式客户端会话、资格、journal 和原生输入沿用同一实现。身份、profile、自启、虚构数据独立，临时模块与构建入口按清单可移除。实现、构建和逐参数验收尚待完成，不将原 Windows 40 条状态改为通过。

- Windows S5 执行准备（待落实）：已沟通优先准备由用户在已登录Windows桌面启动的本地PowerShell测试入口，收集报告后继续分析和补测。真机入口仍需适配与验证；当前没有建立Windows桌面连接，SSH或可视化远控通道也未配置。已有CI脚本不代表真机入口已就绪，设备提供、执行器就绪和实际用例通过须分别登记。此项不改变S3判据或S5未完成状态。

- Windows WIN-12：已提出是否追加旧版修改学习的澄清，当前按本人原先确认的“observe 后移”范围继续，不追加 Windows 修改观察，不把未回复当成新的批准。若本人选择扩围，先回到 S1/S2/S3 补齐再实现；`release` 则按实码保留为既有目标生命周期清理。见 [S1 补充](02-prd/windows-v002-requirements.md)。

- Windows 技术验证 V-04 层级已于 2026-10-29 由本人确认：阶段 1 使用托管机按住键状态、目标一次 Ctrl+V/无卡键实测及物理事件来源/恢复策略单测；物理键盘按住、提前释放等继续是 S5 真机必验。按该判据复验，未据确认本身登记通过；见[决策稿 §6](02-prd/windows-version-decision-20261029.md#6-技术验证方案先于开发)。

- Windows 版本（v0.0.2）范围与 Q1–Q7 已确认，不重复请求评审；本人于 2026-10-29 明确授权推送并按既定门禁继续 S0–S4，同范围工作沿用该授权。阶段 1 技术门禁及 Windows 专属 S0/S1/S2/S3 已依次复核，S4主体实现与三端自动回归已执行；既有通用文档与 v0.0.3 学习草稿不替代本版产物。源码只同步 `feature/0.0.2`，尚无本版 main/tag/Release；发生新需求决策仍先登记本节。
- 快速纠正学习 S1 [需求变更](02-prd/fast-correction-learning-decision-20261028.md)、S2 [技术方案](03-architecture/fast-correction-learning-design-20261028.md)、S3 用例（TC-LEARN-16～31）待本人评审后进入开发。
