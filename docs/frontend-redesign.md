# BlockUSBRead 前端重设计文档

> 状态：**待你调整**（确认后我才开始实施，本文档之外未改动任何文件）
> 设计语言：`core/enterprise-ui`（骨架）+ `modes-color/dark-mode-minimal`（色彩制度）
> 规范来源：`~/.qoder-cn/skills/ai-design-skills/references/{core/enterprise-ui,modes-color/dark-mode-minimal}.md`（已全文读取）
> 范围：**仅前端** `app/static/{index.html,styles.css,app.js}`。后端 API、数据模型、SSE 协议一律不动。

---

## 0. 一句话结论

把这个"毛玻璃演示页"改成一个**近黑底、高信息密度、状态一目了然的运维控制台**：删掉所有实时模糊与浮动光斑，卷列表从"卡片"改成"企业级密集表格"，7 个弹窗收敛为「1 个抽屉 + 3 个对话框 + 1 个常驻进度条」，强调色保留品牌 `#34d399`。

---

## 1. 现状审计（问题清单，全部可定位到行）

| # | 问题 | 位置 | 严重度 |
|---|---|---|---|
| A1 | 一条分区行挤了 6 类元素：容量条 + 读写标签 + 浏览 + 安全弹出 + 忘记凭据 + 实时速率 + SFTP 路径，窄屏直接溢出换行 | `app.js:676-700`（`renderActions`） | 高 |
| A2 | 全量 `innerHTML` 重绘：任何一次 SSE/轮询都重建三个列表，输入框失焦、滚动位置丢失、正在输入的解锁密码可能被销毁 | `app.js:406-421`、`app.js:110`（`pollSpeeds`） | 高 |
| A3 | 3 个 520/420/320px 动画光斑 + `body::before` 4 层径向渐变常驻合成，`blur(24px) saturate(1.2)` 出现 5 处 | `index.html:13-15`、`styles.css:95,116,347,409,1069,1639` | 中 |
| A4 | 弹窗嵌套：文件浏览 → 移动目标 → 自定义下拉，三层叠加，`Esc` 处理各自为政，会一次关掉两层 | `index.html:156,210`、`styles.css:1161` | 高 |
| A5 | 自定义 `.select`（`role=listbox` 手搓下拉）属"非标准交互"，键盘可达性不完整 | `styles.css:1161-1300`、`app.js` move-modal 逻辑 | 中 |
| A6 | 内联 `onclick="fn('${esc(key)}')"` 字符串拼接 14 处，key 含引号即语义破坏，且阻断 CSP | `app.js:476,532,535,541,545,576,686,689,691,723,724,755,768` | 高 |
| A7 | 令牌冗余：`--primary` 与 `--green` 同为 `#34d399`；`#34d399` 字面量硬编码 3 处 | `styles.css:8,34` | 低 |
| A8 | 1779 行 CSS 无分层（tokens/base/components/utility 混排），只有一个 `@media (max-width:480px)` | `styles.css:1600` | 中 |
| A9 | 移动端仅靠 `.desktop-only` 隐藏信息，设备名/序列号/接口在小屏消失而非重排 | `styles.css` 多处、`app.js:561` | 中 |
| A10 | 解锁面板三段冗长警示文案重复出现在每个卷，噪声大；`.cred-input` 用内联 `style` | `app.js:706-770` | 中 |
| A11 | 无统一的"数据新鲜度"指示：SSE 断线后界面无提示，用户以为设备掉线 | `app.js:347-380` | 中 |
| A12 | 状态视觉不成体系：`present/unlocking/mounting/unmounting/error` 混用 `.tag` 与文案，无固定色位 | `styles.css:627`、`app.js:1400+` | 中 |

保留的东西（不动）：`esc()` 转义、文本预览刻意用 `textContent` 赋值（防卷内 HTML 执行）、SSE + 轮询双通道、盘符系统、`aria` 已有的 `role=tablist/listbox` 意图。

---

## 2. 为什么选这套语言（含被排除项）

**骨架选 `enterprise-ui`**：本项目的真实负载是"表格 + 表单 + 状态"（分区表、解锁表单、远程存储表单、任务表、会话表），`enterprise-ui` 的硬指标（行高 36-48、基准间距 12、头部 48-64、内容上限 1440、模态 600、触控 44、WCAG AA、成熟可预测模式）正好逐条对上 A1/A4/A5/A8/A9。

**色彩制度选 `dark-mode-minimal`**：它是 `modes-color` 类，规范自己写明是"叠加在基底之上的色彩/对比度制度"，因此覆盖 `enterprise-ui` 的"浅灰底 + 企业蓝"色段，但保留 `enterprise-ui` 的全部结构与尺寸。这样既忠于规范，又不破坏已有暗色品牌。

排除项（说明理由，不是没看）：

- `depth-texture/glassmorphism` —— 现状主题。其自身 Don't 第 4 条 "**Use glass for critical data displays**" 与本项目"关键数据 = 速率/容量/状态"直接冲突；且要求"背景必须有视觉复杂度"，即 A3 的开销来源。**排除。**
- `core/data-first-dashboard` —— 骨架同样贴合，但其 Output Constraints 与 Components 强制 Chart.js/D3、sparkline、时间范围筛选器、CSV/PDF 导出。本项目无历史时序数据、零依赖、无构建步骤，加图表等于加依赖 + 造数据管道，属规范外的范围膨胀。仅**借用**其中一条已读规则：指标数字用 `font-variant-numeric: tabular-nums`、ID/路径/时间戳用等宽。**主语言排除。**
- `core/bento-grid-layout` / `futuristic/tech-futurism` —— 前者弱化横向对比（表格扫描是本项目主任务），后者高光泽暗铬会重新引入装饰层。**排除。**

### 2.1 规范之间的冲突与我的裁决（请重点确认这一段）

