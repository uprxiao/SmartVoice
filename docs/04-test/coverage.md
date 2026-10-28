2026-10-26判据追踪：FR-02-05/TC-ASR-12的真人ASR词级质量按[用户明确容错决定](../02-prd/asr-quality-decision-20261026.md)，整体原意基本保留可接受少量词语/专名听错；FR-03-02的R2整理忠实性和其他用例仍保持。无实测90%准确率声明。

2026-10-24索引同步：FR03、FR04及学习/跨设备重试相关行已按当前PRD修正；撤销的INSERT04不再作为活动映射。当前R2增量覆盖见R2验证计划（内部记录，未公开）及机器范围（内部记录，未公开）。旧v0.3的219计数和下文“未执行”快照为历史，当前225登记/215产品必需范围、状态及重验集合以新清单为准；本次索引修订不新增通过。

# 需求与测试覆盖映射（v0.3）

> 2026-10-17。依据修订后的 PRD/BR 和 API。**覆盖只表示已定义用例，全部未执行；不代表需求或阶段验收通过。**

[测试用例](test-cases.md) 共 **219 条**：保留此前195个ID，本轮追加24条容量用例。PRD **49/49 项 FR 均显式关联**；固定数据和测量方法见 [fixtures](fixtures.md)。下表按用例“关联”列生成并交叉检查，不凭功能名猜映射。

