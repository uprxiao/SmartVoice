2026-10-23当前Mac目标为Qwen3-ASR 1.7B原生流式＋官方deepseek-flash/DS-R2；运行库/模型清单与独立本地分类按[R2正式设计](r2-production-design-20261023.md)配置。禁止未验证即覆盖运行配置或自动下载；旧部署示例作为历史兼容。

# 部署与运行设计（v0.3）

2026-10-17。实现前置设计，不是已执行的上线记录。V1选择单实例服务、PostgreSQL、自有内网ASR；不引入Redis/消息队列/分布式会话。具体硬件、权重与吞吐阈值仍按P1实测，音频/平台验收按P2记录。

## 1. 进程、地址与网络

开发：FastAPI绑定 `127.0.0.1:17863`，Desktop集中配置同址；Admin开发源由显式allowlist加入。生产：Nginx TLS终止，Desktop构建时配置HTTPS/WSS同源地址，Admin发布于 `/admin/`，API保留完整路径转发。

2026-10-28 本人确定本轮分发入口为同局域网/VPN的 `https://192.168.14.151`。桌面安装包由 `release.config.json` 注入地址和公开内网 CA；后台按浏览器 origin 调用 API。已新增 IP 默认 TLS 站点，原 `centos91` 站点保留；配置见 `deploy/linux/nginx-smartvoiceinput-lan-ip.conf`，证书与验证记录见[固定 IP 入口](distribution-service-address-20261028.md)。

| 外部路径 | 上游路径（不重写） |
|---|---|
| `/api/auth/*`、`/api/users/*`、`/api/devices` | 完整原路径→FastAPI |
| `/api/v1/*`、`/api/admin/*` | 完整原路径→FastAPI |
| `/ws/v1/*` | 完整原路径→FastAPI，升级WebSocket |
| `/health/live`、`/health/ready` | 完整原路径→FastAPI；生产建议仅内部探针访问 |
| `/admin/*` | Admin静态文件，静态路由回退到admin/index.html，不回退API/WS |

Nginx不截除/api或/ws前缀；关闭WS缓冲，保留Upgrade/Connection，proxy_read_timeout=60s；服务端每20秒WS ping避免代理空闲断开，ping不延长上传无数据10秒期限。上游只对回环/容器专网开放；只信任明确Nginx地址的Forwarded头，不能信任任意客户端伪造IP。TLS证书有效、HTTP重定向HTTPS，远程明文拒绝。

**固定一个Uvicorn worker、一个应用replica。**配置workers≠1启动拒绝；发布入口不允许外部覆盖为多worker，启动还取得PostgreSQL会话级advisory lock，同库第二实例拒绝readiness并退出。单独ASR解码子进程只承载模型，不接HTTP，不等于多个应用worker。初始20个活动会话槽，接纳不排队，超额429；模型执行池独立扩展。提高后台上限前先配置并校验部署ceiling/音频预算/模型资源；多API副本的演进门禁见[并发设计](concurrency-target.md)，不靠增加workers绕过。

当前标准入口为 `uv run --frozen python -m app.main`。入口采用生产 `ManagedServer`：在关闭监听与WS之前同步发布停止状态并等待既有单owner业务收尾，真实HTTP探针/新建/重试返回503，完整end保留原剩余预算；随后原lifespan统一释放watcher/provider/实例锁/DB。真实SIGTERM的ASR/refine两臂、create提交前后与同库重启已通过，证据见专项（内部记录，未公开）。开发用 `uvicorn app.main:app --reload` 直接入口用于热重载，部署与停机验收使用标准入口。锁定 Uvicorn 0.54 下显式选用 `websockets` 后端，保持升级前 HTTP 401/403/404/409/503 的 JSON 拒绝响应；`auto` 选到的 SansIO 后端在此路径会产生错误堆栈，不能只靠 TestClient 判断真实网络通过。已实跑 12 组 TCP 拒绝响应；readiness 的 ASGI 组另外记录。入口关闭代理头信任，反向代理部署需按明确可信地址配置，不使用任意来源的 Forwarded 头。

