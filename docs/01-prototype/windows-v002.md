# Windows v0.0.2 平台差异原型

2026-10-29。依据已确认的[Windows 范围与 Q1–Q7](../02-prd/windows-version-decision-20261029.md)补充 S0，沿用方案 B。入口为 [windows.html](../../prototype/windows.html)，双击即可打开；无构建、网络、账号和系统操作。原有完整页面继续使用 [index.html](../../prototype/index.html)，本页集中评审本版变化。

## 页面与状态

平台选择、页面和状态选择均在产品窗口外的评审栏。链接参数可复现状态；示例 `windows.html#settings?platform=win32&state=mic-denied`。所有状态为虚构内存演示，刷新或重置恢复初始值。

| 页面 | 状态入口 | 内容与动作 |
|---|---|---|
| `settings` | `default`、`mic-denied`、`mic-unknown`、`checking`、`check-error` | 麦克风已允许/被系统禁用/未知/检查中/检查失败；重新检查与系统隐私设置入口。Windows 显示“输入到其他应用”能力说明，不显示辅助功能授权；`platform=darwin` 对照保留辅助功能与授权入口 |
| `settings` | `autostart-on`、`autostart-blocked`、`autostart-saving`、`autostart-error` | 自启默认关；保存中禁用、读回后显示开启；保存失败保留旧值；Windows 系统单独禁用时明确提示并给出启动应用设置入口。macOS 无自启项 |
| `settings` | `shortcut-reserved`、`shortcut-error` | 输入法保留键拒绝、注册失败保留原快捷键。默认 Ctrl+Shift+Space 仍是 V-13 真机复核前的候选，不声称已证明与输入法兼容 |
| `onboarding` | `default`、`mic-denied`、`mic-unknown`、`checking`、`granted` | 首次权限、重新检查、试说。未确认/禁用/检查中不可试说；Windows 没有辅助功能系统授权弹窗，允许后再试说 |
| `hud` | `recording` | 116×34 录音胶囊，取消/完成；Windows 不显示 macOS 未授权提示；新的一段可立即开始 |
| `hud` | `higher-integrity`、`protected`、`target-changed`、`helper-unavailable` | 284px 失败卡片，短文案最多两行；复制结果/关闭。管理员窗口不提供“去授权”；主结果区域补充完整原因并保留正文 |
| `hud` | `unverified`、`dismissed` | 不可核验时不报已输入，不弹失败卡片，不提供自动重试；结果可手动复制。关闭提示后结果仍在历史 |
| `tray` | `default`、`first-close`、`hidden`、`autostart`、`exited` | 关闭到托盘、一次性首次关闭确认提示、托盘菜单“打开/退出”、再次启动调出主窗口、自启进托盘、退出。macOS 显示原有 Dock 行为对照 |

共 4 个页面、28 个 Windows 场景。固定设置没有集合空状态；托盘退出、提示已收起是其适用的空状态。按钮悬停/焦点、导航选中沿用共享 CSS；检查/保存中禁用对应操作，错误有明确重试入口。

## 视觉对照与方案 B 裁剪

| 依据 | 对照与变化 |
|---|---|
| 原型 `assets/app.css` 与正式 `desktop/src/renderer/index.css` | 沿用同一系统字体、灰白背景、圆角、边框和阴影；主窗口 1080×750，设置导航约 200px。不引入新的组件库或视觉体系 |
| 正式 `SettingsView.tsx` 的 Group/Row | 分组标题 17px，标签 15px，说明 14px/24px，分组间距 36px、行间距 20px、两列间隔 32px。Windows 新增权限、自启状态；其余设置使用已有完整原型，不复制一套不生效的配置项 |
| 正式 `hud.css` / `hud-view.tsx` | 失败卡宽 284px、字号 13px/18px、两行短文案、24px 操作按钮；保留复制结果与关闭，Mac 对照才有“去授权”。录音胶囊沿用已有尺寸与动效 |
| Windows 窗口与系统托盘 | 补充标准窗口控制示意和托盘菜单；这属于本产品新增平台行为，不虚构第三方产品的 Windows 后台或系统截图 |

已在本地 Chrome 以离线文件实际观察设置页、macOS 权限对照、管理员窗口提示和托盘交互；核对首次关闭只提示一次、菜单可重新打开。截图对照以本会话浏览器工具观察及本地记录为依据，不把浏览器演示当成真实系统操作已通过。真实截图和执行记录不进入公开仓库。

## 交接边界

这里只补本版平台差异，沿用既有登录、历史、词库、后台及其页面规范；不加入 v0.0.3 快速纠正学习。按 [WIN-12](../02-prd/windows-v002-requirements.md)，Windows 设置明确显示本版暂不支持自动纠正学习，已有词库与手动添加仍可用；不关闭其他平台账号偏好。设置侧栏未展示的完整页继续由原原型评审，不因此在正式产品禁用。

原型的复制只显示反馈，不写系统剪贴板；系统设置只提示对应入口，不真的修改权限；自启、托盘、单实例只作状态演示。正式失败提示的 8 秒收起、新录音让位、取消/账号隔离、中文输入法、混合 DPI、真实录音与安装包须进入后续正式设计、用例和真实验收。技术验证门禁未闭合前，不据此登记 S4。

S2 复核：首次关闭使用所属主窗口的信息提示，点击“知道了”后隐藏；重复关闭直接隐藏。原型同步展示确认前主窗仍打开的状态。