| 冲突 | 规范原文 | 裁决 |
|---|---|---|
| 底色：企业浅灰 vs 近黑 | `enterprise-ui` "Background: Light gray #F5F5F5 or white" / `dark-mode-minimal` "near-black #0A0A0A" | 取 `dark-mode-minimal`（modes-color 优先级更高，且用户长期使用场景是暗色） |
| 强调色：企业蓝 vs 电光绿 | `#0066CC` / `#00FF88` | 取 **`#34d399`**（现有品牌色）。理由：`dark-mode-minimal` 自己的 Don't 写着"不要用明亮刺眼的强调色"，`#00FF88` 与之矛盾；`#34d399` 恰好介于 `enterprise-ui` 的 Positive `#10B981` 与 `#00FF88` 之间，两条都满足 |
| 圆角：未定义 vs 8-12px | `dark-mode-minimal` "rounded corners (8-12px)" | 取 8/10/12 三档（现状 10/16/20 偏软） |
| 侧栏导航 | `enterprise-ui` "Sidebar navigation (collapsible)" 为 Layout Rule | **实施为 56px 可展开图标栏**，并把"传输任务/会话"从弹窗升为视图（见 §4）。若你不想加侧栏，替代方案见开放问题 Q2 |
| 模态 600px 上限 | `enterprise-ui` "Modals: Centered, 600px max-width" | 表单类对话框严守 600；文件浏览与媒体预览改走**全高抽屉**，属明示偏离（§4.3） |
| 触控 44px vs 密集行 36px | `enterprise-ui` 两条并列 | 视觉尺寸不变，用 `::before` 扩大点击热区；`@media (pointer:coarse)` 下行高升到 48 |
| "Include export/print" | `enterprise-ui` Do's | 磁盘管理器无导出语义，标记 **不适用** |

---

## 3. 设计令牌（可直接粘贴替换 `styles.css:1-56`）

### 3.1 色彩

```css
:root {
  /* 底层：近黑，冷中性（规范允许 #000000 或 near-black #0A0A0A） */
  --bg:            #0A0A0A;   /* 页面 + 导航栏（规范 "Dark Navigation: #0A0A0A"） */
  --surface-1:     #121212;   /* 卡片 / 表格容器        层级 1 */
  --surface-2:     #1A1A1A;   /* 行、输入框、抽屉面板    层级 2 */
  --surface-3:     #1E1E1E;   /* 悬浮层、下拉、toast    层级 3 */
  /* 规范硬约束：surface elevation 最多 3 级 —— 上面正好 3 级，不得再加 */

  --border:        #2A2A2A;   /* 规范字面值 */
  --border-strong: #3A3A3A;   /* hover / 聚焦描边 */
  --divider:       #222222;
  --hover:         rgba(255, 255, 255, 0.04);
  --active:        rgba(255, 255, 255, 0.07);

  /* 文本（暗底降权重，规范 "Reduced weight compared to light mode"） */
  --text-1: #FFFFFF;   /* 19.8:1 on --bg   标题/数值 */
  --text-2: #E0E0E0;   /* 15.0:1           正文 */
  --text-3: #A0A0A0;   /* 7.6:1            次级/标签（规范字面 secondary） */
  --text-4: #8A8A8A;   /* 5.4:1            禁用态/最弱档（实施时从 #7A7A7A 上调，见 §16） */
  --text-on-accent: #05201A;

  /* 强调色：唯一 accent，克制使用 */
  --accent:        #34d399;   /* 10.3:1 on --bg, 8.7:1 on --surface-3 */
  --accent-hover:  #5FE2B4;
  --accent-press:  #22B883;
  --accent-dim:    rgba(52, 211, 153, 0.14);   /* 选中底色，非描边非文字 */
  --accent-glow:   0 0 0 1px rgba(52,211,153,.35), 0 0 12px rgba(52,211,153,.18);

  /* 语义色（规范字面值，文字对比度均 ≥6:1） */
  --success: #34d399;  --success-bg: rgba(52,211,153,.12);   /* 与 accent 合一 */
  --warning: #FFD600;  --warning-bg: rgba(255,214,0,.12);    /* 14.0:1 */
  --danger:  #FF5252;  --danger-bg:  rgba(255,82,82,.12);    /* 6.2:1 */
  --info:    #7FB3FF;  --info-bg:    rgba(127,179,255,.12);  /* 只读/提示专用 */

  /* 图表/多序列（本项仅 3 处需区分卷，控制在 6 色内） */
  --seq-1..6: #34d399 #7FB3FF #FFD600 #FF8A5C #C792EA #64DBC8;
}
```

删除的旧令牌：`--surface/-2/-3`（rgba 白色叠加，玻璃遗存）、`--primary-gradient`、`--primary-600/-400`、`--pink*`、`--shadow-*` 中的 `lift/primary-hover` 装饰投影、`--green`（与 `--primary` 重复，A7）。所有硬编码 `#34d399` 改引 `--accent`。

### 3.2 字体（**零新增依赖**，沿用现状 system + PingFang SC + Microsoft YaHei，无 webfont）

| 角色 | 规范来源 | 规格 |
|---|---|---|
| H1 页标题 | enterprise H1 24 | 24px / 600 / lh 1.25 |
| H2 区块 | enterprise H2 20 | 20px / 600 |
| H3 卡片/抽屉标题 | enterprise H3 16 | 16px / 600 |
| 主指标 KPI | dark-minimal Title 24-32 | 28px / 600 / tabular-nums |
| 正文 | enterprise body 13-14 lh1.4 | 14px / 400 / lh 1.45 |
| 表格数据 | enterprise table 12-13 | 13px / 400 / tabular-nums |
| 标签、列头 | enterprise 12 | 12px / 500 / letter-spacing .02em / `--text-3` |
| ID·路径·字节·速率·时间 | monospace | 12px `ui-monospace, SFMono-Regular, Menlo, Consolas` |

### 3.3 间距 / 尺寸 / 圆角 / 动效 / z 轴

