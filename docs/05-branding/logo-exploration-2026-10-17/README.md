# Smart Voice Input Logo 候选 · 2026-10-17

状态：用户已选定根目录 `logo.png`（V2 / FOLDED VOICE）作为当前 App Logo，并已派生 ICNS、ICO 与各端界面图标；`logo2.png`（S2）保留备选。接入记录见根目录 [LOGO.md](../../../LOGO.md)，归档预览为 [app-icon-1024.png](app-icon-1024.png) 与 [app-mark-128.png](app-mark-128.png)。

## 查看

打开 [index.html](index.html)：上排 S1、S2、S3，下排 V1、V2、V3；点击卡片查看原图。每张 PNG 是包含 App 图标与单色示意的展示卡，不是可直接打包的生产图标。

## 调研与设计依据

- [Apple：App icons](https://developer.apple.com/design/human-interface-guidelines/app-icons/)：简化核心形态、保持中心内容与小尺寸辨识、跨外观保留核心特征。
- [Microsoft：App icon design](https://learn.microsoft.com/en-us/windows/apps/design/iconography/app-icon-design)：平衡轮廓、避免粗细两极、控制渐变与视觉层次。其通用少用文字的建议不直接套用于本轮用户明确要求的 S/V 字母标志。
- [Linear：Brand Guidelines](https://linear.app/brand)：给予标志足够留白，区分独立标志与图标容器。
- [Stripe：Brand Assets](https://stripe.com/newsroom/information)：明确浅／深底上的有限品牌用色。用于思考配色系统，不借用其字形。
- 用户提供的 App 图标截图：作为复杂度、材质和桌面呈现参考，不复制其中品牌资产。
- 本会话上一轮 S/V 探索图：作为本轮形态延续依据。

以上是针对性网络调研，不代表穷尽全网设计或已完成商标近似检索。

## 六款方案

| 编号 | 方向 | 配色与线条 | 前景宽／高目标占比 |
| --- | --- | --- | --- |
| S1 | 利落声流 | 电光蓝 · 斜切端点 · 收紧转弯 | 60% / 68% |
| S2 | 折带声流 | 蓝紫与冰蓝 · 宽带折转 · 轻曲面 | 64% / 66% |
| S3 | 柔和声流 | 珊瑚橙 · 饱满曲线 · 圆润端点 | 58% / 64% |
| V1 | 轻扬双笔 | 紫罗兰 · 左重右轻 · 开放留白 | 66% / 60% |
| V2 | 一体折带 | 青绿与深墨 · 连接底部 · 折面层次 | 64% / 62% |
| V3 | 几何切面 | 石墨黑 · 连体几何 · 紧凑轮廓 | 62% / 58% |

占比指字母外接框相对图标底板的宽／高，不指填色面积。目标占比与色值均由提示词指定；当前生成稿存在偏差，未作为最终实测值。
V3 初稿与 V1 相似，另做几何连体版本以增加造型差异；V3.png 为本轮最终候选，初稿未作为第七款交付。

## 生成与检查

- 使用内置 imagegen；六款分别生成，V3 追加一次定向修订。
- 完整提示词、编号和来源路径见 [prompts.json](prompts.json)。
- 已在会话中查看生成图，核对 S/V 方向、编号、配色、留白及形态差异。
- 单色小标为模型绘制的示意，部分轮廓与主图并非像素级一致；没有完成菜单栏真实小尺寸验证。
- 选定后已从展示卡提取彩色图标、去除白底并生成原生安装图标；未重新绘制矢量几何，也未单独校准精确色值、纯色与反白适配。
- 已完成 macOS arm64 DMG 构建并挂载核对图标资源；真实 Dock、Windows 开始菜单/任务栏与多缩放目检仍待执行。