## 2. 配置 schema 与加载顺序

预定文件 `config/app.yaml`（非密钥，实际开发时建立）；环境变量前缀 `SVI_`。顺序：类型默认值→显式配置文件→已声明的环境变量；未知配置/变量映射报错，无自动猜测。命令行仅指定配置路径和预定运维操作，不接受明文password/secret参数。

| 配置 | 类型 / 默认 / 约束 |
|---|---|
| app.environment | development/production，必填 |
| server.host / port / workers | dev127.0.0.1 / 17863 / 1；生产内部绑定，workers只能1 |
| server.instance_lock_key | 固定应用级int64，所有同库实例一致 |
| database.url_env | 默认SVI_DATABASE_URL；postgresql URL从环境读取，不记日志 |
| database.pool_size / max_overflow / pool_timeout_seconds | 5 / 5 / 2；连接时区UTC；短事务，超时503 SERVICE_UNAVAILABLE，不在录音/推理中占连接 |
| auth.jwt_secret_env | SVI_JWT_SECRET，至少32随机字节，HS256；算法白名单不信任token alg |
| security.idempotency_secret_env | SVI_IDEMPOTENCY_SECRET，独立至少32随机字节；request_hash用HMAC-SHA256，稳定保存以支持幂等重放，轮换需维护迁移策略 |
| auth.issuer / audience | 明确非空部署标识 / smartvoiceinput；变更会使旧access失效 |
| auth.access_ttl_seconds / refresh_ttl_seconds | 900 / 2592000（30天），V1固定 |
| auth.argon2_memory_kib / time_cost / parallelism | 65536 / 3 / 1（Argon2id）；保存完整编码哈希含独立随机盐；上线硬件压测验证资源预算 |
| auth.max_parallel_hashes | 2，满额立即429 RATE_LIMITED，不阻塞事件循环；每次校验使用完整密码，不trim/截断 |
| audio.max_duration_seconds / chunk_ms | 180 / 250，单声道16kHz PCM s16le |
| sessions.initial_max_active / admission_ceiling | 20 / 20；正整数，运行值仅首次初始化；DB容量记录持久化后台修改；启动不覆盖 |
| sessions.per_user / queue_size | 1 / 0；接纳不排队，模型等待另配 |
| audio.active_pcm_budget_bytes | 134217728；有效ceiling=min(admission_ceiling,floor(budget/5760000)) |
| sessions.connect_timeout_ms / audio_idle_timeout_ms | 10000 / 10000 |
| sessions.unacked_limit_ms / auth_poll_ms | 2000 / 5000（撤销最长生效时间） |
| retry.audio_ttl_seconds / max_cached_sessions / audio_budget_bytes | 600 / 20 / 134217728；600为资格绝对截止，独立失败池数量/字节双上限，不持久化音频；本地PCM≤截止后1秒、worker退出确认≤截止后2秒，不是可配置宽限 |
| capacity.validation_report_path | 可空；受限部署报告JSON路径，只含摘要/性能数据，schema见API；无报告显示未验证，不影响启动 |
| cleanup.history_interval_seconds / audio_interval_seconds | 3600 / 1；读取/重试/派发/提交入口即时判断到期，1秒音频扫描只作回收兜底，不改变600秒资格截止 |
| statistics.timezone | Asia/Shanghai，V1固定 |
| logging.retention_days | 14，结构化脱敏日志 |
| security.cors_origins / ws_origins | 精确scheme+host+port列表；生产默认仅Admin同源，Desktop无Origin走Bearer规则；无通配符 |
| security.trusted_proxy_cidrs | 仅实际代理网段；开发为空 |
| security.asr_allowed_hosts / egress_policy | ASR获准内网主机/IP白名单及网络出口限制；不能只校验URL协议 |
| temporary.root | 专用权限0700目录；默认/dev/shm可用时使用，否则启动创建的本应用临时目录 |
| asr / llm / pipeline | 见[provider配置](provider-config.md)，不接受未实现adapter名 |

