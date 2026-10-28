# Logo 与生产图标

用户于 2026-10-17 指定根目录 `logo.png` 为本产品 App Logo，并确认本次交付接入。

| 根目录文件 | 候选方案 | 状态 |
| --- | --- | --- |
| `logo.png` | **V2 / FOLDED VOICE**：深墨色圆角底板，青绿色一体 V 形折带 | **已采用**，作为当前应用图标 |
| `logo2.png` | **S2 / FOLD SIGNAL**：瓷白圆角底板，蓝紫与冰蓝 S 形折带 | 保留备选，未接入 |

## 源图与提取范围

- 两张源图均为 1254 × 1254 PNG 展示图，包含上方彩色 App 图标、下方单色示意和方案标签。
- 生产图标只提取 `logo.png` 上方彩色图标区域（约 `x=294..960, y=157..816`），去掉展示背景、单色示意和标签。
- 原始彩色图标带圆角和轻微高光；生成时按 8 倍超采样圆角遮罩去除白底，并保留透明外边距，不复制展示卡整体。

## 已生成资源

| 文件 | 用途 |
| --- | --- |
| `desktop/build/icon.icns` | macOS 应用包图标（electron-builder） |
| `desktop/build/icon.ico` | Windows 应用与安装包图标（electron-builder） |
| `desktop/build/icon.png` | 1024 × 1024 主图标与 Linux 构建源 |
| `desktop/resources/icon.png` | 开发态窗口 / Dock 图标与安装包运行时资源 |
| `desktop/src/renderer/assets/logo.png` | 桌面端登录页与侧边栏品牌标记 |
| `admin/src/assets/logo.png` | 管理后台登录页与侧边栏品牌标记 |
| `admin/public/favicon.png` | 管理后台 favicon |
| `admin/public/apple-touch-icon.png` | 管理后台 iOS 主屏图标 |
| `prototype/assets/logo.png` | 高保真原型品牌标记 |
| `docs/05-branding/logo-exploration-2026-10-17/app-icon-1024.png` | 生产图标归档预览 |
| `docs/05-branding/logo-exploration-2026-10-17/app-mark-128.png` | 界面品牌标记归档预览 |

## 验证边界

- 已在本地构建 macOS arm64 DMG，并挂载检查 `Contents/Resources/icon.icns`、`icon.png` 存在。
- 尚未在真实 Dock、Windows 开始菜单/任务栏和不同缩放比例下逐项目检。
- 未做商标近似检索；当前用于个人自测与内部交付。
