# 强力模式（Turbo）设计方案

> 状态：已确认并实现 · 目标位置：传输任务视图工具栏（红框处，紧邻「全部 / 进行中 / 已结束」筛选组左侧）
> 文案：极速模式（Turbo） · 绿色 = 未开启 · 队列空闲 5 分钟自动关闭 · 重启恢复关闭

## 1. 目标与范围

在传输任务视图加入「强力模式」开关，一键放弃后台传输的限速与让路策略，让传输跑满磁盘带宽。

- **做**：一个开关按钮（绿 / 红两态）、实时生效、多端同步、状态可见。
- **不做**：不改变「同一时间只执行一个任务」的串行策略；不做单任务级开关；不绕过任何数据安全校验（目标挂载校验、回滚规则保持不变）。

## 2. 交互与视觉

### 2.1 位置

传输视图 `#view-jobs` 的 `.view-head` 内，位于说明文字与筛选组之间、靠右与筛选组并排：

```
[说明文字 ······]        [⚡ 强力模式]  [全部 | 进行中 | 已结束]
```

### 2.2 两态样式

| 状态 | 外观 | 文案 | 说明 |
| --- | --- | --- | --- |
| 关闭（默认） | 绿色：绿色描边 + 淡绿底 + 绿色文字 | `⚡ 强力模式` | 当前的安全策略：限速 30 MiB/s，SFTP 活跃时降到 8 MiB/s |
| 开启 | 红色：红底 + 白色文字（实心） | `⚡ 强力模式` | 不限速、不降优先级，传输优先抢占带宽 |

- **胶囊按钮**（两端全圆角，`border-radius: 999px`），高度、字号与现有 `.btn.s` 一致，不破坏工具栏基线对齐（4px 栅格）；左右内边距 16px 大于半圆半径，文字不被弧线挤压。
- 手机端（≤768px）只保留 ⚡ 图标，隐藏文字，热区 ≥44×44。
- 悬停提示：
  - 关闭态：「当前限速 30 MiB/s，SFTP 读写时自动降速；点击开启强力模式」
  - 开启态：「已关闭限速与动态让路，传输将占满磁盘带宽；与 SFTP 同时使用可能两边都卡；点击关闭」

### 2.3 切换反馈

点击 → 立即调接口 → 顶部 Toast 提示「强力模式已开启 / 已关闭」，按钮态随服务端广播同步（多标签页、多设备一致）。

## 3. 行为定义

| 策略 | 关闭（绿色） | 开启（红色） |
| --- | --- | --- |
| 带宽上限 | 30 MiB/s（`TRANSFER_RATE_LIMIT_MB`） | 不限速 |
| SFTP 活跃时 | 降到 8 MiB/s（`TRANSFER_RATE_LIMIT_BUSY_MB`） | 不降速，仍按满速 |
| 线程 CPU 优先级 | `nice 19` | 保持容器默认优先级（不额外降低） |
| 线程 I/O 优先级 | `ionice -c2 -n7` | 不设置 ionice |
| 界面提示 | 无 | 传输视图顶部显示一条红色提示条：传输独占带宽中，SFTP 访问可能变慢 |

- 对**正在运行**的任务立即生效：限速器每写一块都检查开关，切到强力模式后剩余部分立即全速，无需等任务重来。
- 对**排队中**的任务同样生效（任务开始时按当前开关建限速器）。
- 打开强力模式不会提高任务并发度，队列语义不变。

## 4. 后端实现

### 4.1 状态载体

`TransferManager` 增加一个线程安全开关（`threading.Event`，工作线程在复制循环中读取，不需要锁）：

```python
class TransferManager:
    def __init__(self, bus):
        ...
        self._turbo = threading.Event()

    def set_turbo(self, enabled: bool) -> bool:
        if enabled:
            self._turbo.set()
        else:
            self._turbo.clear()
        return self._turbo.is_set()
```

### 4.2 限速器改造

`_RateLimiter` 接收一个 `turbo` 查询对象（即上面的 `threading.Event`）：

- `consume()` 开头：`if turbo.is_set(): return` —— 直接不等待。
- `_current_rate()` 不参与判定（强力模式下完全跳过限速逻辑，也跳过 SFTP 活跃探测的采样开销）。

### 4.3 优先级与执行

`_do_transfer(job, turbo)`：

