# 实例锁故障诊断与自动恢复

2026-11-11，用户已确认方案并授权实施、更新既有部署。仅服务端可靠性修复，Desktop/Admin 保持既有 API 契约；不构成客户端或应用版本发布。

## 诊断与所有权

保留每秒探测、原连接/后端 PID/会话级 advisory lock 校验和整个探测 2 秒预算。探测记录固定原因：connection_missing、connection_closed、connection_invalidated、backend_changed、lock_missing、probe_timeout、probe_error；并记录阶段（connection/mutex/query/commit）、耗时毫秒、原后端 PID、异常类型、原驱动异常类型及严格限定为 5 位字母数字的 SQLSTATE（缺失为 none）。异常文本、SQL 参数、DSN、业务正文不得写日志。监测探测轮询调度延迟，超过 1 秒时记录延迟毫秒。

一次所有权无法确认即同步置 fenced，拒新会话及重试，终止旧任务，不在同一进程建立新的所有权代际。记录保护开始和清理完成。已有业务取消、终态记账、音频期限不变。

## 生产进程恢复

`ManagedServer` 观察 fenced 标志，进入既有受控 shutdown；该路径使用失败退出码 75，供 `Restart=on-failure` 识别。启动失败及普通外部停止沿用原语义；受控关闭仍先拒业务、等待旧任务，再关闭网络和 lifespan，不因自动恢复提前释放资源。

Linux unit 使用 Type=notify / NotifyAccess=main / WatchdogSec=30s。主进程以 AF_UNIX datagram 发送 READY=1（监听启动完成，不代表模型业务 ready）；独立 asyncio 任务最多每 5 秒、且小于 watchdog 周期三分之一发送 WATCHDOG=1。普通关闭期间继续通知，实例 fenced 后立即停止通知。故障清理卡住或事件循环失去调度时，systemd 在最后一次通知后 30 秒内触发 SIGKILL；KillMode=control-group / SendSIGKILL=yes 保证旧服务进程组终止后再重启。此期限是故障进程回收上限，不延长任何业务或音频 TTL 预算。

通知仅使用当前进程匹配的 WATCHDOG_PID、有效 WATCHDOG_USEC 与 NOTIFY_SOCKET；非 systemd 启动不通知，不新增依赖。socket 发送非阻塞；发送失败仅记录有限异常类型，不伪报喂狗成功。启动通知超时 60 秒。RestartSec=5s，StartLimitIntervalSec=300s / StartLimitBurst=5 限制持续故障重启风暴；达到上限保留 failed 状态供运维处理。告警由现有运维渠道接入，当前不虚构已连接的外部告警渠道。

## 验证与部署

先编译，再单测及原所有权/取消/关闭回归。用进程独占 PostgreSQL 测试库运行 TCP/进程级故障：原连接终止、真实探测超时、锁释放及清理阻塞；使用固定虚构配置与 Mock provider，不访问正式库或真实音频。Linux systemd 实测在独立临时 unit 中运行相同源码，证明失败码触发重启、看门狗终止主/子进程及重启限流。

部署前比较正式文件摘要，备份旧文件和 unit；只更新本次必要文件，不覆盖其余服务源码或密钥配置，无数据库迁移。新部署使用更新后的 unit 样例；既有部署安装 `deploy/linux/svi-server-recovery.conf` 为 `svi-server.service.d/20-instance-recovery.conf`，保留原 unit 其余设置。检查 systemd unit、编译、重启后独立核对部署摘要、PID、watchdog 时间戳、HTTPS/ready 与受保护 API 的鉴权拒绝。出现故障从已备份的原文件回退，不重建数据库。真实执行证据保存在本地忽略目录，不提交个人数据或本机路径。