生产密钥由部署secret文件/环境注入，配置库只保存变量名；开发使用被.gitignore排除的本地环境文件，不提交示例真实值。env、异常trace、数据库URL不得全文写日志。JWT轮换通过维护发布完成，立即撤销现有登录会话并要求重新登录；V1不建立在线密钥管理页面。

## 3. 认证、限流与CORS

- 注册邮箱trim+lower；密码Argon2id。refresh为至少32随机字节opaque token，只存SHA-256摘要；固定30天、不轮换。JWT带sub/sid/iss/aud/exp/iat/jti，每请求查询当前用户/sid；角色不凭JWT旧claim授权。
- Desktop Main持token，safeStorage不可用则仅内存，Renderer不读取；Admin access/refresh均内存，页面刷新重登，不设身份cookie，不写localStorage。
- CORS精确allowlist，允许Authorization/Content-Type/Idempotency-Key，allow_credentials=false。未允许Origin的浏览器请求不能读取响应；服务端仍必须鉴权，CORS不代替鉴权。WS单独检查Origin，不能假定HTTP中间件会拦WS。
- 单进程内存固定窗限流（窗口从UTC整分/整小时起；重启计数归零，只是资源保护，不是持久风控）：login每IP每分钟10次且每归一化邮箱摘要每10分钟20次；register每IP每小时5次；refresh每sid每分钟60次；其余已认证HTTP每user每分钟120次。超额429 RATE_LIMITED，Retry-After为窗口剩余秒（向上取整，至少1）；不返回邮箱是否存在。后台适用同一user限制。WS帧使用音频大小/时长/ACK背压，不按HTTP每帧计数。
- request body普通JSON≤64KiB；精确POST词典batch-delete路径≤512KiB/5000项。CSV文件≤1MiB/1000记录；代理整体multipart上限2MiB（仅封装余量），应用仍严格文件1MiB。WS二进制≤8008B、控制≤4096B。详情见API；不得拿统一64KiB误替批删/WS/CSV上限。

## 4. 初始化与迁移顺序

以下运维入口已经实现；真实迁移与备份恢复演练的范围见独立验收记录（内部记录，未公开），不据此声称生产备份轮换已经部署：

1. 创建专用PostgreSQL数据库和运行账号；迁移账号可DDL，日常运行账号仅所需DML，不用超级用户运行应用。备妥模型文件/manifest与受限网络出口。
2. 加载/验证配置但不启动接单；运行单独Alembic迁移步骤（目标head）。事务/迁移互斥锁防重复执行，检查已有DDL类型、索引、FK而非只看表存在；初始化DML使用WHERE NOT EXISTS。
3. 核对实际schema revision、组合租户FK、状态/版本CHECK与部分唯一索引；失败不启动旧/新应用混跑。应用只校验schema，不在每个进程启动时自动迁移。
4. 运维CLI `admin init`：邮箱参数+隐藏交互密码或受限stdin；参数列表不带密码，创建首admin幂等，已有同邮箱不得静默改密码/提权；需要变更时显式运维操作并记录无正文审计元数据。

从 `server/` 执行 `uv run --frozen python -m app.cli admin init --email admin@example.com`，密码使用隐藏交互输入；自动化只接受受限 stdin 的 `--password-stdin`，不得把密码拼进命令行。重置使用显式 `account reset-password --email ...`，并撤销该账号所有 sid。旧 `create-admin` 仅为幂等初始化的兼容名称。

恢复库须在未开放业务流量时执行 `uv run --frozen python -m app.cli restore-sanitize --confirm-database <恢复库名>`。CLI 校验实际库名/schema head，取得与服务相同的实例锁；清空全部恢复正文及学习快照、撤销所有 sid，保留独立词库和统计。命令成功后还须查询核对实际清理结果，再启动服务；不得对在线生产库误执行。
5. 用sessions.initial_max_active初始化单例runtime_capacity（WHERE NOT EXISTS），核对DB已保存值不超过有效部署上限；超出时readiness失败但鉴权/容量管理可用以便纠正。启动单实例，取得advisory lock、执行会话重启恢复和过期清理、prepare ASR/LLM；readiness通过才接入代理。用虚构测试账号完成登录/创建/取消/跨用户拒绝与统计读取冒烟，核对实际记录。