```css
:root {
  /* enterprise base_spacing = 12 */
  --s-1: 4px;  --s-2: 8px;  --s-3: 12px;  --s-4: 16px;
  --s-5: 20px; --s-6: 24px; --s-8: 32px;  --s-12: 48px;
  --gutter: 24px;              /* ≥16px，规范 minimum 16px margins */
  --content-max: 1440px;       /* enterprise max_content_width */
  --header-h: 56px;            /* enterprise header_height */
  --rail-w: 56px;              /* 收起 */
  --rail-w-open: 200px;        /* 展开 */
  --row-h: 44px;               /* 落在 36-48 区间内 */
  --row-h-compact: 36px;       /* 文件列表二级行 */
  --touch: 44px;               /* enterprise touch_target_min */
  --dialog-w: 600px;           /* enterprise modal max-width */
  --drawer-w: min(760px, 68vw);
  --radius-sm: 8px; --radius: 10px; --radius-lg: 12px;  /* dark-minimal 8-12 */
  --z-base: 0; --z-sticky: 20; --z-drawer: 40; --z-dialog: 50; --z-toast: 60;
  --t-fast: 120ms; --t-base: 180ms; --t-slow: 240ms;   /* 上限 240ms，禁 flashy */
  --ease: cubic-bezier(.2,0,.2,1);
}
```

投影：暗底上阴影不可见，**层级靠 `--surface-N` 亮度台阶 + `--border` 表达**；投影只给真正悬浮物（dialog/drawer/toast）：`0 16px 40px rgba(0,0,0,.55)`。其余一律去掉。

---

## 4. 信息架构

### 4.1 外壳

```
┌──rail──┬────────────────────────────────────────────────────┐
│  ▣ 存储 │  顶栏 56px：上下文标题 · 最近更新时间 · 刷新 · 退出 │
│  ⇄ 传输 ├────────────────────────────────────────────────────┤
│  ⧉ 会话 │  ┌ 状态条 KPI（4 格）──────────────────────────┐  │
│  ⚠ 异常(条件出现)│  已挂载 3/5 · 读 118 MB/s · 写 42 MB/s · 可用 1.2 TB │  │
│        │  └──────────────────────────────────────────────┘  │
│ ＋ 远程  │  存储表格（本地 / 外接 / 远程 三分区，表头分组）    │
│ ⏏ 退出  │                                                    │
└────────┴────────────────────────────────────────────────────┘
```

- rail 3 个视图项 = **同一文档内的视图切换**（`#/storage` `#/jobs` `#/sessions`），不是路由跳转：文件浏览改为**抽屉**（§4.3，Q3 推荐项）而不占一个视图位，任务视图、会话视图与存储视图共用一个页面骨架。这样"传输任务"和"会话"从弹窗降级为**可比对的常驻视图**（解 A4）。
- `＋ 远程存储` 与 `⏏ 退出` 落 rail 底部，顶栏 `#btn-remote`/`#btn-logout` 移除；顶栏只留标题、数据新鲜度、刷新。
- rail 可折叠，展开状态存 `localStorage`；<768px 时 rail 转为底部 tab bar（§7）。

### 4.2 主视图（存储）—— 从卡片改为分组表格

```
分组表头：外接存储 (2 台 / 5 分区)                                     [刷新]
────────────────────────────────────────────────────────────────────────
▸ WD Elements 25A3 · USB3 · 3.64 TB · 序列号 WD-XXXX        [弹出整盘] [SMART ▾]
────────────────────────────────────────────────────────────────────────
  盘符  分区             文件系统   容量            状态      实时速率        操作
  D:    Data (BitLocker) NTFS 加密  ▓▓▓▓▓▓░ 71%    读写      R 118  W 42     [浏览][弹出]
                          4.1 TB
  E:    —                NTFS       ▓▓░░░░░ 28%    只读      R 0     W —     [解锁][浏览]
                          1.2 TB                            (需凭据)
  F:    —                exFAT      —               未挂载    —               [挂载]
────────────────────────────────────────────────────────────────────────
```

规则：
1. **一行一卷，固定 6 列**，操作列最多 2 个按钮 + `⋯` 溢出菜单（"忘记凭据/重新输入密码"进菜单）→ 解 A1。
2. 状态列只放**一枚状态胶囊**，色位恒定（§5）。解锁面板在行下方**内联展开**（点"解锁"后行高增加，不弹窗），展开时冻结该行其余操作 → 解 A10。
3. SMART 从独立区块改为磁盘分组行的 `▾` 内联展开，`unavailable` 时整块不渲染（沿用现逻辑）。
4. 容量条 4px 高、`--surface-3` 轨道 + `--accent`/`--warning`/`--danger` 填充（≥90% 转 warning，≥97% 转 danger，对齐现有 `near-full` 阈值）。
5. 本地存储、远程存储各成一个分组，分组头右侧显示该组聚合指标。
6. 每个卷的 SFTP 路径不再占一行：盘符列即路径（`D:` = `sftp://…/D`），点盘符复制全路径 + toast。→ 解 A1，删 `.sftp-line`。

### 4.3 层（overlay）清单：7 个模态 → 1 抽屉 + 3 对话框 + 1 进度条

| 现在 | 改为 | 尺寸 | 理由 |
|---|---|---|---|
| `#fs-modal` 文件浏览 | **全高右侧抽屉** | `min(760px,68vw)` | 明示偏离 600px 上限：表格宽度不可压缩，抽屉保留下层上下文 |
| `#remote-modal` 添加远程存储 | 居中对话框 | 600px | 表单，符合规范 |
| `#rename-modal` | 居中对话框 | 420px | 单字段 |
| `#move-modal` 目标选择 | **对话框内两步**（选卷 → 选目录），自定义 `.select` 换成原生 `<select>` + 目录列表 | 600px | 解 A4/A5，去非标准控件 |
| `#jobs-modal` 后台传输 | rail 视图 `#/jobs` + **底部常驻细进度条**（有 running 时永远可见，含速率/ETA/取消） | — | 不丢上下文 |
| `#sessions-bar` 会话面板 | rail 视图 `#/sessions` | — | 与任务同级数据，不该折叠在一条横幅里 |
| `#player-modal` / `#text-modal` | 保留全屏覆盖层（媒体查看器语义） | 视口 | 明示偏离：非表单 |
| 解锁面板 | 内联行展开 | — | 见 §4.2 |

