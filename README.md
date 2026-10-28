# SmartVoice

**SmartVoiceInput** 是一个跨平台语音输入工具：按快捷键说话，客户端流式上传录音，服务端完成语音识别与 AI 文本整理（去口头禅、理顺改口、补标点），再把结果直接输入到当前光标所在的任意应用。

## 功能

- 全局快捷键口述，非激活胶囊浮窗显示录音与处理状态，不抢焦点
- 流式语音识别，单次最长 6 分钟，断线自动重连补传
- AI 整理：忠实于原意，只去掉多余内容，不添加事实
- 通用粘贴插入：适用于聊天、邮件、文档、编辑器、终端等，不自动回车发送
- 个人词库与修改学习：识别错误被纠正后自动学习，可撤销
- 历史记录、使用统计
- 管理后台：账号、用量统计、ASR/LLM 模型服务（provider）配置、并发容量
- 自部署：服务端运行在自己的服务器，音频不长期保存

## 项目结构

| 目录 | 内容 |
|---|---|
| `desktop/` | Electron + React + TypeScript 桌面客户端（macOS / Windows），原生输入桥在 `desktop/native/` |
| `server/` | Python + FastAPI + PostgreSQL 服务端（ASR / LLM provider 可配置） |
| `admin/` | React + Vite 管理后台 |
| `prototype/` | 无需构建即可打开的 HTML 原型 |
| `docs/` | 产品需求、技术架构、测试用例、品牌 |
| `scripts/release/` | 版本、安装包构建、产物校验与 Release 发布脚本 |
| `deploy/` | Linux 部署示例 |

文档入口：[文档索引](docs/README.md) · [产品需求](docs/02-prd/PRD.md) · [业务规则](docs/02-prd/business-rules.md) · [技术架构](docs/03-architecture/architecture.md) · [测试用例](docs/04-test/test-cases.md) · [更新日志](CHANGELOG.md)

## 本地运行

```bash
# 服务端（PostgreSQL 16）
cd server
docker compose up -d
uv sync --frozen --extra dev --extra mac
uv run --frozen alembic upgrade head
uv run --frozen python -m app.main

# 桌面端
cd desktop
pnpm install
pnpm dev

# 管理后台
cd admin
pnpm install
pnpm dev
```

开发期 API 地址集中在 `desktop/src/shared/config.ts`，默认 `http://127.0.0.1:17863`。

需要服务端热重载时使用 `uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 17863 --reload --ws websockets --no-proxy-headers`；单实例与部署配置见[部署设计](docs/03-architecture/deployment.md)。 部署与停机验收使用上面的标准 `python -m app.main` 入口，以执行拒新请求、收尾已接收会话、再关闭网络的顺序。

## 查看原型

直接用浏览器打开 `prototype/index.html`，无需安装依赖。页面说明见[原型文档](docs/01-prototype/README.md)。

## 构建与发布

```bash
pnpm --dir desktop typecheck
pnpm --dir desktop test
pnpm --dir desktop test:release
node scripts/release/version.mjs   # 查看下一版本
```

完整流程：开发分支验证 → 合并 `main` → 创建附注标签 `vX.Y.Z` → 推送 tag → GitHub Actions 构建三个平台安装包 → 核对 Release。详见[构建与发布](docs/03-architecture/release.md)。

## 协作约定

项目工作约定（含 AI 编码助手约定）见 [AGENTS.md](AGENTS.md)。配置与密钥分离，密钥、运行数据、模型权重和临时音频不入库。

## 许可证

[MIT](LICENSE) © uprxiao
