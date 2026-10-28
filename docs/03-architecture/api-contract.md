2026-10-23 R2接入保留本文件对外API/Result形状；内部text、用途路由及故障见[R2正式设计](r2-production-design-20261023.md)。不把内部edits改造为新的客户端字段。

# API 契约（v0.3）

2026-10-20主窗口行为补齐：全局热键回调仅派发hotkey.toggle，不show/showInactive/restore/focus主窗口；HUD由会话状态独立管理，Renderer backgroundThrottling=false保留。结果/错误事件只更新数据与历史，不作为显示主窗口指令，HTTP/WS形状不变。

2026-10-20学习现场补齐：Correction请求形状和分类协议不变；客户端中文词边界扩展后重算码点offset及左右上下文，服务端照常核验原结果与局部guard。known composing持续观察至commit/60秒硬截止，commit后稳定15秒；unknown不放行。明确term仍自动应用并通知可撤销，suggested仅保留内部事件及旧客户端confirm协议，当前Main不发布建议、Renderer不订阅建议，也不自动confirm；只消费learned提示和撤销，不增加服务端字段。

2026-10-17。遵循 [业务规则](../02-prd/business-rules.md)、[会话协议](session-protocol.md)。以下为实现契约，非已经上线接口。开发直连与生产代理路径一致，不重写：认证/账号 `/api/auth`、`/api/users`、`/api/devices`；业务 `/api/v1`；后台 `/api/admin`；音频 `/ws/v1`。

## 1. 通用约定与 schema

- 除 register/login/refresh 和健康探针外均需 Bearer access。逐请求验证 JWT 签名、iss/aud/exp/jti/sid 及数据库当前登录会话、用户状态；admin 逐次查当前 role。跨账号资源统一 404；不信任请求中的 user_id，管理员统计筛选除外。
- 时间为 UTC ISO 8601（带 Z）；UUID 均规范字符串；时长整数 ms、样本数整数。字符串长度未特别注明时按 Unicode 码点。请求额外字段拒绝 422；客户端忽略响应新增字段。
- 普通 JSON 请求上限 64 KiB、Content-Type application/json；精确POST批删路径独立限512KiB以容纳5000个id/version。CSV文件独立限1MiB（multipart请求2MiB）。超过字节上限返回413。统一错误：`{"error":{"code":"VERSION_CONFLICT","message":"内容已变化，请刷新后重试","request_id":"uuid","retryable":false,"details":{"current_version":3}}}`；message 可本地化，客户端依赖 code。details 只含字段名、版本、期限等，不回显正文/密码/token。
- 需幂等的操作见各接口。header `Idempotency-Key` 为 UUID，作用域 `(user_id,operation,key)`；请求摘要采用服务端独立secret的HMAC-SHA256（管理员创建请求含密码，不能保存可离线快速穷举的普通SHA-256）；相同键不同规范化请求摘要返回 409 `IDEMPOTENCY_CONFLICT`。操作包含资源 ID，防止跨资源撞键。事务保存操作结果身份；重放返回该身份的**当前状态**并标 `replayed=true`，不执行新副作用。所有幂等写响应在对应共享类型外统一附加 `replayed:boolean`（首次false、重放true），GET不需要该字段。错误校验/429 不占键。永久的对象身份和键保留无正文摘要，正文过期后不得从幂等记录恢复正文。
- 单对象 UUID/correction_id/insertion_id 与客户端创建键均按用户归属约束，客户端不能指定其他账号。数据见 [数据模型](data-model.md)。列表使用不可解析的 base64url cursor，绑定用户/筛选/排序；非法或换筛选复用 cursor 为 422 `CURSOR_INVALID`。limit 默认 50、范围 1–100。固定排序有 UUID 平局顺序，返回 `{items,next_cursor}`；无更多为 null，不做无限 offset。

共享类型（下文 `?` 为可空，其他字段必需；请求标“可选”才允许省略）：

