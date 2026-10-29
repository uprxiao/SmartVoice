2026-10-19 连续口述修订：ready/review/no-target/inserted等结果场景均可直接“新的一段”，不要求先复制/插入/确认；无目标仅保留历史，无自动写入。原活动态互斥保持，见BR-04与TC-VOICE-15。

# 页面、状态与需求映射（v0.3）

Windows v0.0.2 的 4 页面/28 场景补充与平台差异对应见[独立清单](windows-v002.md)。本表既有通用页面继续有效；快速纠正学习草稿属于 v0.0.3，不作为 Windows 本版开工依据。

2026-10-17。先按已确认 BR-01～12 补齐原型，再对照 S2 契约和 S3 用例；本轮依用户20路并发/后台可调授权增加2项FR，保留原验收标准。状态入口为 `index.html#页面?state=状态`；省略 state 使用 default（结果页默认为 recording）。加载态禁止相关提交，错误态有明确反馈；按钮 hover/focus、导航选中、表单禁用是共用 CSS 状态。

## 1. 页面状态清单

2026-10-20追加 result 的 `processing-slow` 场景，与finalizing/refining同属处理中；准备54×12、录音116×34、处理92×34，处理中无完成按钮，真实结果后完成过渡再收起，耗时较长提示不确诊网络错误。对应FR-02-02/TC-VOICE-03，依据补查（内部记录，未公开）。

| 页面 | 状态名 | 入口 → 操作 → 出口 |
|---|---|---|
| login | default / submitting / invalid / disabled / limited / offline | 首次使用 → 校验邮箱/密码、提交 → onboarding；创建账号 → register；失败保留页面 |
| register | default / submitting / duplicate / offline | 登录页 → 注册 → login；重复邮箱提示直接登录 |
| onboarding | default / mic-denied / access-denied / granted | 登录 → 权限路径/重新检查 → 试说或 home；无麦克风不能试说，缺输入权限可仅复制 |
| home | default / loading / empty / error / learn-observing / learn-learned / learn-final-unconfirmed / learn-unreadable / learn-off | 导航 → 洞察/开始口述/设置；错误重试，空状态与加载分开。2026-10-28 学习卡片改为显示最近一次观察结果（观察中/已学习/改完立即发送无法确认最终修改/输入框不可读/已关闭），不再以“持续学习中”暗示实时采集；见 docs/02-prd/fast-correction-learning-decision-20261028.md |
| history | default / loading / empty / error / asr-failed / audio-expired / refine-retry / expired | 导航 → 详情/复制反馈、保留确认；可重试则进入 result，过期仅重新录音；清理后正文不显示 |
| dictionary | default / loading / empty / error / conflict | 导航 → 来源筛选/搜索 → 编辑/删除/新增/CSV；冲突保留输入不覆盖 |
| newterm | default / conflict | 独立直达或“新词” → 表单 → dictionary；空值禁用/重复报错/取消关闭 |
| settings | default / loading / error / conflict / offline | 导航齿轮 → 账户/偏好/个性化/关于/帮助；退出回 login，冲突保留旧快捷键，离线可重查 |
| result | preparing / recording / finalizing / refining | 开始口述 → 完成后模拟正常输入；处理期间取消 → cancelled，迟到回调不能再输入 |
| result | ready / review / target-changed / no-target / inserted / unverified | 正常结果自动校验原目标；原文回退明确确认；目标改变需重新指定；无目标/不确定结果供复制，后者无再次写入或重试按钮 |
| result | empty / cancelled / error / busy / asr-failed / audio-expired | 显示具体原因；仅完整失败音频可重识别，其他按规则重新录音；恢复结果不自动插入 |
| result | learned / suggested / undo-conflict | 结果 → 自动学习/不确定跳过/撤销；后续编辑产生撤销冲突，不覆盖新版 |
| admin-login | default / submitting / invalid / disabled / forbidden / limited / offline | 后台入口 → 校验/登录 → admin-users；无角色留在错误页 |
| admin-users | default / loading / empty / error / forbidden / conflict | 后台导航 → 搜索/筛选/创建/启停确认；自己不可禁用；状态冲突报错 |
| admin-stats | default / loading / empty / error / forbidden | 导航 → 日期校验 → 使用概览、每日趋势和按用户示例 |
| admin-models | default / loading / empty / error / forbidden | 导航 → 日期校验 → 调用与已知/未知用量 |
| admin-capacity | default / loading / error / forbidden / conflict / offline / submitting / draining / unavailable / verified | 后台导航→容量读取/整数校验/降容确认/保存读回；版本冲突保留输入；失联先查生效值；区分未验证/已验证示例与未知指标 |
| admin-providers（V2） | default / loading / empty / error / forbidden / conflict / submitting / test-failed / egress-denied / fallback / no-provider / linux-host | 后台导航→两类列表→新增/编辑弹窗（白名单、https/wss、重名、密钥只写、录音上限云端≤360/本机≤180）→测试并保存→设为当前（云端需勾选隐私确认，切换前再测）→停用/删除（当前项禁止）→变更日志（仅字段名）。依据 PRD-v2 FR-11、设计 §7–§8 |

