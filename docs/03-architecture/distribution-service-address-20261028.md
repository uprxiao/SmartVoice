# 同局域网/VPN分发的固定后端入口

2026-10-28，状态：implemented，Mac arm64 与实际 HTTPS/WSS 入口验证通过；Windows/x64 新包业务连接尚未执行。本人明确选择“同一局域网/VPN，固定使用192.168.14.151”。对应开发分支 feature/0.0.4；实施基线695d54879eba617791d87a15dc89dedc80b58b01。

## 已复现的问题

- 桌面测试副本用 https://centos91，主机名仅在已有环境解析。desktop/src/shared/config.ts 的开发默认是 http://127.0.0.1:17863；云端安装包构建没有明确设置正式地址。
- admin/src/config.ts 固定127.0.0.1，远程浏览器将访问浏览者自己的本机。
- 现服务器证书仅有DNS:centos91。相同CA校验下，GET https://centos91/health/ready成功；GET https://192.168.14.151/health/ready失败，curl退出60，证书SAN不匹配。

## 本轮实施范围

1. 分发入口固定 https://192.168.14.151，对应WSS。地址和内网CA指纹集中于release.config.json，由统一安装包脚本向Mac/Windows构建注入。保留开发默认，分发构建禁止静默回退本机地址。
2. Admin按浏览器当前origin调用API；本地开发通过Vite代理。登录、词库、历史、模型配置等沿用同一服务入口，凭据/模型密钥不打入包。
3. 创建独立内网CA和包含目标IP的服务器证书；CA私钥仅留服务器受限目录，包中只有公开CA证书。桌面主进程在网络调用前校验打包CA的固定SHA256指纹并加入当前进程TLS信任，证书有效期和IP校验保留。浏览器访问后台需要一次性信任配套内网CA。
4. 新增IP的Nginx默认TLS站点，使不带SNI的IP连接取得IP证书，原centos91站点保留。新增文件拒绝覆盖；切换前nginx配置验证并保留回退依据。
5. 先类型检查、相关测试和真实TLS正负控，再打Mac新副本，核验新包实际API/健康状态和WSS。无新tag/Release；Windows实际网络验收状态另记，不以Mac替代。

Node TLS信任接口依据：[官方文档](https://nodejs.org/api/tls.html#tlssetdefaultcacertificatescerts)。已探测锁定Electron42.11.15包含Node24.19.0，setDefaultCACertificates/getCACertificates均可用；仅当前Node线程增加公开根证书，须先于任何可能缓存的连接调用。不会修改用户系统证书库。

## 当前边界

仅同网或VPN可达此内网IP；这不提供公网入口。此前90条验收结论保留，本次分发地址/证书变更另做受影响回归。独立Main长重试缺陷仍单独跟踪。

## 已部署结果

- 正式访问入口：`https://192.168.14.151`；后台：`https://192.168.14.151/admin/`。现有 `svi-server` 继续运行，未重启或更改模型/数据库配置。
- 新配置 `/etc/nginx/conf.d/smartvoiceinput-ip.conf` 从仓库 `deploy/linux/nginx-smartvoiceinput-lan-ip.conf` 部署；先在独立候选主配置中 `nginx -t` 通过，启用前再次检查通过后平滑 reload。原站点配置 SHA256 前后一致：`ec2b3f2fe19a6f37bbcc16bbfdbf70d3e40fb7a5a904206750db7b0b20d326b8`，原域名与原CA回归成功。
- 新后台静态目录 `/opt/smartvoiceinput/admin/dist-ip-20261028-01`，保留旧目录；SELinux 类型沿用 `httpd_sys_content_t`。IP站点实际返回的 HTML、JS、CSS 与本轮构建哈希一致。
- 独立证书目录 `/etc/ssl/smartvoiceinput/lan-ip-20261028-01`；服务器证书 SAN 含 `IP:192.168.14.151` 与 `DNS:centos91`，到期 `2027-10-28 03:23:53 UTC`。CA 私钥仅在服务器 `/root/svi-deploy/lan-ip-20261028-01` 的0700目录中，以0600保存，未导出。
- 公开CA的DER SHA256：`6a3dc65bb88ffd354ee66eba76c0a54fc16c82d1e3a0571bcd2865618007c885`，CA到期2029-10-27。续签服务器证书时保持同CA与IP SAN可免客户端换根；到期前须更新。浏览器访问后台需要信任此公开CA；不自动修改用户系统信任库。
- 回退：将新增 `smartvoiceinput-ip.conf` 移出 Nginx include 范围后先检查、再 reload，即恢复原默认站点；旧文件与目录不覆盖。本轮未执行回退。

## 本轮验证

原件和匿名证据索引：distribution-ip-20261028-01（内部记录，未公开）。源码与完整日志的路径/哈希见同目录 `source-and-log-hashes.json`。

| 检查 | 实际结果 |
| --- | --- |
| Desktop Main / Web、Admin 类型检查 | 三项通过；修正测试后再做 Main 类型检查通过 |
| Desktop 原有单测 | 624通过；同次新增6项因沙箱禁止本地 listen 报EPERM/未执行，未把该次全量算通过 |
| 新增证书集成测试首轮 | 放开本机端口后3通过/3失败：Node规范化PEM导致原文本比较失败；改按X509指纹比较，未修改TLS策略 |
| 新增证书集成测试最终轮 | 6/6通过：无信任拒绝、HTTPS/Fetch/WSS成功、错误主机拒绝、被替换/非CA/过期证书拒绝、开发模式不加载私有CA |
| Admin | 93/93通过；包含实际配置的同源请求与原传输校验 |
| 发布工具 | 26/26通过；包含缺省/冲突地址及证书指纹校验 |
| Admin生产构建/实际部署 | 构建成功，真实HTTPS返回资源哈希一致；未声称已完成浏览器安装CA后的全后台人工验收 |
| HTTPS 正负控 | IP+新CA、原centos91+原CA均ready；不指定新CA的系统curl仍拒绝证书（退出60），未关闭TLS校验 |
| 新Mac包实际运行 | 新空profile、无 `NODE_EXTRA_CA_CERTS`、无地址覆盖、无代理环境；正常表单登录既有虚构账号成功，Main实读IP地址，ASR/LLM均ready |
| 新Mac包真实WSS | 正常会话RPC进入streaming，360000ms上限、0ms音频，随即取消并读回cancelled；未启动录音器、未上传音频，不冒充新一轮真人语音验收 |
| Mac arm64 DMG | 构建、挂载、应用身份/资源/签名验证通过；DMG内app.asar与CA哈希均与实际运行新包一致 |

实际运行显示 ASR `qwen3-asr-flash-realtime`、LLM `deepseek-flash`。模型仍为服务端配置，包内不包含模型权重或服务密钥；各使用者用自己的账号登录。

## 交付位置与发布边界

- 新App：`/private/tmp/svi-distribution-ip-20261028-01/SmartVoiceInput.app`。
- Mac arm64 DMG：`/private/tmp/svi-distribution-ip-20261028-7mlk7380/installers/SmartVoiceInput-0.0.3-mac-arm64.dmg`，120716350字节，SHA256 `f7a19d1c21f39ad28e7d5766434376c7382e129d90a3540b70de3bc32eb53ddc`。
- 此为feature分支验证副本，沿用现有0.0.3元数据，manifest如实记录基线与dirty工作区；不替换已发布v0.0.3，不新建tag、不合并main。正式下一版本仍需依既有规则准备。
- Windows x64 / Mac x64 共用安装包入口与CA资源配置，但本轮未在对应系统上实际启动新包，不声明已通过其业务连接验收。