运维密码恢复CLI要求本机/部署管理权限，明确目标邮箱并隐藏输入新密码，更新hash并撤销该用户全部sid；不增加Web找回/重置功能。无用户DELETE或设备远程踢出页面。

## 5. 探针、停机与故障恢复

| 路径 | 响应 / 判定 |
|---|---|
| GET `/health/live` | 200 `{status:"alive"}`；ASGI事件循环可响应即可，不返回配置/模型路径 |
| GET `/health/ready` | 200 `{status:"ready"\|"degraded",dependencies:{database:"ok",asr:"ready",llm:"ready"\|"unavailable"}}`；数据库/schema/实例锁/ASR不可用时503 `{status:"not_ready",reason_code}` |

容量占满仍healthy，创建返回429；LLM运行时不可用为degraded可接单，返回原文待确认；ASR模型加载失败/不支持stable partial为not_ready。配置格式错误、secret缺失属于部署错误，不以degraded掩盖。

正常停机：先readiness503并拒新建/重试；已上传完整end的任务最多按原剩余30秒预算结束；仍streaming的任务以SERVER_SHUTDOWN终止不拼接；关闭WS/模型调用，清内存缓存与本次临时文件，释放锁/数据库连接。服务管理器graceful timeout至少35秒，超时强制终止后由下次启动恢复。

崩溃恢复：created/streaming/finalizing遗留标failed/SERVER_RESTARTED；refining有已存raw及提示时提交待确认原文ready，缺raw则failed；pending学习标failed/server_restarted；running模型调用标unknown；音频缓存一律不可恢复；reserved插入资格保持不确定，禁止自动回收重写。恢复动作与统计差量同事务、可重入，详见session-protocol。

恢复身份在schema验证后、任何恢复写入前冻结：使用原advisory专用物理连接，同一SQL核验原PID、精确key和数据库OID，并捕获pending学习的user/event、活动或可重试session的user/id/attempt/state/cache、running调用的user/call。事务成功提交后才发布集合；失败不得换连接或退回广域扫描，空集合不得重新枚举。恢复按准确租户/身份和锁内当前状态CAS执行，后来新建的对象不在旧集合内。统计重建继续在共同User锁内读取当前完整事实，不按旧身份子集覆盖新合法增量。原连接上的快照与心跳串行，等待计入原2秒探测预算。此身份边界仍须结合失锁取消和接纳/结果写入fence，不将它当作发现失锁前绝不误ready的证明；实际验证范围见独立记录（内部记录，未公开）。

数据库不可用时停止接单，不先调用模型再补记账，不给“操作成功”；运行中未提交结果不发送ready。对于失联后底层可能继续的外部模型调用，恢复为unknown用量而不是0；客户端通过GET确定当前状态。

## 6. 音频、正文、日志与备份