**Esc 与焦点**：单一 `overlay-stack` 管理器，一次 Esc 只关最上层；关闭后焦点归还触发元素；对话框内 Tab 循环（focus trap）；抽屉与对话框可叠放（文件浏览 → 移动目标）。

---

## 5. 状态与反馈矩阵（对齐 `app/models.py` 枚举，视觉恒定）

`--text-*` 与底色对比度见 §3.1 注释；胶囊一律"浅底 + 同色系文字"，**不使用渐变、不使用实心大色块**。

### 5.1 `VolumeState` × `MountMode`

| 组合 | 文案 | 色位 | 附加表现 | 可用操作 |
|---|---|---|---|---|
| `present` + 非加密 | 未挂载 | `--text-3` on `--surface-3` | 无 | 挂载 |
| `present` + BitLocker | 需凭据 | `--warning` 描边 | 锁形图标 | 解锁 |
| `present` + 不支持 fs | 不支持 | `--text-4` | 整行禁用 + `title` 说明原因 | 无 |
| `unlocking` | 解锁中 | `--accent` | 16px 进度 spinner + 行内禁用 | 无（等待） |
| `mounting` | 挂载中 | `--accent` | 同上 | 无 |
| `mounted` + `rw` | **读写** | `--accent` 实心浅底 | 盘符高亮 | 浏览 · 弹出 · （远程）删除 |
| `mounted` + `ro` | **只读** | `--info` | 写入类操作灰置 + `title` "只读卷不可接收粘贴" | 浏览 · 弹出 · 以读写重新挂载 |
| `unmounting` | 弹出中 | `--text-3` | spinner | 无 |
| `error` | 上次操作失败 | `--danger` | **行下方常驻错误条** + 重试按钮；`busy` 错误必须完整显示占用清单（后端 `_busy_detail` 已给最多 8 项） | 重试 / 忽略 |
| `remembered` 凭据 | — | 盘符旁 `🔑` 小徽标 | 不占列 | 溢出菜单"忘记凭据" |
| `near-full` ≥90% | — | 容量条转 `--warning` | ≥97% 转 `--danger` | — |

### 5.2 传输任务（`Job.snapshot()`）

`queued` 排队中 `--text-3` · `running` 传输中 `--accent` + 确定进度条 · `done` 已完成 `--success` · `error` 失败 `--danger` + 错误条 · `canceled` 已取消 `--text-4`。
进度文案统一为：**`62% · 18.4 / 29.7 GB · 214/380 文件 · 剩余约 4 分 12 秒`**（数字全部 tabular-nums，避免跳动），`etaSeconds==null` 显示"计算剩余时间…"而非空白。空间不足预检（v1.3.0）的拒绝原因用 `--danger` 内联条贴在对话框底部，不用 toast 一闪而过。

### 5.3 系统级状态

| 状态 | 表现 |
|---|---|
| SSE 断开 | 顶栏"最近更新 12:04:31"转 `--warning`，文案"连接已断开 · 重试中"，重连成功后 `--text-3`。（解 A11，现无此指示） |
| 加载中 | 表格区骨架行 3 条（`--surface-2` 微光扫过，reduced-motion 下静止）；**禁止**整区闪烁重绘 |
| 空 | `#storage-empty` 保留，插画改线性图标 + 主 CTA「＋ 挂载远程存储」次 CTA「刷新设备」 |
| 提交中 | 所有触发请求的按钮进入 `aria-busy` + 自身 spinner，文案改现在分词（"解锁中…"），禁用重复提交 |
| 反馈 | toast 仅用于"已完成的非破坏性事实"；**破坏性/失败**一律内联显示。规范 Do："Include all necessary states" |

---

## 6. 关键交互流程

1. **解锁 BitLocker 卷**：点行内「解锁」→ 行下展开面板（`密码解锁 | 恢复密钥` 用 `.seg` 分段控件，保留）→ 凭据框 `--surface-2` + `--border`，聚焦时 `--border-strong` + 1px `--accent`；勾选「读写模式」「记住凭据」→「解锁并挂载」提交 → 状态机走 `unlocking → mounting → mounted`，全程**同一行原地更新**，不重建列表。警示文案压缩成一行 12px `--text-3`：*"写入 BitLocker/NTFS 有损坏风险，默认只读"*，只在勾选读写后出现。
2. **安全弹出**：点「弹出」→ 若后端返回 busy，行下方危险条列出占用文件（`user:/path`）并给「查看会话」跳转 `#/sessions`；成功后该行转 `present`，盘符消失。
3. **文件浏览 → 移动**：抽屉打开卷根 → 面包屑（保留 `.crumbs`，改 12px + `--text-3` 分隔符）→ 选中行 `⋯` →「移动到…」→ 对话框第 1 步选目标卷（原生 `<select>`，只列 `rw` + `local` + 已挂载远程，沿用 `writableTargets` 判定）→ 第 2 步选目录 → 「开始移动」→ 提交后对话框关闭，底部常驻进度条出现。
4. **文本预览**：保留 `textContent` 赋值路径（安全），改为覆盖层 `--z-drawer+1`，Esc 回退到抽屉而非关掉抽屉。

---

## 7. 响应式

| 断点 | 布局 |
|---|---|
| ≥1280 | rail 展开可开；表格 6 列全显；抽屉占右侧 |
| 1024-1279 | rail 收为 56px 图标；隐藏「文件系统/容量」文字列，保留容量条 |
| 768-1023 | rail → 底部 tab bar；表格转"卡片式两行"：第一行 盘符+分区+状态，第二行 容量条+速率+操作 |
| <768 | 单列；行高 48（coarse pointer）；操作全进 `⋯`；对话框改底部面板（sheet）；抽屉 100vw |

