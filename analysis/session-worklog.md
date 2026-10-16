# 会话工作记录（0.1 → 1.0 及 devlog 基建）

> 本文件由 GitHub App 机器人 `dsh-rimielab[bot]` 记录，内容与 devlog issue
> [#2](https://github.com/SirTamago/astrbot_plugin_quote_collocter_rimielabot/issues/2) 的逐轮评论一致，
> 目的是让改动细节随仓库一起走（不依赖 issue 是否可访问）。
>
> 配套文档：[implementation-and-probability.md](./implementation-and-probability.md)（README 功能与代码位置对照、
> 概率机制拆解、实测数据与已知问题清单）。

---

## 索引

本文是本会话（从"检查插件源码"到"devlog 记录闭环"）逐轮改动的详细记录，精确到函数与实现语义，
与 devlog issue [#2](https://github.com/SirTamago/astrbot_plugin_quote_collocter_rimielabot/issues/2) 的评论一一对应；
滚动记录（每次 push 一条）在 issue [#1](https://github.com/SirTamago/astrbot_plugin_quote_collocter_rimielabot/issues/1)。
行号如无特别说明，均指当前 `main.py`（2037 行、CRLF）。

| 轮次 | 主题 | 提交 | 版本 |
| --- | --- | --- | --- |
| R1 | 源码通读、README 对照、概率逻辑审计 | —（文档后随 `2d6a88a` 入库） | — |
| R2 | 抽卡模型重写 + 三个 Bug 修复 | `4ca5ee0` | 0.2 |
| R3 | 调试模式（第一版：全局开关） | 被 R4 取代 | — |
| R4 | 调试模式改为按群 + 保底计数钳制 | `7d370ab` | 0.3 |
| R5 | 本地测试套件与源码解析文档入库 | `2d6a88a` | 1.0 |
| R6 | 版本线重编号、提交拆分、本地自检脚本 | `4c55208` | — |
| R7 | 提交署名：匿名邮箱踩坑 → bot 身份 | `4c55208` 改作者 / 后续提交 | — |
| R8 | 白名单排障结论、devlog 工作流增强、以 bot 建 issue #2 | `0cca522` `597e0ce` `4043aee` `f570ccf` | — |

> 说明：R3 的实现先按"全局设置"做过，随后按需求在 R4 改为"按群"，因此提交历史里只有最终形态（0.3）。

---

## R1 · 源码通读、README 功能对照与概率逻辑审计

**目标**：README 里每个功能对应哪段代码；概率逻辑写在哪、由什么触发。

**产出**：新增 `analysis/implementation-and-probability.md`（纯文档，无代码改动；最终随 `2d6a88a` 入库）。结构：

- §1 代码结构：`QuoteShuffler`（99–308）负责抽卡，`QuotePlugin`（311–1764）负责指令/监听/AI Tools/Web API。
- §2 README 功能 → 代码位置对照表：12 条指令各自的 handler 与行号、AI Tools 12 个、WebUI Page 前后端、数据存储、版本条目。
- §3 概率点全景（当时全部随机点）：
  - P1 戳一戳是否发语录：`on_poke` 的 `random.random() < poke_quote_probability`；
  - P2 落空时 `random.choice(POKE_TEXTS)`；
  - P3 稀有度权重：`QuoteShuffler._weighted_shuffle` 的 `random.choices(..., weights=..., k=1)` 逐步不放回；
  - P5 去重窗口：`_build_order` 的"head 重排"；
  - P6 保底：`next()` 内的 `since` 计数与 `_pity_pick_target`；
  - P8 `_weight_of` 的 `or 1.0` 兜底。
- §4 三条抽卡链路时序：`/语录` → `random_quote` → `_shuffler().next()`；戳一戳 → `on_poke`；AI `quote_draw` 直接复用 `random_quote`。
- §8 问题清单（含最小复现与修法）、§9 行号速查表 + 上游源码依据。

**审计结论**（后来全部在 R2/R4 落实）：

1. **权重不影响长期出率**：算法生成"一轮的完整全排列"，一轮内每张图恰好出现一次 ⇒ 长期各星级出率 = 图片张数比例。实测每星级 2 张、2000 抽，每星级恰好 400 次。
2. **保底计数方向写反**：自然出货重置写成 `for cr in range(out_r, 6)`，而任何一张牌的 `out_r ≤ 5`，所以 `range` 永远包含 5 ⇒ `since[5]` 每抽都被清零 ⇒ 默认"每 10 抽必出 5 星"**从未触发**。实测 `pity_5=2`、库内 3 张 1 星图，6 抽后 `since` 恒为全 0。
3. **权重 0 无效**：`float(x or 1.0)` 让 0 变成 1.0，WebUI 滑块拖到 0 相当于 1。
4. **去重会退化**：`N ≤ window` 时 `avoid` 覆盖全部图片，去重完全失效（实测 2 张图、window=8 时相邻重复 9997/20000）。

**验证方式**：把 `main.py` 第 66–308 行原样抽出到临时模块（只需 `json/random/time/Path` + 一个 `logger` 桩）跑仿真，结果整理成 §8 的实测表格。

---

## R2 · 抽卡模型重写与三个 Bug 修复（提交 `4ca5ee0` → v0.2）

**目标**：每次抽取独立随机，而不是"一轮内每张各出一次"。

**`main.py`（+168/−159，共 327 行变动）**

- 删除 `class QuoteShuffler`（一轮全排列 + 指针 + `order/index` 队列）及 `_weighted_shuffle` / `_build_order` / `_pity_pick_target`；新增 `class QuoteDrawer`。
- 新模型（`QuoteDrawer.next()`，当前 326 行）四步：
  1. `_pick_rarity()`（255）：只在"本群有图的星级"里 `random.choices(rarities, weights=[...], k=1)` ⇒ **权重占比即出率**；
  2. `random.choice(pools[rarity])`：星级内均匀抽图，不再参考权重（旧版每张图各有权值）；
  3. `_avoid_for()`（309）：同星级内最近 `window` 抽出现过的图不参与，**排除上限 `min(window, len(pool) - 1)`**，保证永远至少剩一张可抽；`window = 0` 关闭去重；
  4. 保底：`since[r] = min(since.get(r, 0) + 1, thr)`（计数钳制在阈值内）；`_pity_rarity()`（279）只向上搜索 `range(min_rarity, 6)`；**该档及以上没有图时保留计数、不消耗、不降档硬凑**。
- 出货重置改为"抽到 N 星 ⇒ 满足所有 ≤ N 的计数"：`for cr in range(1, out_r + 1): since[cr] = 0`（修正 `range(out_r, 6)`）。
- 状态落盘仍是 `<群号>/order.json`，内容改为 `{"since": {...}, "recent": [...]}`；旧的 `order`/`index` 被忽略、`since` 直接继承（无需迁移）。
- `_load_rarity_weights()`（583）：去掉 `or 1.0`，改为 `for rarity in (1..5): max(0.0, float(raw.get(key, default)))`，非法值回退默认 ⇒ **权重 0 = 该星级不出现**；当所有可抽星级权重都为 0 时，`_pick_rarity()` 回退为按星级均匀。
- 缓存与失效：`self._shufflers` → `self._drawers`（工厂 `_drawer()`，615）；删除 `_invalidate_shuffler()` 及其 4 处调用（图片增删改名由 `recent` 过滤已消失文件即可）；`_apply_global_settings()` 改为 `self._drawers.clear()`。

**其他文件**

- `_conf_schema.json`：`recent_window`（0 = 关闭）、`rarity_weights`（权重 = 出率；0 = 不出现）、`pity_config`（该档无图时不生效、保留计数）三条 hint 重写。
- `pages/admin/index.html`：稀有度/保底提示文案更新；去重窗口 `min="1"` → `min="0"`。
- `README.md`：权重/去重/保底/`order.json` 描述 + v0.2 更新段；`metadata.yaml`：`v2.1.1` → `v0.2`。

**实测（20000 抽，默认权重 3/5/8/4/2 ⇒ 理论 5★13.6% 4★22.7% 3★36.4% 2★18.2% 1★9.1%）**

- `window=0`：13.2 / 22.8 / 36.2 / 18.4 / 9.3 %；`window=8`：13.7 / 22.4 / 36.5 / 18.3 / 9.1 % ⇒ 去重**不改变**星级出率（旧版会）。
- 相邻两次同一张：14.2%（≈ Σpᵢ²）→ 7.1%（残余只来自"该星级只有 1 张图"）。
- `pity_5=10`：5★ 最大间隔恰为 10 抽；无 5★ 图时计数停在 10，补入一张 5★ 后**下一抽立即命中**。
- `weight_5=0`：5★ 出现 0 次；全 0：回退为星级均匀（5000 抽中各约 1000）。
- 旧 `order.json`（含 `order`/`index`/`since`）可直接读取并继承计数。

**过程中否掉的方案**：把"最近 N 张"当作**星级候选池**的过滤条件 —— 权重会被反复重新归一化，出率被拉平（5★ 13.6%→19.6%、3★ 36.4%→21.3%），与"权重=出率"的目标冲突，已弃用。

---

## R3 · 调试模式（第一版：全局开关；随后被 R4 改为按群）

**目标**：能直接看到"这一抽的概率模型 + 是不是保底出的"。

**`main.py`（这一版尚未提交，形态被 R4 取代）**

- 新增模块级纯函数 `format_draw_trace(trace, verbose=True)`（当前 101 行）：把一次抽卡的模型状态渲染成文本；`verbose=True` 多出「权重」与「出货后计数」两行 ⇒ 日志版 6 行、群内版 4 行。
- `QuoteDrawer`：
  - `__init__` 增 `self.last_trace: dict | None = None`；
  - 新增 `_rarity_shares()`（265）：各星级在"有图星级"之间归一化后的**实际出率**（与 `_pick_rarity` 口径一致，全 0 时返回均匀）；
  - `next()` 末尾写入 trace：`total`/`counts`（图库构成）、`weights`/`share`（权重与实际出率）、`window`/`recent`/`excluded`/`candidates`/`pool_size`（去重前后）、`rarity_by_weight` vs `rarity_final`（是否被保底覆盖）、`pity.{thresholds, since_used, triggered, skipped, since_after}`、`final`；其中 `since_used` 是"判定依据"快照（判定用，非出货后值）。
- `QuotePlugin`：
  - `_debug_level()`：读档位（0/1/2）；
  - `_draw_quote(group_id, source, user_id) -> (path, note)`（当前 1109）：**唯一抽卡入口**——`/语录`、戳一戳、AI `quote_draw` 全走它，保证三条路径调试口径一致；命中档位 ≥1 时 `logger.info("[语录调试] 来源=… 用户=…")`；
  - `_with_debug_note(chain, note)`（1134）：把附注并入消息链**图片之前**的 `Comp.Plain`（OneBot v11 会丢弃图片之后的文本，所以不能追加在末尾）；
  - 新增管理员指令 `/语录调试 <0|1|2>`（别名 `调试语录`、`语录调试模式`），无参数时回显当前档位与用法。

**设置项**：`debug_mode` 当时写进**全局** `settings.json`（`_default_global_settings` / `_apply_global_settings` / `_conf_schema.json` / WebUI 全局表单都加了它）。

**验证**：临时脚本渲染四类场景并逐项核对 —— 普通抽卡（4 行/6 行）、保底触发（显示"权重抽中 2★ → 保底覆盖为 5★"、`保底：触发 5★（判定 3/3）`）、该档无图（显示"已达阈值但本群无该档及以上图片（计数保留）"）、空图库（`last_trace=None`、`format_draw_trace(None) == ""`、不产生附注）。

---

## R4 · 调试模式改为按群 + 保底计数钳制（提交 `7d370ab` → v0.3）

**目标**：调试档位只影响被指定的群。

**`main.py`（+286/−13）**

- `_load_settings()`（557）的群级默认值新增 `"debug_mode": 0`，与 `mode`/`cooldown`/`last_poke` 同级 ⇒ 档位落在 `<群号>/settings.json`，天然按群隔离。
- `_debug_level(group_id)`（1098）改为读群设置；`_draw_quote()` 按**发起抽卡的群**取档位。
- `_apply_group_settings(group_id, mode=None, cooldown=None, debug_mode=None)` 新增 `debug_mode` 校验（只接受 0/1/2）——指令、AI 工具、Web API 三处复用同一套校验与落盘。
- `_apply_global_settings()`：删除全局 `debug_mode` 分支，改为**显式报错**（"调试模式是按群设置，请用 /语录调试 或 quote_configure_group"），避免旧脚本传入时静默丢失；`_default_global_settings()` 移除该字段。
- 新增两个 Web API（`QuotePlugin.__init__` 注册，路由前缀 `PLUGIN_NAME = "quote_collocter"`）：
  - `GET /quote_collocter/group-debug` → `web_get_group_debug()`：按 `group_id` 返回 `{debug_mode, mode, cooldown}`；
  - `POST /quote_collocter/group-debug` → `web_set_group_debug()`：校验群号为数字后经 `_apply_group_settings(..., debug_mode=...)` 写入；
  - 两者共用 `_group_debug_payload(group_id)`（当前 1970 附近）。
- `/语录调试`（1297）改为读写**本群**，提示"（只影响本群）"；`quote_status` 与 AI 用的 `_quote_stats_text()` 各增一行"调试模式"。
- `ai_quote_configure_group` 增加 `debug_mode` 形参（docstring 同步——框架按 docstring 的 `Args:` 生成参数 schema），"至少提供一项"的判断一并更新。
- **保底计数钳制**：`since[r] = min(since.get(r, 0) + 1, thr)`，本群暂时缺该档图片时显示 `N/N`，不再累加成 `11/10`、`12/10`。

**页面与配置**

- `pages/admin/index.html`：全局设置表单**移除**调试下拉框；新增「按群调试模式」卡片（群号输入 + 档位选择 + 查询/保存按钮 + 状态提示）。
- `pages/admin/app.js`：新增 `loadGroupDebug()` / `saveGroupDebug()`（`bridge.apiGet/apiPost("group-debug", …)`），在 `main()` 里绑定按钮，并对群号输入加 `change` 自动查询；`fillSettings`/`saveSettings` 不再触碰 `debug_mode`。
- `_conf_schema.json`：移除全局 `debug_mode`（群级设置无法用全局 schema 表达）。
- `README.md`：调试模式章节点明"按群生效"、三个入口与样例输出；`metadata.yaml` → `v0.3`。

**实测（桩化 AstrBot 调真实 handler）**

- `_apply_group_settings("111", debug_mode=2)` → 只写 `111/settings.json`（`{mode:0, cooldown:10, debug_mode:2, last_poke:0}`），`222` **不会**被创建；`_debug_level("111")=2`、`_debug_level("222")=0`。
- 非法值 `5` / `"abc"` 被拒且旧值不变；全局 `debug_mode` 被拒且不落盘。
- 抽卡：`111`（档位 2）返回带"语录调试/保底："的附注并写日志（含群号）；`222`（档位 0）**无附注、无日志**。
- 附注并入首个文本段、图片仍在最后；调试文本不影响原卡片文字（"demo.jpg" 仍在）。
- Web API：GET 返回该群档位、非数字群号报错；POST 保存 `333=1` 并落盘、非法档位报错；路由注册共 5 条。

---

## R5 · 本地测试套件与源码解析文档入库（提交 `2d6a88a` → v1.0）

**目标**：不装 AstrBot 也能回归验证。

**新增 `tests/`（+1109 行，纯标准库，不依赖 pytest/aiohttp 之外的东西）**

- `tests/stub_astrbot.py`（376 行）——最小 AstrBot 桩：
  - 消息组件 `Plain/Image/At/Reply/Poke`；`Image.convert_to_file_path()` 返回真实临时文件（用于投稿三级容错下载的本地缓存路径）；
  - `filter.{command,event_message_type,permission_type,llm_tool}` 空装饰器 + `EventMessageType`/`PermissionType` 枚举；
  - `Star` / `Context`（`register_web_api` 记录路由、`get_config()` 返回 `admins_id`）；
  - `astrbot.api.web` 的 `json_response`/`error_response`/`request`（可控 query/body，`type=int` 转换）；
  - 记录型 `logger`；可编程 `bot.api.call_action`（喂 `get_msg` 响应）；假事件 `AstrMessageEvent`（`plain_result`/`chain_result`/`stop_event`/`send`）；
  - `install()`：注入桩模块并 `os.chdir()` 到临时目录（`QUOTES_ROOT` 是相对路径，避免污染仓库 `data/`），可重复调用（多测试模块共用一个环境）；`drive()` / `run()` 分别驱动 async generator 与协程。
- `tests/test_draw_probability.py`（21 例）：出率=权重占比（含"图数比例不同、出率仍相同"）、去重不扭曲出率、权重 0 禁用、全 0 回退、同星级不连续重复、2 张图去重不退化、保底最大间隔 ≤ 阈值、缺档保留计数并补图即命中、重置方向（≤ N 才清零）、旧 `order.json` 兼容、trace 字段齐全与 verbose/compact 差异。全部固定随机种子 + 容差断言（`TOL=0.02`），不会随机飘红。
- `tests/test_plugin_logic.py`（34 例）：按群调试隔离/非法值/全局拒绝、抽卡输出按群生效与附注位置、指令（`/语录`、`/语录调试`、`/语录状态`、`/投稿权限`、`/戳戳冷却`、`/语录拉黑`）、戳一戳（冷却、只响应戳 bot、概率 0 走趣味文本、黑名单不消耗冷却）、投稿→重名拒绝→改名→改稀有→删除/撤回的**日志同步**、回复卡片定位删除、文件名多口径查找、AI Tools 权限与返回值、5 条 Web API 路由与参数校验。

**文档**

- `analysis/implementation-and-probability.md`（694 行）入库（含 §1–§9 的基线实现记录 + §10 的 0.2/0.3 变更与实测数据）。
- `README.md` 新增「本地测试」与「Contributors」两节；`metadata.yaml` → `v1.0`。

**验证**：`python3 -m unittest discover -s tests` → **55 tests OK（约 8 秒）**；单跑两文件分别为 21 / 34 例。桩测试覆盖不到的部分（指令前缀与权限装饰器、AstrBot 事件分发、真实图片下载、WebUI iframe bridge、实际发消息）已在 README 里列明需要在真实环境验证。

---

## R6 · 版本线重编号、提交拆分与本地自检脚本（提交 `4c55208`）

**版本线**（按"以 fork 基线为 0.1、只用两位数字"）

- `0.1` = clone 基线（上游 `v2.1.1`，`b0eb2a5`）—— 只在 README 记为基线，不改写历史；
- `0.2` = 抽卡模型重写 + 三个 Bug 修复（`4ca5ee0`）；
- `0.3` = 按群调试模式（`7d370ab`）；
- `1.0` = 测试套件 + 源码解析文档 + Contributors（`2d6a88a`）。

README 的「版本更新」增加版本号说明段（0.1 基线 → 两位数字递增），上游历史版本保留在原位；`analysis/` 里所有 `v2.2` 字样统一改成 0.2 / 0.3（含目录锚点、§5.2/§5.3/§5.4/§8.x 的"已修复"标注）。

**提交拆分的做法（构造真实中间态，而不是把三轮改动塞进一个提交）**

- 从最终 `main.py` **反向移除** 0.3 才引入的调试代码，得到 0.2 的 `main.py`；共 18 处断言式编辑（每处 `assert count == 1`，避免误替换）：
  `format_draw_trace` 整段、类 docstring 调试段、`self.last_trace` 初始化、`_rarity_shares`、`next()` 内 `last_trace` 赋值与 `since_used`/`skipped` 快照、`_debug_level`/`_draw_quote`/`_with_debug_note`、`/语录调试` 指令、`_load_settings` 的 `debug_mode` 默认值、`_apply_group_settings` 的调试分支与签名、`quote_status`/`_quote_stats_text` 的档位行、`_apply_global_settings` 的拒绝分支、AI 工具参数、`group-debug` 两条路由注册与三个 handler。
- **0.2 中间态自检 13/13**：出率≈权重占比、同星级不连续重复、权重 0 禁用、保底最大间隔 ≤10、缺档补图即命中、空图库返回 None、Web 路由恰好 3 条、无 `format_draw_trace`/`_debug_level` 残留、`/语录` 仍返回消息链。
- **0.3 中间态用整套测试验证 55/55 通过**；1.0 提交前再跑 55/55，并逐文件 `diff` 确认工作树与最终状态一致。
- 每个提交单独核对 `metadata.yaml` 版本号（`v0.2` / `v0.3` / `v1.0`）。
- **行尾处理**：`main.py` 原本是 **CRLF**；第一次用 Python 脚本改写时被规范化成 LF，导致"全文件 diff"（3529 行变动）。发现后用二进制方式还原为 CRLF，最终 diff 收敛到约 160/159 行。后续所有编辑都校验"CRLF 数 > 0 且裸 LF 数 = 0"。

**新增 `tests/run_tests.sh`（+37 行）**

`bash tests/run_tests.sh` 一次跑完四步：① `python3 -m py_compile main.py`；② `_conf_schema.json` 解析与关键字段校验；③ `node --check pages/admin/app.js`（无 node 则跳过）；④ `python3 -m unittest discover -s tests`。README「本地测试」补充该用法。

---

## R7 · 提交署名：匿名邮箱踩坑 → bot 身份

**背景**：你要的是 GitHub 侧边栏的 Contributors（**按提交作者自动统计**），不是 README 里的章节。

**过程与踩坑**

1. 第一次用 `deepseek-harness@users.noreply.github.com` 作为作者邮箱。GitHub 会把 `xxx@users.noreply.github.com` 的 `@` 前缀解析成**用户名**，而 `deepseek-harness` 确实已是一个 GitHub **Organization 账号**（id `288162816`）⇒ 提交被挂到了与我无关的账号上。
   - 修正：改用 IANA 保留域名 `deepseek-harness@example.com`（不会被解析成账号）→ `git commit --amend --author=...` → `git push --force-with-lease`。GitHub API 核实：`author: DeepSeek Harness <deepseek-harness@example.com>`、`login` 为空 ⇒ 匿名贡献者，不再关联那个组织。
2. 你在另一个会话里建了 GitHub App `dsh-rimielab[bot]`（**App ID `5321359`**，机器人账号 id `342685585`），两个 CI 提交用的署名是
   `<机器人账号 id>+<slug>[bot]@users.noreply.github.com`。
   按你选的"以后用 bot 署名"，我改用同一身份（author 与 committer 都写 bot，与你那两个 CI 提交一致），并**没有改动仓库的 git 配置**——所以你自己在这个克隆里提交仍然是 `Rimrose`：
   ```bash
   git -c user.name="DeepSeek Harness" \
       -c user.email="342685585+dsh-rimielab[bot]@users.noreply.github.com" \
       commit -m "…"
   ```
   这样提交在 GitHub 上会被解析为 `login: dsh-rimielab[bot]`（已用 `0cca522` 起多个提交核实）。
3. 为什么不改写已有历史把 `4c55208` 也并到 bot 名下：改写会连带 rebase 你那两个 CI 提交（SHA 变化）、需要 force push，而且会把 `SirTamago` 在这仓库的贡献数抹掉；按你的选择"只从以后开始用 bot 署名"，历史保持不变。

**Contributors 现状（API 实测，`?anon=1`）**：`litsum 32 / liuziheng20091106 16 / SirTamago 3 / dsh-rimielab[bot] 2（缓存滞后，实际已有 6 个提交）/ shangzhimingge 2 / 匿名 DeepSeek Harness 1`。GitHub 的贡献者统计是异步缓存，会滞后数小时。

---

## R8 · 白名单排障结论、devlog 工作流增强、以 bot 身份建 issue #2

### A. 群内非管理员完全无响应 ⇒ AstrBot 白名单，不是插件问题

**判据**：`/语录`（指令）与戳一戳（**非指令**监听）在同一群同时失效 ⇒ 事件在插件派发之前就被丢弃。AstrBot 的固定流水线（`astrbot/core/pipeline/stage_order.py`）：

```
WakingCheck → WhitelistCheck → SessionStatus → RateLimit → ContentSafety → PreProcess → ProcessStage(插件) → ResultDecorate → Respond
```

`WhitelistCheckStage`（`pipeline/whitelist_check/stage.py`）逻辑：`enable_id_white_list` 为真且 `id_whitelist` 非空时，`event.unified_msg_origin` 与 `str(群号)` 都不在白名单里就 `event.stop_event()`；而 `wl_ignore_admin_on_group` **默认 true**，管理员会绕过白名单 ⇒ 表现为"只有管理员能用、普通成员毫无反应"。开启 `id_whitelist_log` 后日志会打印 `is not in the session allowlist` 作为确诊依据。

你确认是自己没把群号加进白名单；结论与排查路径已写进 `README.md` 的「注意事项」（提交 `0cca522`，+1 行）。**插件侧无需改代码**：handler 是全局注册的，第一次回复之前没有任何按群门禁。

### B. devlog 工作流增强（3 个提交）

- `597e0ce`：`workflow_dispatch` 增加 `note` 输入；note 非空时把内容作为一条「（手动补录）」记录发出（评论仍由 bot 发），为空时保持原行为。
- `4043aee`：note 支持 `\n` 换行（Actions 页面 `type: string` 是**单行**输入框）。实现：`printf '%s\n' "$NOTE" | sed 's/\\n/\n/g'` —— 只还原字面量 `\n`，不碰 `|`、`\|` 等表格转义；并做了"单行 ↔ 多行"往返一致性测试。
- `f570ccf`：增加 `new_issue_title` —— 填了就**新建一个 devlog issue**（正文 = note），留空则维持"追加评论"；新建模式下 `boot` 步骤允许 `num` 为空，不再强制依赖 #1 存在。
- 验证：3 个 `run` 块全部 `bash -n` 通过；分支演练（有/无 `new_issue_title` → 建 issue / 评论）。每次 push 触发的 run 均为 success，issue #1 已累计 5 条自动记录。

### C. 以 bot 身份创建本 issue（#2）

依据 `/private/tmp/HOW-TO-create-issue-as-bot.md`（纠正了我此前"没有 token/gh 就做不到"的判断）：

1. 用 App 私钥本地自签 JWT：RS256，`iss` = **App ID `5321359`**（不是机器人账号 id），`iat = now-60`、`exp = now+540`，签名由 `openssl dgst -sha256 -sign`（PKCS#1 私钥可直接用）；
2. `GET /app/installations` → 安装 ID `172153142`（账号 `SirTamago`）；
3. `POST /app/installations/{id}/access_tokens` → 安装令牌（1 小时有效）；
4. `POST /repos/SirTamago/astrbot_plugin_quote_collocter_rimielabot/issues`，带 `labels: ["devlog"]` ⇒ 创建 **#2**，API 核实作者为 `dsh-rimielab[bot] (Bot)`。

**安全处理**：私钥仅用于本地签名，未打印、未复制、未入库；安装令牌只在内存中使用、不落盘（1 小时后自动失效）；复用的是 `/private/tmp/issue_as_bot.py` 的 `make_jwt()`/`call()`，调用脚本 `/private/tmp/devlog_post.py` 同样只含路径参数、不含任何凭据。建议把私钥从 `~/Downloads` 移到 `chmod 600` 的私有目录，或按文档用完后在 App 设置里删除该私钥。