空/加载不适用于登录表单，使用 default/submitting；设置无数据集合空态。弹窗共享 focus trap 与 Esc 关闭：词条编辑/删除、CSV、历史详情、保留缩短、隐私说明、权限路径、重新指定目标、退出、管理员创建/启停。静态场景只能证明布局/交互，不能证明实际授权、并发、超时或清理已实现。

## 2. 需求到界面与接口

分组区间均含首尾；每项 FR 的精确用例见 [49 项覆盖表](../04-test/coverage.md)。下表共覆盖 49 项，不把后台实现要求强行变成新增页面。

| FR | 页面 / 无界面职责 | 规则 | S2 契约 | S3 主要用例组 |
|---|---|---|---|---|
| FR-01-01～05 | login/register/settings.account/admin-users；token/设备主进程与服务端 | BR-01/03/11 | API §2/7、桌面 §1/2 | AUTH、ADMIN |
| FR-02-01～08 | onboarding/result/settings.preferences；真实音频由 Worklet/Main 完成 | BR-03/04/05/12 | 会话协议、桌面 §1/2 | VOICE、API、NFR、CAP |
| FR-03-01～05 | result 原文/整理/待确认；算法由 provider 流水线完成 | BR-06/09 | Provider §3/4、API Result | ASR、HIST |
| FR-04-01～05 | result 正常/重新指定/仅复制/不确定；平台 helper 执行 | BR-07 | 桌面 §3/4、API §4 | INSERT、API |
| FR-05-01～05 | dictionary/newterm/CSV/学习语境；服务端归一化与事务 | BR-02/09 | API §5、数据模型 | DICT、API |
| FR-06-01～04 | result learned/suggested/undo-conflict、settings 学习开关 | BR-08/09 | 桌面 §5、API §4、Provider §3 | LEARN、DICT |
| FR-07-01～04 | history/详情/保留确认；后台清理不伪装页面动作 | BR-02/05 | API §6、部署 §6 | HIST、NFR |
| FR-08-01～04 | home/settings.personalization | BR-10 | API §6、统计数据模型 | STAT |
| FR-09-01～04 | settings 五页、onboarding、服务状态 | BR-01/03/08 | 桌面 §2/6、API settings/capabilities | SET、AUTH |
| FR-10-01～05 | admin-login/users/stats/models/capacity | BR-11/10/01/12 | API §7、并发设计、三端交接 | ADMIN、STAT、CAP |

## 3. 交互数据不变量

- 回退确认插入的内容与刚展示的原文一致；恢复结果需重新指定目标。不确定写入无再次执行入口。
- 2026-10-20用户修订：不确定修改（示例「云」→「芸」）不自动加、不弹确认卡；自动学习示例为 SQL lite→SQLite，无需点击加入。撤销不减累计次数，之后手动编辑阻止覆盖式撤销。
- 词条编辑和导入使用同一内存词库；保存/删除/确认建议后列表与首页词条数可读回。冲突不关闭输入，CSV 按行报告。
- 缩短保留先确认；取消时旧值不变；延长不能恢复已清正文。账号禁用/启用先确认，状态读回；界面不能暴露用户正文。
- 容量设置在后台页内读回同一内存值/版本；调低不减少活动数，未验证不是0路通过。静态draining为5上限/8在途的快照，100部署上限仅用于展示未来服务器调至50的交互；本地默认ceiling仍20。采样值与固定时刻是演示数据，不自动轮询真实服务器。
- 原型首页/历史/统计采用固定演示历史，不随模拟“录音”生成真实会话；全链路的数据落库、分页与统计变化在真实联调测试验证。

## 4. 交接清单

开发时逐项还原当前截图和行为，移除评审工具栏并以真实 adapter 替换演示数据。原型没有提供的 OS 窗口行为，以 [桌面契约](../03-architecture/desktop-contract.md) 为实现依据；完整 Mock/真实 API/原生平台验证顺序见 [三端交接](../03-architecture/fullstack-handoff.md)。新增页面可用于开工评审，未声称用户已逐页确认新截图。