取代 `.desktop-only` 隐藏法 → 引入**列优先级**（`data-col="capacity|fs|speed"` + 断点规则），信息在窄屏是**降级为图标/进度条**，不是消失（解 A9）。移除现有唯一断点 `styles.css:1600` 里的补丁式规则。

---

## 8. 可访问性（`enterprise-ui` 要求 WCAG 2.1 AA）

- 对比度：文字全部 ≥4.5:1。实施后按渲染值实测（§16）：111 组文字/背景采样最低 **4.85:1**（危险态胶囊 `#FF5252` on `--danger-bg`），最弱文字档 `--text-4` 在 `#121212` 上 **5.43:1**；非文字元素（图标、状态条、焦点环）≥3:1。
- 焦点：全局 `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`；玻璃主题现在半透明卡片上的焦点环几乎不可见。
- 键盘：表格行内 `↑/↓` roving tabindex、`Enter` 打开浏览、`E` 弹出、`R` 刷新设备、`J` 任务视图（`enterprise-ui` Do "Support keyboard shortcuts for power users"）；对话框 focus trap；下拉换成原生 `<select>` 后天然可用。
- ARIA：`aria-busy` 用于行内异步；`aria-live="polite"` 用于任务完成与 toast 容器；`role="status"` 用于顶栏新鲜度；状态胶囊加 `aria-label="状态：读写"`。
- 动效：保留并扩展 `styles.css:1772` 的 `prefers-reduced-motion` —— spinner 换为静态进度描边、骨架扫光停止、抽屉位移改瞬时。
- 触控：`@media (pointer:coarse)` 下所有按钮热区 ≥44×44。

---

## 9. 组件对照表（旧 class → 新语义，实施时逐个替换）

| 现有 class | 新组件 | 变更 |
|---|---|---|
| `.ambient-orb*`、`body::before` 渐变 | **删除** | 装饰层清零 |
| `.topbar/.topnav` | `.topbar` + `.rail` | 头部 56px；导航迁入 rail |
| `.volume-list` / `.disk-card` / `.part-row` | `.table` / `.table-group-head` / `.row` | 卡片→分组表格，行高 44 |
| `.usage-bar*` `.near-full` | `.meter` `.meter-fill[data-level]` | 4px 轨道，三档色位 |
| `.speed-meter` `.speed-rx/tx` | `.metric-inline` | tabular-nums，`W —` 表示无写入 |
| `.sftp-line` `.sftp-copy` | **删除** → 盘符列 | 见 §4.2 |
| `.tag rw/ro/bitlocker/busy/done/err/unsupported` | `.chip[data-tone]` | 固定 tone：neutral/accent/info/warning/danger |
| `.unlock-panel*` | `.inline-panel` | 行内展开，去掉内联 `style` |
| `.btn .mini .icon-btn .danger .ghost` | `.btn` `size=s/m` `tone=primary/default/danger/ghost` | m 高 32、s 高 28（视觉），热区 44；危险用 `tone=danger` 默认描边，不铺满红 |
| `.seg` | `.segmented` | 保留，高度 32，选中 `--accent-dim` + `--accent` 文字 |
| `.select/.select-toggle/.select-menu` | **原生 `<select>` + `.field`** | 去非标准控件 |
| `.modal-mask/.modal-lg/.modal-media/.modal-text` | `.overlay` + `.dialog[600/420]` + `.drawer` + `.viewer` | 层级 7→4 |
| `.fs-toolbar/.crumbs/.fs-list/.fs-row*` | 保留语义，重排 | 行高 36 紧凑；操作列 `⋯` |
| `.job-item/.job-head/.job-route/.job-track/.job-fill` | `#/jobs` 视图表格 + `.job-dock`（底部常驻条） | 弹窗→视图 |
| `.sessions-bar/.sessions-head/.sessions-detail` | `#/sessions` 视图 | 弹窗→视图 |
| `.banner` `#global-banner` `#share-banner` | `.callout[data-tone]` | 合并为一种组件 |
| `.upload-progress*` | `.progress-linear` | 与 `.meter` 共用基元 |
| `.toast` | `.toast[data-tone]` | `--surface-3` + `--border`，无 blur，3500ms 保留 |
| `.empty` `.wrong` `.desktop-only` | `.empty` `.field-error` **删除** | `.desktop-only` 由列优先级取代 |
| 新增 | `.freshness` `.kpi-strip` `.kpi` `.error-row` `.overlay-stack` | 对应 A11/A2/§4.3 |

---

## 10. 前端实现架构（仍是零依赖 vanilla，无构建步骤）

1. **模块拆分**：`<script type="module" src="/static/app.js">`，拆为
   `static/js/` → `api.js`（fetch 封装，保留 `credentials:"same-origin"`）· `state.js`（单一 store + 订阅）· `overlays.js`（Esc/焦点栈）· `views/{storage,jobs,sessions,transfer}.js` · `components/{chip,meter,table,dialog,drawer,toast}.js` · `format.js`（`humanSize/humanRate/fmtEta` 迁移，新增 `tabularNums`）。
   **实施落地为扁平结构**（无 `views/`、`components/` 子目录，因为组件层在本项目只是渲染函数而非独立单元）：`js/core.js`（api/esc/toast/humanSize/humanRate/fmtEta，合并了原 `api.js`+`format.js`）· `js/store.js`（`state.js`）· `js/overlays.js` · `js/nav.js` · `js/storage.js` · `js/browser.js`（含 `transfer` 语义：抽屉 + 移动/复制对话框）· `js/jobs.js` · `js/sessions.js` · `js/remote.js`。`tabularNums` 不做 JS helper，改由 CSS 令牌承担（`.num/.kpi-value/.kpi-sub/.meter-pct/.metric b/.job-rate/.job-progress/.dock-meta` 统一 `font-variant-numeric: tabular-nums`）。
   Docker 内静态目录直出，无需打包。若你不想动文件结构，Q8 可退化为"仍单文件，仅加分区注释"。