| 类型 | 字段 |
|---|---|
| User | `id,email,display_name?,role:user\|admin,status:active\|disabled,locale:zh-CN\|en,created_at,last_login_at?` |
| Settings | `history_retention_days:null\|7\|30\|90,learning_enabled:boolean,locale:zh-CN\|en,version:int≥1` |
| Device | `id,installation_id,name,platform:macos\|windows,client_version,last_seen_at,created_at,is_current:boolean` |
| Result | `result_id,attempt_no:int≥1,attempt_kind:initial\|asr\|refine,raw_text,refined_text,review_required,review_reason?,text_hash,dictionary_version:int≥0,stop_to_result_ms:int≥0,created_at` |
| Session | `session_id,mode:dictation,state,current_attempt_no,learning_enabled:boolean,current_result_id?,audio_complete:boolean,received_samples:int≥0,audio_ms:int≥0,created_at,first_terminal_at?,content_expires_at?,content_expired:boolean,error_code?,result:Result?,retry_kind:asr\|refine\|none,retry_expires_at?,retry_unavailable_reason?,insertion_status:reserved\|inserted\|failed\|unverified\|null` |
| Insertion | `insertion_id,session_id,result_id,status:reserved\|inserted\|failed\|unverified,observable:boolean,error_code?,claimed_at,completed_at?` |
| Term | `id,term,norm,aliases:string[],contexts:Context[],scope:default,source:manual\|auto,version:int≥1,dictionary_version:int≥0,created_at,updated_at,ambiguous_aliases:string[]` |
| Context | `alias_norm,before,after,left,right`（仅本账号；left/right各≤80，最多3份） |
| LearningEvent | `event_id,correction_id,status:pending\|learned\|suggested\|ignored\|failed,reason?,classification:term\|suggest\|ignore\|null,ambiguous:boolean\|null,term:Term?,term_id?,after_version?,undone:boolean,undo_available:boolean,created_at` |

Result.attempt_kind来自所属attempt.kind。stop_to_result_ms在initial时从有效audio.end接受到ready提交；asr/refine时从本attempt.started_at到ready提交，仅展示为“本次重试处理耗时”，不参与首次停止到结果的性能统计。Result 中 raw/refined 各最多 32768 码点（ASR 超限进入 failed `ASR_OUTPUT_LIMIT`，不截断原文）；SHA-256 对 refined_text 原样 UTF-8 计算并以 64 位小写 hex 返回。review_required=false 时 reason 必为 null；true 时 refined_text=raw_text，reason 枚举 `llm_unavailable,llm_timeout,llm_invalid_output,llm_refused,llm_truncated,protected_content_changed,audit_rejected,audit_unavailable,budget_exceeded,server_restarted`。audit_rejected/audit_unavailable仅兼容读取旧结果，2026-10-19取消复审后的新结果不产生这两个reason。非当前 result 不提供 claim；正文过期返回 result=null，元数据仍可读取。

Session.learning_enabled 是创建时已持久化的请求值与账号学习设置交集；GET、历史、创建和重试均返回该冻结值，后续偏好修改不重写它。客户端恢复历史时使用此值，并继续与当前账号学习开关共同限制观察；不得用当前偏好补造原会话授权。

