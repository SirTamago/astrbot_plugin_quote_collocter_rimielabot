# 群友语录插件 v2.1.1 源码解析：README 功能对照与概率机制

> 分析对象：本仓库工作区当前代码（`main.py` 1764 行 + `pages/admin/` + `_conf_schema.json` + `metadata.yaml`）
> 对应提交：`b0eb2a5` — *fix: 捕获 AI 抽卡卡片发送超时*（2026-08-08）
> 分析目的：① README 里每个功能分别由哪段代码实现；② 概率/随机逻辑写在哪里、由什么触发、实际数学行为是什么。
>
> 说明：本文件中的“代码事实”均可通过文末的**行号速查表**在源码里复核；“框架事实”来自 AstrBot 上游源码（链接见 §9）；“实测结论”来自 §8 的仿真脚本输出。
>
> **版本提示**：§1–§9 记录的是 **v2.1.1** 的实现（含当时的 Bug），保留下来作为对照；
> 抽卡模型已在 **0.2** 改为「每次独立随机」，**0.3** 追加了按群调试模式，改动内容、修复状态与新的实测数据见 **§10**。
> 0.2 之后 `main.py` 行号已变化，§9.2 的行号速查表只对自用线 0.1 基线（上游 v2.1.1）有效。

---

## 目录