| 需求 | 验收内容 | 对应用例 |
|---|---|---|
| FR-01-01 | 用户可用邮箱 + 密码注册；不发送邮件验证 | TC-AUTH-01、TC-AUTH-02、TC-AUTH-03、TC-AUTH-17、TC-API-09、TC-NFR-07 |
| FR-01-02 | 用户可用邮箱 + 密码登录，获取 access/refresh token | TC-AUTH-04、TC-AUTH-05、TC-AUTH-07、TC-AUTH-12、TC-AUTH-13、TC-AUTH-19 |
| FR-01-03 | 客户端安全存储 token，支持退出登录 | TC-AUTH-08、TC-AUTH-09、TC-AUTH-11、TC-AUTH-13、TC-AUTH-15、TC-AUTH-16、TC-AUTH-18、TC-API-10、TC-NFR-05、TC-NFR-08 |
| FR-01-04 | 管理员可禁用用户；新请求立即拒绝，旧登录全部撤销，进行中 WS 最迟 5 秒终止 | TC-AUTH-06、TC-AUTH-14、TC-AUTH-19、TC-ADMIN-03、TC-NFR-05 |
| FR-01-05 | 登录后记录设备信息（名称、平台、最后活跃时间） | TC-AUTH-04、TC-AUTH-10、TC-AUTH-16 |
| FR-02-01 | 全局快捷键开始/停止录音，快捷键可配置 | TC-VOICE-01、TC-VOICE-02、TC-VOICE-10、TC-VOICE-15、TC-VOICE-22、TC-API-06、TC-API-17、TC-API-18、TC-NFR-02、TC-NFR-03、TC-NFR-12 |
| FR-02-02 | 录音时显示胶囊：波形、取消、完成 | TC-VOICE-03、TC-VOICE-13 |
| FR-02-03 | 音频 16 kHz 单声道 PCM，250 ms 分块上传 | TC-VOICE-04、TC-VOICE-09、TC-VOICE-11、TC-VOICE-13、TC-VOICE-16、TC-VOICE-21、TC-VOICE-23、TC-API-01、TC-API-02、TC-API-05、TC-API-12、TC-API-13、TC-API-20、TC-API-22、TC-CAP-12 |
| FR-02-04 | 录音期间可实时看到稳定片段（partial） | TC-VOICE-05、TC-VOICE-22、TC-ASR-19、TC-API-19、TC-NFR-10、TC-CAP-10、TC-CAP-18 |
| FR-02-05 | 停止后服务端完成尾音识别与全文整理 | TC-VOICE-01、TC-VOICE-14、TC-VOICE-17、TC-VOICE-18、TC-ASR-12、TC-API-03、TC-API-11、TC-API-13、TC-API-21、TC-NFR-01、TC-CAP-19 |
| FR-02-06 | 最长录音时长由服务端能力下发（默认 180 秒） | TC-VOICE-06、TC-API-04 |
| FR-02-07 | ready 前取消后丢弃音频/迟到结果；不回传、不插入、不学习；已提交结果的关闭行为见 BR-04 | TC-VOICE-07、TC-VOICE-08、TC-VOICE-09、TC-VOICE-12、TC-VOICE-19、TC-VOICE-20、TC-API-08、TC-CAP-11、TC-CAP-22 |
| FR-02-08 | 当前容量验收目标为20个不同账号同时口述/处理；每账号最多1个活动会话，未来服务器可配置更高并发，满额创建明确提示繁忙（BR-12） | TC-CAP-05、TC-CAP-07、TC-CAP-08、TC-CAP-09、TC-CAP-10、TC-CAP-11、TC-CAP-12、TC-CAP-13、TC-CAP-14、TC-CAP-15、TC-CAP-16、TC-CAP-18、TC-CAP-19、TC-CAP-20、TC-CAP-21、TC-CAP-22、TC-CAP-23、TC-CAP-24 |
| FR-03-01 | 去除口头禅、无意义重复；明确改口只保留最终说法 | TC-ASR-01、TC-ASR-02、TC-ASR-03、TC-ASR-13、TC-ASR-16 |
| FR-03-02 | 保留有效数字、路径、专名、否定约束、条件、理由；明确改口保留最终说法，以原话验收，不要求模型提供编辑明细（BR-06/R2） | TC-ASR-02、TC-ASR-03、TC-ASR-04、TC-ASR-05、TC-ASR-11、TC-ASR-12、TC-ASR-13、TC-CAP-18 |
| FR-03-03 | 合理分句/分段；明确并列项可输出列表 | TC-ASR-06、TC-ASR-18 |
| FR-03-04 | 固定R2一次完整纯正文；校验非空/正常结束/截断拒绝/取消及预算，故障回退raw待确认；无第二次LLM审核，不声称程序证明语义正确 | TC-ASR-07、TC-ASR-09、TC-ASR-10、TC-ASR-14、TC-ASR-15、TC-ASR-16、TC-ASR-17、TC-ASR-18、TC-ASR-19、TC-ASR-20、TC-NFR-09、TC-CAP-13、TC-CAP-14、TC-CAP-19 |
| FR-03-05 | 返回 `raw_text` 与 `refined_text`，客户端可展示/复制原始转写 | TC-ASR-08、TC-ASR-09 |
| FR-04-01 | 正常听写跟随实际粘贴前最新可输入位置及当前选区；不因早先目标变化拒绝；历史/手动插入仍显式指定目标 | TC-INSERT-01、TC-INSERT-02、TC-INSERT-03、TC-INSERT-12、TC-INSERT-13、TC-INSERT-16、TC-INSERT-17、TC-INSERT-19、TC-API-07、TC-API-14、TC-NFR-01、TC-CAP-20 |
| FR-04-02 | 最终目标能力与当前选区保护；同一原生请求紧邻写入获取目标，不覆盖新输入；兼容粘贴一次尝试且不可读回不计成功；INSERT04已撤销 | TC-INSERT-03、TC-INSERT-10、TC-INSERT-13、TC-INSERT-15、TC-INSERT-21 |
| FR-04-03 | 校验失败时保留结果，提供复制/重新指定，不自动重试 | TC-INSERT-03、TC-INSERT-05、TC-INSERT-09、TC-INSERT-11、TC-INSERT-14、TC-INSERT-16、TC-INSERT-18、TC-INSERT-21、TC-INSERT-22 |
| FR-04-04 | 不自动按 Enter，不自动发送 | TC-INSERT-06、TC-INSERT-20 |
| FR-04-05 | macOS 使用 Accessibility；Windows 使用 UI Automation | TC-INSERT-01、TC-INSERT-02、TC-INSERT-07、TC-INSERT-08、TC-INSERT-09、TC-INSERT-22、TC-NFR-08 |
| FR-05-01 | 支持新增、编辑、删除、搜索词条 | TC-DICT-01、TC-DICT-02、TC-DICT-03、TC-DICT-04、TC-DICT-08、TC-DICT-11、TC-DICT-17、TC-SET-07、TC-API-17 |
| FR-05-02 | 词条包含正确词、别名、来源（手动/自动）、学习语境 | TC-DICT-01、TC-DICT-02、TC-DICT-07、TC-DICT-11、TC-DICT-16、TC-DICT-17、TC-LEARN-15 |
| FR-05-03 | 支持 CSV 导入 | TC-DICT-05、TC-DICT-12、TC-DICT-13、TC-API-09 |
| FR-05-04 | 自动学习可撤销 | TC-DICT-06、TC-DICT-14、TC-DICT-18、TC-LEARN-12 |
| FR-05-05 | 词库服务端按用户隔离并持久化；单会话固定版本，重启后保留 | TC-AUTH-11、TC-DICT-09、TC-DICT-10、TC-DICT-15、TC-DICT-18、TC-NFR-06、TC-NFR-07 |
| FR-06-01 | 插入后观察同一输入框内本次插入片段的修改 | TC-INSERT-17、TC-INSERT-19、TC-LEARN-01、TC-LEARN-06、TC-LEARN-08、TC-LEARN-09、TC-LEARN-10、TC-SET-06、TC-API-15、TC-NFR-04 |
| FR-06-02 | 仅学习明确的局部词语替换；整段改写、清空、切换控件不学习 | TC-LEARN-02、TC-LEARN-03、TC-LEARN-08、TC-LEARN-09、TC-LEARN-13、TC-LEARN-14 |
| FR-06-03 | 满足局部校验、分类证据和无歧义条件时自动学习并提示，可撤销；有歧义跳过，不弹加入确认卡（BR-08） | TC-DICT-16、TC-LEARN-01、TC-LEARN-04、TC-LEARN-07、TC-LEARN-11、TC-LEARN-12、TC-LEARN-13、TC-LEARN-15、TC-API-15、TC-API-16、TC-API-17、TC-CAP-13、TC-CAP-21 |
| FR-06-04 | 词条用于后续识别/词库纠错；唯一有效冻结别名按正确词纠正，可改同句多处；遵守歧义和保护区规则，不做任意子词/链式替换 | TC-DICT-07、TC-LEARN-05 |
| FR-06-05 | 纠正后正常发送即判定学习，按键顺序作最终修改资格（宁漏不错），冻结后不被下一句取消（BR-08 2026-10-28） | TC-LEARN-02、TC-LEARN-03、TC-LEARN-06、TC-LEARN-10、TC-LEARN-16、TC-LEARN-17、TC-LEARN-18、TC-LEARN-19、TC-LEARN-20、TC-LEARN-21、TC-LEARN-22、TC-LEARN-23、TC-LEARN-26、TC-LEARN-27、TC-LEARN-28、TC-LEARN-29、TC-LEARN-30、TC-LEARN-31 |
| FR-06-06 | 首页学习卡片显示最近一次观察结果，不显示“持续学习中” | TC-LEARN-25、TC-LEARN-31 |
| FR-06-07 | Electron 类应用读不到输入框时开启其辅助功能 | TC-LEARN-24 |
| FR-07-01 | 记录会话时间、模式、原始转写、整理稿、状态、耗时 | TC-AUTH-11、TC-VOICE-20、TC-HIST-01、TC-HIST-07、TC-HIST-08、TC-SET-07、TC-ADMIN-11、TC-API-21、TC-NFR-07、TC-NFR-12 |
| FR-07-02 | 全部/听写筛选及原文/整理稿复制；按 retry_kind 重试；同账号其他桌面可重新整理历史文本，ASR重试仍限创建设备，插入失败复用结果（BR-05） | TC-VOICE-18、TC-HIST-02、TC-HIST-03、TC-HIST-04、TC-HIST-09、TC-HIST-10、TC-HIST-11、TC-HIST-14、TC-API-14、TC-API-17 |
| FR-07-03 | 保留时长为永久/7/30/90 天，默认永久；到期正文不可读且 1 小时内清理，统计和独立词库保留 | TC-HIST-05、TC-HIST-13、TC-HIST-14、TC-HIST-15、TC-NFR-06、TC-NFR-11 |
| FR-07-04 | 服务端持久化元数据与本次文本；不长期保存音频，完整音频 ASR 失败仅内存保留最多 600 秒 | TC-HIST-03、TC-HIST-06、TC-HIST-09、TC-HIST-12、TC-NFR-04、TC-CAP-15 |
| FR-08-01 | 展示字数、总口述时长、平均速度 | TC-STAT-01、TC-STAT-05、TC-STAT-06、TC-STAT-07、TC-STAT-08、TC-STAT-09 |
| FR-08-02 | 展示活跃天数、当前/最长连续天数 | TC-STAT-02、TC-STAT-10 |
| FR-08-03 | 展示活动热力图 | TC-STAT-03、TC-STAT-10 |
| FR-08-04 | 显示累计成功学习次数及“持续学习中”，不展示未经定义的个性化百分比 | TC-STAT-04、TC-STAT-11 |
| FR-09-01 | 快捷键配置（语音输入，可扩展多组） | TC-SET-01 |
| FR-09-02 | 界面语言（简中/英文）；设置中可开关自动学习，默认启用 | TC-SET-02、TC-SET-06 |
| FR-09-03 | 显示服务连接状态与账号信息 | TC-ASR-20、TC-SET-03、TC-SET-05、TC-SET-07、TC-SET-08、TC-API-18、TC-NFR-02、TC-NFR-10 |
| FR-09-04 | API 地址集中定义，部署时通过构建配置替换；V1 不提供运行时地址编辑器 | TC-SET-04 |
| FR-10-01 | 管理员登录 | TC-AUTH-17、TC-AUTH-18、TC-ADMIN-01、TC-ADMIN-07、TC-ADMIN-08、TC-ADMIN-11、TC-API-10、TC-CAP-01 |
| FR-10-02 | 用户列表：注册、启用/禁用、查看用量 | TC-ADMIN-02、TC-ADMIN-03、TC-ADMIN-04、TC-ADMIN-09 |
| FR-10-03 | 使用频率统计：日活、会话数、音频时长、成功率 | TC-STAT-05、TC-STAT-07、TC-STAT-08、TC-ADMIN-05、TC-NFR-03、TC-NFR-11 |
| FR-10-04 | 模型与用量概览（ASR/LLM 调用次数、token） | TC-ADMIN-06、TC-ADMIN-10、TC-NFR-09 |
| FR-10-05 | 管理员查看运行容量和模型负载、修改全局会话上限；校验部署上限、版本冲突与生效结果，降容不终止在途任务，区分配置容量与已验证容量（BR-12） | TC-CAP-01、TC-CAP-02、TC-CAP-03、TC-CAP-04、TC-CAP-05、TC-CAP-06、TC-CAP-09、TC-CAP-16、TC-CAP-17、TC-CAP-21、TC-CAP-23、TC-CAP-24 |

