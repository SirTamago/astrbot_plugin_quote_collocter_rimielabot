import base64
import csv
import io
import json
import mimetypes
import os
import random
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote as urlquote

try:  # AstrBot 自带 Pillow；缺失时管理页退化为不显示缩略图
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover - 仅在无 Pillow 的环境触发
    _PILImage = None

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import (
    error_response,
    file_response,
    json_response,
    request,
)
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

PLUGIN_NAME = "quote_collocter"

# 群友语录图片存放根目录（遵循 AstrBot 存储大文件规范：data/plugin_data/{插件名}）
QUOTES_ROOT = Path("data") / "plugin_data" / "quote_collocter"

# 全局设置文件（供 WebUI Page 与插件共享读写）
GLOBAL_SETTINGS_PATH = QUOTES_ROOT / "settings.json"

# 稀有度刻度：1..6 星（明日方舟同款，6★ 最高）。文件名首位数字即星级。
RARITY_ORDER = (6, 5, 4, 3, 2, 1)
MIN_RARITY = 1
MAX_RARITY = 6

# 默认稀有度权重（6★ 约 2%、5★ 约 8%、4★ 约 50%、3★ 约 40%）。
# 语义：每次抽取先按权重决定星级、再在该星级内均匀抽图，
# 因此权重占比就是该星级的实际出率；权重 0 表示该星级不会被抽到。
# 1.6 起 2★（1%）与 1★（0.1%）也参与抽卡。
DEFAULT_RARITY_WEIGHTS = {
    "weight_6": 2,
    "weight_5": 8,
    "weight_4": 50,
    "weight_3": 40,
    "weight_2": 1,
    "weight_1": 0.1,
}

# 默认保底配置：每 N 抽必出一次对应星级，0 表示禁用该等级保底。
# 默认每 10 抽必出一次 6★。保底只在本群存在「该星级或更高星级」图片时生效，
# 否则保留计数不消耗，等有图后立即触发（不会退化成硬凑低星）。
DEFAULT_PITY = {
    "pity_6": 10,
    "pity_5": 0,
    "pity_4": 0,
    "pity_3": 0,
    "pity_2": 0,
    "pity_1": 0,
}

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}

# 戳一戳触发的趣味文本
POKE_TEXTS = [
    "再戳的话……说不定下一张就是你的！",
    "我会一直一直看着你👀",
    "给我出列！",
]

# 语录稀有度文本：直接显示明日方舟同款星星数量（文件名首位数字 6..1）。
RARITY_TEXT = {
    6: "★★★★★★",
    5: "★★★★★",
    4: "★★★★",
    3: "★★★",
    2: "★★",
    1: "★",
}

# 新投稿的默认星级：取「最低可抽档」（3★），避免新投稿落进 1★/2★ 的
# 0 权重档而抽不到；想改默认档位改这里即可。
DEFAULT_SUBMIT_RARITY = 3

# 稀有度刻度迁移标记：0.1~1.0 用的是 1..5 档，1.1 起改为 1..6 星。
RARITY_SCALE_MARKER = QUOTES_ROOT / ".rarity_scale"
RARITY_SCALE_VERSION = "6-star"

# 管理页缩略图尺寸（像素）：页面里以 data URL 内嵌，避免 iframe 直接请求接口
THUMB_SIZE = (160, 120)

# /语录统计 导出的 CSV 通过私聊发给这个 QQ（管理员）
STATS_RECEIVER_QQ = "2632364603"

# /语录help 的内容（按权限分组，改指令时记得同步这里）
COMMAND_HELP = (
    (
        "全体成员",
        (
            ("/语录", "随机抽一张本群语录（别名 /随机语录）"),
            ("/语录投稿 [标题]", "回复一张图片投稿，标题可选"),
            ("/撤回投稿 [名称]", "撤回自己的投稿，不带名称时撤回最近一条"),
            ("/语录状态", "本群总数、各星级张数、权限与冷却（别名 /投稿状态）"),
        ),
    ),
    (
        "管理员",
        (
            ("/语录help", "这张指令表（别名 /语录帮助）"),
            ("/语录查看 <名称>", "直接查看指定语录，不影响抽卡状态"),
            ("/语录删除 <名称>", "删除语录，也可回复 bot 的卡片后直接发"),
            ("/语录改名 <旧名称> <新标题>", "只改标题，保留星级与扩展名"),
            ("/语录改稀有 <名称> <1-6>", "修改星级"),
            ("/语录出货 [页码]", "每张图被抽中的次数与占比（别名 /出货统计）"),
            ("/语录统计", "导出全部统计为 CSV，私聊发给管理员"),
            ("/语录拉黑 @用户 或 <UID>", "全局拉黑：不能投稿或抽卡"),
            ("/语录解封 @用户 或 <UID>", "解除拉黑"),
            ("/投稿权限 <0|1|2>", "0 关闭 / 1 仅管理员 / 2 全体成员"),
            ("/戳戳冷却 <秒>", "戳一戳触发语录的冷却时间（别名 /语录冷却）"),
            ("/语录调试 <0|1|2>", "0 关闭 / 1 写日志 / 2 日志 + 卡片附注"),
        ),
    ),
)


def parse_quote_rarity(filename: str) -> tuple[int, str]:
    """解析 '<1-6>.<string>.<ext>' 格式的文件名（首位数字 = 星级）。

    返回 (星级, 展示名)。
    展示名 = 文件名中的 [string] + 文件扩展名。
    不符合格式或星级超出 1..6 的文件名按 1★ 处理，展示名为去掉编号后的原文。
    """
    p = Path(filename)
    ext = p.suffix
    stem = p.stem
    parts = stem.split(".", 1)
    if len(parts) == 2 and parts[0].isdigit():
        rarity = int(parts[0])
        if rarity not in RARITY_TEXT:
            rarity = 1
        string = parts[1]
    else:
        rarity = 1
        string = stem
    return rarity, f"{string}{ext}"


def quote_chain(path: str) -> list:
    """根据语录图片路径构建 [稀有度文本 + 图片] 消息链。

    文本放在图片之前：aiocqhttp / OneBot v11 对位于图片之后的 Plain 文本
    常会丢弃，采用「文字在前、图片在后」以保证文字可见。
    """
    rarity, display = parse_quote_rarity(Path(path).name)
    text = f"{RARITY_TEXT[rarity]}\n{display}"
    return [Comp.Plain(text=text), Comp.Image.fromFileSystem(path)]


def format_draw_trace(trace: dict | None) -> str:
    """渲染调试信息：标题 + 出货计数 + 各星级「实际(设定)概率」。

    出货计数是各星级**累计抽到次数**，与保底计数无关（保底只影响抽到什么，
    计数按实际抽到的星级累加），因此可以直接核对实际出率是否符合权重。
    概率行只列出「设定权重非 0」或「已有出货」的星级，避免 0 权重档刷屏。
    """
    if not trace:
        return ""

    stats = trace.get("stats") or {}
    weights = trace.get("weights") or {}
    total = int(trace.get("total_draws", 0))

    counts_line = " / ".join(f"{r}★{int(stats.get(r, 0))}" for r in RARITY_ORDER)
    total_weight = sum(max(0.0, float(weights.get(r, 0) or 0)) for r in RARITY_ORDER)

    rate_parts: list[str] = []
    for r in RARITY_ORDER:
        weight = max(0.0, float(weights.get(r, 0) or 0))
        count = int(stats.get(r, 0))
        if weight <= 0 and count <= 0:
            continue
        actual = (count / total * 100) if total else 0.0
        configured = (weight / total_weight * 100) if total_weight > 0 else 0.0
        rate_parts.append(f"{r}★{actual:.1f}({configured:g})%")

    lines = ["[语录调试]", f"出货计数：{counts_line}"]
    if rate_parts:
        lines.append("概率统计：" + " / ".join(rate_parts))
    return "\n".join(lines)


def load_hits(data: dict, image_set: set[str] | None = None) -> dict:
    """读取每张图片的累计被抽中次数；给 image_set 时丢弃已删除的文件。"""
    raw = data.get("hits") or {}
    if not isinstance(raw, dict):
        raw = {}
    hits: dict[str, int] = {}
    for name, count in raw.items():
        if image_set is not None and name not in image_set:
            continue
        try:
            hits[str(name)] = int(count)
        except (TypeError, ValueError):
            continue
    return hits