- 音频优先内存；adapter非用文件不可时只在专用临时目录创建随机文件，权限0600，在finally删除；不共享按用户名命名的路径。每个进程有独立子目录和锁标识，启动清理仅针对本应用已失效进程目录，不递归删除任意系统临时文件。完整ASR失败重试缓存只在内存，临时文件不可充当隐式长期重试来源。
- 成功/取消/empty/不完整上传立即清音频；失败600秒/20份/128MiB上限按协议回收。以首次失败+600秒为D：D立即禁止新ASR重试/迟到ASR输出，本地PCM≤D+1秒清空，worker≤D+2秒完成terminate/kill并确认退出；计时并行、不叠加，DB故障不得延后本地清理，未确认退出的资源保持隔离且不可复用。记录期限、实际清空/退出时间及异常，不记录PCM/正文；超限不记通过。停止后只处理原总预算内任务，收尾不添加推理预算；取消不以“协程已取消”代替确认工作缓冲及临时文件已经释放。
- 历史到期立即在读/claim/retry/confirm/undo入口阻断正文，小时清理results、attempt.raw_text、session.asr_hints/llm_hints、learning局部before/after/context/evidence/undo快照；清理幂等，不删除统计贡献，不从旧幂等快照返回正文。当前已学习词条/有限context按词库规则独立保留。
- 日志字段白名单：timestamp、level、request_id、匿名内部user/session/attempt/call ID、stage、provider/model_id、耗时ms、样本/用量数、error_code；不记请求/响应body、邮箱、词条、转写、音频、target、password/token、Authorization头。默认14天滚动；框架/代理访问日志关闭query及敏感头，token从不放URL。
- PostgreSQL每天加密备份、默认保留7天，离线/受限运维账号访问；备份不含音频但可能含当时尚未到期正文，恢复开放前保守清空所有恢复来的历史正文、session提示、attempt.raw及学习原片段/证据/撤销快照，并撤销全部sid；独立词库与统计保留。旧备份中的保留设置可能比用户后来缩短的设置更旧，不能用备份设置推断哪些正文允许复活。UI/隐私说明区分在线正文期限与备份最长7天滞后；若业务要求零滞后物理擦除须另议备份方案，不能声称在线清理会重写旧备份。
- 发布前备份并记录schema/app/model manifest；升级失败优先切回兼容旧app+原数据库或前向修复，破坏性迁移须用备份恢复且告知恢复点范围。恢复演练在隔离库，检查账号、词库、统计、所有恢复历史正文均不可读取，不连接生产客户端或重复执行插入；账号/词库回到明确备份恢复点，恢复点后的修改可能丢失，运维记录恢复点与损失范围。

## 7. 开发与验收顺序

三端按 [开发交接](fullstack-handoff.md) 同期安排：服务端实现配置/schema/鉴权/租户/provider 与会话状态机；Desktop/Admin 先按同一契约完成 Mock 页面与交互，API 就绪后逐项接真实服务。编译/类型/单测与迁移核对后，进行三端真实联调、真实模型/音频/平台验收。后端骨架跑通不是完整交付，不据此宣告 S4/S5 完成。部署清单应最终填入实际CPU/GPU/RAM、OS、Python/PostgreSQL与模型版本/摘要、网络、并发与冷/热条件；性能目标需该固定环境的原始记录支持。

## 8. 20路开发与服务器扩容配置

配置主表见[并发设计第2节](concurrency-target.md)。本地默认会话20、ASR执行1、LLM在途4只是起步设置，按1/5/10/20逐档实测，不能预设本机达到20。先准备测试账号/登录凭据，再发起负载，避免把注册/密码哈希限流与语音容量混为一谈。

例如未来服务器拟测100路，部署ceiling=100、active_pcm_budget_bytes至少576000000（另留执行池/缓存/模型/系统余量），后台max_active_sessions逐档提高；ASR执行池/LLM配额按真实吞吐配置，不从100直接推导100个模型副本。主机内存/显存不足时配置上限必须降低，不能只看PCM预算。数据库pool按短事务占用与等待指标调优，不按连接用户数一比一分配。

每次发布记录容量配置/模型资源/硬件指纹；部署报告缺失/格式无效为未验证，指纹不符为stale。报告不控制接纳权限，不允许伪造指标；只有真实F-CAPACITY通过才写入validated N。后续多API副本必须经过并发设计第8节专项门禁，当前发布仍限制单协调器。

### 本机容量资源预检材料（不授予 valid）

2026-10-24 R2增补：同一只读预检需识别当前`qwen3_mlx`清单，按正式加载器的文件集合/字节数/SHA规则验证全部文件并保留清单摘要、模型ID；单一safetensors文件名不能冒称完整Qwen。Qwen的模型/清单/运行库相对路径按正式服务的server工作目录解析，工具从仓库根调用也一致。独立`classification_llm`为Ollama时，在已有显式本机元数据开关下分别核验它的目标模型与daemon版本；主正文为官方Flash不因此发公网请求。Qwen原生运行库仅从指定安装目录的distribution元数据与Python源文件摘要取证，不import执行模型包、不预热、不声称已观察服务进程中的运行库。缺文件、版本错误或元数据缺失均列缺项，原默认零网络/数字回环/不输出私有路径与其他模型列表的边界不变。原件校验/可信运行指纹接入及真实容量报告仍是后继缺口，预检快照不能填入trusted state。