```python
turbo_on = turbo.is_set()          # 任务开始时刻
if not turbo_on:
    _lower_priority()              # nice 19 + ionice
limiter = None if turbo_on else _RateLimiter(..., turbo)
```

运行途中开启强力模式时，`_lower_priority()` 已生效的那次不会自动抬回——影响仅限 CPU 调度权重，不影响带宽（带宽由限速器控制），可以接受；下一个任务不会降优先级。

### 4.4 广播

`_publish()` 的 `jobs` 事件增加 `turbo` 字段：

```python
await self._bus.publish("jobs", {"jobs": self.snapshot(), "turbo": self.turbo_enabled})
```

前端 `store.js` 的 `state` 增加 `turbo`，任一页面切换后所有页面同步。

## 5. 接口设计

```http
POST /api/transfers/turbo          （需登录会话）
Content-Type: application/json

{ "enabled": true }

200 → { "ok": true, "turbo": true }
```

- 幂等：重复设同一值直接返回当前状态。
- 无鉴权失败 → 401（复用 `current_session`）。
- 不提供单独 GET：状态随 `GET /api/jobs` 与 SSE `jobs` 事件下发，避免多一次轮询。

## 6. 生命周期与状态

- **内存态**：容器重启后恢复为关闭（安全优先）。不写 `data/`，不涉及磁盘。
- **不自动关闭**：由用户显式控制，避免"传到一半被悄悄降速"的困惑。
- 页面首次加载时从 `/api/jobs` 或首个 SSE 事件取真实状态，不依赖 localStorage（防止多端不一致）。

## 7. 边界与安全

- 强力模式**不影响**安全机制：目标挂载校验（拔盘即中止、不删源）、复制完成后才删源、回滚只覆盖复制阶段，全部保持。
- 「相关硬盘需等任务完成才能弹出」的拦截同样保持。
- 与 SFTP 同时使用时可能两边都卡，这是用户的显式选择；页面用红色提示条明确告知。
- 不做权限分级（当前系统本身只有单一管理员账号）。

## 8. 改动文件清单

| 文件 | 改动 |
| --- | --- |
| `app/transfers/jobs.py` | `TransferManager` 增加 `_turbo` 与 `set_turbo` / `turbo_enabled`；`_RateLimiter` 接受 turbo 并跳过限速；`_do_transfer` 按开关决定降优先级与限速；`_publish` 带 `turbo` |
| `app/transfers/api.py` | 新增 `POST /api/transfers/turbo` |
| `app/static/index.html` | `.view-head` 增加按钮 + 强力模式提示条容器 |
| `app/static/js/jobs.js` | 点击切换、按 `state.turbo` 渲染两态、提示条显隐 |
| `app/static/js/store.js` | `state.turbo`，处理 `jobs` 事件的 `turbo` 字段 |
| `app/static/styles.css` | `.btn-turbo` 绿 / 红两态、提示条样式、手机端只留图标 |
| `tests/test_transfers_jobs.py` | 限速器在 turbo 下不等待；`set_turbo` 幂等 |
| `tests/test_stores_and_transfers.py` | 接口返回与 SSE 字段（如已有 SSE 用例则一并扩展） |
| `README.md` / `VERSION` / `pyproject.toml` / `uv.lock` | 版本 `26.11.25a` + 版本记录 + 功能说明 |

## 9. 测试计划

1. `_RateLimiter(turbo=已置位)` 连续 consume 不产生等待（用 `time.monotonic` 断言 < 50ms）。
2. 限速器中途置位 turbo：后续 consume 立即返回。
3. `POST /api/transfers/turbo` 未登录 → 401；登录后 `{"enabled": true}` → `turbo: true`，重复调用幂等。
4. `GET /api/jobs` 返回 `turbo` 字段；切换后 SSE 收到带 `turbo` 的事件。
5. 原有 74 项测试全部通过（`TRANSFER_RATE_LIMIT_MB=0` 的测试隔离不变）。
6. 手工验收：开启强力模式后网页传输速度回到磁盘上限；关闭后恢复限速；手机上按钮只显示图标且可点击。

## 10. 已确认事项

1. **按钮文案**：「极速模式」，图标为闪电。
2. **绿色语义**：绿色 = 未开启（限速 + 为 SFTP 让路）；红色 = 已开启（独占带宽）。
3. **自动关闭**：队列空闲 5 分钟后自动关闭。
4. **持久化**：不持久化，重启后恢复为关闭。
