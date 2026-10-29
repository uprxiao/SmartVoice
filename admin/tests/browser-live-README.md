# Admin 真实服务浏览器联调

对应 TC-WIN-34/35 的可复跑 L2 测试。使用独立 PostgreSQL 集群、真实 FastAPI HTTP/WS 和已构建 Admin 页面；仅 ASR/LLM、PCM 和账号为虚构夹具。它不替代真实模型、麦克风或 Windows 真机验收。

前置：Node.js 22+、Chrome/Chromium、PostgreSQL 的 `initdb/pg_ctl/createdb` 在 PATH；Python 环境按 server/uv.lock 安装；已安装 `source-driven-ui-test` Skill。以下从仓库根运行，目录均须是本轮新建路径。

1. `pnpm --dir admin build`。
2. 用 server 环境的 Python 执行 `server/tests/admin_browser_environment.py prepare --run-dir <新环境目录>`。该命令新建集群/库，迁移、初始化虚构管理员，并通过真实 HTTP/WS 创建三个一秒会话，生成 inserted/failed/unverified 和重复 claim/receipt；输出 origin、environment.json 和 fixtures.json。
3. `node admin/tests/prepare-browser-bundle.mjs <environment.json> <新测试包目录>`。生成9个子参数组、源码追踪、独立预期和脚本哈希；先用 Skill 的 `check-bundle.mjs --root <测试包> --source-root <仓库>` 检查。
4. 用 Skill 的 `browser.mjs start --run-dir <新浏览器目录> --url <origin>/admin/ --headless` 创建独立浏览器。设置 `SVI_ADMIN_UI_ENVIRONMENT` 为本轮 environment.json、`SVI_ADMIN_UI_PYTHON` 为 server 环境 Python，按 Skill `run-suite.mjs` 执行测试包中的 cases.json/suite.mjs，结果写新目录。
5. 不论通过/失败，运行 `server/tests/admin_browser_environment.py stop --environment <environment.json>`，核对服务/PG退出、残余虚构登录撤销。浏览器执行器自行核验所有权并关闭自己的浏览器/profile；数据和失败证据保留，不清理未知进程或其他数据库。

判据在驱动之前冻结：3个结果会话、48000接收样本、42个Unicode字母/数字、仅1次已插入、33.3%插入成功率、0学习。筛选、空态、真实服务中断/恢复、刷新重新登录及退出也通过实际UI输入验证；不改DOM值、store或业务RPC来模拟操作。日期使用原生日期编辑片段，用户下拉使用真实键盘类型搜索；204只要求与本次请求关联的实际状态，不强求不存在的响应体。

`credentials.private.json` 为本轮生成的随机测试口令/密钥，权限0600，不能提交；报告不保存token/headers。源脚本和虚构夹具定义可入库，运行目录/截图/数据库只放本地忽略目录。原始失败attempt保留，不能把驱动修复后的通过覆盖旧记录。