在真人语料到齐前，用 `scripts/acceptance/capacity_resource_snapshot.py` 只读采集本机 OS/CPU/RAM/GPU、Python 包版本、应用/负载工具摘要、规范化 YAML 配置摘要、本地 MLX 实际权重摘要，以及显式批准的数字回环 Ollama 的目标模型摘要/版本。只 GET 元数据，不加载/卸载/调用模型、不连接 DB、录音或操作前台。默认不发网络请求，显式 `--probe-local-ollama` 才访问 YAML 中的 `http://127.0.0.1:<port>`。

快照不含主机名、序列号、凭据、转写、原模型路径或其他模型清单；配置由 YAML 校验并补默认值，不读取环境密钥，明确不能证明运行进程实际环境覆盖。单纯 initial_max_active 与报告路径不参与资源摘要，资源预算/ceiling/执行池仍参与。原权重目录只读；Qwen按正式manifest全部文件核验，旧Whisper分支按其实际加载顺序选权重。元数据缺失要列缺项，不假定资源已验证。输出仅新建0600文件。

该材料输出 snapshot_sha256 而非 trusted_environment_fingerprint，始终 capacity_validation=unverified。不得复制到服务状态来绕过真实 F-CAPACITY 的原件、四组80×N会话、桌面读回和两小时 soak 验证；可信运行探测/原件校验接入仍待补齐。此工具不新增业务验收门槛。

### 容量协议负载执行器（测试工具，不授予容量通过）

`scripts/acceptance/capacity_stream_runner.py` 使用既有音频清单与独立账号凭据，正式HTTP登录/refresh/create/GET/cancel与WS PCM协议；默认四组（交错/同时停止×A10/A30）各20轮，N可配置。所有客户端先完成WS ready屏障，再按实际样本数和250ms分块节奏发送，不瞬时灌音频；ACK信用/2秒界、稳定前缀和身份逐路核验。发送发生延迟时不通过压缩后续节奏追赶；实际开始/停止偏差保留，失败不删样本、分位按+∞。

凭据只读私有JSON输入，token仅内存持有，不进入事件日志/报告；登录/刷新限流在准备阶段遵守Retry-After，不拿认证准备时间当模型耗时。结果原文/partial仅写新建0700目录下0600私有追踪，公开摘要仅内部lane/会话ID、耗时、状态与摘要；客户端错误不输出异常正文或凭据。已有输出目录拒绝复用。

该执行器只驱动协议端，不能伪造N-1协议+1真实Desktop读回、真人来源/完整人工语义标注、硬件配额可信证明或两小时混合稳定性。输出始终unverified/acceptance_pass_claimed=false；诊断轮数/Mock/TCP smoke不生成CapacityReport、不改变TC-CAP-17～21。完成正式容量报告仍须整合原生端、语义/资源证据与soak。


## 9. macOS 本机预览的应用身份（2026-10-20验收修复）

预览包没有发行证书时采用本机 ad-hoc 签名，根Identifier须为com.smartvoiceinput.desktop，Info.plist和资源被签名绑定；不再将只保留Electron链接签名的包用于系统权限验收。ad-hoc不等于Developer ID/公证，预览manifest的signed仍表示可信发行签名，不冒充稳定版。稳定版沿原证书、stapler、Gatekeeper要求。

Mac实际打包与签名在系统本地临时目录完成；签名前仅清除新产物内会阻断签名的FinderInfo/ResourceFork元数据，不清除quarantine、不修改源包或其他应用。目录内签名后严格验证全部子组件、根应用标识及Info绑定；DMG只读挂载后再次检查。完整DMG校验后才写入指定输出目录，不覆盖已有安装文件；unpacked预览保留本地临时目录，避免同步目录元数据令校验失效。