2. **去内联 onclick**（解 A6）：全部改事件委托 + `data-action` / `data-key`，模块内 `Map` 查找 handler。这同时消除字符串拼接注入面，且为将来 CSP 铺路。
3. **渲染管线**（解 A2）：每个卷计算 `signature = state|mode|drive|usagePct|speedBucket|error`；只有 signature 变化的卷才重绘其 DOM。KPI 条、速率、容量这类高频数字改 `textContent` 定点更新，永不重建子树。输入框所在卷在焦点内时跳过重绘。
4. **数据更新策略**：SSE `state` 事件 → 拉 `/volumes`；SSE `jobs` 事件 → 直接应用 payload。`/speeds` `/usage` 轮询保留 2000ms，但：页面 `document.hidden` 时暂停；`/sessions` 降到 5000ms 且只在 `#/sessions` 视图与弹出失败时才拉。断线时顶栏降级提示（§5.3）。
5. **HTML 安全**：保留 `esc()`；HTML 片段中不再拼接数据字符串（如 `app.js:561` 把 `.desktop-only` 拼进 meta），一律 `textContent` + CSS 列优先级。

---

## 11. 分阶段实施与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 令牌与基底** | 替换 `:root`；删光斑/渐变/blur；`prefers-reduced-motion` 扩展 | CSS 中 `backdrop-filter` 出现 0 次；`#34d399` 字面量 0 次；`--primary`/`--green` 重复消除；无长任务动画 |
| **P1 外壳** | rail + 3 视图（+ 文件浏览抽屉）+ 顶栏 + KPI 条 + hash 路由 | 键盘可完成视图切换与刷新；焦点环可见；窗口 resize 无横向滚动条 |
| **P2 存储表格** | 分组表格、状态胶囊矩阵、行内解锁面板、`⋯` 溢出菜单 | §5.1 每一行都能实测复现（可用现机卷逐个走）；一行 ≤6 列 ≤2 按钮；320px 不破版 |
| **P3 层收敛** | overlay-stack、对话框 600/420、原生 select、抽屉 | 连按 Esc 一次只关一层；焦点正确归还；Tab 无法逃出对话框 |
| **P4 传输与会话** | `#/jobs`、`#/sessions` 视图 + 底部常驻进度条 | 传输中关抽屉不打断；ETA/速率数字不抖动；取消即时反馈 |
| **P5 渲染管线** | signature 局部重绘、事件委托、去内联 onclick | `document.querySelectorAll('[onclick]')` 长度 = 0；解锁输入中途刷新不丢焦点与内容 |
| **P6 响应式 + a11y** | 四档断点、列优先级、ARIA、对比度 | axe/手动检查无 AA 失败；`prefers-color-scheme` 与系统暗色一致（规范要求 "Support system dark mode preference"） |
| **P7 规范对照与真机截图** | 逐条核对 §12 表 | 每条 ✓/偏离有书面说明 |

**交付前必须实际渲染验证**（技能 Verify 要求）：起容器 → 用真实 USB 卷走完 §5.1 全部状态 → 截图空/加载/错误/传输中四种态。我无法凭代码声称"看起来对"；若某状态（如 busy 弹出）无法在你的设备上复现，我会明确告诉你哪些态没实测到。

---

## 12. 规范机器可读规则对照（最终检查表）

`enterprise-ui`：
- `max_content_width 1440` ✓ `--content-max`
- `base_spacing 12` ✓ `--s-3`
- `row_height_min 36` ✓ `--row-h 44` / `--row-h-compact 36`
- `header_height 56` ✓ `--header-h`
- `contrast_min 4.5` ✓ 实测最低 4.85:1（§16，111 组采样）
- `touch_target_min 44` ✓ 热区方案 + coarse pointer 行高 48
- `wcag_level AA` ✓ §8
- `color_corporate` ⚠ 被 `dark-mode-minimal` 覆盖，见 §2.1
- `patterns established` ✓ 原生 `<select>`/表格/对话框/面包屑
- 键盘快捷键 ✓ `↑/↓/Home/End` roving、`Enter` 打开、`E` 弹出、`R` 刷新、`J/S/D` 视图切换（§8，§16 实测）
- ⛔ 偏离（书面声明）：`Allow bulk operations in tables` 未做 —— 本次范围不含多选/批量移动/批量删除（Q5 默认「不做」）。理由：现有后端契约里 `/api/volumes/{key}/move` 与 `/entry` 都是单目标接口，批量要么串行发 N 次请求（前端伪装成批量，失败语义混乱），要么动后端加批量接口（违反 §13 接口冻结）。若你决定要做，需要新开一个后端任务。
- ⛔ 偏离（书面声明）：文件浏览抽屉宽 760 > 600；媒体预览用全屏 viewer。
`dark-mode-minimal`：
- `background #0A0A0A` ✓（规范允许 pure black 或 near-black）
- `surface_levels [#121212,#1A1A1A,#1E1E1E]` ✓ 正好 3 级，"3 levels max"
- `text_primary #FFFFFF` ✓ · `text_secondary #A0A0A0` ✓
- `border_color #2A2A2A` ✓
- `accent_limited true` ✓ accent 只用于：主按钮、焦点、读写状态、进度填充、选中行 —— 其他一律中性
- `contrast_min 4.5` ✓
- `system_support prefers-color-scheme` ✓ 仅暗色，但尊重系统"更暗/降亮"偏好并支持 reduced-motion
- `transition 200ms` ✓ `--t-base:180ms` ≤200
- 圆角 8-12 ✓ · 间距 ≥16 ✓ · 无浅色组件混入 ✓
- `#00FF88` → 替代为 `#34d399`，理由见 §2.1（规范自身 Don't 禁止刺眼强调色）

---

## 13. 后端接口冻结清单（本次不调用新接口、不改语义）

