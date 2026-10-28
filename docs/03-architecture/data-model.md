2026-10-23 R2接入暂不新增DDL：原稿/整理稿、model_calls身份/耗时/token/失败字段复用；提示版本以部署配置/提交/摘要绑定，不冒称已有数据库prompt列。详见[R2设计§6](r2-production-design-20261023.md#6-外部接口表和状态)。

# PostgreSQL 数据模型（v0.3）

2026-10-17。实现依据：[业务规则](../02-prd/business-rules.md)、[API](api-contract.md)、[会话协议](session-protocol.md)。只定义待实现的迁移，不代表已建库。

## 1. 统一约束

- `id` 为服务端 UUID 主键；表中 `user_id` 均 NOT NULL 并引用 users(id)。下表 `?` 代表可空，其他列 NOT NULL；没有标默认值的列必须由业务明确写入。计数为 bigint≥0；版本/attempt为int≥1；时间为timestamptz UTC，created_at默认now()。
- 用户拥有的对象都有 `UNIQUE(user_id,id)`，下游用 `(user_id,object_id)` 组合FK，不仅靠应用查询过滤。关联 session/attempt/result/insertion 还使用含 session_id 的组合键，避免同账号串到另一会话。所有FK默认 ON DELETE RESTRICT；V1不做用户硬删除或级联清空。
- 所有业务查询带user_id，管理统计通过独立授权服务显式扩大范围。email/状态/枚举/非负数/版本有数据库CHECK；复杂JSON结构与Unicode码点长度由Pydantic校验，数据库加jsonb类型/最大容量兜底。
- 用户文本不进入日志、幂等响应快照或统计表；正文只保存在明确列及有保留期限的局部学习快照。音频、输入框原值/选区/全文、明文密码/token不建列。

## 2. 身份与偏好

| 表 | 字段与约束 |
|---|---|
| users | `id,email:text UNIQUE,password_hash:text,display_name:text?,role:text='user',status:text='active',locale:text='zh-CN',history_retention_days:int?=null,learning_enabled:bool=true,settings_version:int=1,created_at,last_login_at?`；email已trim+lower≤254，role=user/admin，status=active/disabled，retention null/7/30/90，locale zh-CN/en |
| devices | `id,user_id,installation_id:uuid,name:text,platform:text,client_version:text,created_at,last_seen_at`；UNIQUE(user_id,installation_id)，platform macos/windows，name≤64，version≤32；按(user_id,last_seen_at DESC,id)索引 |
| login_sessions | `id,user_id,device_id:uuid?,client_type:text,refresh_hash:bytea UNIQUE,refresh_expires_at,revoked_at?,created_at,last_used_at`；desktop必device，admin_web必null；组合FK(user_id,device_id)；refresh_hash长度32，不存明文；索引(user_id,device_id,revoked_at)、(refresh_expires_at) |

JWT sid=login_sessions.id，900秒access；refresh固定30天。登录事务锁定user/device，撤销同账号同安装ID旧sid再插新sid；refresh不轮换，必须查询user status和sid过期/撤销。登录会话可在refresh过期/撤销且相关业务不再需要引用后清理；sessions仅记录创建sid UUID作为审计身份、不建必须永久保留的sid FK，不能用它重新授权。

用户禁用与全部sid撤销同事务；管理员保护在锁定相关admin行后判断，防止并发禁用最后管理员。用户settings_version与locale共用users列，locale仅通过settings PATCH按版本写入；users/me PATCH只改display_name，避免绕开偏好CAS。

## 3. 会话、尝试与结果

### sessions

`id,user_id,device_id,created_login_sid:uuid,client_request_id:uuid,request_hash:bytea,mode:text='dictation',state:text='created',current_attempt_no:int=1,current_result_id:uuid?,dictionary_version:bigint,learning_enabled:bool,received_samples:bigint=0,audio_complete:bool=false,created_at,end_accepted_at?,first_terminal_at?,content_cleared_at?,first_asr_failed_at?,retry_cache_status:text='none',error_code:text?,asr_hints:jsonb?,llm_hints:jsonb?`

- state仅created/streaming/finalizing/refining/ready/empty/failed/cancelled；mode仅dictation。received_samples≤2880000；audio_ms=`floor(received_samples×1000/16000)`，保留样本数为权威，不逐帧对ms舍入再相加。
- UNIQUE(user_id,client_request_id)，UNIQUE(user_id,id,current_attempt_no)用于当前尝试校验；部分唯一索引 `(user_id) WHERE state IN ('created','streaming','finalizing','refining')`。索引(user_id,created_at DESC,id DESC)、(state)、(first_terminal_at)。
- current_result_id组合FK指向本session的results(user_id,session_id,id)，可延迟到提交检查以支持同事务建结果后更新指针。ready必须current_result_id非空；非ready可保留旧指针供追溯，但API不授予插入资格。
- 创建时锁dictionary_state取得版本与同事务快照；ASR提示≤100正确词/2000码点可持久化asr_hints，完整词库快照只在本轮进程内存。ASR完成时按该快照筛出的LLM提示≤50条/4000码点与raw_text、refining状态同事务保存llm_hints。refine重试复用这些提示，不读取新词库。ASR失败缓存同时保留该轮快照；进程重启音频重试失效，避免无法复原旧版本时冒用新词库。
- `retry_cache_status` 为none/available/expired/evicted/server_restarted；资格由首次失败+600秒绝对截止和实际owner共同决定，数据库available不能凭空恢复音频。截止立即失效，本地PCM≤截止后1秒、工作进程退出确认≤截止后2秒；内存释放先于可能阻塞的数据库状态更新，状态更新按租户/session/attempt CAS，不得因晚写覆盖新attempt或恢复资格。计量及元数据保留，不在数据库中保存音频副本。`content_expires_at` 是按当前用户retention与first_terminal_at派生值；未终结时null。改为更长时限不能恢复已经content_cleared的内容。读取先检查到期，清理任务滞后不影响访问限制。

### session_attempts

`id,user_id,session_id,attempt_no:int,kind:text,source_attempt_no:int?,state:text,raw_text:text?,asr_ms:bigint=0,refine_ms:bigint=0,stop_to_result_ms:bigint=0,error_code:text?,started_at,ended_at?`

UNIQUE(user_id,session_id,attempt_no)，组合FK→sessions。kind=initial/asr/refine；state=running/ready/empty/failed/cancelled。raw_text≤32768码点。每次重试新行；源attempt必须同session。只有当前attempt能提交结果。首次录音不在attempt重复保存音频计数，避免重试重复累计用户录音时长。

### results

`id,user_id,session_id,attempt_no:int,raw_text:text?,refined_text:text?,review_required:bool,review_reason:text?,text_hash:char(64),dictionary_version:bigint,char_count:bigint,stop_to_result_ms:bigint,created_at,content_cleared_at?`

UNIQUE(user_id,session_id,id)，UNIQUE(user_id,session_id,attempt_no)；组合FK→session_attempts(user_id,session_id,attempt_no)。未清理时raw/refined非空，SHA-256对应原样refined UTF-8；清理后两者均null，hash/计数字段保留。review=false→reason null；true→reason合法且清理前raw=refined。char_count为Unicode L/N码点数，清理前固化，后续聚合不依赖正文。API attempt_kind由所属session_attempts.kind关联取得，不双存；stop_to_result_ms对initial以end_accepted_at起算，对asr/refine以该attempt.started_at起算，重试耗时不得混入首次性能样本。

新结果提交事务：锁session→验证current attempt与运行态→插results→替换current_result_id、state=ready→更新session_contributions和日聚合→提交→发事件。若cancel先提交，事务CAS失败，丢弃模型输出；绝不补发ready。

## 4. 插入与幂等

### insertions

`id:uuid`（来自insertion_id）、`user_id,session_id,result_id,device_id,text_hash:char(64),review_confirmed:bool,status:text='reserved',observable:bool=false,error_code:text?,request_hash:bytea,claimed_at,completed_at?`

- device_id只从claim时desktop sid获取；receipt/correction要求同设备，另一设备的同账号请求403，跨租户404。组合FK(user_id,session_id,result_id)→results，(user_id,device_id)→devices；UNIQUE(user_id,id)、UNIQUE(user_id,session_id,id)。status reserved/inserted/failed/unverified。
- **部分唯一索引 `(user_id,result_id) WHERE status IN ('reserved','inserted','unverified')`**。failed可以多次，但每次必须新ID且明确用户发起；任何reserved/inserted/unverified阻止模型重试。
- CHECK observable=false OR status='inserted'；completed_at在reserved为null、其余必需；inserted的error_code=null。claim与retry均锁session，不能两个不同请求同时通过检查。
- 回执终态CAS只允许reserved→终态，相同回执重复无副作用。reserved崩溃后不自动改failed或归还资格；永不根据“没有回执”假定没写入。迟到合法回执允许补记统计，正文已清理也不影响该已领取操作的回执。

### operation_requests

`id,user_id,operation:text,request_key:uuid,request_hash:bytea,target_type:text,target_id:uuid,created_at`

UNIQUE(user_id,operation,request_key)，只保存归一化参数的HMAC-SHA256与对象身份，不存正文请求/响应；所有request_hash同样采用此方式，避免含密码的管理员创建幂等摘要退化为快速密码哈希。create的client_request_id、correction_id、insertion_id另有自然唯一键；retry、词库写入、confirm/undo、admin创建、CSV导入使用本表。operation包含资源ID；幂等记录与业务修改同事务，重放读当前对象状态。无永久副作用的400/422/429不写记录。

## 5. 词库与学习

| 表 | 字段与约束 |
|---|---|
| dictionary_state | `user_id PK,version:bigint=0,updated_at`；注册事务创建；每次实际词条变更锁此行并version+1，最多5000条检查也在锁内 |
| dictionary_terms | `id,user_id,term:text,norm:text,aliases:jsonb,contexts:jsonb,scope:text='default',source:text,version:int=1,deleted_at?,created_at,updated_at`；active部分唯一(user_id,norm,scope) WHERE deleted_at IS NULL；term/aliases/contexts遵守BR-09，term/alias禁止换行和控制字符、允许词间普通空格；source manual/auto |
| learning_events | `id,user_id,session_id,insertion_id,correction_id:uuid,request_hash:bytea,char_offset:int,before_text:text?,after_text:text?,left_context:text?,right_context:text?,status:text,reason:text?,classification:text?,ambiguous:bool?,evidence:jsonb?,expected_term_id:uuid?,expected_term_version:int?,term_id:uuid?,term_before:jsonb?,after_version:int?,applied_at?,undone_at?,content_cleared_at?,created_at`；UNIQUE(user_id,correction_id)；组合FK→本session插入、同user词条；status pending/learned/suggested/ignored/failed；classification term/suggest/ignore |
| dictionary_imports | `id,user_id,request_key:uuid,file_hash:bytea,total_rows:int,state:text,created_at,completed_at?`；UNIQUE(user_id,request_key)，state processing/completed/interrupted；不存原文件 |
| dictionary_import_rows | `id,user_id,import_id,row_no:int,status:text,term_id:uuid?,error_code:text?`；UNIQUE(user_id,import_id,row_no)，组合FK→imports/terms；row_no从2，状态created/skipped/conflict/invalid；与该行词条变更同事务 |

- aliases为原显示字符串数组，归一化值用一致的NFKC+casefold派生；contexts元素含alias_norm/before/after/left/right，最多3份，删除alias必须清其context。别名跨词条不唯一，查询派生ambiguous_aliases，不做数据库全局强替换。查询量≤5000时可先采用按用户读取匹配，不要求全文搜索服务。
- 删除term仅写tombstone并version+1，清aliases/contexts等可清学习语境；旧记录保留id/norm/version使旧undo永远不命中新词条。同norm重新创建新UUID，不重置旧对象版本。手动真实修改source=manual；无变化不增词条/词库版本。
- correction内容是观察结束最终局部差异；唯一correction_id+request_hash防重复提交。分类输出decision及歧义/证据，无数值“置信度”门槛。证据必须匹配所报片段，只有term+guard+无歧义冲突才能自动应用。
- correction提交时记录目标词expected_term_id/version；目标不存在则均null，完成时重新按规范词查找并验证，不能覆盖期间新增/修改的词。异步应用事务按统一顺序锁user/session/event/dictionary_state/目标term，重新检查event仍pending、用户active与learning开关、会话learning开关、正文未过期/未清理、目标词预期版本/别名冲突/5000条容量。失败不写词条：event=failed，reason为ACCOUNT_DISABLED/LEARNING_DISABLED/CONTENT_EXPIRED/VERSION_CONFLICT/ALIAS_CONFLICT/DICTIONARY_LIMIT；已非pending仅丢弃回调，不改已有终态。content清理任务与应用任务共用session/event锁，绝不把已清before/after/撤销快照重新落库。通过检查才同时写/改term、词库版本、event.term_before/after_version/applied_at、学习统计。suggested确认另有operation key并重新验证；不能更改首次correction请求来确认。
- undo锁event及term，要求当前term.version=after_version且未清理快照；恢复旧内容也必须生成**新version**（不是倒退至旧version），dictionary_version+1，标undone_at。该学习若创建新term则undo写tombstone。重复undo只返回现状，不能减少累计学习次数。
- 历史到期清除learning_events的before/after/context/evidence/term_before；保留status/版本/计数/时间。已经应用到dictionary_terms的有限context独立保留，未确认suggested随快照清理后不可confirm；API返回CONTENT_EXPIRED。删除词条不删除学习累计事实。

## 6. 模型调用与统计（不依赖正文）

### model_calls

`id,user_id,session_id?,attempt_no?,learning_event_id?,provider:text,model_id:text,purpose:text,call_no:int,started_at,ended_at?,status:text,error_code:text?,elapsed_ms:bigint,processed_audio_samples:bigint=0,prompt_tokens:bigint?,completion_tokens:bigint?,aggregated_at?`

purpose=asr_decode/asr_finish/refine/audit/classify（audit仅兼容历史实际调用，当前口述不派发）；status=running/succeeded/failed/cancelled/unknown。外部调用前先持久化唯一call id，实际传输/解码每次都有新id，应用层重试不会覆盖旧调用；组合FK校验会话/学习归属。供应商未返token为null，不存0冒充无消耗；已发请求但进程崩溃的running改unknown，费用/用量标未知。耗时只从已知时间算，不能推测供应商最终执行状态。索引(user_id,started_at)、(provider,model_id,purpose,started_at)。不保存prompt/响应正文。

### session_contributions

`user_id,session_id PK(组合),business_date:date,received_samples:bigint=0,counted_session:bool=false,outcome:text,has_result:bool=false,char_count:bigint=0,successful_audio_samples:bigint=0,fallback:bool=false,inserted:bool=false,revision:bigint=0`

每session一行，business_date=`created_at AT TIME ZONE 'Asia/Shanghai'`对应日期，永不随重试跨日迁移。outcome=active/ready/failed/empty/cancelled；第一次有效音频使counted_session=true。新attempt运行时outcome=active、暂不计结果/失败/empty分母和口述字数/成功时长；新终态以本session新贡献替换旧贡献而非多加一次。会话数/接收时长/活跃日身份不变，统计可随明确重试的处理中状态暂时变化，与BR-10排除处理中保持一致。received_samples按实际接收累积，不再累计重放；inserted只由首次确认插入的回执置true。

### usage_daily

主键 `(user_id,date)`；字段 `sessions,received_audio_samples,result_sessions,failed_sessions,empty_sessions,cancelled_sessions,chars,successful_audio_samples,fallback_sessions,inserted_sessions,learning_events` 均bigint默认0。

### model_usage_daily

主键 `(user_id,date,provider,model_id,purpose)`；字段 `calls,successes,failures,unknown_calls,elapsed_ms,processed_audio_samples,prompt_tokens_known,completion_tokens_known,prompt_tokens_unknown_calls,completion_tokens_unknown_calls` 均bigint默认0。

successes 只计 succeeded；failures 计本地未成功的 failed/cancelled/unknown。unknown_calls 是其中供应商执行结果仍未知的子集，不表示成功或零费用；token 未返回仍为 null 并单独增加 unknown token calls。网络超时的本地失败不能因远端未知而漏计，也不能重复聚合终态重放。

- session/插入事务在锁住session_contributions后计算旧→新贡献差量，更新usage_daily；同值重复事件差量为0。模型调用终态锁model_calls，同一call只聚合一次，用 `aggregated_at?` 记录；调用归属started_at的业务日。学习累计按applied_at业务日，同一event只应用一次。
- 迟到插入修改该session创建日的inserted数；学习计实际应用日；不是“服务端收到事件当天全部加一”。不得靠读页面触发计数。HTTP成功须业务记录和对应聚合同事务提交。
- 重建日统计的来源为永久session_contributions、已应用learning_events元数据及model_calls；数据库事务失败可安全重放。日聚合可重建，原始轻量贡献不因正文清理删除。 user日表的启动校准在实例互斥/schema校验之后、恢复中断session和开放流量之前执行，避免旧贡献非空但投影缺失时先应用负差量；按用户事务、日期精确覆盖，不重复累计。在线指定用户/日期重建先锁User，与贡献/学习写入串行；只读永久元数据，不依赖正文，也不改模型调用日表。管理员DAU按daily.sessions>0的去重user数，不能把usage_daily行数当DAU（纯学习也可能有该日行）。
- model_usage_daily在实例互斥/schema校验后、开放流量和recover_incomplete之前，按已终态model_calls精确重建10个可加字段；running留给原恢复逻辑转unknown并增量计入。started、finished与重建先取同一User行锁；finished随后按租户锁Call，重建随后读取来源，避免精确覆盖丢失并发增量。历史terminal聚合标记仅补当前实际聚合时间，已有标记及调用状态/token/开始结束时间不变。该入口仅启动使用，不提供在线重建API；投影锁不替代失锁后的实例归属fencing验收。
- rates/速度/连续天数是查询派生值，不保存不可加的百分比；按BR-10公式和单位换算。ASR重复增量解码的processed_audio_samples与received_audio_samples分列。

## 7. 清理、迁移与校验

- 每小时批处理到期会话，查询入口先禁止已到期正文。清sessions提示、attempt.raw_text、results.raw/refined、learning局部正文/撤销快照；保留session/result身份、hash、版本、错误、字数和统计贡献。已清理后更改retention不能复原。音频从未存数据库。
- 同一数据库只运行一次受互斥保护的Alembic迁移步骤；DDL查元数据且验证已有列的类型/约束，不能仅“已存在”就忽略错误结构；初始化DML用WHERE NOT EXISTS。应用启动校验schema revision符合当前发布，不自动迁移。
- 迁移先在空库及上版副本演练并核对列、组合FK、部分唯一索引、CHECK、Alembic revision，再做注册/跨用户关联拒绝/重复请求/统计重建核验。失败用发布前备份恢复或前向修复，不无条件执行破坏性downgrade。
- 表清理不采用盲目CASCADE。数据库备份包含历史正文与账户信息，访问与保留策略见[部署](deployment.md)；备份恢复后先清空所有恢复的历史正文/提示/局部学习快照并撤销登录会话，再开放业务；保留统计和独立词库，避免旧备份的旧保留设置让已清正文复活。

## 8. 全局容量与审计（v0.3新增）

`runtime_capacity`：`id:smallint PK CHECK(id=1), max_active_sessions:int CHECK(>=1), version:int=1 CHECK(>=1), updated_at:timestamptz, updated_by:uuid? REFERENCES users(id)`。仅一行全局服务配置，**不是租户业务表，无user_id租户列**；只有当前active admin可读写。初始updated_by=null，启动初始化按WHERE NOT EXISTS；CHECK限制整数范围，部署上限由启动/请求校验，不把20写成数据库约束。

`capacity_changes`：`id:uuid PK, actor_user_id:uuid REFERENCES users(id), old_max_active:int, new_max_active:int, from_version:int, to_version:int UNIQUE, created_at:timestamptz`；非负/版本递增CHECK，old/new≥1且不同，to=from+1；与配置变更同事务。全局管理审计不按普通用户资源访问，不提供正文/密钥，保留元数据。无需新增历史浏览产品页面。

create/retry/容量PATCH先锁runtime_capacity行；短事务内查询活动会话数并校验，不能把内存计数当唯一权威。新增部分索引 `sessions(state,id) WHERE state IN ('created','streaming','finalizing','refining')`；既有每user活动唯一索引保留。终态路径不反向获取容量行锁。并发行为/锁序/内存预留见[并发设计](concurrency-target.md)。所有初始化与新增约束纳入原迁移幂等核验。

queue_wait_ms、队列长度、拒绝计数与内存bytes属于进程运行指标，不保存正文、不逐帧写模型调用表。model_calls仍仅真实调用才记账；未出队/未实际发出不计调用，不把排队失败算成token=0的真实请求。CapacityReport读取受限部署文件；不另建可由后台手填的“性能通过”表。
