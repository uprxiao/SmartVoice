2026-10-23 R2新包须按R2验证计划（内部记录，未公开）重验相关三端/权限/原生参数；旧包通过不自动移植。

# 构建与发布

本文说明 SmartVoiceInput 的 GitHub Actions 构建、命令与产物核验。commit、push授权、分支、版本进位与tag的统一规则见 [AGENTS.md 第7节](../../AGENTS.md#7-git提交与发布)，沿用SmartDD流程，安装包目标按本产品收敛为macOS与Windows。

## 1. 工作流分工

| 文件 | 触发 | 作用 |
| --- | --- | --- |
| `.github/workflows/ci.yml` | `main`、`feature/**`、`release/**`、`0.*` 分支 push；面向 `main` 的 PR；可被复用 | 工作流语法检查、发布规则测试、服务端 pytest、桌面端类型检查/单测/构建、管理后台构建 |
| `.github/workflows/build.yml` | 手动；被发布流程复用 | 先跑完整 CI，再在 macOS arm64、macOS x64、Windows x64 原生 runner 上构建并验证安装包，上传 Actions artifacts |
| `.github/workflows/release.yml` | 推送 `v*` tag | 校验附注标签、main 归属、版本进位与 CHANGELOG；调用构建；汇集资产到草稿 Release 后公开 |

安装包目标：

- macOS arm64：`SmartVoiceInput-<版本>-mac-arm64.dmg`
- macOS x64：`SmartVoiceInput-<版本>-mac-x64.dmg`
- Windows x64：`SmartVoiceInput-<版本>-win-x64.exe`（NSIS）

每个平台同时生成 `manifest-<平台>-<架构>.json`，记录版本、commit、平台、架构、签名状态、工作区是否脏、安装包 SHA-256。发布前必须三份清单与三个安装包全部存在且校验一致。

## 2. 分支与 tag 规则

遵循 [AGENTS.md §7.1–7.4](../../AGENTS.md#7-git提交与发布)。现有 `0.0.1` 版本分支继续沿用；版本与tag计算实现在 `scripts/release/version.mjs`，附注标签/版本元数据/main归属检查在 `scripts/release/metadata.mjs`。脚本的本地下一版本输出须结合远端全部版本tag核对。

## 3. 发布通道与签名

`release.config.json` 当前为：

```json
{
  "channel": "preview",
  "service": {
    "apiBaseUrl": "https://192.168.14.151",
    "caCertificate": "desktop/resources/service-ca.crt",
    "caSha256": "6a3dc65bb88ffd354ee66eba76c0a54fc16c82d1e3a0571bcd2865618007c885"
  }
}
```

- `preview`：允许无 Apple 签名、无公证；GitHub Release 标记为预发布，不设为 latest。
- `stable`：macOS 必须提供签名与公证凭据；构建脚本会校验 `codesign`、`stapler` 与 `spctl`，失败则阻止发布。
- Windows 当前未接入代码签名；正式对外分发前需补充证书与签名验证。

2026-10-28 分发范围为同局域网/VPN。Mac/Windows 安装包均由统一脚本注入此 HTTPS 地址及公开 CA 指纹；缺少配置、CA 不匹配或冲突的地址环境变量将阻止构建，不静默回退 localhost。安装包不包含账号、密码、模型密钥或 CA 私钥。Admin 使用访问页面的 origin；浏览器后台需一次性信任内网 CA，桌面包无需系统导入证书。实际部署及回归见[固定 IP 入口记录](distribution-service-address-20261028.md)。

stable 通道需要的 GitHub Secrets：

| Secret | 用途 |
| --- | --- |
| `MAC_CERTIFICATE` | macOS Developer ID 证书（base64 或 electron-builder 支持的链接） |
| `MAC_CERTIFICATE_PASSWORD` | 证书密码 |
| `APPLE_ID` | 公证 Apple ID |
| `APPLE_PASSWORD` | App 专用密码 |
| `APPLE_TEAM_ID` | Apple Team ID |

## 4. 标准发布步骤

以下步骤适用于已经明确授权的版本发布；仅整理规则或提交开发分支时按AGENTS.md授权表执行。

1. 核对GitHub认证账号 `uprxiao`、远端 `uprxiao/SmartVoice`，读取远端分支与tag：`git fetch --tags origin`；发现同名不同SHA先查明，不强制覆盖。
2. 计算下一版本：`node scripts/release/version.mjs`，或从远端全部 tag 手工核对。
3. 修改 `desktop/package.json` 版本、同步 `admin/package.json`、`server/pyproject.toml` 与 `CHANGELOG.md` 的 `## <版本>` 段落。
4. 本地执行：
   ```bash
   pnpm --dir desktop typecheck
   pnpm --dir desktop test
   pnpm --dir desktop test:release
   pnpm --dir desktop build
   pnpm --dir admin build
   cd server && uv run pytest -q
   ```
5. 生成目标平台安装包并完成必要验收（见第 5 节）。
6. 提交改动，整合最新 `main` 并合并，必要时补受影响验证；按保护规则推送 `main` 或通过PR合并，独立确认远端包含最终发布提交。
7. 在已核实的最终发布提交上创建附注标签并推送（以下 `v0.0.1` 仅为首版示例，实际版本先按第2步确定）：
   ```bash
   git tag -a v0.0.1 -m "SmartVoiceInput 0.0.1"
   git push origin v0.0.1
   ```
8. 在 GitHub Actions 查看 tag 对应的 Release 运行（推送 tag 未自动触发时，可对该 tag 手动触发：`gh workflow run release.yml --ref vX.Y.Z`，元数据检查会拒绝非附注 tag 或不在 main 中的引用）；确认 metadata、三个 package job、publish 全部成功。
9. 打开 Release 核对版本、commit、说明、三个安装包、manifest 与 `SHA256SUMS.txt`，并实际下载安装验证。报告实际SHA、tag、Actions/Release链接和未验范围；失败重试及已占用tag处理按AGENTS.md §7.4–7.5执行。

2026-10-27现状：已用 `uprxiao` 将开发分支 `0.0.1` 推送到 `a0130f1`，另已存在非版本历史检查点 `ds-r2-acceptance-20261027`，指向同一提交。它不匹配版本格式、不触发Release，不计作占用 `v0.0.1`；当前规则整理没有新建版本tag、合并main或发布Release。后续发布须重新核对远端，不能把本段历史SHA当实时发布目标。

## 5. 构建脚本与产物验证

`scripts/release/build.mjs`：

- 强制在目标平台/架构的原生 runner 上构建，禁止跨平台伪造结果。
- 复用与目标版本、架构匹配的本地 Electron 发行目录；不匹配时由 electron-builder 下载。
- 本地无网络时可显式设置 `SVI_ELECTRON_DIST=<electron/dist 路径>` 作为离线冒烟钩子；该路径内容必须与锁定的 Electron 版本一致，CI 不使用该变量。
- macOS：构建 DMG 后挂载，校验应用名、Bundle ID、`CFBundleShortVersionString`、`icon.icns`、运行时 `icon.png` 与语音 helper；stable 通道额外校验签名、公证票据与 Gatekeeper。
- Windows：构建 NSIS 后静默安装到临时目录，校验 `SmartVoiceInput.exe`、产品版本与运行时图标，再静默卸载。
- 生成 manifest；工作区存在已跟踪文件改动时标记 `dirty: true`，发布脚本拒绝公开。
- 本地验收可加 `--keep-temp` 保留校验临时目录。macOS 在挂载后先记录路径，卸载失败时停止清理并保留原校验错误，不递归删除仍可能挂载的目录。

`scripts/release/publish.mjs`：

- 校验附注标签、tag 指向当前 checkout、已合并到 `origin/main`、版本为下一顺位。
- 校验三份 manifest 与安装包的大小/SHA-256。
- 只允许创建草稿或更新仍为草稿的同名 Release；已公开版本拒绝覆盖。
- 上传安装包、manifest 与 `SHA256SUMS.txt` 后公开，按通道设置 prerelease/latest。

## 6. 当前限制

- 2026-10-17 仅完成本地 macOS arm64 DMG 构建与挂载校验；macOS x64、Windows x64 构建尚未在 GitHub Actions 实际运行。
- 本机网络无法连接 GitHub/Electron 下载源，本地构建使用既有 Electron 42.11.14 发行目录做冒烟；锁文件仍为 42.11.15，CI 会安装锁定版本。
- 2026-10-18 独立复验已用系统正常 TLS 从 Electron 官方 Release 取得 42.11.15，SHA-256=`aa03043128babffe35a22d4a5604606c2f650028fd99057563a1bde21216401b`，实际二进制确认 arm64/42.11.15。此前 42.11.14 冒烟保留为历史，不能代替锁定版本的最终安装验收；最新包结果另登记。
- 预览安装包无签名、无公证；Windows 安装包未签名。
- 安装包构建通过不等于业务验收通过；真实输入法、焦点插入、20 路并发与 219 条验收用例仍需按 `docs/04-test/` 执行。