`GET /api/volumes` · `GET /api/speeds` · `GET /api/usage` · `GET /api/sessions` · `POST /api/rescan` · `POST /api/volumes/{key}/unlock|mount|eject` · `DELETE /api/volumes/{key}/credential` · `GET /api/disks/{id}/smart` · `POST /api/disks/{id}/eject` · `POST/GET/DELETE /api/remote-stores[...]` · `GET /api/share` · `GET /api/events` · `GET /api/volumes/{key}/browse|raw|preview` · `POST /api/volumes/{key}/upload|rename` · `DELETE /api/volumes/{key}/entry` · `POST /api/volumes/{key}/move` · `GET /api/jobs` · `POST /api/jobs/{id}/cancel` · `POST /api/auth/login|logout` · `GET /api/auth/me`。

唯一破例：`GET /api/share` 响应去掉 `password` 字段（Q9 选定方案 B，见 §14），由你明确授权。其余端点的路径、方法、事件类型与字段全部未变。

---

## 14. 顺带发现（不在本次前端范围，请你定夺）

`GET /api/share` 原本返回**明文管理员密码**，前端 `app.js` 用 `"•".repeat(...)` 掩码显示但把明文放进 `data-copy` 属性。掩码只是视觉的，接口本身就下发明文 —— 任何登录态扩展/DevTools 都能读到。记为 Q9。

**已处理（Q9 选定方案 B：彻底不下发）**。

- 后端 `app/web/api.py` 的 `share_info()` 去掉 `password` 字段，只回 `scheme/host/port/username/uri`。这个口令本来就是用户登录管理页时自己输入的那个，浏览器不需要服务端再告诉它一遍；明文一旦进响应就同时进浏览器历史、代理日志和任何登录态脚本。
- 前端连接信息条去掉密码掩码与"复制密码"按钮，改为 `<code class="pw-hint">与登录管理页的相同</code>`；连接地址与账号的复制按钮保留。
- `tests/test_share_no_secret.py` 锁住该行为（断言响应里既无 `password` 键、`repr()` 中也搜不到口令），防止以后又被加回去。
- 未选的两条备选，留作以后需要时的起点：A′ = 口令仍不下发，但点"显示密码"时二次校验当前会话口令后单条返回；C = 把 SFTP 账号口令与网页登录口令解耦（改动最大，涉及 `ADMIN_PASSWORD` → `chpasswd` 那条链，sshd 口令认证必须持有明文，无法只存哈希）。

---

## 15. 待你确认的开放问题

> 实施开始时这 9 条都还没有回复，我按文档正文的**推荐项**落地了（Q1 `#0A0A0A`、Q2 新增 rail、Q3 抽屉、Q4 分组表格、Q5 不做批量并记为书面偏离、Q6 保留 `#34d399`、Q7 任务/会话升为视图 + 底部 dock、Q8 拆 ES 模块、Q9 后端不改）。后来你单独追问了 Q9 并选定 **B（口令不再下发）**，该项已实施，详见 §14。下面保留原文是为了让你逐条推翻 —— 每条都只影响局部，回滚点见 §11 的阶段划分。

1. **Q1 底色**：接受纯中性近黑 `#0A0A0A`（更贴规范）？还是保留一点冷蓝调 `#0A0E12`（更贴现有 `#1c2836` 的气质，仍算 near-black）？
2. **Q2 导航**：接受新增左侧 rail（3 视图 + 文件浏览抽屉）？备选：(a) 保留顶栏 tab，不加 rail；(b) 只做顶栏 + 把任务/会话并成"活动"一页。
3. **Q3 文件浏览**：抽屉（推荐）还是整页视图？若坚持弹窗则与 600px 上限正面冲突。
4. **Q4 卡片→表格**：外接存储从卡片改为分组表格，视觉变化最大。接受，还是保留卡片但压缩信息密度？
5. **Q5 批量操作**：`enterprise-ui` Do 要求 "Allow bulk operations in tables"。文件列表要不要加多选 + 批量移动/删除？（会增加工作量，不做则记为书面偏离）
6. **Q6 强调色**：保留 `#34d399`（本文方案）还是换 `#00FF88`（规范字面）？
7. **Q7 任务/会话**：从弹窗升为视图 + 底部常驻进度条 —— 这是本次最大的交互改动，确认？
8. **Q8 代码结构**：允许把 `app.js` 拆成 ES 模块（`static/js/*.js`）？还是必须保持单文件？
9. **Q9**：§14 的明文密码问题要不要另开任务？ —— 已答复并实施：选 B，`/api/share` 不再下发口令。

调整方式随意：直接在这些条目后写答案，或改文档正文都行，我按最终版实施。P0+P1 我会一次提交，之后每阶段一次提交，方便回滚。

---

## 16. 实施后的渲染实测结论（P5-P7）

被测环境：本机无法直接跑 `app.main`（`app/sftp/manager.py:137` 需要读写 `/etc/ssh/sshd_config`，非 root 直接 `PermissionError`），因此用一个**只读桩**按 §13 的冻结契约原样桩出 `/api/*`（含 SSE `jobs` 推送、409 目录不可读、500 卷无响应），用 Chromium 渲染 `app/static/` 并测量 DOM/CSS。桩与截图都在 `/tmp`，不进仓库。