1. [代码结构与职责划分](#1-代码结构与职责划分)
2. [README 功能 → 代码位置对照](#2-readme-功能--代码位置对照)
3. [概率/随机逻辑全景（重点）](#3-概率随机逻辑全景重点)
4. [抽卡主链路：从一条消息到一张图](#4-抽卡主链路从一条消息到一张图)
5. [概率模型逐条拆解](#5-概率模型逐条拆解)
6. [参数从哪来：三级配置与缓存](#6-参数从哪来三级配置与缓存)
7. [触发条件总清单（什么时候不会触发）](#7-触发条件总清单什么时候不会触发)
8. [已知问题与风险清单（含实测证据与修法）](#8-已知问题与风险清单含实测证据与修法)
9. [附录](#9-附录)
10. [0.2 变更：改为独立同分布（IID）抽取](#10-02-变更改为独立同分布iid抽取)

---

## 1. 代码结构与职责划分

### 1.1 仓库文件

| 文件 | 作用 |
| --- | --- |
| `main.py` | 全部后端逻辑：`QuoteShuffler`（抽卡算法）+ `QuotePlugin`（指令、事件监听、AI Tools、Web API） |
| `_conf_schema.json` | AstrBot 插件配置弹窗的 schema（默认投稿模式、冷却、戳戳概率、去重窗口、稀有度权重、保底） |
| `metadata.yaml` | 插件元信息。注意 `name: quote_collocter`，与 `main.py:18` 的 `PLUGIN_NAME` 相同，这决定了 Web API 路由前缀（见 §9.3） |
| `pages/admin/{index.html,app.js,style.css}` | WebUI 管理页面（投稿日志 + 全局设置表单），通过 `window.AstrBotPluginPage` bridge 调用后端 |
| `requirements.txt` | 仅 `aiohttp` |
| `README.md` | 用户文档 |

### 1.2 `main.py` 内部分区

```
┌─ 常量与纯函数 ───────────────────────────────  18–96
│   PLUGIN_NAME / 路径常量 / 默认权重 / 默认保底
│   parse_quote_rarity()  文件名 → (稀有度, 展示名)
│   quote_chain()         图片路径 → [文本, 图片] 消息链
│
├─ class QuoteShuffler ───────────────────────  99–308   ★概率核心
│   _list_images / _load / _save
│   _weight_of          稀有度权重查表
│   _weighted_shuffle   加权不放回抽样（生成整轮顺序）
│   _build_order        重建一轮顺序 + 软去重
│   _pity_load / _pity_pick_target   保底计数与选图
│   next()              对外唯一入口：返回下一张图的绝对路径
│
└─ class QuotePlugin(Star) ───────────────────  311–1764
    __init__               注册 3 个 Web API
    _group_dir / _ensure_group_dir / _settings_path
    全局设置读写            _default/_load/_save_global_settings
    黑名单                  _blocked_user_ids / _is_blocked_user / _update_blacklist
    群设置读写              _load_settings / _save_settings / _apply_group_settings
    权重与保底读取          _load_rarity_weights / _load_pity
    shuffler 缓存           _shuffler / _invalidate_shuffler
    图片下载                _save_bytes / _download_image（三级容错）
    引用消息解析            _get_replied_message_chain / _resolve_reply_image_file
                            / _resolve_reply_quote_file / _detect_ext
    投稿日志                _log_submission / _read_submit_log / _rewrite_submit_log
                            / _find_submission_record / _remove_/_rename_submission_records
    卡片文本与查找          _quote_info_text / _quote_chain_with_info / _list_quote_files
                            / _existing_displays / _find_quote_file
    文件管理                _delete / _rename / _set_quote_rarity / _revoke_quote_file
                            / _submit_replied_quote
    指令 handler            @filter.command × 12 + on_poke
    AI Tools                @filter.llm_tool × 12
    Web API handler         web_get_settings / web_save_settings / web_submit_log
```

---

## 2. README 功能 → 代码位置对照

### 2.1 指令一览（README 第 11–24 行）

| README 指令 | 权限装饰器 | handler | 行号 | 依赖的核心实现 |
| --- | --- | --- | --- | --- |
| `/语录`（别名 `随机语录`） | 全体 + 群聊 | `random_quote` | 966–984 | `_shuffler().next()` → `quote_chain()` |
| `/语录投稿 [标题]`（`投稿语录`） | 受投稿权限控制 | `submit_quote` | 1012–1034 | `_submit_replied_quote`（927） |
| `/撤回投稿 [名称]`（`取消投稿`/`撤回语录`） | 本人 | `revoke_quote` | 1239–1278 | `_resolve_reply_quote_file` + `_revoke_quote_file`（909） |
| `/语录状态`（`投稿状态`） | 全体 | `quote_status` | 1039–1064 | `_load_settings` + 目录计数 |
| `/语录查看 <名称>`（`查看语录`） | `PermissionType.ADMIN` | `view_quote` | 989–1007 | `_find_quote_file` + `_quote_chain_with_info`（760，含投稿人/时间） |
| `/投稿权限 <0\|1\|2>`（`设置投稿权限`） | ADMIN | `set_submit_mode` | 1069–1084 | `_apply_group_settings`（1373） |
| `/戳戳冷却 <秒>`（`语录冷却`） | ADMIN | `set_poke_cooldown` | 1089–1101 | 同上 |
| `/语录删除 [名称]`（`删除语录`） | ADMIN | `delete_quote` | 1147–1164 | `_delete_quote_file`（849） |
| `/语录改名 <旧> <新>`（`改名语录`） | ADMIN | `rename_quote` | 1169–1195 | `_rename_quote_file`（861） |
| `/语录改稀有 <名称> <1-5>`（`改稀有语录`/`语录改稀有度`） | ADMIN | `change_rarity` | 1200–1234 | `_set_quote_rarity_file`（886） |
| `/语录拉黑 @用户\|UID`（`拉黑语录用户`） | ADMIN | `block_quote_user` | 1106–1123 | `_resolve_target_user_id`（416）+ `_update_blacklist`（1394） |
| `/语录解封 @用户\|UID`（`解封语录用户`/`语录取消拉黑`） | ADMIN | `unblock_quote_user` | 1125–1142 | 同上 |

补充：所有指令都叠了 `@filter.event_message_type(GROUP_MESSAGE)`，即 README “以下指令仅适用于群聊”的落地方式；每条 handler 末尾 `event.stop_event()`，阻止同一事件继续流向下一个 handler（提交 `8af0688` 就是为了修“重复发送”）。

### 2.2 回复式管理（README 26–37 行）

| 行为 | 实现 |
| --- | --- |
| 拿到被回复消息的原始消息链 | `_get_replied_message_chain`（555）：找 `Comp.Reply` → `bot.api.call_action("get_msg", message_id=...)` |
| 从被回复卡片解析出本群语录文件 | `_resolve_reply_quote_file`（611）：收集候选名（image 段的 `file`/`url` basename + text 段逐行），逐个过 `_find_quote_file`（797）验证 |
| 删除/改名/改稀有的“回复模式” | `delete_quote` 1153、`rename_quote` 1177–1184、`change_rarity` 1210–1217：先尝试回复解析，命中则参数含义变为“只给新值” |
| `/撤回投稿` 仍校验归属 | `_revoke_quote_file`（909）：`record["user_id"] != user_id` → 拒绝 |

### 2.3 投稿（README 39–49 行）

| 行为 | 实现 |
| --- | --- |
| 必须是“回复一条含图消息” | `_resolve_reply_image_file`（575）解析被回复消息里的 `image` 段，支持数组与 CQ 码两种格式；解析不到则 `submit_quote` 返回引导文案 |
| 扩展名推断 | `_detect_ext`（600）：URL 后缀 → `file` 字段后缀 → 兜底 `.jpg` |
| 保存为 `1.<标题>.<扩展名>` | `_submit_replied_quote`（927，944–948）：`stem = title or 时间戳`，`filename = f"1.{stem}{ext}"` |
| 无标题时用投稿时间命名 | 同上，`time.strftime("%Y年%m月%d日%H:%M的投稿")` |
| 重名拒绝 | `_existing_displays`（785）返回**忽略稀有度前缀**的“标题+扩展名”集合，949 行比对 |
| 图片三级容错下载 | `_download_image`（499）：① 当前消息 `Comp.Image.convert_to_file_path()` 本地缓存 → ② 协议端 `get_image` API → ③ HTTP(S) URL 下载；全部失败返回 None |
| 投稿日志 | `_log_submission`（653）：向 `data/plugin_data/quote_collocter/submit.log` 追加 JSONL（`ts/time/group_id/user_id/filename`） |

### 2.4 AI Tools（README 51–65 行）

| README 工具名 | 方法 | 行号 | 权限/黑名单校验 |
| --- | --- | --- | --- |
| `quote_list` | `ai_quote_list` | 1470–1513 | 群聊 + 黑名单 |
| `quote_view` | `ai_quote_view` | 1515–1530 | 群聊 + 黑名单 |
| `quote_draw` | `ai_quote_draw` | 1532–1546 | **直接复用 `random_quote`**（黑名单/空库/加权/去重/保底/消息链全一致），发到群内后返回文本 |
| `quote_status` | `ai_quote_status` | 1548–1554 | 群聊 |
| `quote_submit` | `ai_quote_submit` | 1556–1569 | 复用 `_submit_replied_quote`（含黑名单/模式/重名） |
| `quote_delete` | `ai_quote_delete` | 1571–1587 | `_is_admin`（1338） |
| `quote_rename` | `ai_quote_rename` | 1589–1606 | `_is_admin` |
| `quote_set_rarity` | `ai_quote_set_rarity` | 1608–1631 | `_is_admin` |
| `quote_revoke` | `ai_quote_revoke` | 1633–1660 | 归属校验在 `_revoke_quote_file` |
| `quote_configure_group` | `ai_quote_configure_group` | 1662–1683 | `_is_admin` → `_apply_group_settings` |
| `quote_configure_global` | `ai_quote_configure_global` | 1685–1695 | `_is_admin` → `_apply_global_settings`（1414） |
| `quote_manage_blacklist` | `ai_quote_manage_blacklist` | 1697–1710 | `_is_admin` → `_update_blacklist`（1394） |

- “AI 不返回本地绝对路径”：所有 tool 返回的都是 `_quote_detail_text`/`_quote_stats_text` 之类的文本，唯一返回图片的 `quote_draw` 是发给群而不是返回给 Agent。
- “工具结果直接返回给 Agent”：方法用 `return` 而不是 `yield event.plain_result(...)`（v2.1.1 修复，提交 `8f41cce`）。
- tool 的参数 schema 由 `@filter.llm_tool` 解析 **docstring 的 `Args:` 块**生成（不读类型注解），所以每个 tool 的 docstring 格式是功能的一部分（框架依据：`docs.astrbot.app/dev/star/guides/ai.html`）。

### 2.5 WebUI 配置与管理页面（README 67–79 行）

| 功能 | 实现 |
| --- | --- |
| 后端 API 注册 | `QuotePlugin.__init__`（314–337）：`register_web_api("/quote_collocter/settings", GET)`、`.../settings/save`（POST）、`.../submit-log`（GET） |
| 读全局设置 | `web_get_settings`（1715）→ `_load_global_settings`（376） |
| 存全局设置 | `web_save_settings`（1719）→ `_apply_global_settings`（1414），与 AI tool 共用同一套校验；保存后 `self._shufflers.clear()`（1464）让权重/保底立即生效 |
| 投稿日志 + 群号过滤 | `web_submit_log`（1727）：`request.query.get("group_id")`、`limit`（默认 200），按 `ts` 倒序 |
| 前端页面 | `pages/admin/index.html`（表单骨架）+ `app.js`（`bridge.apiGet("settings")`、`bridge.apiPost("settings/save", payload)`、稀有度滑块 / 保底输入动态渲染、日志表格）+ `style.css` |
| 页面与插件配置“同一份运行时设置” | 二者最终都落到 `data/plugin_data/quote_collocter/settings.json`（见 §6，注意“配置弹窗只提供默认值”的细节） |

### 2.6 数据存储（README 81–93 行）

| README 路径 | 代码位置 |
| --- | --- |
| `data/plugin_data/quote_collocter/` | `QUOTES_ROOT`（21） |
| `settings.json`（全局设置、保底、黑名单） | `GLOBAL_SETTINGS_PATH`（24）；`_load/_save_global_settings`（376/396）；`blocked_user_ids` 在 404/1410 |
| `submit.log`（JSONL 投稿日志） | `_log_submission`（653）、`_read_submit_log`（672）、`_rewrite_submit_log`（692） |
| `<群号>/settings.json`（群级权限/冷却/戳戳状态） | `_settings_path`（350）、`_load_settings`（426）、`_save_settings`（443）；字段 `mode` / `cooldown` / `last_poke` |
| `<群号>/order.json`（随机队列、保底计数） | `QuoteShuffler._load/_save`（133/142），结构 `{"order": [...], "index": n, "since": {"1":0,...,"5":0}}` |
| `<稀有度>.<标题>.<扩展名>` | 命名由 `_submit_replied_quote`（948）与 `_set_quote_rarity_file`（894）产生；解析由 `parse_quote_rarity`（66） |

按群隔离、重装不丢：路径全部由 `_group_dir(group_id)`（342）派生，与插件代码目录无关。

### 2.7 版本更新条目（README 95–119 行）对应改动

| 版本条目 | 代码体现 |
| --- | --- |
| v2.1.1 修复 `yield event.plain_result()` 导致 Agent 收不到返回值 | AI Tools 全部 `return <str>`（1470–1710） |
| v2.1.1 `quote_draw` 保留群内发卡 + 返回确认 | `ai_quote_draw`（1532–1546），含 `retcode=1200` 超时的特判 |
| v2.1 `/语录查看` 显示投稿人与时间 | `_quote_info_text`（754）+ `_submission_info_text`（744） |
| v2.1 删除/改名/改稀有度同步维护投稿日志 | `_remove_/_rename_submission_records`（714/728），在 854/879/902 调用 |
| v2.1 回复卡片定位目标 | `_resolve_reply_quote_file`（611） |
| v2.1 完整 AI Tools + 群/全局/黑名单 | §2.4 表 |
| v2.0 标准 AstrBot 结构、去重随机、权重、保底、WebUI Page | `filter.command`/`AstrBotConfig`/`_conf_schema.json`；`QuoteShuffler`；`pages/admin/` |
| v1.5 引用回复投稿、三级容错下载、主动 `/语录` | `_download_image`（499）、`random_quote`（966） |
| “戳一戳/引用读取仅 OneBot v11” | `assert isinstance(event, AiocqhttpMessageEvent)`（504）、`event.bot.api.call_action`（524/569）、`metadata.yaml: support_platforms: [aiocqhttp]` |

---

## 3. 概率/随机逻辑全景（重点）

插件里**所有**随机性与“概率参数”一共只有下面这些点。除此之外没有任何概率逻辑。

| # | 随机点 | 位置 | 参数来源 | 触发前提 |
| --- | --- | --- | --- | --- |
| P1 | **戳一戳要不要发语录**：`random.random() < poke_quote_probability` | `on_poke` **1320–1333** | 全局 `settings.json.poke_quote_probability`（默认 0.85） | 群聊 + `notice/poke` + 戳的是 bot + 未冷却 + 未被拉黑 |
| P2 | **戳一戳失败时随机趣味文本**：`random.choice(POKE_TEXTS)` | `on_poke` **1331**（文本表 50–54） | 常量表 | P1 落空（概率 `1 - p`） |
| P3 | **稀有度权重 → 一轮之内的抽取顺序**：`random.choices(range(len(pool)), weights=weights, k=1)` 逐步不放回 | `QuoteShuffler._weighted_shuffle` **155–171** | 全局 `rarity_weights`（`_load_rarity_weights` 451） | 队列重建时（首次/库变化/一轮走完） |
| P4 | 权重全为 0 时回退均匀洗牌：`random.shuffle` | `_weighted_shuffle` **163–165** | — | `sum(weights) <= 0`（实际因 P8 的 `or 1.0` 很难成立） |
| P5 | **短期去重窗口**：把新队列开头 `window` 张里“上一轮末尾出现过的”挪到后面 | `_build_order` **173–181**；重建调用点 **240–246 / 248–251** | 全局 `recent_window`（默认 8） | 每次重建队列 |
| P6 | **保底计数与强制出图** | `next` **253–293**；`_pity_load` 183；`_pity_pick_target` 193–217 | 全局 `pity_config`（默认 `pity_5=10`，`_load_pity` 463） | 每次抽卡 |
| P7 | 保底在同星级内选图：`random.choice(pool)` | `_pity_pick_target` **211 / 215** | — | 保底触发时 |
| P8 | 权重查表的 `or 1.0` 兜底（0 会被当成 1.0） | `_weight_of` **150–153**；`_load_rarity_weights` **451–461** | — | 每次取权重 |

一句话总结调用关系：

```
/语录 ─┐
戳一戳 ─┼─→ QuotePlugin._shuffler(group_id) ─→ QuoteShuffler.next()
AI 抽卡 ┘        （缓存 + 注入权重/保底/窗口）
                       │
                       ├─ _load() 读 order.json {order, index, since}
                       ├─ 需要时 _build_order() ─→ _weighted_shuffle()   ← P3/P4/P5
                       ├─ since[r] += 1                                   ← P6 计数
                       ├─ 保底判定 ─→ _pity_pick_target() ─→ random.choice ← P6/P7
                       ├─ 自然出货重置计数
                       └─ _save(order.json) → 返回图片绝对路径
```

---

## 4. 抽卡主链路：从一条消息到一张图

### 4.1 `/语录` 指令链路

```
QQ 群消息 "/语录"
  → aiocqhttp 适配器构造 AstrMessageEvent（type=GROUP_MESSAGE）
  → AstrBot 事件总线按 star_handlers_registry 遍历 handler，逐个跑 filter
  → random_quote(main.py:966) 命中：
        @filter.command("语录", alias={"随机语录"})   ← 指令匹配（带前缀）
        @filter.event_message_type(GROUP_MESSAGE)    ← 仅群聊
  → 黑名单检查 (971)
  → group_id = event.message_obj.group_id (976)
  → self._shuffler(group_id).next() (977)
  → 命中：yield event.chain_result(quote_chain(path))     ← [Plain 文本, Image]
    未命中（空图库）：yield plain_result("本群还没有群友语录哦~")
  → event.stop_event() (984) 阻止后续 handler 处理同一条消息
```

- `quote_chain`（88–96）把 `3.标题.jpg` 变成 `Comp.Plain("您抽到了SR卡\n标题.jpg")` + `Comp.Image.fromFileSystem(path)`；注释解释了**文字在前、图片在后**是因为 OneBot v11 会丢弃图片后的 Plain 文本（提交 `ed25c46`）。
- `_shuffler`（476–485）按群缓存实例，构造时快照注入 `recent_window`、`rarity_weights`、`pity`。

### 4.2 戳一戳链路（README “戳一戳机器人随机抽取”）

```
QQ 戳一戳通知（OneBot v11 notice/notify/poke）
  → aiocqhttp_platform_adapter._convert_handle_notice_event()
       group_id 存在 ⇒ AstrBotMessage.type = GROUP_MESSAGE
       raw_message = 原始 notice dict
       message 里追加 Poke(target_id)
  → on_poke(main.py:1283) 命中（只有 event_message_type 过滤，无需指令前缀）
  → 校验 post_type == "notice" / notice_type == "notify" / sub_type == "poke"  (1289–1292)
  → 校验 self_id/user_id/target_id 齐全，且 target_id == self_id（只响应戳 bot）(1294–1301)
  → 黑名单用户：静默 return，且不消耗冷却 (1304–1305)
  → 冷却闸门：now - last_poke < cooldown → return (1307–1315)
  → 立刻写回 last_poke = now（1317–1318）   ← 关键：冷却先扣
  → P1：random.random() < poke_quote_probability
        命中 → next() → 有图则 chain_result(quote_chain(path))；无图静默
        未命中 → chain_result([At(sender), Plain(random.choice(POKE_TEXTS))])
```

框架侧依据（AstrBot 上游 `aiocqhttp_platform_adapter.py`）：poke 通知被判为群消息事件并保留 `raw_message`，这正是 `on_poke` 里“先看 raw dict 再看 group_id”的写法能成立的原因。由于它**没有** `@filter.command`，它对**每一条**群消息事件都会被调用一次，靠函数体前几行的 `return` 快速退出——这是触发成本上的一个注意点（每条群消息都会进一次 `on_poke`）。

### 4.3 AI 抽卡链路

```
Agent 决定调用 tool "quote_draw"
  → ai_quote_draw(1532): async for result in self.random_quote(event): await event.send(result)
  → 复用 /语录 的完整 handler（黑名单、空库提示、加权、去重、保底、消息链一致）
  → return "已完成抽卡并发送到群内。"
  → 发送异常时：retcode=1200 → 提示“请求超时，请确认群内是否已收到”；其它异常 → 返回类型名
```

### 4.4 `QuoteShuffler.next()` 内部顺序（219–308）

1. `_list_images()`（124）：`sorted` 出的图片文件名列表；空则返回 `None`。
2. `_load()`（133）读 `order.json`；`_pity_load()`（183）读 `since` 计数。
3. **队列一致性检查**（239–246）：`set(order) != set(images)`（首次、增删/改名图片）→ 重建队列并重置 `index=0`。
4. **轮次结束检查**（248–251）：`index >= len(order)` → 用“上一轮末尾 window 张”作为 `avoid` 重建。
5. `target = order[index]`。
6. 保底计数累加（253–257）：只对 `pity_r > 0` 的星级 `since[r] += 1`。
7. 保底判定（259–288）：从 5 星往 1 星检查 `since[r] >= thr`，命中则选图并与当前位置交换。
8. 自然出货重置（290–293）：按实际抽到的稀有度重置计数（**方向有误，见 §8.1**）。
9. `index += 1`，写回 `order.json`，返回 `str(self.group_dir / target)`。

---

## 5. 概率模型逐条拆解

### 5.1 戳一戳概率 P1（唯一“真概率”开关）

```python
gs  = self._load_global_settings()                     # 1320
prob = float(gs.get("poke_quote_probability", 0.85))   # 1321
if random.random() < prob:                             # 1322
    path = self._shuffler(group_id).next()             # 1323
    if path: yield event.chain_result(quote_chain(path))  # 1325
    return                                             # 无语录时静默
else:
    yield event.chain_result([Comp.At(qq=sender_id), Comp.Plain(text=random.choice(POKE_TEXTS))])
```

要点：

- 每次戳都重新读 `settings.json`，改 WebUI 概率后**下一次戳立即生效**（不需要重载插件）。
- 冷却与概率是**与关系**：冷却（1307）不过就根本走不到概率判定。
- `last_poke` 在概率判定**之前**写入（1317）：因此①“15% 趣味文本”这一次也会吃掉冷却；②图库为空导致的静默同样吃掉冷却。属于有意设计还是副作用，README 未说明——如果需要“只在真的发出语录时才计时”，得把写入挪到 `path` 非空之后。
- `random` 是标准库 Mersenne Twister，不是密码学随机；卡片内容本身不涉及安全，够用。
- 概率值范围由 WebUI/AI 侧的校验保证 `0 ≤ p ≤ 1`（`_apply_global_settings` 1430–1434）；但 `_conf_schema.json` 的默认值路径（`_default_global_settings` 361）不做范围校验，手改 `settings.json` 也能绕开。

### 5.2 稀有度权重 P3：它影响的是“顺序”，不是长期比例 ★（v2.1.1 行为；0.2 已改为“权重＝出率”，见 §10）

`_weighted_shuffle`（155–171）不是“按权重抽一张就结束”，而是**按权重生成一轮的完整全排列**：

```python
pool = list(images); weights = [self._weight_of(n) for n in pool]
while pool:
    i = random.choices(range(len(pool)), weights=weights, k=1)[0]  # 按剩余权重比例抽
    out.append(pool.pop(i)); weights.pop(i)
```

数学性质（可证）：

- 第一步 `P(第 1 位 = i) = wᵢ / Σw`，即**每轮第一张的分布严格正比于权重**。
- 后续每一步继续按“剩余集合的权重比例”抽，因此这是合法的“加权随机排列”（biased permutation），权重高的图**平均排位更靠前**。
- **但一轮之内每张图恰好出现一次**。`next()` 只有在 `index >= len(order)` 时才重建队列，所以一轮就是一次完整遍历。
- 推论：**长期看，每个星级被抽到的比例 = 该星级图片张数 / 总张数，与权重无关**；权重只改变“什么时候轮到它”。
  - 实测（§8 仿真 B）：权重 `{5:3,4:5,3:8,2:4,1:2}`、每星级各 2 张、抽 2000 次 → 每个星级**都恰好 400 次**（200 轮 × 2 张）。
  - 这意味着 `_conf_schema.json:30` 的说明“数值越大越容易被抽到”在**单轮内前缀**（短窗口、短时间内）成立，在**长期总量**上不成立。
  - 如果目标是改变“5 星出现率”，正确手段是调整各星级的**图片张数**，或改造算法（例如改成“每抽按权重选星级、再在该星级内均匀选图”的独立同分布模型，但那会失去“一轮不重复”的特性）。
- 权重的“感知效果”确实存在：默认 `weight_3 = 8` 最大 → SR 更常出现在一轮的前几张，用户只看几抽时观感差异明显。
- 同一星级内部的顺序也由该权重决定，但同权重之间是等价随机。
- 参数细节：默认权重在 `DEFAULT_RARITY_WEIGHTS`（27–33），磁盘上的当前值在 `settings.json.rarity_weights`，由 `_load_rarity_weights`（451）转成 `{5:…,4:…,3:…,2:…,1:…}`。

### 5.3 去重窗口 P5：软去重，且有退化区间（v2.1.1 行为；0.2 已重写，见 §10）

`_build_order`（173–181）：

```python
queue = self._weighted_shuffle(images)
head  = queue[: self.window]
tail  = queue[self.window :]
head  = [x for x in head if x not in avoid] + [x for x in head if x in avoid]
return {"order": head + tail, "index": 0}
```

- 这是**排列内重排**，不是删除：`avoid` 里的元素一定还在队列里，只是被推到 head 的后面。
- 重建时 `avoid` 的两种来源：
  - 一轮走完（248–251）：`avoid = set(order[len(order) - window:])`，即**上一轮最后 window 张** —— 与注释“避免新一局开头立刻复现上一局末尾”一致。
  - 库发生变化导致中途重建（240–244）：`avoid = set(order[max(0, len(order) - index - window):])`。按 `index` = 已抽数量的约定，这个切片的长度恒为 `index + window`，**始终包含全部尚未抽到的图**（`index` 大时还会带上刚抽过的一部分），把“还没抽的图”当成要避开的对象，与去重意图相反。此处是可疑表达式，若想表达“避开刚抽过的 window 张”，应为 `order[max(0, index - window):index]`。
- **退化区间**：若图片总数 `N ≤ window`（默认 8），那么上一轮末尾的 `window` 张覆盖了全部图片 → `avoid` 包含所有元素 → `head` 过滤后顺序不变 → 去重完全失效。
  - 实测（§8 仿真 E，N=2、window=8）：连续 6 轮的首张为 `b, a, a, a, b, a`，出现连续重复。
- 保底交换（277–284）发生在 `avoid` 排序**之后**，因此**保底可以覆盖去重结果**（应当接受，但需知道“去重不是硬保证”）。
- `window` 只在 `QuoteShuffler` 构造时读取（478–481）；改 WebUI 后 `_apply_global_settings` 会 `self._shufflers.clear()`（1464），下次抽卡重建 → 新窗口生效。

### 5.4 保底 P6：设计意图与实际行为（v2.1.1 行为；0.2 已修复，见 §10）

**设计意图**（注释 220–227 + `_conf_schema.json:62`）：每个星级维护“连续多少抽没出该星级”，达到阈值 `pity_r` 就强制出一张 **稀有度 ≥ r** 的图；`0` 表示禁用；默认 `pity_5 = 10`，即“每 10 抽必出 5 星”。

**实现要点**：

- 计数：`since[r] += 1`（253–257），只对启用的星级累加；命中判定 `since[r] >= thr`（267）。
- 时序：计数在抽卡开始时累加、命中后归零，所以阈值 N 的语义是“**距上次出现该星级最多间隔 N 抽**”（计数涨到 N 的那一抽开始强制出图，直到出货才归零）。对照实验 D′：`pity_5=3`、库内只有 1 张 5 星时，第 1 抽自然出 5 星 → 计数归零 → 第 2、3 抽不出 → 第 4 抽（距上次满 3 抽）被强制出 5 星。
- 选图：`_pity_pick_target(images, r, exclude={target})`（193–217）按 `r, r+1, …, 5` 先找**恰好该档**，再 `r-1 … 1` 降档兜底；`exclude` 优先排除当前待抽目标，失败再用空集合重试（270–272）。
- 落位：把选中的图与 `order[index]` **交换**（282），被换走的图仍在队列里、后续会被抽到 → **一轮完整性不被破坏**。
- 重置：命中档位后 `for cr in range(r, 6): since[cr] = 0`（286–288）；随后自然出货重置再跑一遍（290–293）。

**关键缺陷（详见 §8.1）：290–293 的重置方向写反，导致默认的 5 星保底永远不会触发。**

```python
out_r, _ = parse_quote_rarity(target)   # 291
for cr in range(out_r, 6):              # 292  ← 重置“out_r 及以上”
    since[cr] = 0
```

因为任何一张牌的 `out_r` 都 ≤ 5，`range(out_r, 6)` **总是包含 5**，于是 `since[5]` 每抽都被清零，永远停在 `1`，`thr=10` 的判定 `1 >= 10` 永不成立。实测：`pity_5=2`、库内 3 张 1 星图，连续 6 抽后 `since` 一直是 `{'1':0,…,'5':0}`（§8 仿真 D/对照）。

正确的“≥ X 型保底”应为“抽到 `out_r` 星 ⇒ 满足所有 `X ≤ out_r` 的计数”，即 `for cr in range(1, out_r + 1): since[cr] = 0`；同理 286–288 应为 `range(1, r + 1)`。改为该方向后，对照实验里 `pity_5=3` 在第 4 抽（距上次 5 星满 3 抽）就强制出了 5 星（§8 仿真 D′）。

### 5.5 权重 0 无效 P8（实测）

```python
# _weight_of (150–153)
return float(self.weights.get(rarity, 1.0) or 1.0)   # 0 or 1.0 → 1.0
# _load_rarity_weights (456–460)
5: float(raw.get("weight_5", DEFAULT_RARITY_WEIGHTS["weight_5"]) or 1.0)
```

Python 里 `0 or 1.0` 求值为 `1.0`。所以：

- WebUI 权重滑块（`pages/admin/app.js:26`，`min="0"`）拖到 **0 并不会禁用该星级**，而是把它当作权重 1.0；
- 校验层也允许 0（`_apply_global_settings` 1447–1448 只拒绝负数）；
- 实测（随 §8 脚本一起直测）：`QuoteShuffler(weights={5:0.0})._weight_of('5.x.jpg') == 1.0`；未配置的星级也回退 1.0。

若想让 0 表达“不参与/最低优先”，需要把 `or 1.0` 改成显式 `None` 判断（例如 `w if w is not None else 1.0`），并在 UI/文档里定义语义。

### 5.6 队列持久化与并发

- 每次 `next()` 都 `_load` + `_save` 整个 `order.json`（133–148），包含 `order`（全量文件名数组）、`index`、`since`。重启/重装不丢进度。
- 没有文件锁；AstrBot 同一群的多条抽卡消息并发时会读-改-写竞争，可能丢掉一次 `index++`（表现为偶发“同一张被抽两次”或计数回退）。如果群流量大，建议加按群的 `asyncio.Lock`。
- `since` 的键写成字符串（300），读取时兼容 int/str（183–191）。

---

## 6. 参数从哪来：三级配置与缓存

```
_conf_schema.json（默认值）
      │  插件配置弹窗，存 AstrBotConfig → self.config
      ▼
_default_global_settings()  (356)      ← 提供“默认层”
      │
      ▼
data/plugin_data/quote_collocter/settings.json  ← 实际运行时有效值
      │  _load_global_settings() (376) 逐字段合并（字典类型做二级合并）
      ├─→ poke_quote_probability → P1
      ├─→ rarity_weights        → _load_rarity_weights (451) → P3/P8
      ├─→ pity_config           → _load_pity (463)           → P6
      ├─→ recent_window         → QuoteShuffler.window       → P5
      ├─→ default_submit_mode / default_cooldown → _load_settings (426) 的默认值
      └─→ blocked_user_ids      → 黑名单

data/plugin_data/quote_collocter/<群号>/settings.json   ← 群覆盖层
      mode / cooldown / last_poke（_load_settings 434–438 覆盖全局默认）
```

**必须注意的两个行为**：

1. **插件配置弹窗只在 `settings.json` 不存在该字段时生效。** `_load_global_settings` 先用 config 造默认值，再用磁盘值覆盖；但 `_save_global_settings` 会写出**全量字段**（396–402），而 WebUI 保存（1414）和任意一次黑名单操作（1394–1412）都会写全量。因此一旦发生过任一保存动作，之后再改插件配置弹窗的这几项就“看不出效果”，必须在 WebUI Page 里改。README“两处使用同一份运行时设置”在“读”的意义上成立，但“写”只有 Page/群指令/AI 三条路径。
2. **缓存与失效时机**：`self._shufflers`（318）按群缓存；`_apply_global_settings` 成功后 `.clear()`（1464），所以权重/保底/窗口改完立即生效；但改的是插件配置弹窗时不清缓存，需要重载插件。文件删除/改名/改稀有度走 `_invalidate_shuffler`（845），下次抽卡因 `set(order) != image_set` 自动重建顺序，`since` 计数保留。

---

## 7. 触发条件总清单（什么时候不会触发）

| 场景 | 结果 | 位置 |
| --- | --- | --- |
| 私聊发送 `/语录` | 不触发（`GROUP_MESSAGE` 过滤） | 966–967 |
| 被拉黑用户发 `/语录` | 回复“已被拉黑”，不抽卡、不推进队列 | 971–974 |
| 群图库为空 | 提示“本群还没有群友语录哦~” | 980–983 |
| `/语录查看`、管理类指令非 bot 管理员 | 被 `PermissionType.ADMIN` 拦截（框架层） | 990 等 |
| `/语录删除` 既无名称又非回复卡片 | 输出用法 | 1156–1161 |
| 投稿：群模式 0 / 模式 1 且非管理员 | 拒绝 | 935–938 |
| 投稿：未回复含图消息 | 引导文案（`submit_quote` 1020–1031） | 940–942 |
| 投稿：标题与既有语录重名（忽略稀有度前缀） | 拒绝 | 949–950 |
| 投稿：图片下载三级全失败 | 拒绝 | 952–954 |
| 撤回：无投稿记录 / 非本人 | 拒绝 | 913–917 |
| 戳一戳：不是戳 bot / 冷却中 / 被拉黑 | 静默 return | 1300、1303–1305、1312–1315 |
| 戳一戳：概率落空 | 发随机趣味文本（吃冷却） | 1329–1333 |
| 戳一戳：概率命中但图库为空 | **静默**（吃冷却） | 1324–1328 |
| AI 工具：私聊 | 返回“语录系统仅支持群聊使用。” | `_tool_group_id` 1347 |
| AI 工具：写操作非管理员 | 返回“权限不足…”文本 | 1581、1600、1621、1678、1692、1707 |

另外注意指令参数解析的两个实现细节：

- `rename_quote`（1172–1184）与 `change_rarity`（1203–1217）**不用**框架传入的 `old`/`new`/`name`/`rarity` 参数，而是自己 `event.message_str.split(maxsplit=1)` 重新切分。原因是“回复模式”下参数个数不同（一个参数表示新值）。框架参数只是占位。
- `_resolve_target_user_id`（416–424）优先取消息链里的 `Comp.At`，其次要求纯数字 UID —— 所以 `拉黑` 一个非好友的 QQ 时只能用 UID。

---

## 8. 已知问题与风险清单（含实测证据与修法）

### 8.1 【高】默认 5 星保底永远不会触发（重置方向写反）【0.2 已修复，见 §10.2】

- 位置：`main.py:290–293`（自然出货重置）与 `286–288`（保底命中重置）。
- 原因：`range(out_r, 6)` 在 `out_r ∈ 1..5` 时**总是包含 5**，所以 `since[5]` 每次抽卡后都归零；`since[r]` 只有在“抽到的星级 > r”时才能活过一次抽卡。
- 后果：README 与 `_conf_schema.json` 承诺的“每 10 抽必出 5 星”**完全无效**（只有把 `pity_5` 设成 1 才会每次都“触发”）。`pity_1` 是唯一“可能表现正常”的档位；`pity_4/3/2` 也只有在更高星级频繁出货时才可能累积。
- 证据：仿真 D（`pity_5=2`、3 张 1 星图，6 抽后 `since` 全 0）；对照实验（仅把 290–293 改成 `range(1, out_r + 1)`，`pity_5=3` 时“距上次 5 星满 3 抽”即被强制出 5 星）。
- 修法：

```python
# 290–293：抽到 out_r 星 ⇒ 满足所有 X ≤ out_r 的保底
for cr in range(1, out_r + 1):
    since[cr] = 0

# 286–288：同理
for cr in range(1, r + 1):
    since[cr] = 0
```

修完建议补一个单元测试：固定库为 1 张 3 星 + 若干 1 星、`pity_3=3`，断言“任意两次 3 星之间的间隔不超过 3 抽”（而不是断言“第 3/6/9 抽必为 3 星”这种绝对位置）。

### 8.2 【中】权重 0 被当作 1.0【0.2 已修复，见 §10.2】

见 §5.5。修法：`_weight_of` 与 `_load_rarity_weights` 里改掉 `or 1.0`；同时明确 UI 语义（0 = 最低优先 / 0 = 禁用）。若希望“0 = 该星级不出现”，还要在 `_weighted_shuffle` 里过滤 `weight <= 0` 的项（注意别把图库抽空）。

### 8.3 【低】保底“卡死钳制”分支不可达

`next()` 268–275 的 `pick is None → since[r] = thr; continue` 只有在图库里找不到任何图片时才会走到；而 `next()` 开头已经保证 `images` 非空，`_pity_pick_target` 又会在 `r, r+1..5` 与 `r-1..1` 全范围内兜底，所以总能返回一张（`exclude=set()` 的那次调用必然成功）。属于死代码，可用 `if not images` 单独处理来简化。

另外 274 行注释“该档无图可保底，留给下一档”与实际循环方向不一致：判定是从 5 星向 1 星降序遍历，`continue` 的下一站是**更低**的星级。

### 8.4 【低】去重窗口的两个边界问题【0.2 已重写，见 §10.2】

- `N ≤ window` 时软去重完全失效（§5.3，实测仿真 E）。建议“window 取 `min(window, max(0, N - 1))`”或退化为硬排除。
- 库变化时中途重建的 `avoid` 切片语义可疑（240–244，取到了未抽部分）。建议改为 `order[max(0, index - self.window):index]`。

### 8.5 【中】全局设置一旦写过就冻结插件配置弹窗

见 §6 第 1 点。这是“两处配置同一份设置”的语义歧义，建议在 README/Page 里明确“Page 为准，插件配置仅作初值”；或在 `_load_global_settings` 里记录来源优先级。

### 8.6 【低】代码细节

| 位置 | 问题 |
| --- | --- |
| 1653–1654 | `ai_quote_revoke` 里两句 `return` 紧邻，第二句是死代码 |
| 1682–1683 | `ai_quote_configure_group` 丢弃了 `_apply_group_settings` 的 `ok`，失败与成功都返回同一文本（虽然 1373–1392 已经过滤了非法值，仍建议区分） |
| 303–307 | 保底日志只在 `bumped` 且 `triggered_r` 时打印；若保底命中但目标恰好已经在位（无需交换），不会留下任何日志，难以排查 |
| 208–217 | `_pity_pick_target` 的注释说“按星级降序遍历”，实现是先升档再降档；注释与代码不一致 |
| 1727–1760 | `web_submit_log` 的 `total` 是**截断后**的条数，不是过滤后的总数，前端“共 N 条”会误导 |
| 215 / 211 | 保底在同星级内是均匀 `random.choice`，不参考权重；若期望“保底也偏向高权重”，需另行处理 |
| 1331 | 趣味文本分支同样消耗冷却（见 §5.1），README 未说明 |
| 490–497 | `_save_bytes` 直接用传入文件名覆盖写；虽然上游有重名校验，但 `_download_image` 作为公共方法本身没有防重名/防目录穿越校验（文件名里若含 `/` 会写到子路径） |

### 8.7 与 README 描述不一致/未覆盖的点

| README 说法 | 实际 |
| --- | --- |
| “稀有度权重：数值越大越容易被抽到”（`_conf_schema.json:30`） | 只在轮内顺序/短期窗口成立，长期比例由图片张数决定（§5.2） |
| “每 N 抽必出一次对应星级”（README 78） | 当前不可用（§8.1） |
| “两处使用同一份运行时设置”（README 69） | 读同一份，写只有 Page/指令/AI（§8.5） |
| 未提及 | 一份按群 `asyncio.Lock` 都没有，并发抽卡可能丢 `index`（§5.6） |
| 未提及 | `on_poke` 对每条群消息都会被调用一次（§4.2），大群下有轻微额外开销 |

---

## 9. 附录

### 9.1 复现/验证方式（本次分析怎么做的）

`QuoteShuffler` 是纯逻辑类（只依赖 `json/random/time/Path` 和一个 `logger`），因此可以把 `main.py:66–308` 原样抽出到临时模块里跑仿真，无需 AstrBot 运行时：

```python
# /tmp/quote_sim/shuf.py : main.py 第 66–308 行 + 轻量 logger / 常量桩
import pathlib, sys
src = pathlib.Path("main.py").read_text(encoding="utf-8").splitlines()
head = """
import json, random, time
from pathlib import Path
class _L:
    def warning(self,*a): pass
    def error(self,*a): pass
    def info(self,*a): pass
logger=_L()
IMAGE_EXTENSIONS = {'.jpg','.jpeg','.png','.bmp','.gif','.webp'}
RARITY_TEXT = {5:'a',4:'b',3:'c',2:'d',1:'e'}
"""
pathlib.Path("/tmp/quote_sim/shuf.py").write_text(head + "\n".join(src[65:308]) + "\n", encoding="utf-8")
# 之后 from shuf import QuoteShuffler，用临时目录当“群目录”，放几张空图片文件即可抽卡
```

| 仿真 | 设置 | 结果 | 结论 |
| --- | --- | --- | --- |
| A | 2 张图（5 星权重 3 / 1 星权重 2），4000 轮记录每轮首张 | 2452 : 1548 ≈ 0.613 : 0.387 | 与 `wᵢ/Σw = 0.6/0.4` 吻合 → **首张分布正比于权重** |
| B | 每星级各 2 张、权重 `{5:3,4:5,3:8,2:4,1:2}`，抽 2000 次 | 每星级**恰好 400 次** | **长期比例 = 张数比例，与权重无关** |
| C | 同上 + `pity_5=10`，看 20 抽序列 | 无任何固定位置出 5 星 | 默认保底无效 |
| D | 3 张 1 星图 + `pity_5=2`，逐抽打印 `since` | `since` 恒为全 0 | 计数被每抽清零 |
| D′ | 仅把 290–293 改为 `range(1, out_r + 1)` | `pity_5=3`：第 1 抽自然出 5 星，第 4 抽被强制出 5 星 | 证明修复方向正确 |
| E | 2 张图、`window=8`，观察 6 轮首张 | `b, a, a, a, b, a` | `N ≤ window` 时去重退化 |
| F | 1 张 3 星（极低权重）+ 4 张 1 星 + `pity_3=3` | 3 星只出现在第 5/10/15… 抽（自然位置） | 保底未生效（同 §8.1） |

### 9.2 关键行号速查表（`main.py`）

| 符号 | 行号 | 符号 | 行号 |
| --- | --- | --- | --- |
| 常量区 | 18–63 | `_log_submission` | 653 |
| `parse_quote_rarity` | 66 | `_read_submit_log` | 672 |
| `quote_chain` | 88 | `_rewrite_submit_log` | 692 |
| `class QuoteShuffler` | 99 | `_find_submission_record` | 702 |
| `_list_images` | 124 | `_remove/_rename_submission_records` | 714 / 728 |
| `_load` / `_save` | 133 / 142 | `_quote_info_text` | 754 |
| `_weight_of` | 150 | `_quote_chain_with_info` | 760 |
| `_weighted_shuffle` ★ | 155 | `_list_quote_files` | 771 |
| `_build_order` | 173 | `_find_quote_file` | 797 |
| `_pity_load` | 183 | `_invalidate_shuffler` | 845 |
| `_pity_pick_target` | 193 | `_delete_quote_file` | 849 |
| `next()` ★ | 219 | `_rename_quote_file` | 861 |
| `class QuotePlugin` | 311 | `_set_quote_rarity_file` | 886 |
| `__init__` / Web API 注册 | 314 / 320–337 | `_revoke_quote_file` | 909 |
| `_group_dir` / `_ensure_group_dir` | 342 / 345 | `_submit_replied_quote` | 927 |
| `_default/_load/_save_global_settings` | 356 / 376 / 396 | `random_quote` `/语录` | 966 |
| `_blocked_user_ids` / `_is_blocked_user` | 404 / 411 | `view_quote` `/语录查看` | 989 |
| `_resolve_target_user_id` | 416 | `submit_quote` `/语录投稿` | 1012 |
| `_load_settings` / `_save_settings` | 426 / 443 | `quote_status` `/语录状态` | 1039 |
| `_load_rarity_weights` | 451 | `set_submit_mode` `/投稿权限` | 1069 |
| `_load_pity` | 463 | `set_poke_cooldown` `/戳戳冷却` | 1089 |
| `_shuffler`（缓存工厂） | 476 | `block/unblock` 拉黑/解封 | 1106 / 1125 |
| `_download_image`（三级容错） | 499 | `delete_quote` `/语录删除` | 1147 |
| `_get_replied_message_chain` | 555 | `rename_quote` `/语录改名` | 1169 |
| `_resolve_reply_image_file` | 575 | `change_rarity` `/语录改稀有` | 1200 |
| `_detect_ext` | 600 | `revoke_quote` `/撤回投稿` | 1239 |
| `_resolve_reply_quote_file` | 611 | `on_poke` ★ | 1283–1333 |
| `_is_admin` / `_tool_group_id` | 1338 / 1347 | `_apply_group_settings` | 1373 |
| `_quote_stats_text` | 1352 | `_update_blacklist` | 1394 |
| `_apply_global_settings` | 1414 | AI Tools 12 个 | 1470–1710 |
| Web API handler | 1715 / 1719 / 1727 | `terminate` | 1762 |

### 9.3 框架侧依据（AstrBot 上游）

- 插件可视化视图：`views/` 优先、兼容 `pages/`；bridge 为 `window.AstrBotPluginView`（别名 `AstrBotPluginPage`）；路由为 `/api/v1/plugins/extensions/<plugin_name>/<path>`，`register_web_api` 的 route 必须以插件名为前缀 —— [Plugin Pages 文档](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)。本插件 `PLUGIN_NAME = "quote_collocter"`（`main.py:18`）与 `metadata.yaml: name: quote_collocter` 一致，因此 `bridge.apiGet("settings")` 能命中 `/quote_collocter/settings`。
- `@filter.llm_tool` 的**参数 schema 由 docstring 的 `Args:` 块解析**（`param(type): desc`），不读 Python 注解；写成 `yield event.plain_result(...)` 会让 Agent 拿不到工具返回值 —— [AI 文档](https://docs.astrbot.app/en/dev/star/guides/ai.html)。这解释了 v2.1.1 的修复内容。
- 插件元数据 `name` 决定 WebUI 路由主语：上游 `star_manager._load_plugin_metadata` 用 `metadata["name"]` 构造 `StarMetadata`，页面服务按 `plugin.name` 匹配（[star_manager.py](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/star/star_manager.py)、[plugin_page_service.py](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/dashboard/services/plugin_page_service.py)）。
- poke 通知会变成 `MessageType.GROUP_MESSAGE` 事件且 `raw_message` 保留原始 dict、消息链追加 `Poke(target_id)`（[aiocqhttp_platform_adapter.py](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)）—— 这是 `on_poke` 判定逻辑的成立前提。

> 说明：本机环境未安装 AstrBot（`import astrbot` 失败），以上框架结论来自对上游 `master` 源码与官方文档的在线核对，未在本机运行时验证。

---

## 10. 0.2 变更：改为独立同分布（IID）抽取

> §1–§9 是自用线 0.1 基线（上游 v2.1.1）的实现记录，保留作为对照；本节说明 0.2 的替换内容、0.3 新增的调试模式、旧问题的修复状态与新的实测数据。
> 变更涉及：`main.py`（抽卡核心重写）、`_conf_schema.json`、`pages/admin/index.html`、`README.md`、`metadata.yaml`。

### 10.1 新的抽取模型

`QuoteShuffler`（一轮全排列 + 指针递增）被整体删除，替换为 `QuoteDrawer`（`main.py:168` 起，调试渲染器 `format_draw_trace` 在 `main.py:101`）。**每次 `next()`（`main.py:326`）都是一次独立抽取**，与上一次抽到什么无关：

1. **按权重抽星级**（`_pick_rarity`）：只在「本群有图的星级」里按 `rarity_weights` 归一化抽取。
   → 星级出率严格等于权重占比；权重 `0` 表示该星级不参与；全部为 `0` 时回退为“按星级均匀”。
2. **星级内均匀抽图**：`random.choice(该星级图片)`，不再有队列、指针与“一轮不重复”。
3. **短期去重**（`_avoid_for`）：排除「同一星级内、最近 `window` 抽里出现过的图片」，排除数量上限为 `该星级图片数 − 1`，保证至少剩一张可抽；`window = 0` 关闭。
   去重只作用于“选哪张”，不影响“选哪个星级”，所以**不会扭曲星级出率**。
4. **保底**：`since[星级]` 每抽 `+1`（仅启用档位，并**钳制在阈值以内**，这样本群暂时缺图时计数最多显示 `N/N`）；达到阈值即强制该星级（优先恰好该档、其次升档）；**该档及以上没有图时不消耗计数**；出货后把「`≤ 抽到星级`」的计数全部清零——修正了旧版 §8.1 的方向写反 Bug。

状态文件仍是 `<群号>/order.json`，内容改为 `{"since": {...}, "recent": [...]}`；旧文件的 `order`/`index` 被忽略，`since` 直接沿用，无需迁移。

### 10.2 旧问题在新模型下的状态

| 旧问题 | 0.2 起的状态 |
| --- | --- |
| §8.1 默认 5 星保底永不触发 | **已修复**：计数方向正确；实测最大间隔 = 阈值 |
| §8.2 权重 0 被当成 1.0 | **已修复**：`0` = 该星级不出（`_weight_of` / `_load_rarity_weights` 去掉了 `or 1.0`） |
| §5.2 权重不影响长期比例 | **已修复**：权重占比即出率（实测吻合到 ±0.005） |
| §8.3 保底“钳制”分支不可达 | **已重写**：该分支现在是“该档无图 ⇒ 保留计数”的正常路径 |
| §8.4 去重窗口退化（N ≤ window 失效） | **已修复**：排除上限 = 图数 − 1；2 张图的星级实测相邻重复 0 |
| §8.4 中途重建 `avoid` 语义可疑 | **不再存在**：没有队列与重建逻辑 |
| §5.6 并发读-改-写竞争 | **仍然存在**：`order.json` 无锁，同群并发抽卡可能丢一次 `recent`/`since` 更新 |
| §8.6 其它代码细节（死代码等） | 未受影响，仍如原文所述 |
| §8.5 全局设置冻结插件配置弹窗 | 未受影响（配置来源逻辑未改） |

### 10.3 实测（提取新类仿真，每次 20000 抽）

默认权重 `{5:3, 4:5, 3:8, 2:4, 1:2}` → 理论出率 5★13.6% / 4★22.7% / 3★36.4% / 2★18.2% / 1★9.1%。

| 图库 | window | 实测星级出率（5/4/3/2/1） | 相邻两次同一张 |
| --- | --- | --- | --- |
| 每星级各 2 张 | 0 | 13.6 / 22.5 / 36.3 / 18.3 / 9.3 % | 12.3% |
| 每星级各 2 张 | 8 | 13.9 / 22.7 / 36.4 / 18.3 / 8.8 % | **0%** |
| 真实库（1★20、2★5、3★2、4★1、5★1） | 0 | 13.2 / 22.8 / 36.2 / 18.4 / 9.3 % | 14.2% |
| 真实库 | 1 | 14.0 / 22.8 / 36.1 / 17.7 / 9.5 % | 7.0% |
| 真实库 | 8 | 13.7 / 22.4 / 36.5 / 18.3 / 9.1 % | 6.6% |

- 所有窗口下出率都等于权重占比 —— 去重不再扭曲星级分布。
- 残余的相邻重复 ≈ `4★² + 5★² = 7.0%`，只来自“该星级只有 1 张图”的客观限制，无法避免；`window=0` 时 14.2% ≈ `Σpᵢ²`，与独立同分布理论值一致。
- 保底回归：`pity_5=10` 且有 5★ 图时，最长间隔 = 10 抽；**没有** 5★ 图时计数停在阈值，补入一张 5★ 后下一抽立即命中。
- 权重边界：`weight_5=0` → 5★ 出现 0 次；全 0 → 回退为星级均匀（5000 抽中各约 1000 次）。
- 兼容性：旧 `order.json`（含 `order`/`index`/`since`）可直接读取，`since` 保留。

### 10.4 关于“用当前时间当种子”

不需要，也不建议：`random` 在导入时已用操作系统熵源播种，`random.seed(time.time())` 反而把熵源降级为**秒级时间戳**——可预测、同一秒内会得到相同序列，是“更差”的随机。

v2.1.1 让人觉得“不像随机”的根因是**一轮不重复的洗牌队列**（§5.2）：一轮内每张图必出且只出一次，看起来就像按顺序发牌。0.2 用 IID 模型消除了这个根因，不需要换种子。

若确实想要密码学级随机（例如防止被预测），可以把 `random` 换成 `random.SystemRandom()` 实例（`random()` / `choice()` / `choices()` 接口一致，替换无痛），但对语录抽卡没有必要。

### 10.5 调试模式（0.3 加入，按群生效）

为了让「这一次到底按什么概率抽的、是不是保底出的」可以直接观察，0.3 增加了三档调试模式：`0` 关闭 / `1` 仅日志 / `2` 日志 + 群内卡片附注。

**作用范围是按群**：档位存在该群的 `<群号>/settings.json`（字段 `debug_mode`），与 `mode`/`cooldown` 同级，因此一个群开调试不会影响其它群。切换入口有三个，都落到同一个校验函数 `_apply_group_settings(..., debug_mode=...)`：

| 入口 | 位置 |
| --- | --- |
| 群内管理员指令 `/语录调试 <0|1|2>`（不带参数显示本群档位） | `main.py` `set_debug_mode` |
| AI 工具 `quote_configure_group` 的 `debug_mode` 参数 | `main.py` `ai_quote_configure_group` |
| WebUI Page「按群调试模式」区（填群号 + 查询/保存） | `web_get_group_debug` / `web_set_group_debug`，路由 `GET/POST /quote_collocter/group-debug` |

全局侧的 `debug_mode` 已从 `_default_global_settings`、`_apply_global_settings`（改为显式报错，避免静默丢失）与 `_conf_schema.json` 中移除。`/语录状态` 与 AI 的 `quote_status` 都会显示本群档位。

**数据产生侧**：`QuoteDrawer.next()` 每次抽取后把本次模型状态写入 `self.last_trace`，字段如下：

| 字段 | 含义 |
| --- | --- |
| `total` / `counts` | 图库总张数与各星级张数（`counts` 固定含 1–5 全部档位） |
| `weights` / `share` | 配置权重，以及只在“有图的星级”间归一化后的**本次实际出率** |
| `window` / `recent` / `excluded` / `candidates` / `pool_size` | 去重窗口、最近抽到记录、被排除张数、该星级可选张数与该星级总张数 |
| `rarity_by_weight` / `rarity_final` | 权重原本抽中的星级、最终生效的星级（被保底覆盖时两者不同） |
| `pity.thresholds` / `pity.since_used` | 各档阈值、本次判定用的「距上次出该星级的抽数」快照 |
| `pity.triggered` / `pity.skipped` / `pity.since_after` | 触发档位、因无图而跳过的档位、出货重置后的计数 |
| `final` | 最终命中的文件名 |

**渲染侧**：模块级纯函数 `format_draw_trace(trace, verbose)`（`main.py:101`）把 trace 渲染成文本——日志用 `verbose=True`（多出「权重」与「出货后计数」两行），群内附注用 `verbose=False`。

**输出侧**：`QuotePlugin._draw_quote(group_id, source, user_id)` 是唯一的抽卡入口，指令 `/语录`、戳一戳 `on_poke`、AI `quote_draw` 都经它取图，因此三条路径的调试口径完全一致：先 `next()`，再按 `_debug_level(group_id)` 决定是否 `logger.info("[语录调试] 来源=… 用户=…")`，并把简版附注交给 `_with_debug_note()`。

**注意**：附注必须并入消息链**图片之前**的 `Plain` 段（`_with_debug_note`），因为 OneBot v11 会丢弃图片之后的文本（§2.1 的同一原因）；调试关闭或图库为空时不产生任何附注。日志版样例见 README「调试模式」一节。