## 非功能与关键交叉规则

| 规则/风险 | 对应用例/依据 |
|---|---|
| 实际停止到插入性能、冷暖/样本/失败计算 | TC-NFR-01/02；F-PERF，音频待采集，不能以文档为通过 |
| 容量、无串话、单实例运行 | TC-NFR-03/12、TC-VOICE-15；10账号/2容量只验证接纳与拒绝；TC-CAP-01～24新增后台控制、20路真实负载、突发、资源界限及扩展 |
| 账号撤销/租户/角色隔离 | TC-AUTH-08/11/14/16/17/19、TC-ADMIN-03/07/09/11、TC-NFR-05 |
| 本地token/远程HTTPS与音频/正文隐私 | TC-AUTH-09/15/18、TC-NFR-04/05、TC-HIST-06、TC-LEARN-09 |
| cancel/result/迟到事件原子竞态 | TC-VOICE-19/20、TC-INSERT-21、TC-API-08/19/21 |
| claim与实际写入分离、崩溃/回执丢失 | TC-INSERT-12/15/16/17/18/19、TC-API-07/14/17 |
| 尾音/ACK丢失/终态后坏消息 | TC-VOICE-17/18/23、TC-API-03/05/11/13/20/21/22 |
| 重试与严格600秒缓存/工作副本清除 | TC-HIST-03/09/10/11/12、TC-API-14、TC-NFR-09 |
| 词库冻结、撤销冲突、事务和词条歧义 | TC-DICT-06/08/14/15/16/18、TC-LEARN-11/13/14 |
| 保留确认、访问屏障、统计固化、备份恢复 | TC-HIST-05/13/14/15、TC-STAT-07、TC-NFR-06/11 |
| 模型能力/降级/重试用量unknown | TC-ASR-09/10/17/19/20、TC-ADMIN-06/10、TC-NFR-09/10 |
| 迁移实际约束、打包及真实平台 | TC-NFR-07/08；测试用例第13节兼容矩阵 |

## 未执行与缺项

- 真实音频与逐样本SHA-256尚待采集；A10/A30/A120朗读全文及脚本SHA-256已冻结，固定纯文本和手算夹具已定义。
- Linux生产硬件、ASR/LLM具体版本/模型摘要、网络基线和实际延迟尚未提供；TC-NFR-01保持未执行，不把性能目标改成预期通过。
- Windows10/11 x64实机及打包安装、权限、原生UIA/SendInput插入证据尚未执行；macOS也须在本项目实际代码/安装包实现后重新验收。
- 登录/权限/管理后台与异常状态的离线原型已补齐，并有 浏览器核验与截图（内部记录，未公开）；这些是 UI 演示证据，不能作为应用用例通过。
- 本轮没有开发server/desktop/admin实现，没有执行迁移、provider、模型或应用测试。本轮用例设计与覆盖复核完成；开发和验收门禁以主文档索引登记为准。

2026-10-20新增映射：FR-05-06（多选/原子批删）→TC-DICT-22、23、24、25、26、27；S0本地原型、S1补充、S2批删契约确定后补例，仍待实现验收。