class QuoteDrawer:
    """群聊语录抽取器（独立同分布模型 / IID）。

    每次抽卡都是独立随机，与上一次抽到什么无关：
      1. 先按稀有度权重抽一个【星级】——权重占比即该星级的实际出率，
         权重为 0 的星级不参与；若所有可抽星级的权重都为 0，则回退为按星级均匀；
      2. 再在该星级的图片里【均匀】随机挑一张（不参考权重）；
      3. 短期去重：同一星级内、最近 window 抽里出现过的图片不再参与本次抽取
         （window = 0 关闭）。排除数量上限为「该星级图片数 - 1」，保证至少
         剩一张可抽，因此该星级有 2 张以上图时不会连着出同一张；只有 1 张图
         的星级无法避免重复。去重只作用于「选哪张」，不影响「选哪个星级」，
         所以星级出率始终严格等于权重占比；
      4. 保底：某星级「距上次抽出该星级」的抽数达到阈值时强制改抽该星级，
         优先恰好该档，其次升档；该档及以上都没有图时不生效（保留计数不消耗，
         等有图后立即触发），不会硬凑低星。

    状态持久化到群目录下的 order.json（`since` 保底计数 + `recent` 最近抽到），
    重启后可继续；文件名沿用旧版，升级时旧文件里的 `since` 直接兼容。

    调试：每次抽取后把本次的模型状态写入 `self.last_trace`（图库构成、权重与
    实际出率、去重前后候选数、保底判定与是否命中、最终结果），供插件层按
    「调试模式」输出到日志或群内卡片；未抽到图时为 None。
    """

    def __init__(
        self,
        group_dir: Path,
        window: int,
        weights: dict | None = None,
        pity: dict | None = None,
    ):
        self.group_dir = group_dir
        self.state_file = group_dir / "order.json"
        # 去重窗口：0 表示关闭去重
        self.window = max(0, int(window))
        # 稀有度 -> 权重（0 表示该星级不被抽到）
        self.weights = weights or {}
        # 保底：稀有度 -> 每 N 抽必出（0 表示禁用）
        self.pity = pity or {}
        # 最近一次抽取的调试数据（由 next() 写入）
        self.last_trace: dict | None = None

    def _list_images(self) -> list[str]:
        if not self.group_dir.exists():
            return []
        return sorted(
            f.name
            for f in self.group_dir.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        )

    def _load(self) -> dict:
        try:
            if self.state_file.exists():
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception as e:
            logger.warning(f"读取抽卡状态失败，将重建: {e}")
        return {}

    def _save(self, data: dict) -> None:
        try:
            self.group_dir.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存抽卡状态失败: {e}")

    def _weight_of(self, rarity: int) -> float:
        """星级权重；未配置回退 1.0，0 或负数表示该星级不被抽到。"""
        raw = self.weights.get(rarity)
        if raw is None:
            return 1.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def _rarity_pools(images: list[str]) -> dict[int, list[str]]:
        """把图片按稀有度分组：{星级: [文件名, ...]}。"""
        pools: dict[int, list[str]] = {}
        for name in images:
            rarity, _ = parse_quote_rarity(name)
            pools.setdefault(rarity, []).append(name)
        return pools

    def _pick_rarity(self, pools: dict[int, list[str]]) -> int | None:
        """按权重在「有图的星级」里抽一个；权重全为 0 时回退为均匀抽取。"""
        rarities = sorted(pools)
        if not rarities:
            return None
        weights = [self._weight_of(r) for r in rarities]
        if sum(weights) <= 0:
            return random.choice(rarities)
        return random.choices(rarities, weights=weights, k=1)[0]

    def _rarity_shares(self, pools: dict[int, list[str]]) -> dict[int, float]:
        """本次抽取里各星级的实际出率（只在「有图的星级」间归一化）。

        与 `_pick_rarity` 的口径保持一致：权重全为 0 时回退为均匀。
        """
        rarities = sorted(pools)
        if not rarities:
            return {}
        total = sum(self._weight_of(r) for r in rarities)
        if total <= 0:
            return {r: 1.0 / len(rarities) for r in rarities}
        return {r: self._weight_of(r) / total for r in rarities}

    @staticmethod
    def _pity_rarity(present: set[int], min_rarity: int) -> int | None:
        """保底目标星级：优先恰好 min_rarity，其次向上升档。

        该档及以上都没有图时返回 None（调用方保留计数，不消耗保底）。
        """
        for r in range(min_rarity, MAX_RARITY + 1):
            if r in present:
                return r
        return None

    def _pity_load(self, data: dict) -> dict:
        """读取各星级「距上次抽出该星级」的抽数计数，缺失补 0。"""
        since = data.get("since") or {}
        if not isinstance(since, dict):
            since = {}
        return {
            int(r): int(since.get(str(r), since.get(r, 0)) or 0)
            for r in RARITY_ORDER
        }

    def _stats_load(self, data: dict) -> dict:
        """读取各星级的累计出货次数（核对概率用），缺失补 0。"""
        raw = data.get("stats") or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            int(r): int(raw.get(str(r), raw.get(r, 0)) or 0)
            for r in RARITY_ORDER
        }

    def _recent_load(self, data: dict, image_set: set[str]) -> list[str]:
        """读取最近抽到的图片；过滤掉已删除/改名的文件，只留最后 window 张。"""
        if self.window <= 0:
            return []
        recent = data.get("recent") or []
        if not isinstance(recent, list):
            return []
        kept = [str(name) for name in recent if str(name) in image_set]
        return kept[-self.window :]

    def _avoid_for(self, pool: list[str], recent: list[str]) -> set[str]:
        """本星级在去重窗口内已抽到过的图片（由新到旧），最多排除 len(pool) - 1 张。

        上限保证该星级至少还剩一张可抽，去重不会因为「整个星级都在窗口内」
        而失效；该星级只有 1 张图时返回空集合（无法避免重复）。
        """
        limit = min(self.window, max(0, len(pool) - 1))
        if limit <= 0:
            return set()
        picked: list[str] = []
        for name in reversed(recent):
            if name in pool and name not in picked:
                picked.append(name)
                if len(picked) >= limit:
                    break
        return set(picked)

    def next(self) -> str | None:
        """返回本次抽到的语录图片绝对路径；无可用图片时返回 None。

        每次调用都是一次独立的「权重抽星级 + 星级内均匀抽图」，
        并叠加短期去重与保底两个约束（两者都不会改变基础权重模型，
        只在可用集合上做有意的偏置）。
        """
        images = self._list_images()
        self.last_trace = None
        if not images:
            return None

        image_set = set(images)
        data = self._load()
        since = self._pity_load(data)
        recent = self._recent_load(data, image_set)

        # 去重只作用于「选哪张」，不影响「选哪个星级」：
        # 星级权重不会被去重窗口重新归一化，因此星级出率始终严格等于权重占比。
        pools = self._rarity_pools(images)

        # 保底计数累加（只对启用保底的星级），并钳制在阈值以内，
        # 这样「本群暂时没有该档图片」时计数最多显示成 N/N，不会无限增长。
        for r in RARITY_ORDER:
            thr = int(self.pity.get(r, 0) or 0)
            if thr > 0:
                since[r] = min(since.get(r, 0) + 1, thr)
        since_used = dict(since)  # 本次保底判定依据（调试展示用）

        # 保底判定：从高星到低星依次检查，优先命中等级最高的保底
        target_rarity: int | None = None
        triggered_r = 0
        skipped: list[int] = []
        for r in RARITY_ORDER:
            thr = int(self.pity.get(r, 0) or 0)
            if thr <= 0 or since.get(r, 0) < thr:
                continue
            picked = self._pity_rarity(set(pools), r)
            if picked is None:
                # 本群没有该档及以上的图：钳制计数、跳过该档（等有图立即触发）
                since[r] = thr
                skipped.append(r)
                continue
            target_rarity = picked
            triggered_r = r
            break

        # 权重抽星级：即使保底命中也会抽一次，便于调试看到「本来会抽到什么」
        rarity_by_weight = self._pick_rarity(pools)
        if target_rarity is None:
            target_rarity = rarity_by_weight
        if target_rarity is None:  # 理论上不可达（images 非空）
            return None

        # 星级内均匀选图：排除该星级去重窗口内已抽过的图（至少留一张）
        pool_all = pools[target_rarity]
        avoid = self._avoid_for(pool_all, recent)
        pool = [n for n in pool_all if n not in avoid]
        target = random.choice(pool or pool_all)

        # 出货重置：抽到 out_r 星即满足所有「<= out_r」的保底计数
        out_r, _ = parse_quote_rarity(target)
        for cr in range(1, out_r + 1):
            since[cr] = 0

        # 累计出货统计（与保底无关，只用于核对概率）
        stats = self._stats_load(data)
        stats[out_r] = stats.get(out_r, 0) + 1

        # 每张图片的累计被抽中次数（用于 /语录出货）
        hits = load_hits(data, set(images))
        hits[target] = hits.get(target, 0) + 1

        recent = (recent + [target])[-self.window :] if self.window > 0 else []
        self._save(
            {
                "since": {str(k): v for k, v in since.items()},
                "recent": recent,
                "stats": {str(k): v for k, v in stats.items()},
                "hits": hits,
            }
        )
        self.last_trace = {
            "group": self.group_dir.name,
            "total": len(images),
            "counts": {r: len(pools.get(r, [])) for r in RARITY_ORDER},
            "weights": {r: self._weight_of(r) for r in RARITY_ORDER},
            "share": self._rarity_shares(pools),
            "window": self.window,
            "recent": list(recent),
            "rarity_by_weight": rarity_by_weight,
            "rarity_final": target_rarity,
            "pool_size": len(pool_all),
            "candidates": len(pool),
            "excluded": len(avoid),
            "pity": {
                "thresholds": {
                    r: int(self.pity.get(r, 0) or 0) for r in RARITY_ORDER
                },
                "since_used": dict(since_used),
                "since_after": {r: since.get(r, 0) for r in RARITY_ORDER},
                "triggered": triggered_r or None,
                "skipped": skipped,
            },
            "stats": dict(stats),
            "total_draws": sum(stats.values()),
            "hits": dict(hits),
            "image_hits": hits.get(target, 0),
            "final": target,
        }
        if triggered_r:
            logger.info(
                "保底命中："
                f"群{self.group_dir.name} 第{triggered_r}星，触发抽取 {target}"
            )
        return str(self.group_dir / target)