系统权限验收须以完整.app正常应用方式启动，测试环境用定向环境变量保留隔离userData，不运行包内二进制来代替应用身份验证。遇多个同名权限条目须确认图标/操作关联；本轮旧exec条目未清除或冒充新应用授权。

本机ad-hoc预览更新后的辅助功能授权不能只看系统开关：2026-10-20新包实测开关on而进程false，系统诊断明确旧代码签名要求不匹配，off→on及重启仍未恢复。须由用户确认在系统设置重建该应用授权并复核实际进程状态；不编辑TCC数据库，不添加私有entitlement或放宽签名要求。稳定版继续要求固定发行签名，本机预览的旧授权不当作升级保持证明。

本轮后续已由用户移除这一条旧记录，按精确安装.app路径重新添加；同一新包原生实读accessibility=true，实际物理快捷键捕获目标及关闭后安全降级通过。此结果解决当前验收环境，不宣称后续ad-hoc包替换无需再次授权。

## 10. V2 Linux 部署（2026-10-28，S4 步骤 7）

依据 [V2 设计](v2-provider-design-20261027.md) §9 与 §15 澄清。本节是部署说明和样例，**尚未在真实 Linux 主机上执行**（TC-V2-DEP 全部为 R 层，S5 执行）。

### 10.1 安装

| 项 | 要求 |
|---|---|
| 系统 | Linux x86_64/arm64；Python 3.11–3.13；PostgreSQL 16；时钟同步（NTP） |
| 依赖 | `cd server && uv sync --frozen --no-dev`，**不装 `mac` 可选依赖**（无 MLX）。Linux 上只配置云端 ASR（`bailian_realtime`）与 OpenAI 兼容/Ollama 整理模型；后台创建 `qwen3_mlx` 返回 `host_unsupported` |
| 进程 | 沿用 §1：单 Uvicorn worker、单副本、advisory lock；入口 `uv run --frozen python -m app.main`；迁移是单独运维步骤（§4） |
| 样例 | `deploy/linux/svi-server.service`（systemd）、`deploy/linux/nginx-smartvoiceinput.conf`、`deploy/linux/egress-nftables.example.conf`、`deploy/linux/svi.env.example`、`server/config/app.v2.linux.example.yaml` |

### 10.2 环境注入（不进 Git、不写日志）

`SVI_DATABASE_URL`、`SVI_JWT_SECRET`、`SVI_IDEMPOTENCY_SECRET`、`SVI_SECRET_KEYS`（JSON：`{key_id: base64(32B)}`）、`SVI_SECRET_ACTIVE_KEY_ID`，以及 YAML 兜底引用的 `SVI_DASHSCOPE_API_KEY`、`SVI_DEEPSEEK_API_KEY`。主密钥用 `uv run --frozen python -m app.cli secrets generate-key` 生成；轮换先把新 key 加入 `SVI_SECRET_KEYS` 并重启，再执行 `uv run --frozen python -m app.cli secrets rotate --to <新id>`（单事务，任一条失败全部回滚），确认后把 `SVI_SECRET_ACTIVE_KEY_ID` 改为新 id，最后移除旧 key。主密钥丢失时后台保存的 Key 无法解密，启动回落 YAML 并在“模型配置”和 `/healthz` 显示来源 `yaml`（原因 `secret_unavailable`），需重新录入 Key。

### 10.3 内存与容量