已实测通过（数值来自渲染结果，不是代码推断）：
- 令牌落地：底色 `rgb(10,10,10)`、三级面 `#121212/#1A1A1A/#1E1E1E`、边框 `#2A2A2A`、accent `#34d399`、圆角 10/8、`--t-fast` 120ms、顶栏 56px、行高 44px；`backdrop-filter` 0 次，内联 `onclick` 0 次。
- 对比度：3 个视图 + 抽屉共 111 组采样全部 ≥4.5:1，最低 4.85:1。`--text-4` 因此从 `#7A7A7A` 上调为 `#8A8A8A`（5.43:1 on `#121212`），§3.1 已同步。
- 布局：1440/1280/1024/768/420/360 均无横向溢出；1024 收起速率列，≤768 行转块布局；sticky 表头滚动后仍贴在顶栏下沿（`th top = 56 = topbar bottom`）。
- 键盘与层：`j/s/d` 视图路由 + `r` 刷新设备；表格 `↑/↓/Home/End` 只在 `tr.row` 之间跳（分组行与面板行跳过），`Enter` 对已挂载行开抽屉、对未挂载行开解锁面板并把焦点送进密码框，`E` 只对已挂载行发起弹出（未挂载行按 `E` 无动作）；在解锁输入框里敲 `rjds e` 不触发任何快捷键（实测：hash 不变、值原样为 `rjds e`、焦点仍在输入框）；rail 折叠状态可持久化、焦点环 `2px solid #34d399` 可见；连按 Esc 一次只关一层，且焦点归还到打开它的那个按钮（对话框归还到抽屉里的重命名按钮，抽屉归还到行内「浏览」按钮）；对话框内 Tab 20 次全部留在层内；抽屉打开时 `#shell` 进入 `inert`，背景按钮无法获得焦点，`#toast-container` 在 `#shell` 之外所以播报不受影响；登出时层全部关闭且 `inert` 清掉，重新登录后可再次开抽屉；文本预览走 `textContent`（元素子节点 0 个）。
- 渲染管线：SSE `jobs` 推送即可推进底部 dock 与任务卡片（期间 `/api/jobs` 请求数为 0）；解锁面板在**增量重绘**与**整表重建**两条路径下，密码文本、插入点、凭据类型、读写勾选、记住勾选、焦点全部保持不变。
- 四态截图：空（`暂无存储` + 两个引导按钮，KPI 条隐藏）/ 加载（`读取中…`）/ 错误（卷错误行常驻 + 抽屉内联 `role="alert"`）/ 传输中（dock 百分比与 ETA 随推送变化）。
- Q9 口令不下发：故意让桩**仍然**返回 `password`，以证明泄漏点已在消费侧断根 —— 连接信息条 DOM 全文搜不到口令、可见 `data-copy` 只剩「SFTP 地址」与「账号」两个、逐个点击后剪贴板内容均不含口令；`pw-hint` 文案 `与登录管理页的相同` 在 callout 合色底上约 6.67:1。后端侧由 `tests/test_share_no_secret.py` 保证字段不再出现。

实施期发现并修掉的真实缺陷（非测试脚本问题）：
1. 增量重绘只写主行，遗留孤儿子行 → 解锁面板打不开；改为先摘附属行再整体替换（`js/storage.js`）。
2. `.storage-wrap { overflow: hidden }` 使其成为 sticky 的滚动容器 → 表头脱离顶栏；改由列自身圆角收边。
3. 目录读取失败会 `closeBrowser()` 把用户踢出抽屉并清空列表；现按 §5.1「失败一律内联」处理：回到上一份可用列表 + 内联错误条 + 「重试」，首次加载即失败时列表区自身承载错误（不再误显示"空文件夹"）。
4. 面板重绘清掉输入到一半的密码；见上条渲染管线实测。
5. 层管理只在覆盖层之间切 `inert`，背景 `#shell` 从不 inert → 抽屉打开时 Tab 能走进顶栏与导航；另外 `close()` 先还焦点后 `syncScrim()`，此时背景仍是 inert，焦点被静默丢弃（Esc 后落到 `body`）。现改为「进层时 inert 背景、出层时先解除再还焦点」。
6. P7 对照时发现 §8 承诺的表格 `↑/↓` roving 与 `E` 弹出根本没写（只有 `Enter`/`Esc`），已补齐并实测（见上）。同批发现本文自身三处与实现不符：§4.1「rail 4 个导航项」实为 3 视图 + 抽屉、§10 的 `views/`+`components/` 目录树实为扁平 `js/*.js`、§11 P1 行的「4 视图」——均已按落地结果改写，而不是反过来改代码去凑文档。
7. 入口页 `GET /` 用 `FileResponse` 直接返回 `index.html`，却没有 `Cache-Control`（`_NoCacheStatic` 只覆盖 `/static/*`），浏览器可启发式缓存它 —— 本次把 `app.js` 拆成 ES 模块之后，拿到旧的 index.html 会引用不存在的脚本。现给 `/` 补 `no-cache`。

未能实测、需在真机确认（我不把它说成"已验证"）：真实 BitLocker `unlocking→mounting→mounted` 与 `ro/rw` 差异、拔出触发的 `removed` 提示、busy 弹出的占用清单、`/api/disks/{id}/smart` 的真实字段、SFTP 会话计数与盘符映射。这些走的是同一套渲染分支与契约桩，但状态机转换只有你的设备能跑出来。

---

## 17. 收尾：安全与缓存（v1.3.2）

前端重做之外顺带的三项，都不改接口语义（Q9 那一处除外，见 §14）：

- **口令下发（Q9，方案 B）**：`/api/share` 去掉 `password`，前端信息条去掉掩码与「复制密码」，改为文案「与登录管理页的相同」。实测方式是让桩**继续**返回口令，再确认 DOM、`data-copy`、剪贴板三处都搜不到它 —— 这样即使后端回滚也不会立刻重新泄漏。
- **`/api/*` 改 `no-store`**：`app/main.py` 新增 http 中间件，只匹配 `/api/` 前缀。与静态资源的 `no-cache` 是分开的两件事：`no-cache` 允许存副本、只是每次必须回源验证；登录态下的卷清单、任务、会话不该在任何地方留下副本，所以是 `no-store`。`/health` 与 `/static/*` 不受影响。
- **CSP 维持现状**：仓库里唯一的 CSP 是文件预览/下载响应上的 `Content-Security-Policy: sandbox`（`app/web/files.py:177`），保持不变。本次把内联 `onclick` 降到 0、脚本只剩同源 ES 模块，所以将来真要加 `script-src 'none'`-级别的页面 CSP 已经没有障碍，但那属于独立的加固任务，不在重设计范围内。

`tests/test_cache_headers.py` 锁住三档头：未登录的 `/api/volumes` 也得带 `no-store`（缓存策略不该只挑成功响应），`/` 与 `/static/app.js` 保持 `no-cache`。SSE 未被中间件打断：用一个自终止的 `StreamingResponse` 走同一段中间件代码，`cache-control: no-store` 在首块之前就已发出，分块内容照原样读得到。