class QuotePlugin(Star):
    """群友语录收集插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 群 -> QuoteDrawer（缓存权重/保底/窗口配置快照）
        self._drawers: dict[str, QuoteDrawer] = {}
        # 管理页缩略图缓存：key 含路径与 mtime，值是可内嵌的 data URL
        self._thumb_cache: dict[str, str] = {}
        # 注册 WebUI Page 后端 API
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings",
            self.web_get_settings,
            ["GET"],
            "Get quote plugin settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/save",
            self.web_save_settings,
            ["POST"],
            "Save quote plugin settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/submit-log",
            self.web_submit_log,
            ["GET"],
            "Get submission log",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/group-debug",
            self.web_get_group_debug,
            ["GET"],
            "Get per-group quote settings",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/group-debug",
            self.web_set_group_debug,
            ["POST"],
            "Save per-group debug mode",
        )
        # 语录管理页（可视化改稀有度 / 删除）
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes",
            self.web_quotes,
            ["GET"],
            "List quotes for the management page",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes/image",
            self.web_quote_image,
            ["GET"],
            "Get quote image (direct URL)",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes/full",
            self.web_quote_full,
            ["GET"],
            "Get quote image as data URL",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes/rarity",
            self.web_set_quote_rarity,
            ["POST"],
            "Set quote rarity (single or bulk)",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes/rename",
            self.web_rename_quote,
            ["POST"],
            "Rename a quote",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes/delete",
            self.web_delete_quotes,
            ["POST"],
            "Delete quotes",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/quotes/stats/reset",
            self.web_reset_draw_stats,
            ["POST"],
            "Reset cumulative draw stats",
        )
        # 旧版本（1..5 档）→ 6 星刻度的数据迁移；有标记文件时直接跳过
        try:
            self._migrate_rarity_scale()
        except Exception as e:  # 迁移失败不应阻塞插件加载
            logger.error(f"稀有度刻度迁移失败（可手动处理）: {e}")
        try:
            self._migrate_default_weights()
        except Exception as e:
            logger.error(f"默认权重迁移失败（可手动处理）: {e}")

    # ------------------------------------------------------------------ #
    # 路径与持久化
    # ------------------------------------------------------------------ #
    def _group_dir(self, group_id: str) -> Path:
        return QUOTES_ROOT / str(group_id)

    def _ensure_group_dir(self, group_id: str) -> Path:
        d = self._group_dir(group_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _settings_path(self, group_id: str) -> Path:
        return self._group_dir(group_id) / "settings.json"

    # ------------------------------------------------------------------ #
    # 全局设置（WebUI Page 与插件共享，settings.json）
    # ------------------------------------------------------------------ #
    def _default_global_settings(self) -> dict:
        """全局设置默认值，与 _conf_schema.json 镜像保持一致。"""
        return {
            "default_submit_mode": int(self.config.get("default_submit_mode", 0)),
            "default_cooldown": int(self.config.get("default_cooldown", 10)),
            "poke_quote_probability": float(
                self.config.get("poke_quote_probability", 0.85)
            ),
            "recent_window": int(self.config.get("recent_window", 8)),
            "rarity_weights": {
                **DEFAULT_RARITY_WEIGHTS,
                **(self.config.get("rarity_weights") or {}),
            },
            "pity_config": {
                **DEFAULT_PITY,
                **(self.config.get("pity_config") or {}),
            },
            "blocked_user_ids": [],
        }

    def _load_global_settings(self) -> dict:
        """读取全局设置，缺失字段回退到默认值。"""
        data = self._default_global_settings()
        try:
            if GLOBAL_SETTINGS_PATH.exists():
                with open(GLOBAL_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f) or {}
                # 逐字段合并，保留新加默认、用已保存值覆盖
                for k, v in data.items():
                    if k in saved:
                        if isinstance(v, dict):
                            merged = dict(v)
                            merged.update(saved.get(k) or {})
                            data[k] = merged
                        else:
                            data[k] = saved[k]
        except Exception as e:
            logger.warning(f"读取全局设置失败: {e}")
        return data

    def _save_global_settings(self, settings: dict) -> None:
        try:
            QUOTES_ROOT.mkdir(parents=True, exist_ok=True)
            with open(GLOBAL_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存全局设置失败: {e}")

    def _blocked_user_ids(self) -> set[str]:
        """读取全局语录黑名单并统一转换为字符串集合。"""
        raw = self._load_global_settings().get("blocked_user_ids", [])
        if not isinstance(raw, list):
            return set()
        return {str(user_id) for user_id in raw}

    def _is_blocked_user(self, user_id: str | int) -> bool:
        """判断用户是否被全局禁止使用语录投稿与抽卡功能。"""
        return str(user_id) in self._blocked_user_ids()

    @staticmethod
    def _resolve_target_user_id(
        event: AstrMessageEvent, raw_user_id: str = ""
    ) -> str | None:
        """解析命令中的 @用户 或数字 UID，优先取消息链中的 At 组件。"""
        for component in event.message_obj.message:
            if isinstance(component, Comp.At):
                return str(component.qq)
        user_id = raw_user_id.strip()
        return user_id if user_id.isdigit() else None

    def _load_settings(self, group_id: str) -> dict:
        """读取群级设置，合并全局设置作为默认。"""
        gs = self._load_global_settings()
        data = {
            "mode": int(gs.get("default_submit_mode", 0)),
            "cooldown": int(gs.get("default_cooldown", 10)),
            "debug_mode": 0,
            "last_poke": 0,
        }
        path = self._settings_path(group_id)
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data.update(json.load(f) or {})
        except Exception as e:
            logger.warning(f"读取群设置失败: {e}")
        return data

    def _save_settings(self, group_id: str, settings: dict) -> None:
        try:
            self._ensure_group_dir(group_id)
            with open(self._settings_path(group_id), "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存群设置失败: {e}")

    # ------------------------------------------------------------------ #
    # 数据迁移：1..5 档（≤1.0）→ 1..6 星（1.1 起）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _shift_rarity_name(name: str) -> str | None:
        """把 'n.标题.扩展名'（n 为 1..5 的旧档）升一档；不符合旧刻度时返回 None。"""
        head, sep, rest = name.partition(".")
        if not sep or not head.isdigit():
            return None
        old = int(head)
        if MIN_RARITY <= old <= MAX_RARITY - 1:
            return f"{old + 1}.{rest}"
        return None

    def _migrate_order_file(self, group_dir: Path) -> None:
        """把 order.json 的保底计数键与最近记录里的文件名一起升一档。"""
        path = group_dir / "order.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return
        since = data.get("since")
        if isinstance(since, dict):
            shifted: dict[str, int] = {}
            for key, value in since.items():
                try:
                    rarity = int(key)
                except (TypeError, ValueError):
                    continue
                if MIN_RARITY <= rarity <= MAX_RARITY - 1:
                    shifted[str(rarity + 1)] = value
            shifted.setdefault(str(MIN_RARITY), 0)
            data["since"] = {str(r): int(shifted.get(str(r), 0) or 0) for r in RARITY_ORDER}
        recent = data.get("recent")
        if isinstance(recent, list):
            data["recent"] = [
                self._shift_rarity_name(str(name)) or str(name) for name in recent
            ]
        try:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"迁移抽卡状态失败 {path}: {e}")

    def _migrate_rarity_scale(self) -> None:
        """把旧数据从 1..5 档迁移成 1..6 星。

        - 群目录图片：5.x→6.x、4.x→5.x、…、1.x→2.x（按旧档降序重命名，避免撞名）
        - submit.log：filename 同步升档
        - order.json：since 键 +1、recent 文件名 +1
        - 全局 settings.json：权重/保底换成新的 6 档默认（明日方舟出率）
        完成后写标记文件；重复调用直接返回。
        """
        marker = RARITY_SCALE_MARKER
        if marker.exists():
            try:
                saved = json.loads(marker.read_text(encoding="utf-8")) or {}
                if saved.get("scale") == RARITY_SCALE_VERSION:
                    return
            except Exception:
                pass

        renamed = 0
        if QUOTES_ROOT.exists():
            for group_dir in sorted(p for p in QUOTES_ROOT.iterdir() if p.is_dir()):
                if not group_dir.name.isdigit():
                    continue
                for old in range(MAX_RARITY - 1, MIN_RARITY - 1, -1):
                    for f in sorted(group_dir.iterdir()):
                        if not f.is_file() or f.suffix.lower() not in IMAGE_EXTENSIONS:
                            continue
                        head, sep, rest = f.name.partition(".")
                        if not sep or not head.isdigit() or int(head) != old:
                            continue
                        target = f.with_name(f"{old + 1}.{rest}")
                        if target.exists():
                            logger.warning(f"稀有度迁移跳过（目标已存在）: {target}")
                            continue
                        f.rename(target)
                        renamed += 1
                self._migrate_order_file(group_dir)

        log_changed = 0
        records = self._read_submit_log()
        for record in records:
            new_name = self._shift_rarity_name(str(record.get("filename", "")))
            if new_name:
                record["filename"] = new_name
                log_changed += 1
        if log_changed:
            self._rewrite_submit_log(records)

        if GLOBAL_SETTINGS_PATH.exists():
            try:
                saved = json.loads(GLOBAL_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
                saved["rarity_weights"] = dict(DEFAULT_RARITY_WEIGHTS)
                saved["pity_config"] = dict(DEFAULT_PITY)
                self._save_global_settings(saved)
            except Exception as e:
                logger.warning(f"迁移全局设置失败: {e}")

        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "scale": RARITY_SCALE_VERSION,
                        "migrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"写入迁移标记失败: {e}")
        logger.info(
            f"稀有度刻度已迁移为 1..6 星：重命名 {renamed} 张图片、"
            f"更新 {log_changed} 条投稿记录"
        )

    # 1.1–1.5 的旧默认权重（2★ / 1★ 为 0，不参与抽卡）
    _LEGACY_DEFAULT_WEIGHTS = {6: 2, 5: 8, 4: 50, 3: 40, 2: 0, 1: 0}

    def _migrate_default_weights(self) -> None:
        """把「仍原样保留旧默认权重」的全局设置升级为新默认。

        只在这六项权重与旧默认完全一致时改写；用户自定义过权重的配置不动。
        用 ``.weights_rev`` 标记保证只迁移一次。
        """
        marker = QUOTES_ROOT / ".weights_rev"
        if marker.exists():
            return
        if GLOBAL_SETTINGS_PATH.exists():
            try:
                saved = json.loads(GLOBAL_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                saved = {}
            weights = saved.get("rarity_weights")
            if isinstance(weights, dict) and all(
                f"weight_{r}" in weights for r in RARITY_ORDER
            ):
                try:
                    current = {r: float(weights[f"weight_{r}"]) for r in RARITY_ORDER}
                except (TypeError, ValueError):
                    current = None
                if current == self._LEGACY_DEFAULT_WEIGHTS:
                    saved["rarity_weights"] = dict(DEFAULT_RARITY_WEIGHTS)
                    self._save_global_settings(saved)
                    logger.info("默认权重已升级：2★ / 1★ 开始参与抽卡")
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps({"version": 2}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"写入权重迁移标记失败: {e}")

    def _load_rarity_weights(self) -> dict:
        """优先从全局设置读取稀有度权重，整理成 {稀有度数字: 权重}。

        权重是该星级在抽取中的相对占比；0 表示该星级不被抽到
        （不再被 `or 1.0` 当成 1.0）。缺字段回退默认，非法值回退默认。
        """
        gs = self._load_global_settings()
        raw = gs.get("rarity_weights") or DEFAULT_RARITY_WEIGHTS
        out: dict[int, float] = {}
        for rarity in RARITY_ORDER:
            key = f"weight_{rarity}"
            try:
                out[rarity] = max(
                    0.0, float(raw.get(key, DEFAULT_RARITY_WEIGHTS[key]))
                )
            except (TypeError, ValueError):
                out[rarity] = float(DEFAULT_RARITY_WEIGHTS[key])
        return out

    def _load_pity(self) -> dict:
        """从全局设置读取保底配置，整理成 {星级: 每 N 抽必出, 0=禁用}。"""
        gs = self._load_global_settings()
        raw = gs.get("pity_config") or DEFAULT_PITY
        out = {}
        for r in RARITY_ORDER:
            key = f"pity_{r}"
            try:
                out[r] = max(0, int(raw.get(key, DEFAULT_PITY.get(key, 0)) or 0))
            except (TypeError, ValueError):
                out[r] = 0
        return out

    def _drawer(self, group_id: str) -> QuoteDrawer:
        """取本群的抽取器（缓存权重/保底/窗口配置快照）。"""
        if group_id not in self._drawers:
            gs = self._load_global_settings()
            self._drawers[group_id] = QuoteDrawer(
                self._group_dir(group_id),
                int(gs.get("recent_window", 8)),
                self._load_rarity_weights(),
                self._load_pity(),
            )
        return self._drawers[group_id]

    # ------------------------------------------------------------------ #
    # 图片下载
    # ------------------------------------------------------------------ #
    async def _save_bytes(self, group_id: str, data: bytes, filename: str) -> str:
        """将图片字节写入群目录，按指定文件名保存，返回保存路径。"""
        d = self._ensure_group_dir(group_id)
        path = d / filename
        with open(path, "wb") as f:
            f.write(data)
        logger.info(f"语录图片已保存: {path}")
        return str(path)

    async def _download_image(
        self, event: AstrMessageEvent, file_id: str, group_id: str, filename: str
    ) -> str | None:
        """三级容错下载图片：本地缓存 -> 协议端 API -> URL 下载，保存为指定文件名。"""
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot

            # 1. 优先从当前消息的本地缓存读取
            image_obj = next(
                (c for c in event.message_obj.message if isinstance(c, Comp.Image)),
                None,
            )
            if image_obj:
                try:
                    local_path = await image_obj.convert_to_file_path()
                    if local_path and os.path.exists(local_path):
                        logger.info(f"从本地缓存读取图片: {local_path}")
                        with open(local_path, "rb") as f:
                            return await self._save_bytes(group_id, f.read(), filename)
                except Exception as e:
                    logger.warning(f"读取本地缓存失败: {e}")

            # 2. 通过协议端 get_image API 获取
            try:
                result = await client.api.call_action("get_image", file=file_id)
                api_path = result.get("file") if result else None
                if api_path and os.path.exists(api_path):
                    logger.info(f"从协议端 API 读取图片: {api_path}")
                    with open(api_path, "rb") as f:
                        return await self._save_bytes(group_id, f.read(), filename)
            except Exception as e:
                logger.warning(f"通过 get_image API 下载失败: {e}")
                result = None

            # 3. 通过 URL 下载
            url = result.get("url") if result else None
            if url:
                logger.info(f"从 URL 下载图片: {url}")
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                return await self._save_bytes(
                                    group_id, await resp.read(), filename
                                )
                            logger.error(f"从 URL 下载失败: HTTP {resp.status}")
                except Exception as e:
                    logger.error(f"从 URL 下载出错: {e}")

            logger.error("所有下载方式均失败")
            return None
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
            return None

    async def _get_replied_message_chain(self, event: AstrMessageEvent):
        """读取被回复消息的原始消息链，未引用或读取失败时返回 None。"""
        reply = next(
            (
                component
                for component in event.message_obj.message
                if isinstance(component, Comp.Reply)
            ),
            None,
        )
        if not reply:
            return None
        try:
            reply_id = int(reply.id) if str(reply.id).isdigit() else reply.id
            reply_msg = await event.bot.api.call_action("get_msg", message_id=reply_id)
            return reply_msg.get("message") if reply_msg else None
        except Exception as e:
            logger.error(f"获取引用消息失败: {e}")
            return None

    async def _resolve_reply_image_file(
        self, event: AstrMessageEvent
    ) -> tuple[str, str] | None:
        """从被回复的消息中解析出 (图片 file_id, 推断的扩展名)。

        兼容数组/ CQ 码两种格式。扩展名优先取图 URL 后缀，否则取 file 字段后缀，
        无法判断时回退到 .jpg。
        """
        chain = await self._get_replied_message_chain(event)
        if not chain:
            return None
        parts = chain if isinstance(chain, list) else []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "image":
                data = part.get("data", {}) or {}
                file_id = data.get("file")
                ext = self._detect_ext(data.get("url"), data.get("file"))
                return file_id, ext
        if isinstance(chain, str):
            m = re.search(r"\[CQ:image,[^\]]*file=([^,\]]+)", chain)
            if m:
                return m.group(1), self._detect_ext(None, m.group(1))
        return None

    @staticmethod
    def _detect_ext(url: str | None, file: str | None) -> str:
        """从图片 URL 或 file 字段推断扩展名，回退 .jpg。"""
        for src in (url, file):
            if not src:
                continue
            lower = src.lower().split("?", 1)[0]
            for ext in IMAGE_EXTENSIONS:
                if lower.endswith(ext):
                    return ext
        return ".jpg"

    async def _resolve_reply_quote_file(
        self, event: AstrMessageEvent, group_id: str
    ) -> Path | None:
        """从被回复的 bot 语录卡片中解析并验证本群语录文件。

        优先使用图片段携带的文件路径；再使用卡片 Plain 文本的最后一行
        （即展示文件名）。所有候选都必须经 _find_quote_file 验证。
        """
        chain = await self._get_replied_message_chain(event)
        if not chain:
            return None

        candidates: list[str] = []
        if isinstance(chain, list):
            for part in chain:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image":
                    data = part.get("data", {}) or {}
                    for source in (data.get("file"), data.get("url")):
                        if source:
                            candidates.append(Path(str(source).split("?", 1)[0]).name)
                elif part.get("type") in {"text", "plain"}:
                    text = str((part.get("data", {}) or {}).get("text", ""))
                    if text:
                        # /语录查看 会在文件名后追加投稿人和投稿时间；
                        # 因此逐行加入候选，不假设文件名永远在最后一行。
                        candidates.extend(
                            line.strip() for line in text.splitlines() if line.strip()
                        )
        elif isinstance(chain, str):
            image_match = re.search(r"\[CQ:image,[^\]]*file=([^,\]]+)", chain)
            if image_match:
                candidates.append(Path(image_match.group(1)).name)

        for name in candidates:
            if name:
                target = self._find_quote_file(group_id, name)
                if target:
                    return target
        return None

    def _log_submission(
        self, group_id: str, user_id: str, filename: str, ts: float
    ) -> None:
        """将一次投稿记录以 JSONL 追加到插件数据目录下的 submit.log。"""
        QUOTES_ROOT.mkdir(parents=True, exist_ok=True)
        log_path = QUOTES_ROOT / "submit.log"
        record = {
            "ts": ts,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "group_id": str(group_id),
            "user_id": str(user_id),
            "filename": filename,
        }
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"写入投稿日志失败: {e}")

    def _read_submit_log(self) -> list[dict]:
        """读取全部投稿日志记录（JSONL）。"""
        log_path = QUOTES_ROOT / "submit.log"
        if not log_path.exists():
            return []
        out: list[dict] = []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        out.append(json.loads(s))
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"读取投稿日志失败: {e}")
        return out

    def _rewrite_submit_log(self, records: list[dict]) -> None:
        """用给定记录列表覆盖写入 submit.log。"""
        log_path = QUOTES_ROOT / "submit.log"
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"重写投稿日志失败: {e}")

    def _find_submission_record(self, group_id: str, filename: str) -> dict | None:
        """返回本群指定文件最新的一条投稿记录。"""
        records = [
            record
            for record in self._read_submit_log()
            if str(record.get("group_id")) == str(group_id)
            and record.get("filename") == filename
        ]
        if not records:
            return None
        return max(records, key=lambda record: float(record.get("ts", 0)))

    def _remove_submission_records(self, group_id: str, filename: str) -> None:
        """删除本群指定文件的全部投稿记录。"""
        records = self._read_submit_log()
        self._rewrite_submit_log(
            [
                record
                for record in records
                if not (
                    str(record.get("group_id")) == str(group_id)
                    and record.get("filename") == filename
                )
            ]
        )

    def _rename_submission_records(
        self, group_id: str, old_filename: str, new_filename: str
    ) -> None:
        """同步更新改名或改稀有度后的投稿记录文件名。"""
        records = self._read_submit_log()
        changed = False
        for record in records:
            if (
                str(record.get("group_id")) == str(group_id)
                and record.get("filename") == old_filename
            ):
                record["filename"] = new_filename
                changed = True
        if changed:
            self._rewrite_submit_log(records)

    def _submission_info_text(self, group_id: str, filename: str) -> str:
        """生成语录的投稿信息文本；不存在记录时返回空字符串。"""
        record = self._find_submission_record(group_id, filename)
        if not record:
            return ""
        return (
            f"\n投稿人：{record.get('user_id', '未知')}"
            f"\n投稿时间：{record.get('time', '未知')}"
        )

    def _quote_info_text(self, group_id: str, path: Path) -> str:
        """生成卡片文本及可用的投稿信息。"""
        rarity, display = parse_quote_rarity(path.name)
        submission = self._submission_info_text(group_id, path.name)
        return f"{RARITY_TEXT[rarity]}\n{display}{submission}"

    def _quote_chain_with_info(self, group_id: str, path: Path) -> list:
        """构建包含投稿信息的「文字在前、图片在后」语录消息链。"""
        return [
            Comp.Plain(text=self._quote_info_text(group_id, path)),
            Comp.Image.fromFileSystem(str(path)),
        ]

    def _quote_detail_text(self, group_id: str, path: Path) -> str:
        """生成供指令与 AI 共用的语录文字详情。"""
        return self._quote_info_text(group_id, path)

    def _list_quote_files(self, group_id: str, rarity: int = 0) -> list[Path]:
        """列出本群语录，可按 1-6 星筛选，并按文件名稳定排序。"""
        d = self._group_dir(group_id)
        if not d.exists():
            return []
        files = [
            f
            for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        ]
        if rarity in RARITY_TEXT:
            files = [f for f in files if parse_quote_rarity(f.name)[0] == rarity]
        return sorted(files, key=lambda path: path.name)

    def _existing_displays(self, group_id: str) -> set[str]:
        """返回该群已存在的「标题+扩展名」集合（忽略稀有度前缀 1-6）。"""
        d = self._group_dir(group_id)
        out: set[str] = set()
        if not d.exists():
            return out
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                _, disp = parse_quote_rarity(f.name)
                out.add(disp)
        return out

    def _find_quote_file(self, group_id: str, name: str) -> Path | None:
        """按多种口径查找群目录下的语录文件。

        匹配顺序：
        1. 完整文件名精确匹配（如 "1.杰克的奇妙比喻.jpg"）
        2. 展示名匹配（"标题+扩展名"，忽略稀有度前缀，如 "杰克的奇妙比喻.jpg"）
        3. 旧「1.<name>」形式兼容
        4. 自动补扩展名兜底：输入未带扩展名时，
           分别尝试 .jpg/.jpeg/.png/.bmp/.gif/.webp 等再走 1/2/3 匹配
        """
        d = self._group_dir(group_id)
        if not d.exists():
            return None
        candidates = [
            f
            for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        ]
        target = name.strip()

        def match(t: str) -> Path | None:
            # 1. 完整文件名精确匹配
            for f in candidates:
                if f.name == t:
                    return f
            # 2. 展示名匹配（忽略稀有度前缀）
            for f in candidates:
                _, disp = parse_quote_rarity(f.name)
                if disp == t:
                    return f
            # 3. 旧「1.<t>」形式兼容
            for f in candidates:
                if f.name == f"1.{t}":
                    return f
            return None

        # 先按原样匹配
        hit = match(target)
        if hit:
            return hit
        # 输入未带扩展名时，逐一补常见扩展名再试
        if not Path(target).suffix:
            for ext in sorted(IMAGE_EXTENSIONS):
                hit = match(f"{target}{ext}")
                if hit:
                    return hit
        return None

    def _delete_quote_file(self, group_id: str, target: Path) -> tuple[bool, str]:
        """删除语录文件及其投稿记录（抽卡状态按现存文件自动收敛）。"""
        try:
            filename = target.name
            target.unlink()
            self._remove_submission_records(group_id, filename)
            return True, f"⭐已删除语录：{filename}"
        except Exception as e:
            logger.error(f"删除语录失败: {e}")
            return False, f"⭐删除失败: {e}"

    def _rename_quote_file(
        self, group_id: str, target: Path, new_title: str
    ) -> tuple[bool, str]:
        """重命名语录标题，并同步投稿日志。"""
        new_title = new_title.strip()
        if not new_title:
            return False, "⭐新标题不能为空"
        rarity, _ = parse_quote_rarity(target.name)
        ext = target.suffix
        new_display = f"{new_title}{ext}"
        if new_display in self._existing_displays(group_id):
            return False, f"⭐改名失败：已存在同名语录「{new_title}」，请换一个标题"
        new_path = target.with_name(f"{rarity}.{new_title}{ext}")
        if new_path.exists():
            return False, "⭐改名失败：目标文件已存在"
        try:
            old_filename = target.name
            target.rename(new_path)
            self._rename_submission_records(group_id, old_filename, new_path.name)
            return True, f"⭐已重命名：\n{old_filename} → {new_path.name}"
        except Exception as e:
            logger.error(f"重命名失败: {e}")
            return False, f"⭐改名失败: {e}"

    def _set_quote_rarity_file(
        self, group_id: str, target: Path, rarity: int
    ) -> tuple[bool, str]:
        """修改语录稀有度前缀，并同步投稿日志。"""
        if rarity not in RARITY_TEXT:
            return False, "⭐稀有度只能是 1-6"
        ext = target.suffix
        stem = target.stem.split(".", 1)[1] if "." in target.stem else target.stem
        new_path = target.with_name(f"{rarity}.{stem}{ext}")
        if new_path == target:
            return True, f"⭐稀有度已是 {rarity}，无需修改"
        if new_path.exists():
            return False, "⭐改稀有度失败：目标文件名已存在"
        try:
            old_filename = target.name
            target.rename(new_path)
            self._rename_submission_records(group_id, old_filename, new_path.name)
            return True, f"⭐已修改稀有度：\n{old_filename} → {new_path.name}"
        except Exception as e:
            logger.error(f"改稀有度失败: {e}")
            return False, f"⭐改稀有度失败: {e}"

    def _revoke_quote_file(
        self, group_id: str, user_id: str, target: Path
    ) -> tuple[bool, str]:
        """撤回用户自己的语录投稿，校验归属后删除文件与投稿记录。"""
        record = self._find_submission_record(group_id, target.name)
        if not record:
            return False, "⭐该语录无投稿记录，无法撤回（可联系管理员删除）"
        if str(record.get("user_id")) != str(user_id):
            return False, "⭐只能撤回自己投的语录"
        try:
            target.unlink()
            self._remove_submission_records(group_id, target.name)
            return True, f"⭐已撤回投稿：{target.name}"
        except Exception as e:
            logger.error(f"撤回时删除文件失败: {e}")
            return False, f"⭐撤回失败：删除文件出错 {e}"

    async def _submit_replied_quote(
        self, event: AstrMessageEvent, group_id: str, user_id: str, title: str
    ) -> tuple[bool, str]:
        """保存回复图片为语录，复用投稿权限、重名和日志规则。"""
        if self._is_blocked_user(user_id):
            return False, "⭐您已被语录系统拉黑，无法投稿"
        settings = self._load_settings(group_id)
        mode = int(settings.get("mode", 0))
        if mode == 0:
            return False, "⭐投稿系统未开启，请联系 bot 管理员使用 /投稿权限 开启"
        if mode == 1 and not self._is_admin(event):
            return False, "⭐权限不足，当前仅 bot 管理员可投稿"

        resolved = await self._resolve_reply_image_file(event)
        if not resolved:
            return False, "⭐请回复一条含图消息后再投稿"
        file_id, ext = resolved
        ts = time.time()
        stem = title.strip() or time.strftime(
            "%Y年%m月%d日%H:%M的投稿", time.localtime(ts)
        )
        filename = f"{DEFAULT_SUBMIT_RARITY}.{stem}{ext}"
        if f"{stem}{ext}" in self._existing_displays(group_id):
            return False, f"⭐投稿失败：已存在同名语录「{stem}」，请改个标题重试"

        path = await self._download_image(event, file_id, group_id, filename)
        if not path or not os.path.exists(path):
            return False, "⭐语录投稿失败，图片下载失败"
        self._log_submission(group_id, user_id, filename, ts)
        return (
            True,
            "⭐语录投稿成功！\n"
            "请确认您的投稿格式正确（聊天记录截屏）。\n"
            "如果投稿有误，请使用「/撤回投稿 <名称>」取消投稿。",
        )

    # ------------------------------------------------------------------ #
    # 调试模式与抽卡入口
    # ------------------------------------------------------------------ #
    def _debug_level(self, group_id: str) -> int:
        """本群调试模式等级：0 关闭 / 1 仅日志 / 2 日志 + 群内卡片附注。

        与 mode / cooldown 一样存在群级 settings.json，因此只影响该群。
        """
        try:
            value = int(self._load_settings(group_id).get("debug_mode", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, min(2, value))

    def _draw_quote(
        self, group_id: str, source: str = "", user_id: str = ""
    ) -> tuple[str | None, str]:
        """抽一张语录，并按本群调试模式输出本次的概率模型信息。

        返回 (图片路径, 需要附在群内卡片上的调试文本)；调试关闭、未抽到图时
        第二项为空字符串。指令、戳一戳、AI 抽卡都走这里，保证调试口径一致。
        """
        drawer = self._drawer(group_id)
        path = drawer.next()
        level = self._debug_level(group_id)
        if level <= 0 or not drawer.last_trace:
            return path, ""
        text = format_draw_trace(drawer.last_trace)
        body = text.split("\n", 1)[1] if "\n" in text else text
        logger.info(
            f"[语录调试] 群{group_id}｜来源={source or '未知'} "
            f"用户={user_id or '未知'}\n{body}"
        )
        note = text if level >= 2 and path else ""
        return path, note

    @staticmethod
    def _with_debug_note(chain: list, note: str) -> list:
        """把调试文本并入消息链的文本段（必须留在图片之前）。"""
        if not note:
            return chain
        first = chain[0] if chain else None
        if isinstance(first, Comp.Plain):
            return [Comp.Plain(text=f"{note}\n{first.text}"), *chain[1:]]
        return [Comp.Plain(text=note), *chain]

    # ------------------------------------------------------------------ #
    # 指令：随机查看一条语录
    # ------------------------------------------------------------------ #
    @filter.command("语录", alias={"随机语录"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def random_quote(self, event: AstrMessageEvent):
        """随机查看一条本群语录"""
        user_id = str(event.get_sender_id())
        if self._is_blocked_user(user_id):
            yield event.plain_result("⭐您已被语录系统拉黑，无法抽卡查看语录")
            event.stop_event()
            return

        group_id = str(event.message_obj.group_id)
        path, debug_note = self._draw_quote(group_id, "指令 /语录", user_id)
        if path:
            yield event.chain_result(
                self._with_debug_note(quote_chain(path), debug_note)
            )
        else:
            yield event.plain_result(
                "⭐本群还没有群友语录哦~\n请发送“/语录投稿+图片”来添加！"
            )
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：查看指定语录（仅管理员，不影响随机/保底状态）
    # ------------------------------------------------------------------ #
    @filter.command("语录查看", alias={"查看语录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def view_quote(self, event: AstrMessageEvent, name: str = ""):
        """直接查看本群指定语录，不影响抽卡状态"""
        if not name.strip():
            yield event.plain_result("⭐用法：/语录查看 <语录名>")
            event.stop_event()
            return

        group_id = str(event.message_obj.group_id)
        target = self._find_quote_file(group_id, name)
        if not target:
            yield event.plain_result(f"⭐未找到名为「{name}」的语录")
            event.stop_event()
            return

        yield event.chain_result(self._quote_chain_with_info(group_id, target))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：语录投稿
    # ------------------------------------------------------------------ #
    @filter.command("语录投稿", alias={"投稿语录"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def submit_quote(self, event: AstrMessageEvent, title: str = ""):
        """回复一张含图消息进行语录投稿，可选附带标题作为文件名"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        msg_id = str(event.message_obj.message_id)
        ok, text = await self._submit_replied_quote(event, group_id, user_id, title)
        if not ok and "请回复一条含图消息" in text:
            yield event.chain_result(
                [
                    Comp.At(qq=user_id),
                    Comp.Plain(
                        text=(
                            "\n请回复一条含图消息并发送“/语录投稿 [标题]”进行投稿。\n"
                            "示例：/语录投稿 杰克的奇妙比喻"
                        )
                    ),
                ]
            )
        else:
            yield event.chain_result([Comp.Reply(id=msg_id), Comp.Plain(text=text)])
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：查看群状态
    # ------------------------------------------------------------------ #
    @filter.command("语录状态", alias={"投稿状态"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def quote_status(self, event: AstrMessageEvent):
        """查看本群语录总数、各星级张数、投稿权限与冷却设置"""
        group_id = str(event.message_obj.group_id)
        body = "\n".join(
            f"  {line}" for line in self._quote_stats_text(group_id).splitlines()
        )
        yield event.plain_result(f"⭐本群语录状态\n{body}")
        event.stop_event()

    @filter.command("语录出货", alias={"出货统计"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def quote_hits(self, event: AstrMessageEvent, page: str = ""):
        """查看每张语录被抽出的次数（按次数降序，分页）"""
        group_id = str(event.message_obj.group_id)
        files = self._list_quote_files(group_id)
        if not files:
            yield event.plain_result("⭐本群还没有语录")
            event.stop_event()
            return

        hits = load_hits(
            self._read_order_state(group_id), {path.name for path in files}
        )
        rows = sorted(
            ((path, hits.get(path.name, 0)) for path in files),
            key=lambda item: (-item[1], item[0].name.lower()),
        )
        total_hits = sum(hits.values())
        page_size = 15
        pages = max(1, (len(rows) + page_size - 1) // page_size)
        try:
            page_no = max(1, min(int(str(page).strip()), pages)) if str(page).strip() else 1
        except ValueError:
            page_no = 1

        lines = [
            f"⭐本群语录出货统计（共 {len(rows)} 张，累计 {total_hits} 抽）"
        ]
        start = (page_no - 1) * page_size
        for index, (path, count) in enumerate(rows[start : start + page_size], start + 1):
            rarity, display = parse_quote_rarity(path.name)
            share = (count / total_hits * 100) if total_hits else 0.0
            lines.append(f"{index}. {rarity}★ {display} ｜ {count} 次（{share:.1f}%）")
        if pages > 1:
            nxt = page_no + 1 if page_no < pages else 1
            lines.append(f"第 {page_no}/{pages} 页，发送 /语录出货 {nxt} 翻页")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("语录help", alias={"语录帮助", "语录指令"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def quote_help(self, event: AstrMessageEvent):
        """查看所有语录指令与用法（仅管理员可用）"""
        lines = ["⭐群友语录 · 指令一览"]
        for group, items in COMMAND_HELP:
            lines.append("")
            lines.append(f"【{group}】")
            for usage, desc in items:
                lines.append(f"  {usage}  —— {desc}")
        lines.append("")
        lines.append("提示：管理类指令的权限/启停可在 AstrBot 面板「插件 → 管理行为 → 指令」里调整。")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    def _write_stats_csv(self, group_id: str) -> tuple[Path, int, int]:
        """生成 CSV：每张图的星级、次数、占比、投稿信息。返回 (路径, 张数, 总抽数)。"""
        files = self._list_quote_files(group_id)
        hits = load_hits(self._read_order_state(group_id), {path.name for path in files})
        weights = self._load_rarity_weights()
        total_weight = sum(max(0.0, weights.get(r, 0)) for r in RARITY_ORDER)
        totals = {r: sum(1 for p in files if parse_quote_rarity(p.name)[0] == r) for r in RARITY_ORDER}
        total_draws = sum(hits.values())

        rows = []
        for path in files:
            rarity, display = parse_quote_rarity(path.name)
            count = hits.get(path.name, 0)
            record = self._find_submission_record(group_id, path.name) or {}
            rows.append(
                [
                    f"{rarity}★",
                    display,
                    path.name,
                    count,
                    round(count / total_draws * 100, 2) if total_draws else 0.0,
                    totals[rarity],
                    round(weights.get(rarity, 0) / total_weight * 100, 2) if total_weight else 0.0,
                    record.get("user_id", ""),
                    record.get("time", ""),
                    path.stat().st_size,
                ]
            )
        rows.sort(key=lambda row: (-int(row[0].rstrip("★")), -row[3], row[2].lower()))

        filename = f"quote_stats_{group_id}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = Path(tempfile.gettempdir()) / filename
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "星级",
                    "标题",
                    "文件名",
                    "被抽中次数",
                    "实际占比(%)",
                    "该星级张数",
                    "该星级设定占比(%)",
                    "投稿人",
                    "投稿时间",
                    "文件大小(字节)",
                ]
            )
            writer.writerows(rows)
        return path, len(rows), total_draws

    async def _send_stats_file(
        self, event: AstrMessageEvent, user_id: str, path: Path
    ) -> tuple[bool, str]:
        """优先私聊发文件给管理员，失败则退回发到当前群。"""
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None or not hasattr(api, "call_action"):
            return False, "当前平台不支持发送文件"
        try:
            await api.call_action(
                "upload_private_file",
                user_id=int(user_id),
                file=str(path),
                name=path.name,
            )
            return True, f"已私聊发送给 {user_id}"
        except Exception as e:
            logger.warning(f"私聊发送统计文件失败: {e}")
        try:
            await api.call_action(
                "upload_group_file",
                group_id=int(event.message_obj.group_id),
                file=str(path),
                name=path.name,
            )
            return True, "私聊发送失败，已改发到本群"
        except Exception as e:
            logger.warning(f"群文件发送统计失败: {e}")
            return False, "文件发送失败"

    @filter.command("语录统计")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def export_stats(self, event: AstrMessageEvent):
        """把本群全部语录统计导出为 CSV，私聊发给管理员"""
        group_id = str(event.message_obj.group_id)
        files = self._list_quote_files(group_id)
        if not files:
            yield event.plain_result("⭐本群还没有语录")
            event.stop_event()
            return

        path, row_count, total_draws = self._write_stats_csv(group_id)
        try:
            ok, detail = await self._send_stats_file(event, STATS_RECEIVER_QQ, path)
        finally:
            if path.exists():
                path.unlink(missing_ok=True)

        lines = [
            "⭐本群语录统计已导出",
            f"  共 {row_count} 张图，累计 {total_draws} 抽",
            f"  文件：{path.name}",
            f"  {detail}",
        ]
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：设置投稿权限（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("投稿权限", alias={"设置投稿权限"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def set_submit_mode(self, event: AstrMessageEvent, mode: int):
        """设置投稿权限模式：0=关闭 1=仅管理员 2=全体"""
        group_id = str(event.message_obj.group_id)
        ok, text = self._apply_group_settings(group_id, mode=mode)
        if not ok:
            yield event.plain_result(
                "⭐模式只能是 0、1、2\n  0：关闭\n  1：仅管理员\n  2：全体成员"
            )
            event.stop_event()
            return
        mode_text = {0: "关闭", 1: "仅管理员", 2: "全体成员"}[mode]
        yield event.plain_result(f"⭐投稿权限已设置为：{mode_text}")
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：设置戳戳冷却（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("戳戳冷却", alias={"语录冷却"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def set_poke_cooldown(self, event: AstrMessageEvent, cooldown: int):
        """设置戳一戳触发语录的冷却时间（秒）"""
        group_id = str(event.message_obj.group_id)
        ok, text = self._apply_group_settings(group_id, cooldown=cooldown)
        if not ok:
            yield event.plain_result("⭐冷却时间不能为负数")
            event.stop_event()
            return
        yield event.plain_result(f"⭐戳戳冷却已设置为：{cooldown} 秒")
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：调试模式（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("语录调试", alias={"调试语录", "语录调试模式"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def set_debug_mode(self, event: AstrMessageEvent, mode: str = ""):
        """查看或设置本群抽卡调试模式：0 关闭 / 1 仅日志 / 2 日志 + 卡片附注"""
        group_id = str(event.message_obj.group_id)
        if not mode.strip():
            level = self._debug_level(group_id)
            mode_text = {
                0: "关闭",
                1: "仅写入 AstrBot 日志",
                2: "写入日志，并在语录卡片上附注（群内可见）",
            }.get(level, "未知")
            yield event.plain_result(
                f"⭐本群语录调试模式：{level}（{mode_text}）\n"
                "用法：/语录调试 <0|1|2>（只影响本群）\n"
                "  0：关闭\n"
                "  1：每次抽卡把出率、去重与保底判定写入日志\n"
                "  2：额外把简版调试信息附在语录卡片上"
            )
            event.stop_event()
            return

        ok, text = self._apply_group_settings(group_id, debug_mode=mode.strip())
        if not ok:
            yield event.plain_result(f"⭐{text}\n用法：/语录调试 <0|1|2>")
            event.stop_event()
            return
        yield event.plain_result(
            f"⭐本群语录调试模式已设置为：{self._debug_level(group_id)}"
        )
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：拉黑/解封语录用户（仅管理员，全局生效）
    # ------------------------------------------------------------------ #
    @filter.command("语录拉黑", alias={"拉黑语录用户"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def block_quote_user(self, event: AstrMessageEvent, user: str = ""):
        """全局拉黑用户，禁止其投稿和抽卡查看语录"""
        target_id = self._resolve_target_user_id(event, user)
        if not target_id:
            yield event.plain_result("⭐用法：/语录拉黑 @用户 或 /语录拉黑 <UID>")
            event.stop_event()
            return

        ok, text = self._update_blacklist("block", target_id)
        if ok:
            text = f"⭐{text[:-1]}，其无法投稿或抽卡查看语录"
        else:
            text = f"⭐{text}"
        yield event.plain_result(text)
        event.stop_event()

    @filter.command("语录解封", alias={"解封语录用户", "语录取消拉黑"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def unblock_quote_user(self, event: AstrMessageEvent, user: str = ""):
        """从全局语录黑名单中解除用户"""
        target_id = self._resolve_target_user_id(event, user)
        if not target_id:
            yield event.plain_result("⭐用法：/语录解封 @用户 或 /语录解封 <UID>")
            event.stop_event()
            return

        ok, text = self._update_blacklist("unblock", target_id)
        if ok:
            text = f"⭐{text}"
        else:
            text = f"⭐{text}"
        yield event.plain_result(text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：删除语录（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("语录删除", alias={"删除语录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def delete_quote(self, event: AstrMessageEvent, name: str = ""):
        """按名称或回复 bot 语录卡片删除一条本群语录"""
        group_id = str(event.message_obj.group_id)
        target = await self._resolve_reply_quote_file(event, group_id)
        if not target and name.strip():
            target = self._find_quote_file(group_id, name)
        if not target:
            yield event.plain_result(
                "⭐用法：/语录删除 <名称>，或回复 bot 的语录卡片后直接发送 /语录删除"
            )
            event.stop_event()
            return
        _, text = self._delete_quote_file(group_id, target)
        yield event.plain_result(text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：重命名语录（仅管理员，改标题部分，保留稀有度与扩展名）
    # ------------------------------------------------------------------ #
    @filter.command("语录改名", alias={"改名语录"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def rename_quote(self, event: AstrMessageEvent, old: str = "", new: str = ""):
        """重命名语录，支持回复 bot 语录卡片后仅提供新标题"""
        group_id = str(event.message_obj.group_id)
        raw_parts = event.message_str.split(maxsplit=1)
        argument = raw_parts[1].strip() if len(raw_parts) > 1 else ""
        target = await self._resolve_reply_quote_file(event, group_id)
        if target:
            new = argument
        else:
            parts = argument.split(maxsplit=1)
            old = parts[0] if parts else ""
            new = parts[1].strip() if len(parts) > 1 else ""
            target = self._find_quote_file(group_id, old) if old else None

        if not target or not new:
            yield event.plain_result(
                "⭐用法：/语录改名 <旧名称> <新标题>；\n"
                "或回复 bot 的语录卡片后发送 /语录改名 <新标题>"
            )
            event.stop_event()
            return
        _, text = self._rename_quote_file(group_id, target, new)
        yield event.plain_result(text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：修改语录稀有度（仅管理员）
    # ------------------------------------------------------------------ #
    @filter.command("语录改稀有", alias={"改稀有语录", "语录改稀有度"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def change_rarity(
        self, event: AstrMessageEvent, name: str = "", rarity: str = ""
    ):
        """修改语录稀有度，支持回复 bot 语录卡片后仅提供星级"""
        group_id = str(event.message_obj.group_id)
        raw_parts = event.message_str.split(maxsplit=1)
        argument = raw_parts[1].strip() if len(raw_parts) > 1 else ""
        target = await self._resolve_reply_quote_file(event, group_id)
        if target:
            rarity = argument
        else:
            parts = argument.split(maxsplit=1)
            name = parts[0] if parts else ""
            rarity = parts[1].strip() if len(parts) > 1 else ""
            target = self._find_quote_file(group_id, name) if name else None

        if not target or not rarity:
            yield event.plain_result(
                "⭐用法：/语录改稀有 <名称> <1-6>；\n"
                "或回复 bot 的语录卡片后发送 /语录改稀有 <1-6>"
            )
            event.stop_event()
            return
        try:
            r = int(rarity)
        except ValueError:
            yield event.plain_result("⭐稀有度必须是 1-6 的整数")
            event.stop_event()
            return
        _, text = self._set_quote_rarity_file(group_id, target, r)
        yield event.plain_result(text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令：撤回自己的投稿（仅本人）
    # ------------------------------------------------------------------ #
    @filter.command("撤回投稿", alias={"取消投稿", "撤回语录"})
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def revoke_quote(self, event: AstrMessageEvent, name: str = ""):
        """撤回自己的投稿；可回复 bot 语录卡片，不带目标时撤回最新一条"""
        group_id = str(event.message_obj.group_id)
        user_id = str(event.get_sender_id())
        target = await self._resolve_reply_quote_file(event, group_id)
        if not target and name.strip():
            target = self._find_quote_file(group_id, name)

        if not target:
            if name.strip() or any(
                isinstance(component, Comp.Reply)
                for component in event.message_obj.message
            ):
                yield event.plain_result(
                    "⭐未找到目标语录；可回复 bot 的语录卡片后发送 /撤回投稿"
                )
                event.stop_event()
                return
            records = [
                record
                for record in self._read_submit_log()
                if str(record.get("group_id")) == group_id
                and str(record.get("user_id")) == user_id
            ]
            if not records:
                yield event.plain_result("⭐您在本群没有可撤回的投稿记录")
                event.stop_event()
                return
            latest = max(records, key=lambda record: float(record.get("ts", 0)))
            target = self._group_dir(group_id) / str(latest.get("filename", ""))
            if not target.exists():
                yield event.plain_result("⭐最近一条投稿文件已不存在，无法撤回")
                event.stop_event()
                return

        _, text = self._revoke_quote_file(group_id, user_id, target)
        yield event.plain_result(text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 监听：戳一戳触发随机语录
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_poke(self, event: AstrMessageEvent):
        """被戳一戳时随机发送一张语录（带冷却与去重）"""
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict):
            return
        if raw.get("post_type") != "notice":
            return
        if raw.get("notice_type") != "notify" or raw.get("sub_type") != "poke":
            return

        bot_id = raw.get("self_id")
        sender_id = raw.get("user_id")
        target_id = raw.get("target_id")
        if not (bot_id and sender_id and target_id):
            return
        # 只响应戳到 bot 自己
        if str(target_id) != str(bot_id):
            return

        # 拉黑用户的戳一戳静默忽略，且不得消耗本群冷却
        if self._is_blocked_user(str(sender_id)):
            return

        group_id = str(event.message_obj.group_id)
        settings = self._load_settings(group_id)
        cooldown = int(settings.get("cooldown", 10))
        last_poke = float(settings.get("last_poke", 0))

        if time.time() - last_poke < cooldown:
            remaining = cooldown - (time.time() - last_poke)
            logger.info(f"戳戳语录冷却中，剩余 {remaining:.0f} 秒")
            return

        settings["last_poke"] = time.time()
        self._save_settings(group_id, settings)

        gs = self._load_global_settings()
        prob = float(gs.get("poke_quote_probability", 0.85))
        if random.random() < prob:
            path, debug_note = self._draw_quote(group_id, "戳一戳", str(sender_id))
            if path:
                yield event.chain_result(
                    self._with_debug_note(quote_chain(path), debug_note)
                )
                event.stop_event()
            # 无语录时静默
            return
        else:
            yield event.chain_result(
                [Comp.At(qq=sender_id), Comp.Plain(text=random.choice(POKE_TEXTS))]
            )
            event.stop_event()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断调用者是否为 bot 管理员。"""
        # AstrBot 管理员标识存在会话配置中
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            return str(event.get_sender_id()) in {str(a) for a in admins}
        except Exception:
            return False

    def _tool_group_id(self, event: AstrMessageEvent) -> str | None:
        """返回 AI tool 所在群号；私聊场景返回 None。"""
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        return group_id or None

    def _quote_stats_text(self, group_id: str) -> str:
        """生成本群语录统计文本，供指令与 AI tool 共用。

        每个星级单独一行（含 0 张的档位），方便一眼看出哪些档位是空的。
        """
        files = self._list_quote_files(group_id)
        counts = {rarity: 0 for rarity in RARITY_TEXT}
        for path in files:
            rarity, _ = parse_quote_rarity(path.name)
            counts[rarity] += 1
        settings = self._load_settings(group_id)
        mode_text = {0: "关闭", 1: "仅管理员", 2: "全体成员"}.get(
            int(settings.get("mode", 0)), "未知"
        )
        rarity_lines = "\n".join(
            f"{rarity}★ {counts[rarity]} 张" for rarity in RARITY_ORDER
        )
        return (
            f"总数：{len(files)} 张\n"
            f"{rarity_lines}\n"
            f"投稿权限：{mode_text}\n"
            f"戳戳冷却：{int(settings.get('cooldown', 10))} 秒\n"
            f"调试模式：{self._debug_level(group_id)}"
        )

    def _apply_group_settings(
        self,
        group_id: str,
        mode: int | None = None,
        cooldown: int | None = None,
        debug_mode: int | None = None,
    ) -> tuple[bool, str]:
        """校验并更新本群投稿模式、戳戳冷却与调试模式。"""
        settings = self._load_settings(group_id)
        try:
            if mode is not None:
                mode = int(mode)
                if mode not in (0, 1, 2):
                    return False, "投稿权限模式只能是 0、1、2"
                settings["mode"] = mode
            if cooldown is not None:
                cooldown = int(cooldown)
                if cooldown < 0:
                    return False, "戳戳冷却不能为负数"
                settings["cooldown"] = cooldown
            if debug_mode is not None:
                debug_mode = int(debug_mode)
                if debug_mode not in (0, 1, 2):
                    return False, "调试模式只能是 0、1、2"
                settings["debug_mode"] = debug_mode
        except (TypeError, ValueError):
            return False, "群设置项必须是整数"
        self._save_settings(group_id, settings)
        return True, "群设置已保存"

    def _update_blacklist(self, action: str, user_id: str) -> tuple[bool, str]:
        """更新全局语录黑名单，action 为 block 或 unblock。"""
        if action not in {"block", "unblock"} or not user_id.isdigit():
            return False, "action 必须是 block/unblock，user_id 必须是数字 UID。"
        settings = self._load_global_settings()
        blocked = self._blocked_user_ids()
        if action == "block":
            if user_id in blocked:
                return False, f"用户 {user_id} 已在黑名单中。"
            blocked.add(user_id)
            text = f"已全局拉黑用户 {user_id}。"
        else:
            if user_id not in blocked:
                return False, f"用户 {user_id} 不在黑名单中。"
            blocked.remove(user_id)
            text = f"已解除用户 {user_id} 的黑名单限制。"
        settings["blocked_user_ids"] = sorted(blocked)
        self._save_global_settings(settings)
        return True, text

    def _apply_global_settings(self, payload: dict) -> tuple[bool, str, dict]:
        """校验并保存全局设置，供 WebUI 与 AI 共用。"""
        if not isinstance(payload, dict):
            return False, "请求体必须是 JSON 对象", {}
        cur = self._load_global_settings()
        try:
            if "default_submit_mode" in payload:
                value = int(payload["default_submit_mode"])
                if value not in (0, 1, 2):
                    return False, "default_submit_mode 必须是 0/1/2", cur
                cur["default_submit_mode"] = value
            if "default_cooldown" in payload:
                value = int(payload["default_cooldown"])
                if value < 0:
                    return False, "default_cooldown 不能为负数", cur
                cur["default_cooldown"] = value
            if "poke_quote_probability" in payload:
                value = float(payload["poke_quote_probability"])
                if not 0 <= value <= 1:
                    return False, "poke_quote_probability 必须在 0~1 之间", cur
                cur["poke_quote_probability"] = value
            if "recent_window" in payload:
                value = int(payload["recent_window"])
                if value < 0:
                    return False, "recent_window 必须 >= 0（0 表示关闭去重）", cur
                cur["recent_window"] = value
            if "rarity_weights" in payload:
                weights = payload["rarity_weights"]
                if not isinstance(weights, dict):
                    return False, "rarity_weights 必须是对象", cur
                for key in (f"weight_{r}" for r in RARITY_ORDER):
                    if key in weights:
                        value = float(weights[key])
                        if value < 0:
                            return False, f"{key} 不能为负", cur
                        cur.setdefault("rarity_weights", {})[key] = value
            if "pity_config" in payload:
                pity = payload["pity_config"]
                if not isinstance(pity, dict):
                    return False, "pity_config 必须是对象", cur
                for key in (f"pity_{r}" for r in RARITY_ORDER):
                    if key in pity:
                        value = int(pity[key])
                        if value < 0:
                            return False, f"{key} 不能为负", cur
                        cur.setdefault("pity_config", {})[key] = value
            if "debug_mode" in payload:
                # 调试模式自 v2.2 起按群保存，这里显式拒绝以免静默丢失
                return False, "调试模式是按群设置，请用 /语录调试 或 quote_configure_group", cur
        except (TypeError, ValueError):
            return False, "设置项类型无效", cur

        self._save_global_settings(cur)
        self._drawers.clear()
        return True, "全局设置已保存", cur

    # ------------------------------------------------------------------ #
    # AI 语录 Tools
    # ------------------------------------------------------------------ #
    @filter.llm_tool(name="quote_list")
    async def ai_quote_list(
        self,
        event: AstrMessageEvent,
        rarity: int = 0,
        page: int = 1,
        page_size: int = 10,
    ):
        """查看当前群的语录列表，支持按稀有度筛选和分页。

        Args:
            rarity(number): 星级筛选，0为全部，1到6为对应星级
            page(number): 页码，从1开始
            page_size(number): 每页条数，1到50
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        if self._is_blocked_user(event.get_sender_id()):
            return "该用户已被语录系统拉黑，无法查看语录。"
        try:
            rarity = int(rarity)
            page = max(1, int(page))
            page_size = min(50, max(1, int(page_size)))
        except (TypeError, ValueError):
            return "rarity、page 和 page_size 必须是整数。"
        if rarity not in (0, *RARITY_ORDER):
            return "rarity 必须是 0 到 5 之间的整数。"
        files = self._list_quote_files(group_id, rarity)
        total = len(files)
        pages = max(1, (total + page_size - 1) // page_size)
        if page > pages:
            return f"页码超出范围，共 {pages} 页。"
        current = files[(page - 1) * page_size : page * page_size]
        header = f"语录列表：共 {total} 张，第 {page}/{pages} 页"
        if rarity:
            header += f"，筛选 {rarity} 星"
        lines = [header]
        for index, path in enumerate(current, start=(page - 1) * page_size + 1):
            quote_rarity, display = parse_quote_rarity(path.name)
            record = self._find_submission_record(group_id, path.name)
            submitter = f"，投稿人 {record.get('user_id')}" if record else ""
            lines.append(f"{index}. [{RARITY_TEXT[quote_rarity]}] {display}{submitter}")
        return "\n".join(lines)

    @filter.llm_tool(name="quote_view")
    async def ai_quote_view(self, event: AstrMessageEvent, name: str):
        """查看当前群指定语录的详细信息。

        Args:
            name(string): 语录名称，可填写完整文件名、展示名或不带扩展名的标题
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        if self._is_blocked_user(event.get_sender_id()):
            return "该用户已被语录系统拉黑，无法查看语录。"
        target = self._find_quote_file(group_id, name)
        if not target:
            return f"未找到名为「{name}」的语录。"
        return self._quote_detail_text(group_id, target)

    @filter.llm_tool(name="quote_draw")
    async def ai_quote_draw(self, event: AstrMessageEvent):
        """按 /语录 的完整逻辑抽取并发送一张当前群语录。"""
        # 不在 AI Tool 中复制抽卡流程：直接复用 /语录 handler，保证黑名单、
        # 空图库、加权随机、去重、保底和最终消息链都与手动指令完全一致。
        try:
            async for result in self.random_quote(event):
                await event.send(result)
        except Exception as e:
            logger.warning(f"AI 抽卡卡片发送失败: {e}")
            # QQ NT 有时已成功接收发送请求，但协议端等待回执超时（retcode=1200）。
            if "retcode=1200" in str(e):
                return "抽卡已完成，卡片发送请求超时，请确认群内是否已收到卡片。"
            return f"抽卡已完成，但卡片发送失败：{type(e).__name__}"
        return "已完成抽卡并发送到群内。"

    @filter.llm_tool(name="quote_status")
    async def ai_quote_status(self, event: AstrMessageEvent):
        """查看当前群的语录数量、稀有度分布和群设置状态。"""
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        return self._quote_stats_text(group_id)

    @filter.llm_tool(name="quote_submit")
    async def ai_quote_submit(self, event: AstrMessageEvent, title: str = ""):
        """将用户回复的一张图片投稿到当前群语录库。

        Args:
            title(string): 可选标题，留空时使用投稿时间命名
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        ok, text = await self._submit_replied_quote(
            event, group_id, str(event.get_sender_id()), title
        )
        return text

    @filter.llm_tool(name="quote_delete")
    async def ai_quote_delete(self, event: AstrMessageEvent, name: str):
        """删除当前群的一条语录，仅 bot 管理员可执行。

        Args:
            name(string): 要删除的语录名称
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        if not self._is_admin(event):
            return "权限不足，仅 bot 管理员可删除语录。"
        target = self._find_quote_file(group_id, name)
        if not target:
            return f"未找到名为「{name}」的语录。"
        _, text = self._delete_quote_file(group_id, target)
        return text

    @filter.llm_tool(name="quote_rename")
    async def ai_quote_rename(self, event: AstrMessageEvent, name: str, new_title: str):
        """重命名当前群一条语录，仅 bot 管理员可执行。

        Args:
            name(string): 原语录名称
            new_title(string): 新标题，不包含稀有度和扩展名
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        if not self._is_admin(event):
            return "权限不足，仅 bot 管理员可重命名语录。"
        target = self._find_quote_file(group_id, name)
        if not target:
            return f"未找到名为「{name}」的语录。"
        _, text = self._rename_quote_file(group_id, target, new_title)
        return text

    @filter.llm_tool(name="quote_set_rarity")
    async def ai_quote_set_rarity(
        self, event: AstrMessageEvent, name: str, rarity: int
    ):
        """修改当前群一条语录的稀有度，仅 bot 管理员可执行。

        Args:
            name(string): 语录名称
            rarity(number): 目标星级，1到6
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        if not self._is_admin(event):
            return "权限不足，仅 bot 管理员可修改稀有度。"
        target = self._find_quote_file(group_id, name)
        if not target:
            return f"未找到名为「{name}」的语录。"
        try:
            rarity = int(rarity)
        except (TypeError, ValueError):
            return "稀有度必须是 1 到 5 之间的整数。"
        _, text = self._set_quote_rarity_file(group_id, target, rarity)
        return text

    @filter.llm_tool(name="quote_revoke")
    async def ai_quote_revoke(self, event: AstrMessageEvent, name: str = ""):
        """撤回当前对话发起人自己的投稿；不填名称时撤回最新一条。

        Args:
            name(string): 可选的语录名称
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        user_id = str(event.get_sender_id())
        target = self._find_quote_file(group_id, name) if name.strip() else None
        if not target:
            records = [
                record
                for record in self._read_submit_log()
                if str(record.get("group_id")) == group_id
                and str(record.get("user_id")) == user_id
            ]
            if not records:
                return "您在本群没有可撤回的投稿记录。"
                return
            latest = max(records, key=lambda record: float(record.get("ts", 0)))
            target = self._group_dir(group_id) / str(latest.get("filename", ""))
        if not target.exists():
            return "目标投稿文件已不存在，无法撤回。"
        _, text = self._revoke_quote_file(group_id, user_id, target)
        return text

    @filter.llm_tool(name="quote_configure_group")
    async def ai_quote_configure_group(
        self,
        event: AstrMessageEvent,
        mode: int | None = None,
        cooldown: int | None = None,
        debug_mode: int | None = None,
    ):
        """调整当前群投稿模式、戳戳冷却或调试模式，仅 bot 管理员可执行。

        Args:
            mode(number): 可选，投稿模式，0关闭、1仅管理员、2全体成员
            cooldown(number): 可选，戳戳冷却秒数，不能为负数
            debug_mode(number): 可选，本群抽卡调试模式，0关闭、1仅写入日志、2日志并在卡片上附注
        """
        group_id = self._tool_group_id(event)
        if not group_id:
            return "语录系统仅支持群聊使用。"
        if not self._is_admin(event):
            return "权限不足，仅 bot 管理员可调整群设置。"
        if mode is None and cooldown is None and debug_mode is None:
            return "请至少提供 mode、cooldown 或 debug_mode 其中一项。"
        ok, text = self._apply_group_settings(group_id, mode, cooldown, debug_mode)
        return text

    @filter.llm_tool(name="quote_configure_global")
    async def ai_quote_configure_global(self, event: AstrMessageEvent, settings: dict):
        """调整语录系统全局设置，仅 bot 管理员可执行。

        Args:
            settings(object): 可包含投稿模式、冷却、触发概率、去重窗口、权重与保底配置

        注意：抽卡调试模式是按群设置，请用 quote_configure_group 的 debug_mode。
        """
        if not self._is_admin(event):
            return "权限不足，仅 bot 管理员可调整全局设置。"
        ok, text, _ = self._apply_global_settings(settings)
        return text

    @filter.llm_tool(name="quote_manage_blacklist")
    async def ai_quote_manage_blacklist(
        self, event: AstrMessageEvent, action: str, user_id: str
    ):
        """管理全局语录黑名单，仅 bot 管理员可执行。

        Args:
            action(string): 操作，只能是 block 或 unblock
            user_id(string): 要拉黑或解封的数字 UID
        """
        if not self._is_admin(event):
            return "权限不足，仅 bot 管理员可管理黑名单。"
        _, text = self._update_blacklist(action, str(user_id))
        return text

    # ------------------------------------------------------------------ #
    # WebUI Page 后端 API
    # ------------------------------------------------------------------ #
    async def web_get_settings(self):
        """读取全局设置，供 Page 渲染。"""
        return json_response(self._load_global_settings())

    async def web_save_settings(self):
        """保存全局设置，复用与 AI tool 相同的校验规则。"""
        payload = await request.json(default={})
        ok, text, settings = self._apply_global_settings(payload)
        if not ok:
            return error_response(text)
        return json_response(settings)

    def _group_debug_payload(self, group_id: str) -> dict:
        """返回指定群的设置快照，供 Page 的按群调试面板展示。"""
        settings = self._load_settings(group_id)
        return {
            "group_id": group_id,
            "debug_mode": self._debug_level(group_id),
            "mode": int(settings.get("mode", 0)),
            "cooldown": int(settings.get("cooldown", 10)),
        }

    async def web_get_group_debug(self):
        """按群号读取群设置（调试模式 + 投稿权限 + 戳戳冷却）。"""
        group_id = str(request.query.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        return json_response(self._group_debug_payload(group_id))

    async def web_set_group_debug(self):
        """按群保存调试模式，复用 _apply_group_settings 的校验。"""
        payload = await request.json(default={})
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        ok, text = self._apply_group_settings(
            group_id, debug_mode=payload.get("debug_mode")
        )
        if not ok:
            return error_response(text)
        return json_response(self._group_debug_payload(group_id))

    async def web_submit_log(self):
        """返回投稿日志（JSONL 解析），支持按群号过滤和限制条数。"""
        group_filter = request.query.get("group_id") or None
        try:
            limit = request.query.get("limit", 200, type=int)
        except Exception:
            limit = 200
        if limit < 0:
            limit = 200

        log_path = QUOTES_ROOT / "submit.log"
        records: list[dict] = []
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if group_filter and str(rec.get("group_id")) != str(
                            group_filter
                        ):
                            continue
                        records.append(rec)
            except Exception as e:
                logger.error(f"读取投稿日志失败: {e}")
        # 按时间倒序，限制条数
        records.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
        records = records[:limit]
        return json_response({"total": len(records), "items": records})

    async def terminate(self):
        """插件卸载时调用。"""
        self._drawers.clear()

    # ------------------------------------------------------------------ #
    # WebUI Page 后端 API：语录管理页（表格 / 改星级 / 删除 / 缩略图）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _names_from_payload(payload: dict) -> list[str]:
        names = payload.get("names") or []
        if isinstance(names, str):
            names = [names]
        return [str(name) for name in names if str(name).strip()]

    def _match_quotes(
        self, group_id: str, rarity: int = 0, query: str = ""
    ) -> list[Path]:
        """按星级与关键词筛选本群语录（文件名或展示名包含关键词）。"""
        files = self._list_quote_files(group_id, rarity if rarity in RARITY_TEXT else 0)
        needle = query.strip().lower()
        if needle:
            files = [
                path
                for path in files
                if needle in path.name.lower()
                or needle in parse_quote_rarity(path.name)[1].lower()
            ]
        return files

    def _image_data_url(
        self, path: Path, max_size: tuple[int, int] | None = None
    ) -> str | None:
        """把图片转成 data URL；max_size 非空时用 Pillow 生成缩略图。

        插件页 iframe 与 dashboard 隔离（拿不到它的 cookie），不能直接用
        ``<img src="/api/...">`` 请求接口，所以缩略图内嵌成 data URL。
        Pillow 缺失、图片损坏或格式不支持时返回 None（前端显示占位）。
        """
        if not path.exists():
            return None
        cache_key = None
        if max_size:
            try:
                cache_key = (
                    f"{max_size[0]}x{max_size[1]}|{path}|{path.stat().st_mtime_ns}"
                )
            except OSError:
                cache_key = None
            if cache_key and cache_key in self._thumb_cache:
                return self._thumb_cache[cache_key]

        try:
            data = path.read_bytes()
        except OSError:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        if max_size:
            if _PILImage is None:
                return None  # 没有 Pillow 就不内嵌原图（可能好几 MB × 每页 20 张）
            try:
                with _PILImage.open(io.BytesIO(data)) as image:
                    image = image.convert("RGB")
                    image.thumbnail(max_size)
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=72)
                data = buffer.getvalue()
                mime = "image/jpeg"
            except Exception:
                return None

        data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        if cache_key:
            if len(self._thumb_cache) > 500:
                self._thumb_cache.clear()
            self._thumb_cache[cache_key] = data_url
        return data_url

    def _read_order_state(self, group_id: str) -> dict:
        """读取群目录下的 order.json（不存在或损坏时返回空字典）。"""
        path = self._group_dir(group_id) / "order.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    @staticmethod
    def _quote_sort_key(group_id: str, path: Path, sort: str, finder) -> tuple:
        """管理页排序键；submitter/time 需要查投稿记录。"""
        if sort == "rarity":
            return (parse_quote_rarity(path.name)[0], path.name.lower())
        if sort == "size":
            return (path.stat().st_size, path.name.lower())
        if sort in ("submitter", "time"):
            record = finder(group_id, path.name) or {}
            key = "user_id" if sort == "submitter" else "time"
            return (str(record.get(key, "")), path.name.lower())
        if sort == "display":
            return (parse_quote_rarity(path.name)[1].lower(), path.name.lower())
        return (path.name.lower(),)

    def _quote_rows(
        self,
        group_id: str,
        rarity: int = 0,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
        sort: str = "name",
        order: str = "asc",
    ) -> dict:
        """管理页表格数据：筛选/排序/分页 + 缩略图 URL + 投稿信息。"""
        files = self._match_quotes(group_id, rarity, query)
        if sort not in {"name", "display", "rarity", "submitter", "time", "size"}:
            sort = "name"
        files.sort(
            key=lambda path: self._quote_sort_key(
                group_id, path, sort, self._find_submission_record
            ),
            reverse=(order == "desc"),
        )
        total = len(files)
        page_size = max(1, min(100, page_size))
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        items = []
        for path in files[(page - 1) * page_size : page * page_size]:
            rarity_value, display = parse_quote_rarity(path.name)
            record = self._find_submission_record(group_id, path.name) or {}
            items.append(
                {
                    "name": path.name,
                    "display": display,
                    "rarity": rarity_value,
                    "stars": RARITY_TEXT[rarity_value],
                    "size": path.stat().st_size,
                    "thumb": self._image_data_url(path, THUMB_SIZE),
                    "submitter": record.get("user_id", ""),
                    "time": record.get("time", ""),
                    "image_url": (
                        f"/{PLUGIN_NAME}/quotes/image"
                        f"?group_id={urlquote(group_id)}"
                        f"&name={urlquote(path.name)}"
                    ),
                }
            )
        return {
            "group_id": group_id,
            "total": total,
            "page": page,
            "pages": pages,
            "page_size": page_size,
            "sort": sort,
            "order": order,
            "items": items,
        }

    async def web_quotes(self):
        """分页列出某群语录（管理页表格）。"""
        group_id = str(request.query.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        rarity = self._as_int(request.query.get("rarity", 0, type=int), 0)
        if rarity not in (0, *RARITY_ORDER):
            return error_response("rarity 必须是 0 或 1-6")
        query = str(request.query.get("query") or "")
        page = self._as_int(request.query.get("page", 1, type=int), 1) or 1
        page_size = self._as_int(request.query.get("page_size", 20, type=int), 20) or 20
        sort = str(request.query.get("sort") or "name")
        order = str(request.query.get("order") or "asc")
        return json_response(
            self._quote_rows(group_id, rarity, query, page, page_size, sort, order)
        )

    async def web_quote_image(self):
        """返回语录图片本体，供管理页缩略图使用（只允许群目录内的文件）。"""
        group_id = str(request.query.get("group_id") or "").strip()
        name = str(request.query.get("name") or "").strip()
        if not group_id.isdigit() or not name:
            return error_response("参数不完整")
        target = self._find_quote_file(group_id, name)
        if not target:
            return error_response("未找到该语录", status_code=404)
        try:
            target.resolve().relative_to(self._group_dir(group_id).resolve())
        except (ValueError, OSError):
            return error_response("非法路径", status_code=400)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return file_response(target, content_type=mime)

    async def web_quote_full(self):
        """返回一张语录的完整图片（data URL），供管理页点开预览。"""
        group_id = str(request.query.get("group_id") or "").strip()
        name = str(request.query.get("name") or "").strip()
        if not group_id.isdigit() or not name:
            return error_response("参数不完整")
        target = self._find_quote_file(group_id, name)
        if not target:
            return error_response("未找到该语录", status_code=404)
        data_url = self._image_data_url(target)
        if not data_url:
            return error_response("读取图片失败", status_code=500)
        return json_response({"name": target.name, "data_url": data_url})

    async def web_set_quote_rarity(self):
        """把一批语录（或「当前筛选的全部」）改成指定星级。

        body: ``{group_id, rarity, names?: [...], filter_rarity?, filter_query?,
        page?, page_size?}``；给了 names 就只改这些，否则按筛选条件批量改。
        """
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        rarity = self._as_int(payload.get("rarity"), -1)
        if rarity not in RARITY_TEXT:
            return error_response("rarity 必须是 1-6 的整数")

        names = self._names_from_payload(payload)
        filter_rarity = self._as_int(payload.get("filter_rarity"), 0)
        filter_query = str(payload.get("filter_query") or "")
        if names:
            targets = [
                found
                for found in (self._find_quote_file(group_id, name) for name in names)
                if found
            ]
        else:
            targets = self._match_quotes(group_id, filter_rarity, filter_query)

        changed = skipped = 0
        failed: list[str] = []
        for path in targets:
            ok, text = self._set_quote_rarity_file(group_id, path, rarity)
            if not ok:
                failed.append(text)
            elif "无需修改" in text:
                skipped += 1
            else:
                changed += 1
        return json_response(
            {
                "total": len(targets),
                "changed": changed,
                "skipped": skipped,
                "failed": failed,
                "rows": self._quote_rows(
                    group_id,
                    filter_rarity,
                    filter_query,
                    self._as_int(payload.get("page"), 1) or 1,
                    self._as_int(payload.get("page_size"), 20) or 20,
                    str(payload.get("sort") or "name"),
                    str(payload.get("order") or "asc"),
                ),
            }
        )

    async def web_rename_quote(self):
        """重命名一条语录（只改标题，保留星级与扩展名）。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        name = str(payload.get("name") or "").strip()
        new_title = str(payload.get("new_title") or "").strip()
        if not name or not new_title:
            return error_response("name / new_title 不能为空")
        target = self._find_quote_file(group_id, name)
        if not target:
            return error_response("未找到该语录", status_code=404)
        ok, text = self._rename_quote_file(group_id, target, new_title)
        if not ok:
            return error_response(text)
        return json_response(
            {
                "message": text,
                "rows": self._quote_rows(
                    group_id,
                    self._as_int(payload.get("filter_rarity"), 0),
                    str(payload.get("filter_query") or ""),
                    self._as_int(payload.get("page"), 1) or 1,
                    self._as_int(payload.get("page_size"), 20) or 20,
                    str(payload.get("sort") or "name"),
                    str(payload.get("order") or "asc"),
                ),
            }
        )

    async def web_reset_draw_stats(self):
        """清零本群累计出货计数（不影响保底计数、去重与图库）。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        state_path = self._group_dir(group_id) / "order.json"
        data: dict = {}
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        data["stats"] = {str(r): 0 for r in RARITY_ORDER}
        try:
            self._group_dir(group_id).mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"清零出货统计失败: {e}")
            return error_response(f"清零失败：{e}")
        return json_response(
            {
                "group_id": group_id,
                "stats": {r: 0 for r in RARITY_ORDER},
                "total_draws": 0,
            }
        )

    async def web_delete_quotes(self):
        """删除一批语录（连带投稿记录与抽卡队列重建）。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id.isdigit():
            return error_response("请填写数字群号")
        names = self._names_from_payload(payload)
        if not names:
            return error_response("names 不能为空")
        deleted = 0
        failed: list[str] = []
        for name in names:
            found = self._find_quote_file(group_id, name)
            if not found:
                failed.append(f"未找到 {name}")
                continue
            ok, text = self._delete_quote_file(group_id, found)
            if ok:
                deleted += 1
            else:
                failed.append(text)
        return json_response({"deleted": deleted, "failed": failed})