Session.retry_expires_at只表示首次完整ASR失败后600秒的资格截止D，不包含清理收尾。到D即禁止新ASR重试和过期ASR结果；既定鉴权/租户/正文及终态校验后，仍依赖失败音频且其期限已到的会话返回 `retry_kind=none,retry_expires_at=null,retry_unavailable_reason=audio_expired`，新的ASR retry请求为409 `RETRY_UNAVAILABLE`、details.reason=`audio_expired`。D前已完成ASR的文本整理与ready结果仍按原规则；不将其改为audio_expired。同一已成功接纳的幂等键仍按通用约定200重放当前状态，不重新派发。进程内PCM≤D+1秒、工作进程退出≤D+2秒的清理收尾不改变HTTP资格；DB缓存标志未更新不允许继续重试。完整生命周期见[会话协议第5节](session-protocol.md#5-重试释放与重启)。

上述重试能力按当前调用身份展示。仅当全局条件已经允许 ASR、调用方为创建设备的 desktop 时展示 `asr`；同账号其他设备或 admin_web 的能力字段为 `none/null/null`，不虚构额外 unavailable reason。正文过期、终态、缓存到期/驱逐/重启等原有原因优先保留。跨设备 refine 按已确认的同账号 desktop 规则执行；能力展示不改变 POST、WS 或插入权限。

HTTP：400 INVALID_JSON（非法JSON/编码）；401 AUTH_INVALID；403 ACCOUNT_DISABLED/FORBIDDEN；404 NOT_FOUND；409 状态/版本/幂等冲突；410 CONTENT_EXPIRED；413 REQUEST_TOO_LARGE；415 UNSUPPORTED_MEDIA_TYPE；422 VALIDATION_ERROR；429 CAPACITY_EXCEEDED/RATE_LIMITED（含 Retry-After 秒）；500 INTERNAL_ERROR；503 SERVICE_UNAVAILABLE。WS 错误及 close code 单列于会话协议，不能把 429 等 HTTP 状态当作已升级连接的返回值。

## 2. 认证、账号、设备、设置

| 接口 | 请求 | 成功响应 / 规则 |
|---|---|---|
| POST `/api/auth/register` | `{email,password}` | 201 `{user:User}`；普通 user；不自动登录；重复邮箱 409 EMAIL_EXISTS |
| POST `/api/auth/login` | `{email,password,client_type:"desktop"\|"admin_web",device?:{installation_id,name,platform,client_version}}` | 200 TokenPair；desktop 必需 device，admin_web 禁止 device 且必须当前 admin；同账号同安装 ID 重登撤销旧 sid |
| POST `/api/auth/refresh` | `{refresh_token}` | 200 TokenPair；**无需 access**，只凭 refresh 查其哈希/登录会话；不轮换、不滑动延期；失效 401，禁用 403 |
| POST `/api/auth/logout` | `{}` | 204；撤销当前 JWT sid，对其他设备不影响；相同已撤销 sid 重放亦 204（仍需可验证签名且未过期的该 sid access） |
| GET `/api/users/me` | 无 | 200 `{user:User,settings:Settings}` |
| PATCH `/api/users/me` | `{display_name:string\|null}` | 200 `{user:User}`；display_name trim 后 1–64 或 null；locale只通过带version的settings PATCH修改 |
| GET `/api/devices` | 无 | 200 `{items:Device[]}`；按 last_seen_at DESC,id DESC；只列本账号 desktop 安装记录，无 Web 伪设备 |
| GET `/api/v1/settings` | 无 | 200 Settings |
| PATCH `/api/v1/settings` | `{version,history_retention_days?,learning_enabled?,locale?}` | 200 Settings；version CAS，冲突 409 VERSION_CONFLICT；设置缩短保留即时限制读取、后台小时清理；学习关闭立即拒绝新 correction/confirm |

TokenPair：`{access_token,refresh_token,token_type:"Bearer",expires_in:900,refresh_expires_at,user:User}`。密码 8–128 码点且不 trim；邮箱 trim+lower，最长254。device.name 1–64，client_version 1–32，installation_id UUID。refresh 使用至少32随机字节的 opaque token，URL安全编码；服务端只存 SHA-256 摘要；Argon2id 密码参数见部署。登录错误统一 401 AUTH_INVALID；不能从“邮箱不存在”与“密码错误”区分。admin_web role 变更后下一次请求生效。无 DELETE device/user、无 Web 密码重置接口。

## 3. 能力、会话与结果

`GET /api/v1/capabilities` → 200：

```json
{
  "api_version":"v1","ready":true,"degraded":false,"unavailable_reason":null,
  "modes":["dictation"],
  "audio":{"encoding":"pcm_s16le","sample_rate_hz":16000,"channels":1,"chunk_ms":250,"max_duration_seconds":180,"unacked_limit_ms":2000},
  "asr":{"stable_partial":true,"provider":"configured-adapter","model_id":"configured-id"},
  "llm":{"available":true,"provider":"configured-adapter","model_id":"configured-id"},
  "limits":{"active_sessions_per_user":1,"global_sessions":20,"queued_sessions":0,"capacity_version":1},
  "learning":{"enabled":true,"observer_sample_ms":300,"observer_idle_seconds":15,"observer_max_seconds":60},
  "statistics_timezone":"Asia/Shanghai"
}
```

limits.global_sessions为DB当前运行值，capacity_version随保存变化，客户端能力快照不能预留槽，创建时重验；示例20不写死在客户端。示例模型名仅占位；不返回地址/路径/secret。ASR 未准备好时 ready=false；LLM 暂不可用时 ready=true/degraded=true，允许口述并返回待确认原文。enabled 为账号设置与全局能力交集。

| 接口 | 请求 | 成功响应 / 规则 |
|---|---|---|
| POST `/api/v1/sessions` | `{client_request_id:UUID,mode:"dictation",learning_enabled:boolean}`；Idempotency-Key 必须等于 client_request_id | 201 Session；重复200原Session；只允许 desktop，设备来自 sid。learning_enabled=true 不能越过账号关闭设置。占槽失败429；已有活动会话409；服务未就绪503 |
| WS `/ws/v1/sessions/{id}/audio` | Authorization 头、二进制PCM与控制帧 | [会话协议](session-protocol.md)，一会话一连接 |
| GET `/api/v1/sessions/{id}` | 无 | 200 Session；查询当下能力，不保证缓存一直存在 |
| POST `/api/v1/sessions/{id}/cancel` | `{}` | 200 Session；重复 cancelled 返回同状态；ready/empty/failed 返回409 SESSION_TERMINAL；不删除历史 |
| POST `/api/v1/sessions/{id}/retry` | `{retry_kind:"asr"\|"refine"}`；Idempotency-Key必需 | 仅desktop；refine允许同账号其他桌面设备，asr仍限创建设备（不匹配403）；202 Session（新 current_attempt_no）；重放200现状且不更换执行sid；不允许409 RETRY_UNAVAILABLE（details 含 reason）；正文过期410；没容量429 |

session 查询和历史最多返回当前有效结果；attempt 明细只用于服务内部恢复/统计，不新增用户“版本浏览”功能。error_code 为本次失败原因，ready 时 null。result字段仅state=ready且正文未过期时返回，其他状态为null。ready/refine回退已满足 raw 可读取条件，不因 LLM 暂不可用把整个会话标 failed。

## 4. 插入资格、回执、学习

claim、receipt和corrections只允许desktop登录会话；device_id只从当前sid取得，不接受客户端传入。claim可以由同账号另一桌面在用户显式重新指定目标后申请；receipt/correction必须匹配该claim.device_id，同账号不同设备为403 FORBIDDEN，跨账号仍404 NOT_FOUND。校验设备发生在幂等重放之前，不能用已知insertion_id绕过。

| 接口 | 请求 | 成功响应 / 规则 |
|---|---|---|
| POST `/api/v1/sessions/{id}/insertions/claim` | `{result_id,insertion_id:UUID,text_hash,review_confirmed:boolean}` | 首次201 Insertion；同 insertion_id 同参数200/replayed=true；旧结果409 STALE_RESULT、hash不符409 RESULT_HASH_MISMATCH、待确认但false为409 REVIEW_REQUIRED、已有资格409 INSERTION_ALREADY_CLAIMED |
| GET `/api/v1/sessions/{id}/insertions/{insertion_id}` | 无 | 200 Insertion；仅查询，不授予再次执行许可 |
| POST `/api/v1/sessions/{id}/insertions/{insertion_id}/receipt` | `{status:"inserted"\|"failed"\|"unverified",observable:boolean,error_code?:string\|null}` | 200 Insertion；相同回执幂等；未claim的insertion_id为404 NOT_FOUND；终态改值409 INSERTION_STATE_CONFLICT。error_code 枚举 TARGET_CHANGED/TARGET_UNSUPPORTED/PERMISSION_DENIED/ZERO_WRITE/VERIFY_FAILED/CLIENT_INTERRUPTED，成功必null |
| POST `/api/v1/corrections` | 见下 | 首次202 LearningEvent(status=pending)；同 correction_id同内容200当前状态；额外确认不得修改首次请求 |
| GET `/api/v1/learning-events/{id}` | 无 | 200 LearningEvent；客户端1秒轮询至非pending，最多30秒，网络中断后可继续查同ID |
| POST `/api/v1/learning-events/{id}/confirm` | `{}`；Idempotency-Key必需 | 200 LearningEvent learned；仅 suggested；冲突409 LEARNING_CONFLICT；重复原键不重复应用 |
| POST `/api/v1/learning-events/{id}/undo` | `{}`；Idempotency-Key必需 | 200 `{event_id,undone:true,term:Term\|null,dictionary_version}`；当前term版本不等于after_version时409 VERSION_CONFLICT；已清理快照410 CONTENT_EXPIRED |

correction 请求：

```json
{"session_id":"uuid","insertion_id":"uuid","correction_id":"uuid","offset":12,"before":"cloud","after":"claw","left":"","right":""}
```

offset 按 refined_text Unicode 码点，整数≥0；校验 `[offset:offset+len(before)]==before`，左右上下文必须为本次插入文本中紧邻片段，分别≤80。before/after 各1–32、无换行；后端不能采集其他输入框原文。必须该 insertion 已 inserted/observable=true、会话记录及当前账号均启用学习、正文未过期。数字/路径/否定/非单一词语替换返回 ignored，不调用分类；其他条件不满足409 LEARNING_NOT_ALLOWED。suggested→confirm 重新校验词条版本/别名冲突，明确用户确认可接受建议，但不能静默覆盖现存词条。分类为term、无歧义、原片段证据匹配、局部guard通过且无别名冲突时自动learned；其他可能词语修正suggested、非词语ignored，不使用数值置信门槛。offset或上下文不匹配返回422 VALIDATION_ERROR。

异步分类完成后必须在应用事务内重新检查pending、当前账号active且learning仍启用、会话正文未过期/未清理、目标词版本/别名冲突及容量；不以发起时检查代替提交时检查。不满足时不应用词条，event=failed，reason为ACCOUNT_DISABLED/LEARNING_DISABLED/CONTENT_EXPIRED/VERSION_CONFLICT/ALIAS_CONFLICT/DICTIONARY_LIMIT；已非pending则仅丢弃迟到输出，不改已有终态。

学习处理抢不到LLM槽时status=failed/reason=MODEL_BUSY，不自动应用词条。学习处理任务总预算10秒，最多一次传输/结构重试且预算不延长；失败 event.status=failed、reason明确，不能影响已插入文字。重启时 pending 标 failed/server_restarted，不后台悄悄重新学习；用户可手动维护词库。

## 5. 词库与 CSV

| 接口 | 请求 | 成功响应 / 规则 |
|---|---|---|
| GET `/api/v1/dictionary/terms` | `q?`≤64、`scope=default`、`source=all\|manual\|auto`（默认all，绑定cursor）、cursor/limit | 200 `{items:Term[],next_cursor,dictionary_version}`；term/alias归一化子串搜索，norm ASC,id ASC；cursor绑定dictionary_version，期间变更返回409 DICTIONARY_CHANGED，客户端刷新首屏 |
| POST `/api/v1/dictionary/terms` | `{term,aliases:string[],scope:"default"}`；Idempotency-Key必需 | 201 Term；重复规范词409 TERM_EXISTS；上限409 DICTIONARY_LIMIT |
| PATCH `/api/v1/dictionary/terms/{id}` | `{version,term?,aliases?}`；Idempotency-Key必需 | 200 Term；整组替换aliases；没有实际变化不增加版本；冲突409 VERSION_CONFLICT；手动有变化source=manual |
| DELETE `/api/v1/dictionary/terms/{id}?version=N` | Idempotency-Key必需 | 200 `{id,deleted:true,version,dictionary_version,replayed}`；软删除并递增版本；重放不再删除新版本 |
| POST `/api/v1/dictionary/import` | multipart/form-data，file为CSV；Idempotency-Key必需 | 200 ImportResult；同键文件摘要不同409；超过文件/行数413；编码/列头/整体CSV语法不合法422，整体不写入 |

term trim 后1–64，aliases≤10，每个trim后1–64；term/alias禁止换行及控制字符，允许词间普通空格；norm=NFKC+casefold，归一化去重，不能等于term。总有效词条≤5000。多个正确词共享别名允许，ambiguous_aliases派生展示，自动学习不得制造/扩大冲突。删除词条为 tombstone；后续同norm重新建立必须新UUID，旧版本/旧学习事件不能命中新词条。修改删除别名同时删除其context。

CSV UTF-8（可带 BOM）、标准引号、必需列term、可选aliases（分号分隔）、拒绝其他/重复列名；最多1000数据行，行号按记录序号（表头1，允许引号内换行但字段业务规则仍校验）。先验证完整文件结构，再按行顺序独立事务；合法行成功不因其他行失败回滚，已存在完全相同term/alias集合为skipped，不同内容为conflict，不覆盖。每个实际词条变更分别增加dictionary_version。

ImportResult：`{import_id,created:int,skipped:int,conflicts:int,invalid:int,dictionary_version,rows:[{row:int,status:"created"|"skipped"|"conflict"|"invalid",term_id?:UUID,error_code?:string}],replayed:boolean}`。rows包含每条数据记录，无原始CSV正文。文件完成后立即释放；异常重启可用同键原文件续跑尚未提交行，已提交行通过 `(user_id,import_id,row)` 去重，不能重复累计。

## 6. 历史与统计

`GET /api/v1/history?cursor=&limit=&type=all|dictation` → `{items:Session[],next_cursor}`。按 created_at DESC,id DESC，首次游标固定 as_of 时间，避免分页插入打乱；新会话刷新首屏才显示。不显示 content_expired=true 记录；未产生正文的 failed/empty/cancelled 在有效保留期仍显示状态/重试。仅当前有效正文可复制，无导出/删除历史接口。

`GET /api/v1/stats/me?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD`：均必需、Asia/Shanghai自然日含首尾、最多366天。200：

```json
{
  "timezone":"Asia/Shanghai","date_from":"2026-10-01","date_to":"2026-10-17",
  "totals":{"sessions":0,"received_audio_ms":0,"result_sessions":0,"failed_sessions":0,"empty_sessions":0,"cancelled_sessions":0,"chars":0,"successful_audio_ms":0,"average_chars_per_minute":0.0,"fallback_sessions":0,"inserted_sessions":0,"processing_success_rate":0.0,"insertion_success_rate":0.0,"fallback_rate":0.0,"learning_events":0},
  "active_days_in_range":0,"current_streak_days":0,"longest_streak_days":0,
  "dictionary_terms":0,"cumulative_learning_events":0,
  "daily":[{"date":"2026-10-01","sessions":0,"chars":0,"successful_audio_ms":0,"heat_level":0}]
}
```

daily补齐区间每一天；rate为0–1小数，零分母返回0。totals/active_days为区间；current_streak以今天/昨天向前看、longest_streak为账号全部历史，不受筛选区间截断；其余当前词条/累计学习也是全时值。字数按Unicode L/N，详见BR-10；保留清理不改变统计。响应不得把未知token当0。

## 7. 管理后台

全部要求当前 active admin；不提供任何用户的正文、词条语境、密码哈希或 token。date区间遵守第6节。

| 接口 | 请求 | 成功响应 |
|---|---|---|
| GET `/api/admin/users` | cursor/limit；q≤254匹配邮箱/显示名；status? active\|disabled | 200 `{items:[{user:User,totals:{sessions,received_audio_ms,result_sessions,last_session_at?}}],next_cursor}`，created_at DESC,id DESC |
| POST `/api/admin/users` | `{email,password,display_name?:string}`；Idempotency-Key必需 | 201 `{user:User,replayed:false}`，只创建user；与注册同校验 |
| PATCH `/api/admin/users/{id}` | `{status:"active"\|"disabled"}` | 200 `{user:User}`；同值幂等；禁用事务撤销全部sid；禁用自己/最后active管理员409 ADMIN_PROTECTED |
| GET `/api/admin/stats/overview` | date_from/date_to | 200 `{timezone,date_from,date_to,registered_users,active_users,disabled_users,distinct_active_users,totals:StatTotals,daily:[{date,daily_active_users,sessions,received_audio_ms,result_sessions,failed_sessions,empty_sessions,cancelled_sessions}]}` |
| GET `/api/admin/stats/usage` | date_from/date_to，user_id?，cursor/limit | 200 `{timezone,items:[{date,user_id,sessions,received_audio_ms,result_sessions,chars,successful_audio_ms,fallback_sessions,inserted_sessions,learning_events}],next_cursor}`，date DESC,user_id ASC |
| GET `/api/admin/stats/models` | date_from/date_to，user_id? | 200 `{timezone,items:[ModelUsage]}`，按date/provider/model_id/purpose排序，最多366天×实际配置维度 |

StatTotals 为stats/me.totals相同统计字段；registered/active/disabled_users 为查询时账号状态计数，不伪装为历史状态。ModelUsage：`{date,provider,model_id,purpose:"asr_decode"|"asr_finish"|"refine"|"audit"|"classify",calls,successes,failures,unknown_calls,elapsed_ms,processed_audio_ms,prompt_tokens_known,completion_tokens_known,prompt_tokens_unknown_calls,completion_tokens_unknown_calls}`，known 是已知部分的和；任一unknown>0时UI标“不完整/未知部分”。audit仅保留历史实际调用，当前流水线不再派发。实际调用含重试，asr增量重复处理时长不能当录音时长。

### 7.1 容量管理（v0.3新增）

| 接口 | 请求 | 响应与失败 |
|---|---|---|
| GET `/api/admin/capacity` | 无；active admin | 200 CapacityView；DB不可用503，运行指标不可取时runtime=null，配置不伪造 |
| PATCH `/api/admin/capacity` | `{version:int>=1,max_active_sessions:int>=1}`；拒绝bool/小数/未知字段 | 200 CapacityView，按version CAS；冲突409 VERSION_CONFLICT（details.current_version）；超有效ceiling/非法值422 VALIDATION_ERROR（details.max_allowed）；401/403同其他后台接口 |

CapacityView固定字段：

```typescript
type CapacityView = {
  config: { version: number; max_active_sessions: number; updated_at: string; updated_by: string | null };
  deployment: { admission_ceiling: number; effective_ceiling: number; active_pcm_budget_bytes: number;
    retry_pcm_budget_bytes: number; asr_execution_slots: number; llm_max_inflight: number };
  runtime: null | {
    sampled_at: string; active_sessions: number; available_sessions: number; draining: boolean;
    asr_running: number; asr_partial_waiting: number; asr_finish_waiting: number;
    llm_running: number; llm_foreground_waiting: number; llm_classify_waiting: number;
    active_pcm_bytes: number; reserved_pcm_bytes: number; retry_pcm_bytes: number;
    window_seconds: 300; sample_count: number; rejected_count: number; failed_count: number; fallback_count: number;
    stop_to_result_p50_ms: number | null; stop_to_result_p95_ms: number | null;
  };
  validation: { status: "unverified" | "valid" | "stale"; report: CapacityReport | null };
};
type CapacityReport = {
  report_id: string; tested_at: string; target_sessions: number; environment_fingerprint: string;
  app_commit: string; config_sha256: string; fixture_manifest_sha256: string;
  hardware_model_manifest_sha256: string; evidence_sha256: string;
  normal_completed: number; normal_total: number; rejected: number; failed: number; fallback: number;
  stop_to_result_p50_ms: number; stop_to_result_p95_ms: number;
  desktop_stop_to_readback_p50_ms: number; desktop_stop_to_readback_p95_ms: number;
  soak_passed: boolean;
};
```

所有count/bytes/ms为非负安全整数（p50/p95无样本为null），版本≥1；max_active_sessions/version受int32范围约束。报告target_sessions≥1、normal_total=80×target_sessions（四个负载/时长组各20轮）；normal_completed必须等于normal_total；时间ISO8601 UTC，sha256小写64hex，report_id≤64字符。部署报告由真实压测产物生成、运维部署只读；environment_fingerprint覆盖硬件/OS/驱动/模型/adapter/配额/资源预算/应用及负载版本，排除单纯运行会话上限。report仅在正常批次全数完成且零拒绝/失败/回退、两组时延均达标且soak通过时可标valid；不能仅检查JSON格式。证据原件路径/凭据不返回后台，只有摘要；资源指纹不符为stale，缺失/非法为unverified及report=null。超过report.target_sessions时UI提示超出验证范围；不将报告当部署硬上限。

reserved_pcm_bytes为当前活动会话的当前attempt.kind∈{initial,asr}数量×5760000；初始录音ASR成功后逻辑预留仍保持至终态，refine重试不预留PCM。聚合必须限定user_id/session_id/current_attempt_no，不能重复计历史attempt；缺少当前attempt元数据时runtime=null明确未知，不把所有活动会话乘音频预留。实际active/retry_pcm_bytes仍按受控内存所有者采样，与逻辑额度分开。

available_sessions=max(0,min(config.max_active_sessions,effective_ceiling)-active_sessions)，readiness或PCM资源不可用时0；draining=active_sessions>config.max_active_sessions。负载窗口按服务器当前时间前300秒：sample_count为initial attempt终态且audio_complete的非取消样本；failed为其中失败数，fallback为其中ready/review_required数；失败时延按+∞参与nearest-rank，该分位落在失败项时API用null并结合failed_count显示“含失败，无法给出有效分位”，不能当无样本。rejected_count为同期CAPACITY_EXCEEDED创建/重试数。只聚合内部ID和耗时，不返回任何正文。观察指标不是业务usage_daily口径。

先校验version再处理同值；同version同值200不增版本，不写变更记录；实际修改version+1并与审计同事务。PATCH响应读取已提交配置，runtime是独立采样并标时间，不保证与配置同一瞬间。丢响应先GET确认，旧version重发可409，禁止盲目覆盖。无Idempotency-Key要求，CAS已防重复写；调低确认属于UI操作，服务端不允许超上限或跳过权限/CAS。

## 8. 兼容与实现要求

破坏性业务变更升v2；未带v1的账号/admin接口如需破坏性修改同样走新版本，不静默变更本契约。OpenAPI由实现schema生成，并以本文件字段/枚举/错误码对照；WS schema独立生成同一共享类型。所有成功写入须以返回对象、数据库约束与二次读取确认实际结果；不能以HTTP 2xx代替目标插入读回验证。


## 9. 内部 schema 映射说明（S4复核）

词库纠正的 `term_correction` 证据见 [Provider 第7节](provider-config.md#7-逐位置词库纠正s4内部契约复核)。2026-10-19 用户允许唯一有效别名在同句多处纠正，不再强制专名/普通词义消歧。这不新增 HTTP/WS 接口、客户端插入方式或 Result 字段：成功仍返回既有 raw_text/refined_text/text_hash；证据错误、别名归属歧义或其他程序校验失败仍走现有 review_required 原文回退。

内部词条 ID/alias/锚点和模型 prompt 不额外返回客户端。原始 raw_text、码点定义、选定 refined_text 的原样 UTF-8 hash、insertion claim/receipt 和保留清理规则保持原契约。旧历史冻结提示不足以重建位置证据时保守回退，不查询当前词库冒充历史快照。内部映射已复核，真实模型及音频验收仍须执行。

## 2026-10-20 词典原子批量删除（FR-05-06）

`POST /api/v1/dictionary/terms/batch-delete`，鉴权同词典CRUD，必须UUID `Idempotency-Key`。请求`{"items":[{"id":"UUID","version":1}]}`，1–5000项，id规范化后不得重复，version必须正整数。服务端按id排序规范化payload；不同顺序的同一批同key视为重放。

沿现有User→DictionaryState锁顺序、同事务HMAC OperationRequest，操作名dictionary.terms.batch-delete、target_type=dictionary_batch、target_id=key；无迁移/额外模型调用。先查该用户全部目标并校验活跃/version，任一缺失或跨租户返回404 NOT_FOUND、任一版本不符409 VERSION_CONFLICT，事务回滚含幂等预留。全部校验后统一soft-delete，每词version+1、词库version+1。已有会话快照、历史、学习事件不改。

成功200 `{"deleted_ids":["UUID"],"dictionary_version":4,"replayed":false}`；同payload/key重放只返回原批目标及当前词库版本，不重复写；换payload同key409 IDEMPOTENCY_CONFLICT。原批词条不可被replay复活或删到同名新词。

Desktop typed RPC `dictionary.removeMany(items,requestKey)`→Main ApiClient→POST；renderer不持token。Store按账号generation/规范payload缓存同批UUID，网络错误或读回失败保留key/选择，重试重放；读取成功确认后清选择。切账号使旧回调失效，空/重复并发提交拒绝。UI全选仅已加载列表；加载更多不自动选中。

批删POST请求体上限单独为512KiB（含分块累计边界），仅精确该路径；其他JSON接口保持64KiB，CSV文件仍1MiB/multipart请求2MiB。超字节上限413；体积内超过5000项/重复项/字段非法422。首轮5001项被旧64KiB拦为413，已据此修正入口预算；原5000项判据不降。

## V2 增量（2026-10-28，S4 实现后同步）

以 [V2 设计](v2-provider-design-20261027.md) §8 为准，此处只登记已实现的契约变化：

| 变化 | 内容 |
|---|---|
| `POST /api/v1/sessions`、`GET /api/v1/sessions/{id}` | 响应新增 `max_record_seconds`（本会话创建时固定；V2 之前的会话为 `null`，客户端回退 180） |
| WS `ready` | `audio.max_samples` 为本会话上限（云端 5,760,000，本地 2,880,000） |
| `GET /api/v1/capabilities` | `asr.max_record_seconds`（新会话当前会得到的上限）、`asr.supports_hints` |
| 创建会话 429 | `CAPACITY_EXCEEDED` 新增 `details.reason=provider_limit`（云端 provider 并发满），`Retry-After: 1` |
| 创建/重试 503 | 无可用 ASR 时 `SERVICE_UNAVAILABLE` 带 `details.reason=asr_unavailable` |
| 重试 409 | `RETRY_UNAVAILABLE`、`details.reason=provider_changed`；会话 `retry_unavailable_reason=provider_changed` |
| `/api/admin/providers*` | 8 个接口：`GET/POST /providers`、`PATCH/DELETE /providers/{id}`、`POST /providers/{id}/test`、`GET/PUT /providers/active`、`GET /providers/audit`；错误码 `PROVIDER_NAME_EXISTS`、`PROVIDER_ACTIVE`、`PROVIDER_DISABLED`、`PROVIDER_TEST_FAILED`，`VALIDATION_ERROR` 的 `details.reason ∈ {egress_denied, host_unsupported, cloud_confirmation_required}`；Key 只写不读（`secret_set`、`secret_last4`）；未配置主密钥时保存/读取 Key 返回 503 `SERVICE_UNAVAILABLE`、`details.reason=secret_unavailable` |
| `GET /healthz` | 新增 `providers.{asr,llm}.{source,reason}`（不含地址、Key） |