| 预算 | 配置 | 说明 |
|---|---|---|
| 活动 PCM | `audio.active_pcm_budget_bytes: 1073741824` | 每会话预留 `audio.max_duration_seconds × 32000` = 11.52 MB（360 秒）；有效接纳上限 = min(`admission_ceiling`, 1 GiB // 11.52 MB = 93) |
| 失败音频 | `retry.audio_budget_bytes: 268435456`、`max_cached_sessions: 20` | **不计入**活动预算（S4 澄清 C2），超出按到期先后淘汰 |
| 进程 | systemd `MemoryMax=3G` | 活动 1 GiB + 失败 256 MiB + 运行时开销；DEP-06 在目标机实测 RSS 后定值 |
| 云端并发 | `asr.params.max_concurrent_sessions: 8` | 超出返回 429 `CAPACITY_EXCEEDED`、`details.reason=provider_limit`；目标环境实测后调整（T9） |

### 10.4 网络

- 入站：只暴露 HTTPS/WSS（Nginx TLS 终止，80 端口只做 301）；WS `proxy_read_timeout 480s` ≥ 最长会话 360 s + 120 s。后端只监听 `127.0.0.1:17863`。
- 出站：应用层 `security.egress_policy: allowlist` + `egress_allowlist`（后台不可改，改清单需改部署配置并重启）；网络层另以防火墙/出口代理只放行清单域名的 443。`*.maas.aliyuncs.com` 这类通配符无法写成静态 IP 集，建议使用支持 SNI/域名规则的出口代理，或部署脚本定期解析后刷新 nftables 集合；两层同时生效（TC-V2-DEP-04）。
- 云端 ASR 默认直连并忽略系统代理（`use_env_proxy: false`），建连前解析一次并固定已校验 IP；不跟随重定向。服务器 DNS 不能是 fake-IP 代理（如 Clash 的 198.18.0.0/15），否则解析结果被判为非公网而拒绝建连（设计 §15 C15）。
- 整理模型（LLM）的地址在保存/测试/切换时校验白名单与解析结果；HTTP 客户端建连时未固定 IP（与 ASR 不同），依赖网络层出口规则兜底——已知限制，记录于此。

### 10.5 上线检查清单

1. `uv sync --frozen --no-dev` 成功且无 MLX；`alembic upgrade head` 后核对 `ck_sessions_samples` 为 `<= 5760000`、`provider_active` 有 `asr`/`llm` 两行。
2. `svi admin init` 创建管理员；后台“模型配置”新增云端识别与整理模型并通过连通测试（云端测试会发送 1 秒静音，产生少量费用）；把云端识别设为当前时勾选隐私确认。
3. `/healthz` 中 `providers.asr.source=db`；用虚构测试账号完成一次口述冒烟（创建响应 `max_record_seconds=360`）。
4. 桌面端改 `desktop/src/shared/config.ts` 的服务地址为 HTTPS 并重新打包。

### 10.6 CentOS Stream 9 实机部署记录（2026-10-28，S5）

已在 centos91 按本节部署并验收（S5 记录（内部记录，未公开）），与上文的差异/补充：

- 主机无 uv、系统 Python 3.9：用 AppStream `python3.12` 建 venv，依赖由 `uv export --frozen --no-dev --no-emit-project` 导出带哈希的 requirements，经可达的 PyPI 镜像 `pip install --require-hashes` 安装（与锁文件逐包一致）；systemd `ExecStart` 用 venv python。
- PostgreSQL 16（`dnf module enable postgresql:16`），迁移账号与运行账号分离，127.0.0.1 scram。
- SELinux Enforcing：`setsebool -P httpd_can_network_connect 1`；后台静态目录 `semanage fcontext -t httpd_sys_content_t` + `restorecon`。
- firewalld 只放行 `https`（80 不开放，明文请求直接不可达）。
- `SVI_SECRET_KEYS` 写成单行无空格并整体单引号：`SVI_SECRET_KEYS='{"k1":"<base64>"}'`（shell 与 systemd 均可解析）。
- 网络层出站：静态 IP 集合会被 CDN 轮换打破（DeepSeek 实测），且须同时覆盖 IPv6；生产请用按域名/SNI 的出口代理。
- 内存：20 路满 360 s 活动 + 20 份失败音频保留，实测峰值 RSS 813 MB。
- 初始并发：全局 `initial_max_active` 与云端 `max_concurrent_sessions` 实测 12 路可用（0 重连，p50 3.3 s）；更高并发需按云端账号配额再测。
